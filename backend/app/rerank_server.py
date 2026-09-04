"""macOS 本地 Cross-Encoder Rerank 服务。

提供与主应用 ``app.knowledge.reranker`` 兼容的接口：

- ``GET /v1/models``：模型探测。
- ``POST /v1/rerank``：按 query-document 对计算相关性并排序。
- ``GET /health``：进程与设备状态。

模型在进程启动时只加载一次。Apple Silicon 默认使用 MPS，不可用时回退 CPU。
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RerankServerSettings:
    """独立 Rerank 进程配置。"""

    model_id: str = "BAAI/bge-reranker-v2-m3"
    served_model_name: str = "text-embedding-bge-reranker-v2-m3"
    device: str = "auto"
    max_length: int = 1024
    batch_size: int = 4
    max_documents: int = 128

    @classmethod
    def from_env(cls) -> RerankServerSettings:
        return cls(
            model_id=os.getenv("RERANK_SERVER_MODEL_ID", cls.model_id).strip(),
            served_model_name=os.getenv(
                "RERANK_SERVER_SERVED_MODEL_NAME", cls.served_model_name
            ).strip(),
            device=os.getenv("RERANK_SERVER_DEVICE", cls.device).strip().lower(),
            max_length=max(128, int(os.getenv("RERANK_SERVER_MAX_LENGTH", cls.max_length))),
            batch_size=max(1, int(os.getenv("RERANK_SERVER_BATCH_SIZE", cls.batch_size))),
            max_documents=max(
                1, int(os.getenv("RERANK_SERVER_MAX_DOCUMENTS", cls.max_documents))
            ),
        )


class RerankRequest(BaseModel):
    """Cohere/vLLM 风格的 Rerank 请求。"""

    model: str | None = None
    query: str = Field(min_length=1)
    documents: list[str] = Field(min_length=1)
    top_n: int | None = Field(default=None, ge=1)
    return_documents: bool = True

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value

    @field_validator("documents")
    @classmethod
    def _documents_not_blank(cls, value: list[str]) -> list[str]:
        cleaned = [document.strip() for document in value]
        if any(not document for document in cleaned):
            raise ValueError("documents must not contain blank text")
        return cleaned


class RerankRuntime(Protocol):
    """便于生产运行时与测试替身共享的最小协议。"""

    model_id: str
    served_model_name: str
    device: str
    loaded: bool

    def load(self) -> None: ...

    def rerank(
        self, query: str, documents: list[str], top_n: int
    ) -> tuple[list[tuple[int, float]], int]: ...


class TransformersRerankRuntime:
    """使用 Transformers 分类头执行真正的 Cross-Encoder 推理。"""

    def __init__(self, settings: RerankServerSettings):
        self.settings = settings
        self.model_id = settings.model_id
        self.served_model_name = settings.served_model_name
        self.device = "unresolved"
        self.loaded = False
        self._torch: Any = None
        self._tokenizer: Any = None
        self._model: Any = None

    @staticmethod
    def _resolve_device(torch_module: Any, requested: str) -> str:
        valid = {"auto", "mps", "cuda", "cpu"}
        if requested not in valid:
            raise RuntimeError(
                f"RERANK_SERVER_DEVICE must be one of {sorted(valid)}, got {requested!r}"
            )
        if requested == "auto":
            if torch_module.backends.mps.is_available():
                return "mps"
            if torch_module.cuda.is_available():
                return "cuda"
            return "cpu"
        if requested == "mps" and not torch_module.backends.mps.is_available():
            raise RuntimeError("MPS requested but unavailable")
        if requested == "cuda" and not torch_module.cuda.is_available():
            raise RuntimeError("CUDA requested but unavailable")
        return requested

    def load(self) -> None:
        """加载 tokenizer 与分类模型；该方法只在进程启动时调用一次。"""
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.device = self._resolve_device(torch, self.settings.device)
        dtype = torch.float16 if self.device in {"mps", "cuda"} else torch.float32
        logger.info(
            "加载 Rerank 模型 model=%s device=%s dtype=%s",
            self.model_id,
            self.device,
            dtype,
        )
        self._tokenizer = AutoTokenizer.from_pretrained(self.model_id)
        self._model = AutoModelForSequenceClassification.from_pretrained(
            self.model_id,
            torch_dtype=dtype,
        )
        self._model.to(self.device)
        self._model.eval()
        self._torch = torch
        self.loaded = True
        logger.info("Rerank 模型加载完成: %s", self.model_id)

    def rerank(
        self, query: str, documents: list[str], top_n: int
    ) -> tuple[list[tuple[int, float]], int]:
        """分批联合编码 query-document 对，返回排序结果与输入 token 数。"""
        if not self.loaded:
            raise RuntimeError("rerank model is not loaded")

        scored: list[tuple[int, float]] = []
        total_tokens = 0
        for start in range(0, len(documents), self.settings.batch_size):
            batch = documents[start : start + self.settings.batch_size]
            encoded = self._tokenizer(
                [query] * len(batch),
                batch,
                padding=True,
                truncation=True,
                max_length=self.settings.max_length,
                return_tensors="pt",
            )
            total_tokens += int(encoded["attention_mask"].sum().item())
            encoded = {key: value.to(self.device) for key, value in encoded.items()}
            with self._torch.inference_mode():
                logits = self._model(**encoded, return_dict=True).logits.reshape(-1)
                scores = self._torch.sigmoid(logits).float().cpu().tolist()
            scored.extend(
                (start + offset, float(score))
                for offset, score in enumerate(scores)
            )

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_n], total_tokens


def create_app(
    *,
    settings: RerankServerSettings | None = None,
    runtime: RerankRuntime | None = None,
) -> FastAPI:
    """创建服务；测试可以注入轻量 runtime，避免加载真实模型。"""
    server_settings = settings or RerankServerSettings.from_env()
    model_runtime = runtime or TransformersRerankRuntime(server_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.runtime = model_runtime
        app.state.inference_lock = asyncio.Lock()
        if not model_runtime.loaded:
            await run_in_threadpool(model_runtime.load)
        yield

    app = FastAPI(
        title="MyAgentDemo1 local rerank server",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health(request: Request) -> dict:
        current: RerankRuntime = request.app.state.runtime
        return {
            "status": "ok" if current.loaded else "starting",
            "model": current.served_model_name,
            "source_model": current.model_id,
            "device": current.device,
        }

    @app.get("/v1/models")
    async def models(request: Request) -> dict:
        current: RerankRuntime = request.app.state.runtime
        return {
            "object": "list",
            "data": [
                {
                    "id": current.served_model_name,
                    "object": "model",
                    "owned_by": "local",
                }
            ],
        }

    @app.post("/v1/rerank")
    async def rerank(body: RerankRequest, request: Request) -> dict:
        current: RerankRuntime = request.app.state.runtime
        accepted_names = {current.served_model_name, current.model_id}
        if body.model and body.model not in accepted_names:
            raise HTTPException(status_code=404, detail="rerank model not found")
        if len(body.documents) > server_settings.max_documents:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"too many documents: {len(body.documents)}; "
                    f"maximum is {server_settings.max_documents}"
                ),
            )

        top_n = min(body.top_n or len(body.documents), len(body.documents))
        async with request.app.state.inference_lock:
            try:
                ranked, total_tokens = await run_in_threadpool(
                    current.rerank,
                    body.query,
                    body.documents,
                    top_n,
                )
            except RuntimeError as exc:
                logger.exception("Rerank 推理失败")
                raise HTTPException(status_code=503, detail=str(exc)) from exc

        results = []
        for index, score in ranked:
            item: dict[str, Any] = {
                "index": index,
                "relevance_score": round(score, 6),
            }
            if body.return_documents:
                item["document"] = {"text": body.documents[index]}
            results.append(item)
        return {
            "id": f"rerank-{uuid.uuid4().hex}",
            "model": current.served_model_name,
            "usage": {"total_tokens": total_tokens},
            "results": results,
        }

    return app


app = create_app()


app = create_app()
