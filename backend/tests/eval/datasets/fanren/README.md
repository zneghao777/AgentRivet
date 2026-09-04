# 《凡人修仙传》检索测评集

这组数据可直接用于当前项目的知识库“检索评测”面板或
`POST /api/v1/knowledge-bases/{kb_id}/evaluate` 接口。数据已绑定
当前本机 `凡人修仙传` 知识库的分块 ID。

## 当前数据绑定

这些 ID 来自 September 2, 2026 的本机索引快照：

- 知识库 ID：`72c7d944-1fa0-4501-9ab4-ee6831208f41`
- 租户：`default`
- 文档 ID：`1153`
- 源文件：`《凡人修仙传》-UTF8.txt`
- 分块数：`9674`
- 分块 ID 范围：`4526` 到 `14199`

> **Warning:** 重新解析文档、删除后重新上传文档或重建分块，可能生成
> 新的 `chunk_id`。发生这些操作后，必须重新标注本目录中的 ID。

## 数据集划分

三份数据分别观察不同检索能力。不要把它们混成一个总分后再
判断优化效果。

同一事实可能在小说中多次出现。`expected_chunk_ids` 包含经过人工复核、
能够独立支持参考答案的多个分块，避免把正确的替代证据误判为未命中。

- `lexical.json`：13 条带有明确专名和术语的问题，主要检查
  关键词召回。
- `semantic.json`：13 条口语化或间接表达的问题，主要检查
  向量语义召回。
- `relational.json`：13 条因果、用途和关系组合问题，检查较长查询和
  复合事实。

## 在界面运行

界面运行不需要额外脚本。按以下步骤执行每一份数据集：

1. 打开 `凡人修仙传` 知识库详情页。
2. 进入 **检索评测** 标签页。
3. 复制一份 JSON 文件的完整数组到文本框。
4. 保留 `baseline`、`hybrid` 和 `hybrid+rerank` 三个 variant。
5. 单击 **运行评测**，比较 `HitRate@k`、`MRR@k` 和逐条排名。

当前界面请求没有发送 `judge: true`，因此界面只执行检索评测。
JSON 中的 `reference_answer` 会随数据集保存，但不会触发 LLM judge。

## 通过 API 运行

API 方式可以显式开启或关闭 LLM judge。以下命令从 JSON 文件
构造请求体；把 `YOUR_API_KEY` 替换为当前后端 API Key。

```bash
cd /Users/zenghao/CodeBuddy/MyAgentDemo1

jq -n \
  --arg name "凡人修仙传-lexical" \
  --slurpfile dataset backend/tests/eval/datasets/fanren/lexical.json \
  '{name: $name, dataset: $dataset[0],
    variants: ["baseline", "hybrid", "hybrid+rerank"], judge: false}' \
| curl --fail-with-body \
    -H 'Content-Type: application/json' \
    -H 'X-API-Key: YOUR_API_KEY' \
    -H 'X-Tenant-Id: default' \
    --data-binary @- \
    http://127.0.0.1:8000/api/v1/knowledge-bases/72c7d944-1fa0-4501-9ab4-ee6831208f41/evaluate \
| jq '.data.results'
```

把文件名和 `name` 换成 `semantic` 或 `relational` 即可运行另外两组。
仅在已配置可用回答模型、并接受额外模型调用成本时，把 `judge`
改为 `true`。

## 如何解释结果

当前评测固定使用 `RAG_TOP_K`，默认值是 `5`。每个 variant 对
每条 query 执行一次检索：

- `baseline`：纯向量召回，不执行 rerank。
- `hybrid`：向量与 `pg_trgm` 关键词召回，经 RRF 融合，
  不执行 rerank。
- `hybrid+rerank`：混合召回和 RRF 后，再执行 Cross-Encoder rerank。
- `HitRate@5`：Top 5 中出现任一标注分块的用例比例。
- `MRR@5`：第一个标注分块排名倒数的平均值；第一名命中记 `1`，
  第二名命中记 `0.5`，未命中记 `0`。

关系集可能为一个问题标注多个证据块，但当前 HitRate 和 MRR
只要求命中其中任意一个。因此该集合能检查“是否找到相关证据”，
不能证明多个证据均被召回，也不能证明最终回答完整。

## 建议比较方式

先分别比较每组，再查看逐条排名变化。可采用以下最低观察线作为
本次回归基线，但不要把它当作未经实测的验收结论：

- `lexical`：观察 `hybrid` 是否优于或不低于 `baseline`。
- `semantic`：观察纯向量是否能召回不含原文专名的表达。
- `relational`：重点检查逐条 Top 5，不只看总体 HitRate。
- `hybrid+rerank`：观察 MRR 是否提升，以及相关块是否更靠前。

## 已知边界

这套评测只覆盖已有正例。当前项目要求每条用例至少包含一个
`expected_chunk_ids` 或 `expected_doc_ids`，所以无法直接表达
“正确结果应为空”的拒答负例。整本小说只有一个文档，也不适合用
`expected_doc_ids: [1153]` 做质量评测：任何来自该文档的分块
都会被算作命中，结果会接近无意义的满分。

`reference_answer` 在 `judge: true` 时被当作待评分答案，而不是
与系统回答比较的标准答案。当前 judge 只给出 faithfulness 和
relevancy，不计算答案正确率。

## Rerank 服务要求

项目需要 Cross-Encoder rerank 服务提供 `POST /rerank`，并返回
`results[].index` 和 `results[].relevance_score`。macOS 开发机运行：

```bash
./start-reranker.sh start
```

脚本使用独立虚拟环境加载 `BAAI/bge-reranker-v2-m3`，默认在
`http://127.0.0.1:1235/v1` 提供兼容接口。仅把该模型作为 LM Studio
embedding 模型加载不等价于 Cross-Encoder rerank。

## Next steps

运行三组评测并保存首次结果后，再调整分块、Embedding、RRF 或
rerank 参数。每次只改变一个变量，并使用同一份 JSON 重跑，
才能把指标变化归因到该变量。
