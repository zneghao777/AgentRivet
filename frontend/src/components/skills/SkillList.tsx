// Skill 列表（支持点击选中查看详情、开关启停）
import { List, Popconfirm, Switch, Tag } from "antd";
import type { SkillItem } from "@/types/api";

export default function SkillList({
  skills,
  selectedName,
  toggling,
  onSelect,
  onToggle,
}: {
  skills: SkillItem[];
  selectedName: string | null;
  toggling: string | null;
  onSelect: (s: SkillItem) => void;
  onToggle: (name: string, enabled: boolean) => void;
}) {
  return (
    <List
      dataSource={skills}
      locale={{ emptyText: "暂无 Skill" }}
      renderItem={(s) => (
        <List.Item
          onClick={() => onSelect(s)}
          style={{
            cursor: "pointer",
            padding: "12px",
            borderRadius: 8,
            background: selectedName === s.name ? "#e6f4ff" : "transparent",
            border:
              selectedName === s.name ? "1px solid #91caff" : "1px solid transparent",
            marginBottom: 4,
          }}
          actions={[
            <Popconfirm
              key="toggle"
              title={s.enabled ? "停用该 Skill？" : "启用该 Skill？"}
              description={
                s.enabled
                  ? "停用后该 Skill 将不再被 Agent 加载"
                  : "启用后该 Skill 可被 Agent 加载使用"
              }
              okText={s.enabled ? "停用" : "启用"}
              cancelText="取消"
              onConfirm={(e) => {
                e?.stopPropagation();
                onToggle(s.name, !s.enabled);
              }}
            >
              <span onClick={(e) => e.stopPropagation()}>
                <Switch
                  checked={s.enabled}
                  loading={toggling === s.name}
                  checkedChildren="启用"
                  unCheckedChildren="停用"
                />
              </span>
            </Popconfirm>,
          ]}
        >
          <List.Item.Meta
            title={
              <span>
                {s.name}{" "}
                <Tag color="blue">v{s.version}</Tag>
                {s.tags.map((t) => (
                  <Tag key={t}>{t}</Tag>
                ))}
              </span>
            }
            description={s.description}
          />
        </List.Item>
      )}
    />
  );
}
