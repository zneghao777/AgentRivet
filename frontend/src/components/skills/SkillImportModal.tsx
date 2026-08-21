import { useMemo, useState } from "react";
import { Alert, Button, Checkbox, Modal, Upload, message } from "antd";
import type { UploadFile } from "antd";
import { isAxiosError } from "axios";
import { postData } from "@/api/http";
import type { SkillImportResponse } from "@/types/api";

type ImportMode = "package" | "folder" | "markdown";

const MODE_META: Record<ImportMode, { icon: string; title: string; description: string }> = {
  package: { icon: "inventory_2", title: "Skill 包", description: "ZIP、TAR、TGZ 或 .skill" },
  folder: { icon: "folder_open", title: "文件夹", description: "保留 scripts、references 等资源" },
  markdown: { icon: "markdown", title: "单 Markdown", description: "包含 frontmatter 的技能说明" },
};

function readableBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function errorMessage(error: unknown) {
  if (isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string") return detail;
  }
  return error instanceof Error ? error.message : "导入失败，请检查文件后重试";
}

export default function SkillImportModal({
  open,
  onClose,
  onImported,
}: {
  open: boolean;
  onClose: () => void;
  onImported: (names: string[]) => void;
}) {
  const [mode, setMode] = useState<ImportMode>("package");
  const [files, setFiles] = useState<UploadFile[]>([]);
  const [overwrite, setOverwrite] = useState(false);
  const [activate, setActivate] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState<SkillImportResponse | null>(null);

  const rawFiles = useMemo(
    () => files.flatMap((file) => file.originFileObj ? [file.originFileObj as File] : []),
    [files],
  );
  const totalBytes = rawFiles.reduce((sum, file) => sum + file.size, 0);
  const hasManifest = rawFiles.some((file) => {
    const path = file.webkitRelativePath || file.name;
    return path.toLowerCase().endsWith("skill.md");
  });
  const hasScripts = rawFiles.some((file) => {
    const path = (file.webkitRelativePath || file.name).replace(/\\/g, "/");
    return /(^|\/)scripts\/[^/]+\.py$/i.test(path);
  });

  const reset = () => {
    setMode("package");
    setFiles([]);
    setOverwrite(false);
    setActivate(false);
    setSubmitting(false);
    setResult(null);
  };

  const close = () => {
    if (submitting) return;
    reset();
    onClose();
  };

  const changeMode = (next: ImportMode) => {
    setMode(next);
    setFiles([]);
    setResult(null);
  };

  const submit = async () => {
    if (!rawFiles.length) {
      message.warning("请先选择要导入的内容");
      return;
    }
    const body = new FormData();
    const paths: string[] = [];
    rawFiles.forEach((file) => {
      body.append("files", file, file.name);
      paths.push(file.webkitRelativePath || file.name);
    });
    body.append("relative_paths", JSON.stringify(paths));
    body.append("overwrite", String(overwrite));
    body.append("activate", String(activate));

    setSubmitting(true);
    try {
      const imported = await postData<SkillImportResponse>("/skills/import", body);
      setResult(imported);
      onImported(imported.skills.map((skill) => skill.name));
    } catch (error) {
      message.error(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  };

  const accept = mode === "package" ? ".zip,.skill,.tar,.gz,.tgz" : mode === "markdown" ? ".md,text/markdown" : undefined;

  return (
    <Modal
      className="skill-import-modal"
      width={680}
      open={open}
      onCancel={close}
      destroyOnHidden={false}
      title={<div className="skill-import-title"><span className="material-symbols-rounded">system_update_alt</span><div><strong>导入 Skill</strong><small>从通用 Skill 目录结构创建可管理的外部技能</small></div></div>}
      footer={result ? <Button type="primary" onClick={close}>完成</Button> : <><Button onClick={close} disabled={submitting}>取消</Button><Button type="primary" loading={submitting} disabled={!rawFiles.length} onClick={() => void submit()}>校验并导入</Button></>}
    >
      {result ? (
        <div className="skill-import-result" role="status" aria-live="polite">
          <div className="skill-result-hero"><span className="material-symbols-rounded">check_circle</span><div><strong>导入完成</strong><p>已加入 {result.skills.length} 个 Skill，默认可在列表中继续检查与启用。</p></div></div>
          <div className="skill-result-list">
            {result.skills.map((skill) => <div key={skill.name}><span className="material-symbols-rounded">extension</span><span><b>{skill.name}</b><small>{skill.description}</small></span><em>{skill.enabled ? "已启用" : "待启用"}</em></div>)}
          </div>
          {result.replaced.length ? <Alert type="info" showIcon message={`已替换：${result.replaced.join("、")}`} /> : null}
          {result.warnings.map((warning) => <Alert key={warning} type="warning" showIcon message={warning} />)}
        </div>
      ) : (
        <div className="skill-import-flow">
          <div className="skill-import-modes" role="radiogroup" aria-label="导入来源">
            {(Object.keys(MODE_META) as ImportMode[]).map((value) => {
              const meta = MODE_META[value];
              return <button type="button" role="radio" aria-checked={mode === value} className={mode === value ? "active" : ""} key={value} onClick={() => changeMode(value)}><span className="material-symbols-rounded">{meta.icon}</span><span><b>{meta.title}</b><small>{meta.description}</small></span><i className="material-symbols-rounded">check_circle</i></button>;
            })}
          </div>

          <Upload.Dragger
            className="skill-import-dropzone"
            accept={accept}
            directory={mode === "folder"}
            multiple={mode === "folder"}
            maxCount={mode === "folder" ? undefined : 1}
            fileList={files}
            showUploadList={false}
            beforeUpload={() => false}
            onChange={({ fileList }) => setFiles(mode === "folder" ? fileList : fileList.slice(-1))}
          >
            <span className="skill-drop-icon material-symbols-rounded">{MODE_META[mode].icon}</span>
            <p className="ant-upload-text">点击选择或拖放{MODE_META[mode].title}</p>
            <p className="ant-upload-hint">单次最多 50 MB / 1000 个文件，导入前会进行结构与安全校验</p>
          </Upload.Dragger>

          {rawFiles.length ? (
            <div className="skill-file-summary" aria-live="polite">
              <span className="material-symbols-rounded">task_alt</span>
              <div><b>已选择 {rawFiles.length} 个文件</b><small>{readableBytes(totalBytes)}{mode === "folder" ? ` · ${hasManifest ? "已发现 SKILL.md" : "未发现 SKILL.md"}` : ""}</small></div>
              <Button type="text" size="small" onClick={() => setFiles([])}>重新选择</Button>
            </div>
          ) : null}
          {mode === "folder" && rawFiles.length && !hasManifest ? <Alert type="warning" showIcon message="文件夹中没有发现 SKILL.md，导入校验将无法通过" /> : null}
          {(hasScripts || (mode === "package" && rawFiles.length > 0)) ? <Alert type="warning" showIcon message="外部 Skill 可能包含可执行脚本。仅导入可信来源，导入后默认保持停用。" /> : null}

          <div className="skill-import-options">
            <label><Checkbox checked={overwrite} onChange={(event) => setOverwrite(event.target.checked)} /><span><b>覆盖同名外部 Skill</b><small>内置 Skill 始终不会被替换</small></span></label>
            <label><Checkbox checked={activate} onChange={(event) => setActivate(event.target.checked)} /><span><b>导入后立即启用</b><small>可能注册并运行包内 Python 脚本</small></span></label>
          </div>
        </div>
      )}
    </Modal>
  );
}
