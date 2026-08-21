# Graph RAG 知识库方案设计

> 版本: v0.1(草案)
> 状态: 待评审
> 关联文档: [RAG_SPEC.md](./RAG_SPEC.md)、[AGENTIC_RAG_UPGRADE_SPEC.md](./AGENTIC_RAG_UPGRADE_SPEC.md)、[KB_MANAGEMENT_SPEC.md](./KB_MANAGEMENT_SPEC.md)

---

## 0. 背景与目标

### 0.1 现状

当前项目已具备一套完整的 **向量 RAG(Embedding RAG)** 能力:

```
上传文档 → 解析 → 分块(chunk_text) → 向量化(embed_texts) → 写入 KnowledgeChunk(pgvector)
检索: retrieve / retrieve_hybrid(向量召回 + 关键词 BM25 + 可选 Rerank)
工具: knowledge_search(StructuredTool) 注册进 Agent 主图
```

这套方案能回答"哪段原文与问题相关",但存在三个结构性短板:

| 短板 | 表现 | 典型场景(以《凡人修仙传》为例) |
|------|------|--------------------------------|
| **全局性问题弱** | 无法跨多个分散段落聚合推理 | "韩立一共经历了哪几次大境界突破?" |
| **多跳推理弱** | 相关答案分散在不同章节,单次向量召回召回不全 | "韩立和紫灵的关系是如何一步步建立的?" |
| **实体关系缺失** | 只存文本,不存实体与关系 | "七玄门的叛徒都有谁?"(需要角色-门派-事件关系链) |

### 0.2 目标

在不破坏现有向量 RAG 的前提下,**新增一条并行的图谱检索路径**,与向量检索形成"双通道融合":

1. **导入文档时自动构建知识图谱**(实体 + 关系 + 社区 + 事件时间线);
2. 提供 **图查询 / 图检索 / 图摘要(Cypher-free 的语义入口)** 能力;
3. 与现有 `KnowledgeBase` 多租户模型、`KnowledgeChunk` 表完全兼容,复用同一套 `tenant_id` 隔离;
4. 以**低侵入**方式接入 Agent(新增 `knowledge_graph_search` 工具,主图结构不变)。

### 0.3 非目标(本期不做)

- 不做流式实时图更新(改为异步后台索引任务);
- 不做跨库多图谱合并;
- 不重写现有 `KnowledgeChunk` / `retrieve_v2` 逻辑(图路径为并列新增);
- 不做复杂时空推理(如"三十年后的韩立"),仅覆盖角色/门派/事件/地点等常见实体。

---

## 1. 技术选型

### 1.1 存储选型

核心矛盾:**要不要引入独立的图数据库?**

| 方案 | 优点 | 缺点 | 结论 |
|------|------|------|------|
| **A. Neo4j** | 图查询能力强(Cypher)、生态成熟 | 新增重型依赖、运维成本高、需额外部署 | ❌ 不适合当前轻量架构 |
| **B. 内存图(NetworkX)序列化** | 零依赖、上手快 | 不可扩展、无法 SQL 查询、大图易 OOM | ❌ 仅适合原型 |
| **C. SQL 关系表建模(推荐)** | 复用现有 Postgres16、事务/多租户天然支持、可持久化、可 join 向量 | 多跳查询用递归 CTE 表达,需封装 | ✅ **推荐** |

**推荐:方案 C —— 用 Postgres 关系表承载图数据,把"图"物化为三张表 + 一层查询封装。**

理由:
- 当前已是 Postgres16 单库,无需新增中间件;
- 实体/关系的量级(单本小说几千实体、几万关系)完全在 SQL 能力范围内;
- 多租户(`tenant_id`)、备份、事务、鉴权全部复用现有能力;
- 图查询通过 **递归 CTE(Recursive CTE)** 实现 2~3 跳邻居遍历,性能足够;
- 若未来图规模爆炸,表结构可直接导出给 Neo4j(提供迁移脚本即可)。

