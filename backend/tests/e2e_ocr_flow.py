"""OCR 端到端联调：上传图片 → GLM OCR 解析 → 分块 → 向量化 → 检索。

依赖：
- 本地后端运行于 127.0.0.1:8000
- .env 已配置 GLM_FILES_API_KEY 与 EMBEDDING_BASE_URL（BGE-M3）
- 测试图片 /tmp/ocr_test.png（白底黑字中文：员工手册 / 休假制度 / 年假 / 提前申请）

用法: .venv/bin/python tests/e2e_ocr_flow.py
"""

from __future__ import annotations

import sys
import time
import uuid

import httpx

BASE = "http://127.0.0.1:8000/api/v1"
HEADERS = {"X-API-Key": "sk-dev-key-1", "X-Tenant-Id": "e2e-ocr"}
IMG_PATH = "/tmp/ocr_test.png"


def check(ok: bool, label: str) -> None:
    print(("  ✓ " if ok else "  ✗ ") + label)
    if not ok:
        sys.exit(f"OCR E2E 失败: {label}")


def main() -> None:
    client = httpx.Client(timeout=60)
    name = f"ocr-{uuid.uuid4().hex[:6]}"

    # 1. 创建知识库
    resp = client.post(
        f"{BASE}/knowledge-bases", json={"name": name, "description": "OCR 联调库"}, headers=HEADERS
    )
    check(resp.status_code == 200 and resp.json()["code"] == 0, "创建知识库")
    kb_id = resp.json()["data"]["kb_id"]

    # 2. 上传图片（触发 GLM OCR 异步解析）
    with open(IMG_PATH, "rb") as f:
        image_bytes = f.read()
    resp = client.post(
        f"{BASE}/knowledge-bases/{kb_id}/documents/upload",
        files={"file": ("ocr_manual.png", image_bytes, "image/png")},
        data={"replace": "true", "parse_method": "ocr"},
        headers=HEADERS,
    )
    check(resp.status_code == 200, "上传图片成功")
    body = resp.json()["data"]
    check(body["parse_status"] == "pending", "上传返回 pending")
    doc_id = body["doc_id"]

    # 3. 轮询 OCR 解析完成（GLM 处理约 10-20s）
    doc = None
    for _ in range(40):
        time.sleep(1.0)
        resp = client.get(f"{BASE}/knowledge-bases/{kb_id}", headers=HEADERS)
        detail = resp.json()["data"]
        doc = next((d for d in detail["documents"] if d["doc_id"] == doc_id), None)
        if doc and doc["parse_status"] in ("parsed", "failed"):
            break
    check(doc is not None, "文档出现在列表中")
    check(doc["parse_status"] == "parsed", f"OCR 解析完成 parse_status={doc['parse_status']} error={doc.get('error')}")
    check(doc["parse_source"] == "glm", f"解析来源为 GLM（parse_source={doc['parse_source']}）")
    check(doc["chunk_count"] >= 1, f"产生分块 {doc['chunk_count']} 个")

    # 4. 分块列表（OCR 文本内容）
    resp = client.get(
        f"{BASE}/knowledge-bases/{kb_id}/chunks",
        params={"doc_id": doc_id, "page_size": 20},
        headers=HEADERS,
    )
    items = resp.json()["data"]["items"]
    check(len(items) >= 1, "分块列表非空")
    ocr_text = items[0]["text"] if items else ""
    print(f"   OCR 识别文本预览: {ocr_text[:80]}...")
    check("年假" in (ocr_text + (items[1]["text"] if len(items) > 1 else "")), "OCR 识别出「年假」关键词")

    # 5. 检索验证（真实 BGE-M3 向量化后召回 OCR 文本）
    resp = client.post(
        f"{BASE}/knowledge-bases/{kb_id}/test-search",
        json={"query": "员工每年有多少天带薪年假", "top_k": 3},
        headers=HEADERS,
    )
    hits = resp.json()["data"]
    check(len(hits) >= 1, f"OCR 文本被检索召回（{len(hits)} 条）")
    if hits:
        hit = hits[0]
        check(hit["source_file"] == "ocr_manual.png", "命中来源为图片文档")
        check(hit.get("chunk_id"), "命中携带 chunk_id 溯源主键")
        print(f"   top-1 score={hit['score']} chunk={hit['chunk_id']}")
        print(f"   召回文本: {hit['text'][:80]}...")

    # 6. 清理
    client.delete(f"{BASE}/knowledge-bases/{kb_id}", headers=HEADERS)
    print("\n✅ OCR 端到端跑通：图片上传 → GLM OCR 解析 → 分块向量化 → 语义检索")


if __name__ == "__main__":
    main()
