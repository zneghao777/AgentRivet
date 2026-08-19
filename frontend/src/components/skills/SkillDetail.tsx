// Skill 详情
import { Descriptions, Tag } from "antd";
import type { SkillItem } from "@/types/api";

export default function SkillDetail({ skill }: { skill: SkillItem | null }) {
  if (!skill) return <div style={{ color: "#999" }}>选择一个 Skill 查看详情</div>;
  return (
    <Descriptions column={1} bordered size="small">
      <Descriptions.Item label="名称">{skill.name}</Descriptions.Item>
      <Descriptions.Item label="版本">{skill.version}</Descriptions.Item>
      <Descriptions.Item label="作者">{skill.author}</Descriptions.Item>
      <Descriptions.Item label="标签">
        {skill.tags.map((t) => (
          <Tag key={t}>{t}</Tag>
        ))}
      </Descriptions.Item>
      <Descriptions.Item label="状态">
        <Tag color={skill.enabled ? "green" : "red"}>
          {skill.enabled ? "已启用" : "已停用"}
        </Tag>
      </Descriptions.Item>
      <Descriptions.Item label="描述">{skill.description}</Descriptions.Item>
      <Descriptions.Item label="路径">{skill.path}</Descriptions.Item>
    </Descriptions>
  );
}
