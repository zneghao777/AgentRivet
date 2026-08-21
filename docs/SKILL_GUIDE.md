# Skill 开发与导入指南

Agent Harness 提供一套通用的 Skill 生命周期：你可以从页面导入 Skill
包、文件夹或单个 Markdown 文件，然后完成校验、启停、重载和删除。

## Skill 结构

一个 Skill 是由 `SKILL.md` 和可选资源组成的能力目录。推荐使用以下
结构：

```text
research-helper/
├── SKILL.md          # 必需：元数据和指令正文
├── scripts/          # 可选：可注册为工具的 Python 脚本
│   └── summarize.py
├── references/       # 可选：指令引用的说明或数据
└── assets/           # 可选：模板、示例和静态资源
```

系统会递归扫描包含 `SKILL.md` 的目录。内置 Skill 位于
`backend/app/skills/builtin_skills/`，外部 Skill 由系统托管在 `skills/`。

## 编写 SKILL.md

`SKILL.md` 使用 YAML frontmatter 描述元数据，frontmatter 后的 Markdown
正文会在本次对话选择该 Skill 时注入 Agent 系统提示词。

```markdown
---
name: research-helper
description: 当用户需要整理研究材料时使用；普通闲聊不要使用。
version: 1.0.0
author: research-team
tags: [research, summary]
enabled: false
---

# 职责

整理来源、区分事实与推断，并输出简洁的研究摘要。

# 工作流程

1. 识别研究问题和范围。
2. 整理证据并标注来源。
3. 输出结论、限制和后续问题。
```

字段规则如下：

- `name` 可包含字母、数字、点、下划线和连字符，长度不得超过 128。
- `description` 是必填字段。写清楚何时使用和何时不要使用。
- `version`、`author`、`tags` 和 `enabled` 是可选字段。
- `tags` 可以是 YAML 数组，也可以是逗号分隔的字符串。
- `enabled` 仅用于首次目录发现。系统会在数据库中保留后续启停状态。

## 从页面导入

在 **技能库** 页面选择 **导入 Skill**，然后选择一种来源：

1. 选择 **Skill 包**，导入 `.zip`、`.skill`、`.tar`、`.tar.gz` 或
   `.tgz` 文件。
2. 选择 **文件夹**，导入包含 `SKILL.md` 的完整目录。系统会保留
   `scripts/`、`references/` 和 `assets/` 等资源。
3. 选择 **单 Markdown**，把一个带有效 frontmatter 的 `.md` 文件导入
   为独立 Skill。
4. 可选：选择 **覆盖同名外部 Skill**。内置 Skill 始终不可覆盖。
5. 可选：选择 **导入后立即启用**。仅对你信任且已经检查的 Skill
   使用此选项。
6. 选择 **校验并导入**，查看导入结果和风险提示。

外部 Skill 默认保持停用。你可以先在详情面板检查来源、文件数量和
脚本标记，再手动启用。

## 导入校验和安全边界

导入服务会先校验整批内容，再写入托管目录。它会执行以下检查：

- 拒绝绝对路径、`..` 路径穿越、符号链接和特殊归档成员。
- 限制单次导入为 50 MB 和 1000 个文件。
- 使用完整 YAML 解析 frontmatter，并校验 `name`、`description` 和
  `tags`。
- 拒绝同一批次中的重复名称和未授权的同名覆盖。
- 使用临时目录和原子替换更新同名外部 Skill。

> **Warning:** 启用包含 `scripts/*.py` 的外部 Skill 时，后端会在当前
> Python 进程中加载脚本。此执行过程不提供沙箱隔离。只导入和启用你
> 已经审查且信任的代码，且不要把密钥或凭据写入 Skill 文件。

## 注册脚本工具

每个 `scripts/*.py` 文件可以定义顶层 `run()` 函数。启用 Skill 时，
系统会将该函数注册为 `skill_{skill_name}__{file_name}` 工具。

```python
DESCRIPTION = "把输入文本整理成简短摘要"


def run(text: str) -> str:
    return f"摘要：{text[:200]}"
```

`DESCRIPTION` 是可选值。工具参数 schema 由 `run()` 的函数签名和类型
注解生成。停用或删除 Skill 时，系统会注销对应工具。

## API

所有端点都使用 `/api/v1` 前缀，并要求 `X-API-Key` 请求头。

| 操作 | 方法和端点 |
| --- | --- |
| 获取列表 | `GET /skills` |
| 获取详情 | `GET /skills/{name}` |
| 导入 | `POST /skills/import` |
| 启用 | `POST /skills/{name}/enable` |
| 停用 | `POST /skills/{name}/disable` |
| 重新扫描 | `POST /skills/reload` |
| 删除外部 Skill | `DELETE /skills/{name}` |

导入端点接收 `multipart/form-data`：

- `files`：一个或多个上传文件。
- `relative_paths`：与文件一一对应的 JSON 路径数组。单文件可省略。
- `overwrite`：是否覆盖同名外部 Skill，默认为 `false`。
- `activate`：是否在导入后立即启用，默认为 `false`。

以下命令导入单个 Markdown 文件：

```bash
curl -X POST http://localhost:8000/api/v1/skills/import \
  -H "X-API-Key: $HARNESS_API_KEY" \
  -F "files=@research-helper.md" \
  -F 'relative_paths=["research-helper.md"]' \
  -F "overwrite=false" \
  -F "activate=false"
```

## 验证 Skill

导入完成后，按以下顺序验证行为：

1. 在 **技能库** 列表确认来源、版本和状态。
2. 在详情面板确认描述、标签、文件数量和脚本标记。
3. 启用 Skill，并在对话中选择该 Skill。
4. 使用一条应当触发和一条不应触发的输入检查 `description` 边界。
5. 如果 Skill 包含脚本，检查工具调用结果和后端日志。

如果你直接修改了 `skills/` 中的文件，选择 **重新扫描** 让运行时加载
最新 manifest，同时保留数据库中的启停状态。