### 1.2 抽取 LLM 选型

复用项目中已有的 **GLM 异步解析器**(`backend/app/knowledge/ingest.py` 中已有 GLM 解析能力)。实体/关系抽取不占用主 Agent 对话流,改为**异步后台任务**。

### 1.3 图谱检索算法

- **实体/关系精确与向量双召回**(实体名 BM25 + 实体描述向量);
- **社区摘要**(基于连通分量/Leiden 的粗粒度社区聚合,用于全局性问题);
- **图扩散(Graph Expansion)**:由种子实体沿关系边做 2~3 跳扩展,补充上下文。

---

## 2. 图数据模型(核心)

### 2.1 逻辑模型

```
(实体 Entity) ──[关系 Relation]──> (实体 Entity)
       │
       └── 事件(Event): 带有时间/顺序属性,可排序
       └── 属性(Property): 实体的属性键值对
       └── 归属(Chunk): 实体/关系出现的原文位置(chunk_id 关联)
```

### 2.2 物理模型(三张核心表 + 辅助表)

新增以下表,全部挂在 `KnowledgeBase` 之下,全部带 `tenant_id` 隔离:

#### 表 1: `graph_entities` — 图实体

| 列 | 类型 | 说明 |
|----|------|------|
| id | UUID PK | 实体主键 |
| tenant_id | UUID FK | 租户隔离(必填,索引) |
| kb_id | UUID FK | 所属知识库 |
| doc_id | UUID FK(可空) | 来源文档(跨文档实体可为空) |
| entity_type | VARCHAR | 实体类型: person / place / organization / item / cultivation(功法境界) / event / concept |
| name | VARCHAR | 实体名(归一化后的规范名) |
| canonical_name | VARCHAR | 规范名(用于合并别名,如"韩跑跑"→"韩立") |
| description | TEXT | 实体描述(LLM 抽取,用于向量化) |
| meta | JSONB | 附加属性(修为、境界、门派等) |
| embedding | VECTOR(1024) | 实体描述向量(用于实体语义召回) |
| created_at / updated_at | TIMESTAMPTZ | 时间戳 |
| chunk_ids | UUID[] | 关联的原文 chunk(倒排锚点) |

#### 表 2: `graph_relations` — 图关系

| 列 | 类型 | 说明 |
|----|------|------|
| id | UUID PK | 关系主键 |
| tenant_id | UUID FK | 租户隔离 |
| kb_id | UUID FK | 所属知识库 |
| source_entity_id | UUID FK | 起点实体 |
| target_entity_id | UUID FK | 终点实体 |
| relation_type | VARCHAR | 关系类型: 属于 / 位于 / 修行 / 拥有 / 杀害 / 师徒 / 联盟 / 敌人 / 发现 / 来自 … |
| description | TEXT | 关系描述(证据句) |
| weight | FLOAT | 关系强度/置信度(0~1) |
| source_chunk_id | UUID | 证据原文 chunk |
| created_at | TIMESTAMPTZ | 时间戳 |

> 为加速遍历,建联合索引 `(source_entity_id, target_entity_id, relation_type)` 与 `(kb_id, tenant_id)`。

#### 表 3: `graph_communities` — 图社区(摘要单元)

| 列 | 类型 | 说明 |
|----|------|------|
| id | UUID PK | 社区主键 |
| tenant_id / kb_id | UUID FK | 隔离与归属 |
| level | INT | 社区层级(0 = 最细粒度) |
| summary | TEXT | 社区摘要(LLM 生成,用于全局问答) |
| keywords | TEXT[] | 社区关键词 |
| member_entity_ids | UUID[] | 社区成员实体(用于反查) |
| created_at | TIMESTAMPTZ | 时间戳 |

#### 表 4(辅助): `graph_events` — 事件时间线

