"""macOS 本地 Rerank FastAPI 服务契约测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.rerank_server import RerankServerSettings, create_app


class _FakeRuntime:
    model_id = "BAAI/bge-reranker-v2-m3"
    served_model_name = "text-embedding-bge-reranker-v2-m3"
    device = "mps"
    loaded = True

    def load(self) -> None:
        self.loaded = True

    def rerank(self, query: str, documents: list[str], top_n: int):
        assert query
        scored = sorted(
            enumerate(documents),
            key=lambda item: ("大衍诀" in item[1], len(item[1])),
            reverse=True,
        )
        return [(index, 0.9 - rank * 0.1) for rank, (index, _) in enumerate(scored[:top_n])], 42


def _client(*, max_documents: int = 128) -> TestClient:
    settings = RerankServerSettings(max_documents=max_documents)
    return TestClient(create_app(settings=settings, runtime=_FakeRuntime()))


def test_health_and_models_contract():
    with _client() as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {
            "status": "ok",
            "model": "text-embedding-bge-reranker-v2-m3",
            "source_model": "BAAI/bge-reranker-v2-m3",
            "device": "mps",
        }

        models = client.get("/v1/models")
        assert models.status_code == 200
        assert models.json()["data"][0]["id"] == "text-embedding-bge-reranker-v2-m3"


def test_rerank_response_matches_main_backend_contract():
    with _client() as client:
        response = client.post(
            "/v1/rerank",
            json={
                "model": "text-embedding-bge-reranker-v2-m3",
                "query": "大衍诀的核心作用是什么？",
                "documents": ["天气晴朗。", "大衍诀可以强化神识并操纵傀儡。"],
                "top_n": 2,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "text-embedding-bge-reranker-v2-m3"
    assert body["usage"] == {"total_tokens": 42}
    assert body["results"][0]["index"] == 1
    assert body["results"][0]["relevance_score"] == 0.9
    assert body["results"][0]["document"]["text"].startswith("大衍诀")


def test_rerank_accepts_source_model_name_and_clamps_top_n():
    with _client() as client:
        response = client.post(
            "/v1/rerank",
            json={
                "model": "BAAI/bge-reranker-v2-m3",
                "query": "q",
                "documents": ["a", "b"],
                "top_n": 9,
                "return_documents": False,
            },
        )

    assert response.status_code == 200
    assert len(response.json()["results"]) == 2
    assert "document" not in response.json()["results"][0]


def test_rerank_rejects_unknown_model_and_excess_documents():
    with _client(max_documents=1) as client:
        unknown = client.post(
            "/v1/rerank",
            json={"model": "unknown", "query": "q", "documents": ["a"]},
        )
        excessive = client.post(
            "/v1/rerank",
            json={"query": "q", "documents": ["a", "b"]},
        )

    assert unknown.status_code == 404
    assert excessive.status_code == 413


def test_rerank_rejects_blank_input():
    with _client() as client:
        blank_query = client.post(
            "/v1/rerank", json={"query": "  ", "documents": ["a"]}
        )
        blank_document = client.post(
            "/v1/rerank", json={"query": "q", "documents": ["  "]}
        )

    assert blank_query.status_code == 422
    assert blank_document.status_code == 422
