# Skill 开发指南（给业务团队）

本文档指导业务团队在 Agent Harness 底座之上开发 Skill。

## 1. 什么是 Skill

Skill 是对「提示词增强包」的工程化封装：一个包含 `SKILL.md` 指令与可选脚本/资源的能力包，按需被 Agent 加载。

## 2. 目录规范

```
my_skill/
├── SKILL.md          # 必须
├── scripts/          # 可选：Python 脚本 → 自动注册为工具
│   └── my_tool.py
├── references/       # 可选
└── assets/           # 可选
```

## 3. SKILL.md 编写

```markdown
---
name: my_skill
description: 何时使用 / 何时不使用（模型触发依据）
version: 1.0.0
author: your-team
tags: [category]
enabled: true
---

# 职责
...

# 工作流程
1. ...
2. ...

# 禁止事项
- ...
```

### 关键点

- `name` 必须与目录名一致，全局唯一。
- `description` 是模型触发依据，请写明 **positive（何时用）** + **negative（何时不用）**。
- `enabled: false` 时默认不加载。

## 4. 注册脚本工具

在 `scripts/` 放置 Python 文件，定义：

```python
DESCRIPTION = "工具描述，供模型触发"

def run(text: str) -> str:
    return f"处理结果: {text}"
```

加载后自动注册为工具 `skill_{skill_name}__{file_name}`。

## 5. 生命周期操作（API）

| 操作 | 端点 |
|---|---|
| 列表 | `GET /api/v1/skills` |
| 详情 | `GET /api/v1/skills/{name}` |
| 启用 | `POST /api/v1/skills/{name}/enable` |
| 停用 | `POST /api/v1/skills/{name}/disable` |
| 重载 | `POST /api/v1/skills/reload` |

> 所有请求需携带 `X-API-Key`。

## 6. 完整流程示例

1. 在 `skills/` 下新建目录 `my_skill/` 并编写 `SKILL.md`。
2. 重启后端或调用 `/skills/reload` 触发扫描。
3. 调用 `/skills/my_skill/enable` 启用。
4. 在对话中提问触发该 Skill，观察其指令是否生效。

## 7. 最佳实践

- description 务必清晰，避免模型误触发。
- 脚本工具做好参数校验，禁止任意代码执行。
- 敏感信息（Key/凭据）不得写入 SKILL.md 或工具返回值。