| 列 | 类型 | 说明 |
|----|------|------|
| id | UUID PK | 事件主键 |
| tenant_id / kb_id / doc_id | UUID FK | 归属 |
| event_type | VARCHAR | 事件类型(进阶/结盟/大战/死亡…) |
| subject_entity_id | UUID | 事件主体实体 |
| predicate | VARCHAR | 事件动作(例:突破至) |
| object | VARCHAR | 事件对象(例:结丹期) |
| order_no | INT | 文档内事件顺序(用于时间线排序) |
| source_chunk_id | UUID | 证据原文 |
| summary | TEXT | 事件一句话描述 |

### 2.3 与现有表的关系

```
KnowledgeBase (tenant_id, kb_id)
   ├── DocumentMeta (doc_id)
   │     └── KnowledgeChunk (chunk_id, embedding)   ← 现有向量检索路径
   └── graph_entities (kb_id, entity_type, name, embedding)
         ├── graph_relations (source→target)
         ├── graph_communities (level, summary)
         └── graph_events (order_no 时间线)
```

- `graph_entities.chunk_ids` 通过 `chunk_id` 回指 `KnowledgeChunk`,实现 **"实体 → 原文段落"** 的双向跳转;
- 向量检索命中某个 chunk 时,可反查该 chunk 涉及的实体,再走图扩散补充上下文。

---

## 3. 文档导入时如何构建图谱(核心流程)

以 **《凡人修仙传》** 为例,完整说明导入流程。

### 3.1 总体流水线(挂到现有 ingest 之后,异步执行)

```
现有 ingest:  上传 → 解析 → 分块 → 向量化 → 写 KnowledgeChunk
                          │ (完成后触发)
Graph 流水线:  ┌────────────────────────────────────────────┐
               │  1. 实体抽取(entity extraction)            │
               │  2. 关系抽取(relation extraction)          │
               │  3. 事件抽取(event extraction)             │
               │  4. 实体归一化(entity resolution/对齐)     │
               │  5. 社区发现(community detection + 摘要)   │
               │  6. 落库 + 索引                             │
               └────────────────────────────────────────────┘
```

### 3.2 各步骤详解(以《凡人修仙传》为例)

#### 步骤 1 — 实体抽取

**输入**:每个 chunk 的文本。
**处理**:调用 GLM,按预设 schema 抽出实体。

> 为控制成本与幻觉,采用**两阶段**:
> 1. **候选抽取**:让 LLM 从 chunk 中抽出所有 `(实体名, 类型, 描述)` 候选;
> 2. **去重归一**:用规则 + 轻量分类器对候选做过滤(丢弃无信息量的名词,如"天地""道法")。

**《凡人修仙传》示例输出**:

| 实体名 | 类型 | 描述 |
|--------|------|------|
| 韩立 | person | 主角,七玄门入门弟子,后拜入墨大夫门下 |
| 紫灵 | person | 韩立的道侣,合欢宗出身 |
| 墨大夫 | person | 七玄门医师,韩立的第一个师父 |
| 七玄门 | organization | 韩立初入修真界所在门派 |
| 乱星海 | place | 韩立后期修行与历练的主要海域 |
| 大衍神君 | person | 上古大能,留下大衍诀 |
| 结丹期 | cultivation | 修仙境界之一 |
| 噬金虫 | item | 韩立早期获得的奇虫 |
| 掌天瓶 | item | 韩立的金手指法宝 |

**存储**:写入 `graph_entities`。`description` 字段用于后续向量化。

#### 步骤 2 — 关系抽取

**输入**:每个 chunk 文本 + 已抽取实体。
**处理**:LLM 抽出实体间的 `(source, relation_type, target, description, weight)`。

**《凡人修仙传》示例输出**:

