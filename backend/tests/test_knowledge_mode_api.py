"""KBX 模式、文档状态与任务 API 集成测试。"""

import uuid

import pytest

AUTH_A = {"X-API-Key": "test-key-1", "X-Tenant-Id": "kbx-api-a"}
AUTH_B = {"X-API-Key": "test-key-1", "X-Tenant-Id": "kbx-api-b"}


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_legacy_create_defaults_to_standard(app_client):
    response = await app_client.post(
        "/api/v1/knowledge-bases", json={"name": unique("standard")}, headers=AUTH_A
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["index_mode"] == "standard"
    assert data["graph_build_policy"] == "manual"
    assert data["index_status"] == "empty"
    assert data["graph_status"] == "disabled"


@pytest.mark.asyncio
async def test_graph_enhanced_manual_upload_has_three_stages(app_client, monkeypatch):
    monkeypatch.setattr("app.knowledge.service._schedule_parse", lambda doc_id: None)
    response = await app_client.post(
        "/api/v1/knowledge-bases",
        json={
            "name": unique("graph-manual"),
            "index_mode": "graph_enhanced",
            "graph_build_policy": "manual",
        },
        headers=AUTH_A,
    )
    kb_id = response.json()["data"]["kb_id"]
    upload = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/upload",
        files={"file": ("org.md", b"# Org\nAlice works for Example.", "text/markdown")},
        data={"graph_override": "inherit", "parse_method": "text"},
        headers=AUTH_A,
    )
    assert upload.status_code == 200
    data = upload.json()["data"]
    assert data["parse_status"] == "pending"
    assert data["parse_method"] == "text"
    assert data["vector_status"] == "pending"
    assert data["graph_status"] == "pending"
    assert len(data["job_ids"]) == 1

    jobs = await app_client.get(
        f"/api/v1/knowledge-bases/{kb_id}/jobs?status=active", headers=AUTH_A
    )
    assert jobs.status_code == 200
    assert jobs.json()["data"][0]["job_type"] == "parse"

    hidden = await app_client.get(
        f"/api/v1/knowledge-bases/{kb_id}/jobs?status=active", headers=AUTH_B
    )
    assert hidden.status_code == 404

    deleted = await app_client.delete(
        f"/api/v1/knowledge-bases/{kb_id}", headers=AUTH_A
    )
    assert deleted.status_code == 200


@pytest.mark.asyncio
async def test_upload_requires_parse_method(app_client, monkeypatch):
    """上传必须显式选择解析方式，不能静默走远程 OCR。"""
    monkeypatch.setattr("app.knowledge.service._schedule_parse", lambda doc_id: None)
    created = await app_client.post(
        "/api/v1/knowledge-bases", json={"name": unique("parse-required")}, headers=AUTH_A
    )
    kb_id = created.json()["data"]["kb_id"]

    response = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/upload",
        files={"file": ("guide.md", b"# Guide", "text/markdown")},
        headers=AUTH_A,
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_upload_rejects_unknown_parse_method(app_client, monkeypatch):
    """后端只接受受支持的解析方式，不能由客户端任意传值。"""
    monkeypatch.setattr("app.knowledge.service._schedule_parse", lambda doc_id: None)
    created = await app_client.post(
        "/api/v1/knowledge-bases", json={"name": unique("parse-invalid")}, headers=AUTH_A
    )
    kb_id = created.json()["data"]["kb_id"]

    response = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/upload",
        files={"file": ("guide.md", b"# Guide", "text/markdown")},
        data={"parse_method": "unknown"},
        headers=AUTH_A,
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_standard_document_include_returns_stable_conflict(app_client):
    created = await app_client.post(
        "/api/v1/knowledge-bases", json={"name": unique("conflict")}, headers=AUTH_A
    )
    kb_id = created.json()["data"]["kb_id"]
    response = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/documents/upload",
        files={"file": ("guide.md", b"# Guide", "text/markdown")},
        data={"graph_override": "include", "parse_method": "text"},
        headers=AUTH_A,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "INDEX_MODE_CONFLICT"


@pytest.mark.asyncio
async def test_mode_transition_reports_scope(app_client):
    created = await app_client.post(
        "/api/v1/knowledge-bases", json={"name": unique("transition")}, headers=AUTH_A
    )
    kb_id = created.json()["data"]["kb_id"]
    response = await app_client.post(
        f"/api/v1/knowledge-bases/{kb_id}/index-mode-transition",
        json={
            "target_mode": "graph_enhanced",
            "graph_build_policy": "manual",
            "build_existing_documents": True,
        },
        headers=AUTH_A,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["affected_documents"] == 0
    assert data["estimated_chunks"] == 0
    detail = await app_client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=AUTH_A)
    assert detail.json()["data"]["kb"]["index_mode"] == "graph_enhanced"
