// 技能库页: 列出内置和外部技能，可启用 / 禁用
import { useEffect, useState } from "react";
import { Button, Empty, Switch, Tag, message } from "antd";
import { getData, postData } from "@/api/http";
import { useSettingsStore } from "@/stores/settingsStore";
import type { SkillItem, SkillCategory } from "@/types/api";

const CATEGORY_LABEL: Record<SkillCategory, string> = {
  builtin: "内置技能",
  external: "外部技能",
};

const CATEGORY_ICON: Record<SkillCategory, string> = {
  builtin: "handshake",
  external: "cloud_download",
};

export default function SkillsPage() {
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const { baseUrl } = useSettingsStore();

  const load = async () => {
    try {
      const data = await getData<SkillItem[]>("/skills");
      setSkills(data);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载技能失败");
    }
  };

  useEffect(() => {
    load();
  }, []);

  const toggle = async (s: SkillItem) => {
    if (!s.enabled && (s.source ?? "builtin") === "external" && !baseUrl) {
      message.warning("请先配置外部仓库地址");
      return;
    }
    setBusy(s.name);
    try {
      const url = `/skills/${s.name}/${s.enabled ? "disable" : "enable"}`;
      await postData(url, {});
      await load();
      message.success(s.enabled ? "已禁用" : "已启用");
    } catch (e) {
      message.error(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusy(null);
    }
  };

  const groups: Record<string, SkillItem[]> = {};
  for (const s of skills) {
    // 后端可能未返回 source 字段, 默认作为 builtin
    const key = (s.source ?? "builtin") as SkillCategory;
    if (!groups[key]) groups[key] = [];
    groups[key].push(s);
  }

  return (
    <div
      style={{
        flex: 1,
        minWidth: 0,
        height: "100%",
        overflowY: "auto",
        padding: "28px 32px 60px",
      }}
    >
      <header
        style={{
          display: "flex",
          alignItems: "center",
          gap: 14,
          padding: "14px 22px",
          marginBottom: 24,
          background:
            "linear-gradient(135deg, rgba(0,80,203,0.06), rgba(0,184,169,0.04))",
          borderRadius: "var(--r-2xl)",
          border: "1px solid var(--brand-100)",
        }}
      >
        <div
          style={{
            width: 44,
            height: 44,
            borderRadius: 12,
            background:
              "linear-gradient(135deg, var(--brand-500), var(--brand-400))",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            boxShadow: "var(--shadow-brand)",
          }}
        >
          <span
            className="material-symbols-rounded icon-filled"
            style={{ color: "#fff", fontSize: 22 }}
          >
            auto_awesome
          </span>
        </div>
        <div style={{ flex: 1 }}>
          <div
            style={{
              fontFamily: "var(--font-display)",
              fontSize: 20,
              fontWeight: 700,
              color: "var(--gray-900)",
            }}
          >
            技能库
          </div>
          <div
            style={{
              fontSize: 12,
              color: "var(--gray-500)",
              marginTop: 4,
            }}
          >
            启用你需要的技能，让 Agent 拥有更专注的能力
          </div>
        </div>
        <Button onClick={load}>刷新</Button>
      </header>

      {skills.length === 0 && <Empty description="暂无技能" />}

      {Object.entries(groups).map(([cat, list]) => (
        <section key={cat} style={{ marginBottom: 22 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              marginBottom: 12,
              padding: "0 4px",
            }}
          >
            <span
              className="material-symbols-rounded"
              style={{
                color: "var(--brand-500)",
                fontSize: 18,
              }}
            >
              {CATEGORY_ICON[cat as SkillCategory] ?? "package"}
            </span>
            <h3
              style={{
                margin: 0,
                fontSize: 14,
                fontWeight: 600,
                color: "var(--gray-900)",
              }}
            >
              {CATEGORY_LABEL[cat as SkillCategory] ?? cat}
            </h3>
            <span
              style={{
                fontSize: 12,
                color: "var(--gray-500)",
                marginLeft: "auto",
              }}
            >
              {list.length} 项
            </span>
          </div>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
              gap: 12,
            }}
          >
            {list.map((s) => (
              <article
                key={s.name}
                style={{
                  background: "var(--bg-card)",
                  borderRadius: "var(--r-xl)",
                  border: s.enabled
                    ? "1px solid var(--brand-200)"
                    : "var(--border-soft)",
                  padding: 18,
                  boxShadow: s.enabled
                    ? "0 6px 20px -8px rgba(0,80,203,0.16)"
                    : "var(--shadow-xs)",
                  transition: "all var(--dur-base) var(--ease-out)",
                  display: "flex",
                  flexDirection: "column",
                  gap: 8,
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                  }}
                >
                  <div
                    style={{
                      width: 34,
                      height: 34,
                      borderRadius: 10,
                      background: s.enabled
                        ? "linear-gradient(135deg, var(--brand-500), var(--brand-400))"
                        : "var(--gray-100)",
                      color: "#fff",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      flexShrink: 0,
                    }}
                  >
                    <span
                      className="material-symbols-rounded"
                      style={{ fontSize: 18 }}
                    >
                      handshake
                    </span>
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div
                      style={{
                        fontSize: 14,
                        fontWeight: 600,
                        color: "var(--gray-900)",
                      }}
                    >
                      {s.name}
                    </div>
                    <Tag
                      color={(s.source ?? "builtin") === "builtin" ? "blue" : "purple"}
                      style={{
                        marginTop: 4,
                        borderRadius: "var(--r-full)",
                      }}
                    >
                      {(s.source ?? "builtin") === "builtin" ? "内置" : "外部"}
                    </Tag>
                  </div>
                  <Switch
                    checked={s.enabled}
                    onChange={() => toggle(s)}
                    loading={busy === s.name}
                  />
                </div>
                <div
                  style={{
                    fontSize: 12,
                    color: "var(--gray-600)",
                    lineHeight: 1.6,
                    minHeight: 36,
                  }}
                >
                  {s.description || "（无描述）"}
                </div>
                <div
                  style={{
                    fontSize: 11,
                    color: "var(--gray-400)",
                    display: "flex",
                    alignItems: "center",
                    gap: 4,
                  }}
                >
                  <span
                    className="material-symbols-rounded"
                    style={{ fontSize: 14 }}
                  >
                    label
                  </span>
                  <span>{s.path || "@builtin/skills"}</span>
                </div>
              </article>
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}
