# 企业 Skill 仓库

本目录为**外部 Skill 仓库**，业务团队可在此增量扩展 Skill，无需改动 Harness 内核。

## Skill 结构规范

每个 Skill 是一个目录，必须包含 `SKILL.md`：

```
skill_name/                 # 目录名 = skill 标识（小写下划线）
├── SKILL.md                # 必须：frontmatter + 指令正文
├── scripts/                # 可选：脚本（被注册为 skill_{name}__{file} 工具）
│   └── xxx.py
├── references/             # 可选：模型按需加载的参考文档
└── assets/                 # 可选：静态资源
```

## SKILL.md frontmatter 字段

| 字段 | 必须 | 说明 |
|---|---|---|
| `name` | 是 | 唯一标识，须与目录名一致 |
| `description` | 是 | 模型触发依据（写清何时用/何时不用） |
| `version` | 否 | 语义化版本 |
| `author` | 否 | 作者/团队 |
| `tags` | 否 | 分类标签 |
| `enabled` | 否 | 默认 true |

## 内置示例 Skill

- `skill_hello`（位于 `backend/app/skills/builtin_skills/skill_hello`）：演示加载、触发、工具注册全链路。

## 扩展步骤

1. 在本目录新建 Skill 目录（含 `SKILL.md`）。
2. 可选：在 `scripts/` 放置 Python 脚本，定义顶层 `run()` 函数与 `DESCRIPTION`。
3. 调用 `POST /api/v1/skills/reload` 触发扫描，或在后端启动时自动发现。
4. 通过 `POST /api/v1/skills/{name}/enable` 启用。

详细开发指南见 `docs/SKILL_GUIDE.md`。
