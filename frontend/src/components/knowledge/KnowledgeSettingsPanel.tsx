import { useState } from "react";
import { Button, Modal, Select, message } from "antd";
import { http, patchData, postData } from "@/api/http";
import type { GraphBuildPolicy, KnowledgeBaseItem } from "@/types/api";
import IndexHealthBadge from "./IndexHealthBadge";
import KnowledgeModeBadge from "./KnowledgeModeBadge";

export default function KnowledgeSettingsPanel({ kb, onChanged }: { kb: KnowledgeBaseItem; onChanged: () => void }) {
  const [saving, setSaving] = useState(false);

  const updatePolicy = async (policy: GraphBuildPolicy) => {
    setSaving(true);
    try {
      await patchData(`/knowledge-bases/${kb.kb_id}/index-settings`, { graph_build_policy: policy });
      message.success("图谱构建策略已更新");
      onChanged();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "更新失败");
    } finally {
      setSaving(false);
    }
  };

  const transition = (target: "standard" | "graph_enhanced") => {
    Modal.confirm({
      title: target === "graph_enhanced" ? "升级为图谱增强知识库？" : "切换为标准知识库？",
      content: target === "graph_enhanced" ? `将评估 ${kb.doc_count} 个现有文档，并在文本索引不受影响的前提下建立图谱。` : "将停止新的图谱任务并禁用图检索；现有图谱数据会保留，便于恢复。",
      okText: "确认切换",
      onOk: async () => {
        setSaving(true);
        try {
          await postData(`/knowledge-bases/${kb.kb_id}/index-mode-transition`, { target_mode: target, graph_build_policy: target === "graph_enhanced" ? "auto" : "manual", build_existing_documents: true });
          message.success("索引模式切换已提交");
          onChanged();
        } finally {
          setSaving(false);
        }
      },
    });
  };

  const clearGraph = () => {
    Modal.confirm({
      title: "删除全部图谱数据？",
      content: "将删除实体、关系、事件和社区，但保留原文件、分块、向量与文本检索。",
      okText: "删除图谱数据",
      okButtonProps: { danger: true },
      onOk: async () => {
        await http.delete(`/knowledge-bases/${kb.kb_id}/graph?confirm=true`);
        message.success("图谱数据已删除，文本索引不受影响");
        onChanged();
      },
    });
  };

  return (
    <div className="kb-settings-panel kb-settings-workbench">
      <header className="kb-settings-intro">
        <div><span className="eyebrow">INDEX CONFIGURATION</span><h2>索引与检索设置</h2><p>控制知识进入检索系统的方式。所有模式切换都会保留原文件与文本索引。</p></div>
        <div className="kb-settings-current"><small>当前模式</small><KnowledgeModeBadge mode={kb.index_mode} /></div>
      </header>

      <section className="kb-setting-group">
        <header><span className="kb-setting-icon material-symbols-rounded">route</span><div><h3>索引模式</h3><p>选择 Agent 可以使用的知识通道和检索范围。</p></div></header>
        <div className="kb-settings-mode-grid">
          <article className={kb.index_mode === "standard" ? "active" : ""}>
            <div><span className="material-symbols-rounded">manage_search</span>{kb.index_mode === "standard" ? <b className="active-mark">当前</b> : null}</div>
            <h4>标准检索</h4><p>解析、分块和向量检索。适合制度、手册与精确事实问答。</p>
            <ul><li>更低构建成本</li><li>文本索引独立可用</li></ul>
            {kb.index_mode !== "standard" ? <Button loading={saving} onClick={() => transition("standard")}>切换为标准模式</Button> : <Button disabled>当前模式</Button>}
          </article>
          <article className={kb.index_mode === "graph_enhanced" ? "active" : ""}>
            <div><span className="material-symbols-rounded">account_tree</span>{kb.index_mode === "graph_enhanced" ? <b className="active-mark">当前</b> : null}</div>
            <h4>图谱增强</h4><p>在文本检索之上抽取实体、关系、事件和社区结构。</p>
            <ul><li>支持关系与多跳问题</li><li>图谱失败不阻断文本问答</li></ul>
            {kb.index_mode !== "graph_enhanced" ? <Button type="primary" loading={saving} onClick={() => transition("graph_enhanced")}>升级为图谱增强</Button> : <Button disabled>当前模式</Button>}
          </article>
        </div>
      </section>

      {kb.index_mode === "graph_enhanced" ? <section className="kb-setting-group kb-graph-policy-setting">
        <header><span className="kb-setting-icon material-symbols-rounded">schema</span><div><h3>图谱构建策略</h3><p>决定文本索引就绪后，是否立即进入图谱抽取流水线。</p></div></header>
        <div className="kb-policy-layout">
          <label><span>构建方式</span><Select size="large" value={kb.graph_build_policy} loading={saving} onChange={(value) => void updatePolicy(value)} options={[{ value: "auto", label: "自动构建 — 文本就绪后立即开始" }, { value: "manual", label: "手动构建 — 由管理员确认触发" }]} /></label>
          <div className="kb-policy-facts"><span><small>最大扩散深度</small><b>{Number(kb.settings.graph_max_hop ?? 3)} 跳</b></span><span><small>图谱版本</small><b>v{kb.graph_version}</b></span><span><small>当前状态</small><IndexHealthBadge status={kb.graph_status} /></span></div>
        </div>
        <div className="kb-setting-guidance"><span className="material-symbols-rounded">lightbulb</span><p><b>{kb.graph_build_policy === "auto" ? "适合持续更新的知识库" : "适合需要控制成本或审核内容的知识库"}</b><small>{kb.graph_build_policy === "auto" ? "新文档完成向量化后自动排队构建图谱。" : "新文档会显示“待构建”，文本检索仍可立即使用。"}</small></p></div>
      </section> : null}

      <section className="kb-setting-group kb-danger-zone">
        <header><span className="kb-setting-icon material-symbols-rounded">warning</span><div><h3>危险操作</h3><p>这些操作影响派生索引数据，但不会删除原始文档。</p></div></header>
        <div className="kb-danger-action"><div><b>删除全部图谱数据</b><p>清除实体、关系、事件与社区。文本分块和向量继续保留。</p></div>{kb.index_mode === "graph_enhanced" ? <Button danger onClick={clearGraph}>删除图谱数据</Button> : <span>当前模式没有图谱数据</span>}</div>
      </section>
    </div>
  );
}