| 起点 | 关系 | 终点 | 证据句 |
|------|------|------|--------|
| 韩立 | 拜师于 | 墨大夫 | "韩立拜入墨大夫门下" |
| 韩立 | 属于 | 七玄门 | "韩立是七玄门弟子" |
| 韩立 | 修行 | 结丹期 | "韩立突破至结丹期" |
| 韩立 | 获得 | 掌天瓶 | "韩立获得了神秘小瓶" |
| 韩立 | 结为道侣 | 紫灵 | "韩立与紫灵结为道侣" |
| 墨大夫 | 属于 | 七玄门 | "墨大夫是七玄门的医师" |
| 乱星海 | 位于 | 天南 | "乱星海位于天南海域" |
| 大衍神君 | 留下 | 大衍诀 | "大衍神君留下大衍诀传承" |

**存储**:写入 `graph_relations`。`weight` 表示置信度(多次出现 +1,矛盾则 -1)。

#### 步骤 3 — 事件抽取

**输入**:每个 chunk。
**处理**:LLM 抽取出有明确动作和顺序的事件 `(subject, predicate, object, order_no)`。

**《凡人修仙传》示例**:

| order | 主体 | 动作 | 对象 | 事件摘要 |
|-------|------|------|------|----------|
| 1 | 韩立 | 拜入 | 七玄门 | 韩立进入七玄门修行 |
| 2 | 韩立 | 获得 | 掌天瓶 | 韩立得到掌天瓶 |
| 3 | 韩立 | 拜师 | 墨大夫 | 韩立拜墨大夫为师 |
| 4 | 韩立 | 突破至 | 结丹期 | 韩立结丹成功 |
| 5 | 韩立 | 结为道侣 | 紫灵 | 韩立与紫灵成婚 |

**存储**:写入 `graph_events`。`order_no` 基于文档章节顺序累加,保证时间线可排序。

#### 步骤 4 — 实体归一化(实体对齐)

**问题**:同一实体在书中常有多别名/异写。
- "韩立" / "韩跑跑" / "韩道友" → **合并为 canonical_name="韩立"**
- "紫灵" / "紫灵仙子" → **合并为 "紫灵"**

**处理策略**:
1. **精确别名表**(项目可维护 `alias_rules`):命中直接归并;
2. **向量相似度**:两实体描述向量 cosine 相似度 > 阈值(如 0.9)且同类型 → 合并;
3. **LLM 确认**:低置信度候选由 LLM 判定是否同指。

**存储**:被合并的实体统一 `canonical_name`,关系表全部指向规范实体 id。

> ⚠️ 归一化是质量关键,做不好会产生产品噪音(同一个"韩立"出现多个节点)。方案建议归一化准确率优先于召回率:宁可漏合,不可错合。

#### 步骤 5 — 社区发现 + 摘要

**目的**:让图谱能回答**全局性问题**(如"总结韩立的主要成长阶段")。

**处理**:
1. 用轻量社区检测(连通分量 + 基于关系权重的 Louvain/Leiden)把实体聚类成社区;
2. 对每个社区,把成员实体+关系汇总给 LLM,生成 `community.summary`;
3. 生成多级社区(level 0 细粒度,level 1 粗粒度)形成金字塔。

**《凡人修仙传》示例社区**:

| level | 社区 | 成员 | 摘要 |
|-------|------|------|------|
| 0 | "韩立-掌天瓶-噬金虫" | 韩立、掌天瓶、噬金虫 | 韩立核心机缘与法宝 |
| 0 | "七玄门-墨大夫-韩立" | 七玄门、墨大夫、韩立 | 韩立早期师门关系 |
| 0 | "紫灵-韩立" | 紫灵、韩立 | 道侣关系 |
| 1 | "韩立主线-境界成长" | 以上全部 | 韩立从凡人到修士的成长主线 |

#### 步骤 6 — 落库 + 索引

- 事务写入 `graph_entities` / `graph_relations` / `graph_events` / `graph_communities`;
- 对 `graph_entities.name` 建 BM25 全文索引,对 `embedding` 建 HNSW 向量索引;
- 更新 `KnowledgeBase.graph_status = 'ready'`。

### 3.3 图谱增量与更新策略

