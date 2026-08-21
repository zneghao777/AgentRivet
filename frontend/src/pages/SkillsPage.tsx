import { useCallback, useDeferredValue, useEffect, useMemo, useState } from "react";
import { Button, Empty, Input, Popconfirm, Select, Spin, Switch, message } from "antd";
import { deleteData, getData, postData } from "@/api/http";
import { InspectorField, ResponsiveInspector, StatusDot, WorkspaceHeader } from "@/components/common/Workspace";
import SkillImportModal from "@/components/skills/SkillImportModal";
import type { SkillCategory, SkillItem } from "@/types/api";

const CATEGORY_LABEL: Record<SkillCategory, string> = { builtin: "内置", external: "外部" };

function compactPath(path: string | null | undefined) {
  if (!path) return "@builtin/skills";
  const parts = path.split("/").filter(Boolean);
  return parts.length > 4 ? `…/${parts.slice(-4).join("/")}` : path;
}

export default function SkillsPage() {
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedName, setSelectedName] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [keyword, setKeyword] = useState("");
  const [category, setCategory] = useState<SkillCategory | "all">("all");
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [reloading, setReloading] = useState(false);
  const deferredKeyword = useDeferredValue(keyword);

  const load = useCallback(async () => {
    try {
      const data = await getData<SkillItem[]>("/skills");
      setSkills(data);
      setSelectedName((current) => current && data.some((item) => item.name === current) ? current : data[0]?.name ?? null);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "加载技能失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const toggle = async (skill: SkillItem) => {
    setBusy(skill.name);
    try {
      await postData(`/skills/${skill.name}/${skill.enabled ? "disable" : "enable"}`, {});
      await load();
      message.success(skill.enabled ? "已禁用" : "已启用");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(null);
    }
  };

  const reload = async () => {
    setReloading(true);
    try {
      await postData<SkillItem[]>("/skills/reload", {});
      await load();
      message.success("已重新扫描 Skill 目录");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "重新扫描失败");
    } finally {
      setReloading(false);
    }
  };

  const remove = async (skill: SkillItem) => {
    setBusy(skill.name);
    try {
      await deleteData(`/skills/${encodeURIComponent(skill.name)}`);
      setInspectorOpen(false);
      await load();
      message.success(`已删除 ${skill.name}`);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "删除失败");
    } finally {
      setBusy(null);
    }
  };

  const visibleSkills = useMemo(() => {
    const query = deferredKeyword.trim().toLowerCase();
    return skills.filter((skill) => {
      const source = skill.source;
      return (category === "all" || source === category) && (!query || `${skill.name} ${skill.description} ${skill.tags.join(" ")}`.toLowerCase().includes(query));
    });
  }, [category, deferredKeyword, skills]);

  const selected = skills.find((skill) => skill.name === selectedName) ?? null;
  const enabledCount = skills.filter((skill) => skill.enabled).length;
  const externalCount = skills.filter((skill) => skill.source === "external").length;

  return (
    <div className="page-scroll workspace-page skills-page">
      <WorkspaceHeader
        icon="bolt"
        title="技能库"
        description="集中导入、校验与管理 Agent 可复用的能力包"
        actions={<><StatusDot label={`已启用 ${enabledCount} / ${skills.length} 项`} tone="success" /><Button loading={reloading} onClick={() => void reload()} icon={<span className="material-symbols-rounded">sync</span>}>重新扫描</Button><Button type="primary" onClick={() => setImportOpen(true)} icon={<span className="material-symbols-rounded">system_update_alt</span>}>导入 Skill</Button></>}
      />
      <div className="workspace-toolbar">
        <Input allowClear prefix={<span className="material-symbols-rounded">search</span>} placeholder="搜索技能名称、描述或标签" value={keyword} onChange={(event) => setKeyword(event.target.value)} />
        <Select value={category} onChange={setCategory} options={[{ value: "all", label: `全部 ${skills.length}` }, { value: "builtin", label: `内置 ${skills.length - externalCount}` }, { value: "external", label: `外部 ${externalCount}` }]} />
        <span className="skill-toolbar-note"><span className="material-symbols-rounded">verified_user</span>外部 Skill 导入后默认停用</span>
      </div>
      <div className="workspace-master-detail">
        <section className="data-surface skills-master" aria-label="技能列表" role="listbox">
          <div className="data-list-header skill-grid-row"><span>技能名称</span><span>类型</span><span>状态</span><span>版本</span></div>
          {loading ? <div className="skill-loading"><Spin size="small" />正在加载 Skill…</div> : null}
          {!loading && visibleSkills.length === 0 ? <Empty description={skills.length ? "没有匹配的 Skill" : "还没有 Skill，点击右上角开始导入"} /> : null}
          {visibleSkills.map((skill) => {
            const source = skill.source;
            const active = skill.name === selectedName;
            return (
              <div role="option" tabIndex={active ? 0 : -1} key={skill.name} className={`data-list-row skill-grid-row${active ? " selected" : ""}`} onClick={() => { setSelectedName(skill.name); setInspectorOpen(true); }} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setSelectedName(skill.name); setInspectorOpen(true); } }} aria-selected={active}>
                <span className="skill-primary-cell"><span className="row-leading-icon material-symbols-rounded">{source === "builtin" ? "bolt" : "cloud_download"}</span><span><b>{skill.name}</b><small>{skill.description || "暂无描述"}</small></span></span>
                <span className="subtle-label">{CATEGORY_LABEL[source]}</span>
                <span onClick={(event) => event.stopPropagation()}><Switch checked={skill.enabled} loading={busy === skill.name} onChange={() => void toggle(skill)} /></span>
                <span className="mono subtle-label">v{skill.version}</span>
              </div>
            );
          })}
          <footer className="data-list-footer">共 {visibleSkills.length} 项</footer>
        </section>
        <ResponsiveInspector open={inspectorOpen && Boolean(selected)} onClose={() => setInspectorOpen(false)} title={selected?.name ?? "技能详情"} subtitle={selected ? <StatusDot label={selected.enabled ? "已启用" : "已停用"} tone={selected.enabled ? "success" : "neutral"} /> : undefined} actions={selected ? <Switch checked={selected.enabled} loading={busy === selected.name} onChange={() => void toggle(selected)} /> : undefined}>
          {!selected ? <Empty description="选择一个技能查看详情" /> : <>
            <section className="inspector-section"><h3>概览</h3><InspectorField label="类型">{CATEGORY_LABEL[selected.source]}</InspectorField><InspectorField label="版本">v{selected.version}</InspectorField><InspectorField label="作者">{selected.author || "—"}</InspectorField><InspectorField label="文件">{selected.file_count} 个{selected.has_scripts ? " · 含脚本" : ""}</InspectorField><InspectorField label="来源路径" mono><span title={selected.path || "@builtin/skills"}>{compactPath(selected.path)}</span></InspectorField></section>
            <section className="inspector-section"><h3>描述</h3><p className="inspector-copy">{selected.description || "暂无描述"}</p></section>
            <section className="inspector-section"><h3>标签</h3><div className="quiet-tags">{selected.tags.length ? selected.tags.map((tag) => <span key={tag}>{tag}</span>) : <span>无标签</span>}</div></section>
            {selected.has_scripts ? <section className="inspector-section skill-security-note"><span className="material-symbols-rounded">security</span><p><b>此 Skill 包含可执行脚本</b>启用前请确认文件来源与脚本内容可信。</p></section> : null}
            {selected.source === "external" ? <section className="inspector-section"><Popconfirm title={`删除 ${selected.name}？`} description="将移除托管目录中的全部文件，此操作无法撤销。" okText="删除" cancelText="取消" okButtonProps={{ danger: true }} onConfirm={() => void remove(selected)}><Button danger block loading={busy === selected.name} icon={<span className="material-symbols-rounded">delete</span>}>删除外部 Skill</Button></Popconfirm></section> : null}
          </>}
        </ResponsiveInspector>
      </div>
      <SkillImportModal open={importOpen} onClose={() => setImportOpen(false)} onImported={(names) => { void load().then(() => { setSelectedName(names[0] ?? null); setInspectorOpen(Boolean(names[0])); }); }} />
    </div>
  );
}
