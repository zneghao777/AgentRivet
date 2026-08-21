import { useEffect, useState } from "react";
import { Button, Form, Input, Modal, Radio, message } from "antd";
import { useNavigate } from "react-router-dom";
import { getData, postData } from "@/api/http";
import type { GraphBuildPolicy, IndexMode, KnowledgeBaseItem } from "@/types/api";

interface CreateValues {
  name: string;
  description?: string;
  index_mode: IndexMode;
  graph_build_policy: GraphBuildPolicy;
}

export default function KnowledgeCreateModal({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: () => void }) {
  const navigate = useNavigate();
  const [form] = Form.useForm<CreateValues>();
  const [step, setStep] = useState(0);
  const [creating, setCreating] = useState(false);
  const [graphEnabled, setGraphEnabled] = useState(true);
  const mode = Form.useWatch("index_mode", form) ?? "standard";
  const policy = Form.useWatch("graph_build_policy", form) ?? "manual";

  useEffect(() => {
    if (!open) return;
    setStep(0);
    form.setFieldsValue({ index_mode: "standard", graph_build_policy: "manual" });
    void getData<{ graph_enabled: boolean }>("/knowledge-bases/capabilities")
      .then((data) => setGraphEnabled(data.graph_enabled))
      .catch(() => setGraphEnabled(true));
  }, [form, open]);

  const next = async () => {
    await form.validateFields(["name", "description"]);
    setStep(1);
  };

  const submit = async () => {
    const values = await form.validateFields();
    setCreating(true);
    try {
      const created = await postData<KnowledgeBaseItem>("/knowledge-bases", {
        ...values,
        graph_build_policy: values.index_mode === "standard" ? "manual" : values.graph_build_policy,
      });
      message.success("知识库已创建，可以开始上传文档");
      form.resetFields();
      onClose();
      onCreated();
      navigate(`/knowledge/${created.kb_id}?tab=documents`);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "创建失败");
    } finally {
      setCreating(false);
    }
  };

  const name = Form.useWatch("name", { form, preserve: true }) || "未命名知识库";
  return (
    <Modal className="kb-create-modal" width={720} title={`新建知识库 · ${step + 1}/2`} open={open} closable={!creating} maskClosable={!creating} keyboard={!creating} onCancel={onClose} footer={step === 0 ? [<Button key="cancel" onClick={onClose}>取消</Button>, <Button key="next" type="primary" onClick={() => void next()}>下一步：选择索引模式</Button>] : [<Button key="back" disabled={creating} onClick={() => setStep(0)}>上一步</Button>, <Button key="create" type="primary" loading={creating} onClick={() => void submit()}>创建知识库</Button>]}>
      <Form form={form} layout="vertical" initialValues={{ name: "", description: "", index_mode: "standard", graph_build_policy: "manual" }}>
        {step === 0 ? (
          <div className="kb-create-step">
            <p className="kb-step-intro">先定义知识范围，再选择索引能力。创建后仍可通过设置执行模式迁移。</p>
            <Form.Item name="name" label="名称" rules={[{ required: true, message: "请输入知识库名称" }, { max: 128, message: "名称不能超过 128 个字符" }]}><Input autoFocus placeholder="例如：组织与人员知识库" maxLength={128} showCount /></Form.Item>
            <Form.Item name="description" label="描述"><Input.TextArea rows={4} placeholder="说明知识范围和预期回答的问题" /></Form.Item>
          </div>
        ) : (
          <div className="kb-create-step">
            <p className="kb-step-intro">图谱增强包含全部标准检索能力，并增加实体关系、多跳与全局问题支持。</p>
            <Form.Item name="index_mode" label="索引模式">
              <Radio.Group className="kb-mode-grid">
                <Radio.Button value="standard" className="kb-mode-card"><span className="material-symbols-rounded">manage_search</span><b>标准知识库</b><p>快速构建可追溯的文档检索</p><small>向量 · 关键词 · Rerank · 来源引用</small><em>适合制度、FAQ、手册和技术文档</em></Radio.Button>
                <Radio.Button value="graph_enhanced" disabled={!graphEnabled} className="kb-mode-card"><span className="material-symbols-rounded">account_tree</span><b>图谱增强知识库</b><p>在标准检索上增加实体关系和多跳推理</p><small>标准能力 · 实体 · 关系 · 事件 · 社区</small><em>{graphEnabled ? "构建较慢，会产生额外模型调用" : "当前环境未启用图谱服务"}</em></Radio.Button>
              </Radio.Group>
            </Form.Item>
            {mode === "graph_enhanced" ? <Form.Item name="graph_build_policy" label="图谱构建方式"><Radio.Group className="kb-policy-options"><Radio value="auto">文档索引完成后自动构建（推荐）</Radio><Radio value="manual">由我手动触发构建</Radio></Radio.Group></Form.Item> : null}
            <div className="kb-create-summary" aria-live="polite">将创建“{name}”，使用{mode === "graph_enhanced" ? `图谱增强索引，上传后${policy === "auto" ? "自动构建图谱" : "等待手动构建图谱"}` : "标准索引，快速建立文本检索"}。</div>
          </div>
        )}
      </Form>
    </Modal>
  );
}
