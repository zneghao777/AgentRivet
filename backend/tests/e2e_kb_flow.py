"""端到端联调脚本（KB_MANAGEMENT_SPEC §6 K4）：真实 BGE-M3 + 本地解析。

流程：创建库 → 上传 → 轮询解析 → 分块列表 → 编辑分块 → test-search → 溯源锚点。
依赖本地后端运行于 127.0.0.1:8000，API Key 默认 sk-dev-key-1。
用法: .venv/bin/python tests/e2e_kb_flow.py
"""

from __future__ import annotations

import os
import re
import sys
import time
import uuid

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8000/api/v1"
HEADERS = {"X-API-Key": "sk-dev-key-1", "X-Tenant-Id": "e2e-tenant"}
ANCHOR_RE = re.compile(r"KB-([0-9a-fA-F-]{36})/(\d+)")

DOC_MD = (
    "# 员工手册 v2\n\n"
    "## 休假制度\n"
    "正式员工每年享有 15 天带薪年假，需提前 3 天申请。\n\n"
    "## 考勤规则\n"
    "工作日为周一至周五，弹性工作时间为 9:00-18:00。\n"
)


def check(ok: bool, label: str) -> None:
    print(("  ✓ " if ok else "  ✗ ") + label)
    if not ok:
        sys.exit(f"E2E 失败: {label}")


def main() -> None:
    client = httpx.Client(timeout=30)
    name = f"e2e-{uuid.uuid4().hex[:6]}"

    # 1. 创建知识库
    resp = client.post(f"{BASE}/knowledge-bases", json={"name": name, "description": "端到端联调库"}, headers=HEADERS)
    check(resp.status_code == 200 and resp.json()["code"] == 0, f"创建知识库 {name}")
    kb_id = resp.json()["data"]["kb_id"]

    # 2. 上传文档（异步解析）
    resp = client.post(
        f"{BASE}/knowledge-bases/{kb_id}/documents/upload",
        files={"file": ("manual.md", DOC_MD.encode("utf-8"), "text/markdown")},
        data={"replace": "true", "parse_method": "text"},
        headers=HEADERS,
    )
    check(resp.status_code == 200 and resp.json()["data"]["parse_status"] == "pending", "上传返回 pending")
    doc_id = resp.json()["data"]["doc_id"]

    # 3. 轮询解析完成
    parse_status = "pending"
    for _ in range(40):
        time.sleep(0.5)
        resp = client.get(f"{BASE}/knowledge-bases/{kb_id}", headers=HEADERS)
        doc = next((d for d in resp.json()["data"]["documents"] if d["doc_id"] == doc_id), None)
        if doc and doc["parse_status"] in ("parsed", "failed"):
            parse_status = doc["parse_status"]
            break
    check(parse_status == "parsed", f"解析完成 parse_status={parse_status} source={doc['parse_source'] if doc else '?'}")
    check((doc or {}).get("chunk_count", 0) >= 3, f"分块数 {doc['chunk_count']} >= 3")

    # 4. 分块预览
    resp = client.post(
        f"{BASE}/knowledge-bases/{kb_id}/documents/{doc_id}/preview",
        json={"text": DOC_MD, "chunk_size": 500},
        headers=HEADERS,
    )
    check(resp.status_code == 200 and len(resp.json()["data"]) >= 3, "分块预览返回多块")

    # 5. 分块列表 + 编辑分块
    resp = client.get(f"{BASE}/knowledge-bases/{kb_id}/chunks", params={"doc_id": doc_id, "page_size": 50}, headers=HEADERS)
    items = resp.json()["data"]["items"]
    check(len(items) >= 3, f"分块列表 {len(items)} 条")
    chunk = items[0]
    new_text = chunk["text"] + "\n（已编辑补充，向量应重算）"
    resp = client.put(f"{BASE}/knowledge-bases/{kb_id}/chunks/{chunk['chunk_id']}", json={"text": new_text}, headers=HEADERS)
    check(resp.status_code == 200 and resp.json()["data"]["text"] == new_text, "编辑分块成功（向量重算）")

    # 6. 测试检索（真实 BGE-M3）
    resp = client.post(f"{BASE}/knowledge-bases/{kb_id}/test-search", json={"query": "员工年假多少天", "top_k": 3}, headers=HEADERS)
    check(resp.status_code == 200, "test-search 返回 200")
    hits = resp.json()["data"]
    check(len(hits) >= 1, f"检索命中 {len(hits)} 条")
    if hits:
        hit = hits[0]
        check(hit.get("kb_id") == kb_id and hit.get("chunk_id"), "命中携带 kb_id/chunk_id 溯源主键")
        check(hit.get("source_file") == "manual.md", "命中来源正确")
        print(f"   top-1 命中: score={hit['score']} file={hit['source_file']} chunk={hit['chunk_id']}")
        print(f"   文本预览: {hit['text'][:60]}...")

    # 7. 停用分块后不再被检索
    resp = client.patch(f"{BASE}/knowledge-bases/{kb_id}/chunks/{chunk['chunk_id']}/enabled", json={"enabled": False}, headers=HEADERS)
    check(resp.status_code == 200 and resp.json()["data"]["enabled"] is False, "停用分块成功")
    resp = client.post(f"{BASE}/knowledge-bases/{kb_id}/test-search", json={"query": new_text[:20], "top_k": 10}, headers=HEADERS)
    check(all(h["chunk_id"] != chunk["chunk_id"] for h in resp.json()["data"]), "停用分块不再命中")

    # 8. 工具锚点格式（与 hits_to_markdown 一致）
    from app.knowledge.retriever import Hit, hits_to_markdown

    import uuid as uuid_mod

    fake_hit = Hit(
        text="x", source_file="manual.md", chunk_index=0, score=0.9,
        kb_id=uuid_mod.UUID(kb_id), chunk_id=chunk["chunk_id"],
    )
    md = hits_to_markdown([fake_hit])
    anchors = ANCHOR_RE.findall(md)
    check(len(anchors) == 1 and anchors[0][1] == str(chunk["chunk_id"]), f"溯源锚点格式正确: {anchors}")

    # 清理
    client.delete(f"{BASE}/knowledge-bases/{kb_id}", headers=HEADERS)
    print("\n✅ 端到端联调全部通过（上传→解析→分块→编辑→向量化→检索→溯源锚点）")


if __name__ == "__main__":
    main()