| 场景 | 策略 |
|------|------|
| 新增文档 | 对新增文档跑完整抽取,实体按 canonical_name 合并进已有图谱 |
| 重新解析文档 | 先删除该 `doc_id` 的实体/关系/事件,再重建(保证无脏数据) |
| 手动修正实体 | 提供"重命名/合并/删除实体"API,级联更新关系与社区 |
| 图谱重建 | 提供 `rebuild_graph(kb_id)` 接口,全量重跑 |

---

## 4. 图检索与查询(接入 Agent)

### 4.1 检索策略(三类问题 → 三类检索)

| 问题类型 | 示例 | 检索策略 |
|----------|------|----------|
| **单实体问题** | "韩立的师父是谁?" | 实体精确/向量召回 → 1 跳关系遍历 |
| **多跳推理问题** | "韩立和紫灵是怎么认识的?" | 实体召回 → 2~3 跳图扩散(Graph Expansion) |
| **全局性问题** | "韩立修仙的主要阶段?" | 社区摘要 MapReduce → 顶层社区摘要 + 细节补全 |

### 4.2 查询原语(封装为 Python 接口,不暴露 Cypher)

`backend/app/knowledge/graph_retriever.py`:

```python
class GraphRetriever:
    async def query_entity(self, name: str, kb_id: str, hop: int = 1) -> EntityResult
        # 精确名 + 向量召回实体 → 返回实体 + 1~N 跳邻居关系

    async def expand(self, seed_entity_ids: list[str], kb_id: str, hop: int = 2) -> list[Relation]
        # 递归 CTE 沿关系边做 2~3 跳扩散,收集证据 chunk

    async def community_summary(self, question: str, kb_id: str) -> str
        # 先向量召回相关社区,聚合社区摘要回答问题

    async def timeline(self, entity_name: str, kb_id: str) -> list[Event]
        # 按 order_no 返回实体的事件时间线
```

### 4.3 工具接入(低侵入)

新增 `backend/app/tools/knowledge_graph_search.py`,参照现有 `knowledge_search` 的注册方式:

```python
register_knowledge_graph_tools(registry)  # 注册为 StructuredTool
```

**工具参数**:`query`(自然语言问题)、`kb_ids`、`strategy`(auto / entity / hop / global / timeline)。

**内部路由**:
1. `auto` 时先用实体召回判断问题是否含明确实体;
2. 含实体 → 走图扩散;无实体 → 走社区摘要;
3. 结果与向量检索结果**合并去重**,统一返回带 `chunk_id` 的证据段落。

### 4.4 与向量 RAG 的融合(最终召回)

**推荐:混合双通道,两路并行取并集,按相关性加权排序。**

```
用户问题
   ├─[向量通道] retrieve_v2 → 候选 chunk(现有)
   └─[图谱通道] graph_retriever → 候选实体 + 关系 + 证据 chunk
                ↓
        合并证据 chunk,去重,可选 Rerank
                ↓
        注入 LLM 生成答案
```

- 向量通道负责"原文精确匹配";
- 图谱通道负责"全局/多跳/关系型"补充;
- 两者以 `chunk_id` 为锚点统一,避免答案碎片化。

---

## 5. 目录结构与新增文件

```
backend/app/knowledge/
  ├── graph/                        # 新增图谱模块
  │   ├── __init__.py
  │   ├── models.py                 # GraphEntity/GraphRelation/GraphCommunity/GraphEvent ORM
  │   ├── extractor.py              # 实体/关系/事件抽取(调用 GLM)
  │   ├── resolver.py               # 实体归一化/别名合并
  │   ├── community.py              # 社区发现 + 摘要
  │   ├── builder.py                # 图谱构建流水线(编排 1~6 步)
  │   ├── graph_retriever.py        # 图查询/检索原语(4.2)
  │   └── repository.py             # 图谱表 CRUD + 递归 CTE 查询
backend/app/tools/
  ├── knowledge_graph_search.py     # 新增 Agent 工具(4.3)
backend/app/api/
  ├── router_knowledge_graph.py     # 图谱管理/查询 API(见 6)
backend/app/config.py               # 增加 graph 配置项
backend/app/storage/db.py           # 建表注册
```

