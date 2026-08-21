import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getData } from "@/api/http";
import type { KnowledgeBaseItem, McpServerItem, SkillItem, ToolItem } from "@/types/api";
import { DEFAULT_CHAT_MODEL } from "@/constants";
import { useSettingsStore } from "@/stores/settingsStore";

const toolIcon = (name: string) => {
  const value = name.toLowerCase();
  if (value === "knowledge_graph_search") return "account_tree";
  if (value === "knowledge_search") return "find_in_page";
  if (value.includes("calc")) return "calculate";
  if (value.includes("time")) return "schedule";
  if (value.includes("echo")) return "terminal";
  if (value.includes("knowledge")) return "database";
  return "build";
};

const toolLabel = (name: string) => {
  const value = name.toLowerCase();
  if (value === "knowledge_graph_search") return "知识图谱专用检索";
  if (value === "knowledge_search") return "统一知识检索";
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
  const [mcpServers, setMcpServers] = useState<McpServerItem[]>([]);
  const [tools, setTools] = useState<ToolItem[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseItem[]>([]);

  useEffect(() => {
    Promise.all([
      getData<SkillItem[]>("/skills").catch(() => []),
      getData<McpServerItem[]>("/mcp/servers").catch(() => []),
      getData<ToolItem[]>("/tools").catch(() => []),
      getData<KnowledgeBaseItem[]>("/knowledge-bases").catch(() => []),
    ]).then(([skillItems, serverItems, toolItems, kbItems]) => {
      setSkills(skillItems);
      setMcpServers(serverItems);
      setTools(toolItems);
      setKnowledgeBases(kbItems);
    });
  }, []);

  const activeKnowledgeBases = knowledgeBases.filter((kb) => kb.status === "active");

  const enabledSkills = skills.filter((skill) => skill.enabled);
  const connectedMcpServers = mcpServers.filter((server) => server.health_status === "ready");
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
            <strong title={model || DEFAULT_CHAT_MODEL}>{model || DEFAULT_CHAT_MODEL}</strong>
          </div>
          <div className={`api-pill${apiKey ? " ready" : ""}`}>
            <i />{apiKey ? "API Key 已配置" : "API Key 未配置"}
          </div>
        </section>

        <section className="context-card skills-card">
          <div className="card-heading"><span className="eyebrow">已加载插件</span><b>{enabledSkills.length + connectedMcpServers.length}</b></div>
          <div className="skill-stack">
            {enabledSkills.map((skill) => (
              <div className="skill-row" key={skill.name}>
                <div className="skill-icon"><span className="material-symbols-rounded">handshake</span></div>
                <div><strong>{skill.name}</strong><p>{skill.description}</p></div>
              </div>
            ))}
            {connectedMcpServers.map((server) => (
              <div className="skill-row mcp-plugin-row" key={server.id}>
                <div className="skill-icon"><span className="material-symbols-rounded">hub</span></div>
                <div><strong>{server.name}</strong><p>{server.description || "已连接的 MCP Server"}</p></div>
              </div>
            ))}
          </div>
          <button className="manage-skills" onClick={() => navigate("/skills")}>
            <span className="material-symbols-rounded">manage_search</span>管理插件
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
              <div className={`tool-chip${tool.name === "knowledge_search" ? " text-search-tool" : tool.name === "knowledge_graph_search" ? " graph-search-tool" : ""}`} key={tool.name} title={tool.description}>
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
