"""Rerank 封装测试（AGENTIC_RAG_UPGRADE_SPEC §6 / G-RAG-2 验收）：

- HTTP 成功：解析 {results:[{index, relevance_score}]} 并按分数降序映射回候选。
- HTTP 错误 / 网络异常 / 响应格式异常 → 抛 RerankError。
- is_rerank_available：探测缓存（60s TTL 内不重复探测）、RERANK_ENABLED=False 恒 False。
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.config import settings
from app.knowledge import reranker as reranker_mod
from app.knowledge.reranker import RerankError, is_rerank_available, rerank
from app.knowledge.retriever import Hit
from app.observability.metrics import metrics


def _hit(chunk_id: int, text: str) -> Hit:
    return Hit(
        text=text,
        source_file="doc.md",
        chunk_index=chunk_id,
        score=0.5,
        kb_id=uuid.uuid4(),
        chunk_id=chunk_id,
    )


class _FakeResponse:
    def __init__(self, status_code: int = 200, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self) -> dict:
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeAsyncClient:
    """可配置的 httpx.AsyncClient 替身（模块级状态，用例内重置）。"""

    response: _FakeResponse | None = None
    exc: Exception | None = None
    calls: list[dict] = []

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers=None):
        _FakeAsyncClient.calls.append({"method": "GET", "url": url})
        if _FakeAsyncClient.exc:
            raise _FakeAsyncClient.exc
        assert _FakeAsyncClient.response is not None
        return _FakeAsyncClient.response

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.calls.append({"method": "POST", "url": url, "json": json})
        if _FakeAsyncClient.exc:
            raise _FakeAsyncClient.exc
        assert _FakeAsyncClient.response is not None
        return _FakeAsyncClient.response

    @classmethod
    def reset(cls, response=None, exc=None):
        cls.response = response
        cls.exc = exc
        cls.calls = []


@pytest.fixture(autouse=True)
def _mock_http(monkeypatch):
    """拦截 reranker 内 httpx.AsyncClient + 复位探测缓存。"""
    monkeypatch.setattr(reranker_mod.httpx, "AsyncClient", _FakeAsyncClient)
    reranker_mod.reset_probe_cache()
    _FakeAsyncClient.reset()
    yield
    reranker_mod.reset_probe_cache()


@pytest.mark.asyncio
async def test_rerank_http_success_maps_by_index():
    """成功响应：index 映射回候选，按 relevance_score 降序，保留溯源主键。"""
    candidates = [_hit(1, "文本一"), _hit(2, "文本二"), _hit(3, "文本三")]
    _FakeAsyncClient.reset(
        response=_FakeResponse(
            json_data={
                "results": [
                    {"index": 2, "relevance_score": 0.95},
                    {"index": 0, "relevance_score": 0.42},
                ]
            }
        )
    )

    out = await rerank("query", candidates, top_n=2)

    assert len(out) == 2
    assert out[0].chunk_id == 3 and out[0].score == pytest.approx(0.95)
    assert out[1].chunk_id == 1 and out[1].score == pytest.approx(0.42)
    assert all(h.strategy == "rerank" for h in out)
    assert all(h.rerank_score == h.score for h in out)
    # 请求体校验
    post = next(c for c in _FakeAsyncClient.calls if c["method"] == "POST")
    assert post["json"]["query"] == "query"
    assert post["json"]["documents"] == ["文本一", "文本二", "文本三"]
    assert post["json"]["top_n"] == 2


@pytest.mark.asyncio
async def test_rerank_http_error_raises():
    """HTTP 非 200 → RerankError。"""
    _FakeAsyncClient.reset(response=_FakeResponse(status_code=500))
    with pytest.raises(RerankError):
        await rerank("q", [_hit(1, "a")], top_n=1)


@pytest.mark.asyncio
async def test_rerank_network_error_raises():
    """网络异常（连接失败/超时）→ RerankError。"""
    _FakeAsyncClient.reset(exc=httpx.ConnectError("refused"))
    with pytest.raises(RerankError):
        await rerank("q", [_hit(1, "a")], top_n=1)

    _FakeAsyncClient.reset(exc=httpx.ReadTimeout("timeout"))
    with pytest.raises(RerankError):
        await rerank("q", [_hit(1, "a")], top_n=1)


@pytest.mark.asyncio
async def test_rerank_bad_payload_raises():
    """响应缺 results 字段 → RerankError。"""
    _FakeAsyncClient.reset(response=_FakeResponse(json_data={"unexpected": []}))
    with pytest.raises(RerankError):
        await rerank("q", [_hit(1, "a")], top_n=1)


@pytest.mark.asyncio
async def test_rerank_empty_candidates_short_circuits():
    """空候选直接返回，不发起 HTTP。"""
    out = await rerank("q", [], top_n=5)
    assert out == []
    assert _FakeAsyncClient.calls == []


@pytest.mark.asyncio
async def test_is_rerank_available_caches_probe(monkeypatch):
    """探测结果缓存：TTL 内二次调用不再发 HTTP。"""
    monkeypatch.setattr(settings, "RERANK_ENABLED", True)
    monkeypatch.setattr(settings, "RERANK_BASE_URL", "http://fake-rerank.local/v1")
    _FakeAsyncClient.reset(response=_FakeResponse(status_code=200))

    assert await is_rerank_available() is True
    probe_calls = [c for c in _FakeAsyncClient.calls if c["method"] == "GET"]
    assert len(probe_calls) == 1

    # 服务随后宕机：缓存期内仍返回 True（不再探测）
    _FakeAsyncClient.exc = httpx.ConnectError("down")
    assert await is_rerank_available() is True
    assert len([c for c in _FakeAsyncClient.calls if c["method"] == "GET"]) == 1  # 无新增调用


@pytest.mark.asyncio
async def test_is_rerank_available_false_when_disabled(monkeypatch):
    """RERANK_ENABLED=False → 恒 False，零 HTTP 开销。"""
    monkeypatch.setattr(settings, "RERANK_ENABLED", False)
    assert await is_rerank_available() is False
    assert _FakeAsyncClient.calls == []


@pytest.mark.asyncio
async def test_rerank_metrics_recorded():
    """指标记录：成功 ok / 失败 error。"""
    before_ok = metrics.rerank_calls.get("ok", 0)
    before_err = metrics.rerank_calls.get("error", 0)

    _FakeAsyncClient.reset(
        response=_FakeResponse(json_data={"results": [{"index": 0, "relevance_score": 0.9}]})
    )
    await rerank("q", [_hit(1, "a")], top_n=1)
    assert metrics.rerank_calls.get("ok", 0) == before_ok + 1

    _FakeAsyncClient.reset(response=_FakeResponse(status_code=502))
    with pytest.raises(RerankError):
        await rerank("q", [_hit(1, "a")], top_n=1)
    assert metrics.rerank_calls.get("error", 0) == before_err + 1