---

## 6. API 设计

### 6.1 图谱管理

| Method | Path | 说明 |
|--------|------|------|
| POST | `/api/kbs/{kb_id}/graph/build` | 触发图谱构建(异步任务) |
| GET | `/api/kbs/{kb_id}/graph/status` | 查询构建进度/状态 |
| POST | `/api/kbs/{kb_id}/graph/rebuild` | 全量重建图谱 |
| GET | `/api/kbs/{kb_id}/graph/entities?type=&q=` | 查询实体列表(支持过滤) |
| GET | `/api/kbs/{kb_id}/graph/entities/{id}` | 实体详情 + 邻居 |
| PUT | `/api/kbs/{kb_id}/graph/entities/{id}` | 修正/合并实体 |
| DELETE | `/api/kbs/{kb_id}/graph/entities/{id}` | 删除实体(级联关系) |

### 6.2 图谱查询

| Method | Path | 说明 |
|--------|------|------|
| POST | `/api/kbs/{kb_id}/graph/search` | 语义图检索 body:`{query, strategy, hop}` |
| GET | `/api/kbs/{kb_id}/graph/timeline/{entity}` | 实体事件时间线 |

---

## 7. 配置项(backend/app/config.py 新增)

```python
class GraphConfig(BaseModel):
    enabled: bool = True                    # 图谱开关
    extract_model: str = "glm-4-flash"      # 抽取模型(默认用低成本模型)
    batch_size: int = 8                     # 每批 chunk 抽取数
    max_entities_per_chunk: int = 30        # 单 chunk 实体上限(防爆炸)
    entity_vector_model: str = "embed-text-v3"  # 复用现有向量模型
    alias_similarity_threshold: float = 0.90   # 实体合并阈值
    max_hop: int = 3                        # 图扩散最大跳数
    community_enabled: bool = True          # 社区摘要开关
    concurrency: int = 4                    # 异步抽取并发
```

---

## 8. 质量与成本控制

### 8.1 成本控制(针对长文档尤其重要)

《凡人修仙传》约 2400 章、数百万字。**必须控制抽取成本**:

| 手段 | 说明 |
|------|------|
| 批量合并 chunk | 每批合并 8~10 个 chunk 给 LLM,减少调用次数 |
| 低成本抽取模型 | 用 `glm-4-flash` 等便宜模型,主 Agent 用高质量模型 |
| 去重去噪 | 候选实体过滤,丢弃无信息量名词 |
| 增量构建 | 只对新文档抽取,复用已有实体 |
| 抽样验证 | 默认抽取完整,但提供"仅构建骨架/社区"的降级模式 |

### 8.2 质量评估

- **实体抽取准确率/召回率**:抽样 50 个 chunk,人工标注对比;
- **关系事实准确率**:重点检查 `weight` 高但事实错误的关系;
- **归一化正确率**:检查别名合并是否错并/漏并;
- **端到端问答评测**:与纯向量 RAG 对比"全局问题/多跳问题"的答案质量(Faithfulness + Answer Relevancy)。

---

## 9. 落地步骤(建议排期)

| 阶段 | 内容 | 产出 |
|------|------|------|
| **P0** | 建表 + ORM + 递归 CTE 查询封装 | `graph_*` 表可读写 |
| **P1** | 实体/关系/事件抽取(接 GLM) | 导入小说能生成图谱 |
| **P2** | 实体归一化 + 社区摘要 | 图谱质量达标 |
| **P3** | `graph_retriever` + `knowledge_graph_search` 工具 + 双通道融合 | Agent 可用图检索 |
| **P4** | 管理/查询 API + 前端入口 + 文档 | 完整可交付 |

---

## 10. 以《凡人修仙传》为例的端到端演示

