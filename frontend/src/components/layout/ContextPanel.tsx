import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getData } from "@/api/http";
import type { KnowledgeBaseItem, SkillItem, ToolItem } from "@/types/api";
import { useSettingsStore } from "@/stores/settingsStore";

const toolIcon = (name: string) => {
  const value = name.toLowerCase();
  if (value.includes("calc")) return "calculate";
  if (value.includes("time")) return "schedule";
  if (value.includes("echo")) return "terminal";
  if (value.includes("knowledge")) return "database";
  return "build";
};

const toolLabel = (name: string) => {
  const value = name.toLowerCase();
  if (value.includes("calc")) return "计算器";
  if (value.includes("time")) return "时间";
  if (value.includes("echo")) return "回显";
  if (value.includes("knowledge")) return "知识库检索";
  return name.replace(/^builtin_/, "");
};

export default function ContextPanel() {
  const navigate = useNavigate();
  const { model, apiKey } = useSettingsStore();
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [tools, setTools] = useState<ToolItem[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseItem[]>([]);

  useEffect(() => {
    Promise.all([
      getData<SkillItem[]>("/skills").catch(() => []),
      getData<ToolItem[]>("/tools").catch(() => []),
      getData<KnowledgeBaseItem[]>("/knowledge-bases").catch(() => []),
    ]).then(([skillItems, toolItems, kbItems]) => {
      setSkills(skillItems);
      setTools(toolItems);
      setKnowledgeBases(kbItems);
    });
  }, []);

  const activeKnowledgeBases = knowledgeBases.filter((kb) => kb.status === "active");

  const enabledSkills = skills.filter((skill) => skill.enabled);
  return (
    <aside className="context-panel">
      <header className="context-header">
        <span className="material-symbols-rounded">info</span><h2>上下文信息</h2>
      </header>
      <div className="context-scroll">
        <section className="context-card model-card">
          <div className="eyebrow">当前模型</div>
          <div className="model-row">
            <div className="model-icon"><span className="material-symbols-rounded">memory</span></div>
            <strong>{model || "deepseek-chat"}</strong>
          </div>
          <div className={`api-pill${apiKey ? " ready" : ""}`}>
            <i />{apiKey ? "API Key 已配置" : "API Key 未配置"}
          </div>
        </section>

        <section className="context-card skills-card">
          <div className="card-heading"><span className="eyebrow">已加载技能</span><b>{enabledSkills.length}</b></div>
          <div className="skill-stack">
            {enabledSkills.map((skill) => (
              <div className="skill-row" key={skill.name}>
                <div className="skill-icon"><span className="material-symbols-rounded">handshake</span></div>
                <div><strong>{skill.name}</strong><p>{skill.description}</p></div>
              </div>
            ))}
          </div>
          <button className="manage-skills" onClick={() => navigate("/skills")}>
            <span className="material-symbols-rounded">manage_search</span>管理技能
          </button>
        </section>

        <section className="context-card knowledge-card">
          <div className="card-heading"><span className="eyebrow">已启用知识库</span><b>{activeKnowledgeBases.length}</b></div>
          <div className="skill-stack">
            {activeKnowledgeBases.length === 0 ? (
              <div className="kb-empty">暂无知识库，上传文档后可让 Agent 基于知识回答</div>
            ) : null}
            {activeKnowledgeBases.map((kb) => (
              <div className="skill-row" key={kb.kb_id}>
                <div className="skill-icon"><span className="material-symbols-rounded">database</span></div>
                <div><strong>{kb.name}</strong><p>{kb.chunk_count} 个分块 · {kb.doc_count} 个文档</p></div>
              </div>
            ))}
          </div>
          <button className="manage-skills" onClick={() => navigate("/knowledge")}>
            <span className="material-symbols-rounded">database</span>管理知识库
          </button>
        </section>

        <section className="context-card tools-card">
          <div className="eyebrow">内置工具</div>
          <div className="tool-grid">
            {tools.map((tool) => (
              <div className="tool-chip" key={tool.name} title={tool.description}>
                <span className="material-symbols-rounded">{toolIcon(tool.name)}</span>
                {toolLabel(tool.name)}
              </div>
            ))}
          </div>
        </section>
      </div>
    </aside>
  );
}