**导入动作**:用户上传《凡人修仙传》.txt → 系统:
1. 走现有 ingest 分块、向量化;
2. 异步触发图谱构建 → 抽出 **韩立、紫灵、墨大夫、七玄门、乱星海、掌天瓶、噬金虫、大衍神君、结丹期** 等实体;
3. 抽出 **拜师于、属于、获得、修行、结为道侣** 等关系;
4. 归一化:"韩跑跑"→"韩立";"紫灵仙子"→"紫灵";
5. 生成社区摘要 + 事件时间线。

**提问**:用户问"韩立的成长经历了哪几个主要阶段?"
- 向量通道:召回若干"突破境界"的段落;
- 图谱通道:召回社区摘要 → "韩立从七玄门入门 → 结丹 → 元婴 → 化神 → 大乘"的成长主线;
- 融合输出:一段有阶段划分、有证据支撑的完整回答。

**提问**:用户问"韩立和紫灵是怎么认识的?"
- 图谱通道:韩立 → 2 跳扩散 → 找到紫灵,证据 chunk 描述相识过程;
- 补充:向量通道召回相识段落原文,共同输出。

---

## 11. 附录:建表 SQL(示意)

```sql
CREATE TABLE graph_entities (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     UUID NOT NULL,
  kb_id         UUID NOT NULL,
  doc_id        UUID,
  entity_type   VARCHAR(32) NOT NULL,
  name          TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  description   TEXT,
  meta          JSONB DEFAULT '{}',
  embedding     VECTOR(1024),
  chunk_ids     UUID[] DEFAULT '{}',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_graph_entities_kb  ON graph_entities(kb_id, tenant_id);
CREATE INDEX idx_graph_entities_name ON graph_entities(canonical_name);
CREATE INDEX idx_graph_entities_vec ON graph_entities USING hnsw(embedding vector_cosine_ops);

CREATE TABLE graph_relations (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        UUID NOT NULL,
  kb_id            UUID NOT NULL,
  source_entity_id UUID NOT NULL REFERENCES graph_entities(id),
  target_entity_id UUID NOT NULL REFERENCES graph_entities(id),
  relation_type    VARCHAR(64) NOT NULL,
  description      TEXT,
  weight           FLOAT DEFAULT 0.5,
  source_chunk_id  UUID,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_graph_rel_src ON graph_relations(source_entity_id, tenant_id);
CREATE INDEX idx_graph_rel_tgt ON graph_relations(target_entity_id, tenant_id);
CREATE INDEX idx_graph_rel_type ON graph_relations(kb_id, relation_type);

CREATE TABLE graph_communities (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id         UUID NOT NULL,
  kb_id             UUID NOT NULL,
  level             INT NOT NULL DEFAULT 0,
  summary           TEXT,
  keywords          TEXT[] DEFAULT '{}',
  member_entity_ids UUID[] DEFAULT '{}',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE graph_events (
  id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        UUID NOT NULL,
  kb_id            UUID NOT NULL,
  doc_id           UUID,
  event_type       VARCHAR(64),
  subject_entity_id UUID NOT NULL REFERENCES graph_entities(id),
  predicate        VARCHAR(64),
  object           TEXT,
  order_no         INT,
  source_chunk_id  UUID,
  summary          TEXT,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_graph_events_subj ON graph_events(subject_entity_id, tenant_id);
```

---

## 12. 风险与对策

| 风险 | 影响 | 对策 |
|------|------|------|
| LLM 抽取幻觉(虚构实体/关系) | 图谱污染 | 置信度阈值 + 人工修正 API + 高频证据校验 |
| 实体归一化错误 | 图谱碎片化 | 归一化优先准确率 + 别名表 + LLM 确认 |
| 抽取成本过高(长文本) | 成本失控 | 批量合并 + 低成本模型 + 增量构建 |
| 递归 CTE 多跳性能 | 查询变慢 | 限制 max_hop=3 + 物化热门子图 + 索引优化 |
| 大图谱写入阻塞 | 导入慢 | 异步后台任务 + 分批事务提交 |
