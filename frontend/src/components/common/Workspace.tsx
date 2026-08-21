import { useEffect, useState, type ReactNode } from "react";
import { Drawer } from "antd";
import { GlassSurface } from "./Glass";

export function WorkspaceHeader({
  icon,
  title,
  description,
  actions,
  eyebrow,
}: {
  icon: string;
  title: string;
  description: string;
  actions?: ReactNode;
  eyebrow?: ReactNode;
}) {
  return (
    <GlassSurface as="header" tone="chrome" className="workspace-header">
      <div className="workspace-header-icon" aria-hidden="true">
        <span className="material-symbols-rounded">{icon}</span>
      </div>
      <div className="workspace-header-copy">
        {eyebrow ? <div className="workspace-eyebrow">{eyebrow}</div> : null}
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {actions ? <div className="workspace-header-actions">{actions}</div> : null}
    </GlassSurface>
  );
}

export function MetricStrip({
  items,
}: {
  items: Array<{
    label: string;
    value: ReactNode;
    meta?: ReactNode;
    tone?: "default" | "success" | "warning" | "danger" | "teal";
    onClick?: () => void;
  }>;
}) {
  return (
    <section className="metric-strip" aria-label="关键指标">
      {items.map((item) => (
        <GlassSurface as={item.onClick ? "button" : "article"} type={item.onClick ? "button" : undefined} onClick={item.onClick} tone="panel" className={`metric-cell tone-${item.tone ?? "default"}${item.onClick ? " is-action" : ""}`} key={item.label}>
          <span className="metric-label"><i aria-hidden="true" />{item.label}</span>
          <strong>{item.value}</strong>
          {item.meta ? <span className="metric-meta">{item.meta}</span> : null}
        </GlassSurface>
      ))}
    </section>
  );
}

export function StatusDot({
  label,
  tone = "neutral",
}: {
  label: string;
  tone?: "success" | "warning" | "danger" | "info" | "neutral";
}) {
  return (
    <span className={`status-dot status-${tone}`}>
      <i aria-hidden="true" />
      {label}
    </span>
  );
}

export function Inspector({
  title,
  subtitle,
  actions,
  children,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <aside className="workspace-inspector">
      <header className="inspector-header">
        <div>
          <h2>{title}</h2>
          {subtitle ? <div className="inspector-subtitle">{subtitle}</div> : null}
        </div>
        {actions ? <div className="inspector-actions">{actions}</div> : null}
      </header>
      <div className="inspector-body">{children}</div>
    </aside>
  );
}

function useCompactInspector() {
  const getMatches = () => typeof window !== "undefined" && window.matchMedia("(max-width: 1180px)").matches;
  const [compact, setCompact] = useState(getMatches);

  useEffect(() => {
    const media = window.matchMedia("(max-width: 1180px)");
    const update = () => setCompact(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  return compact;
}

export function ResponsiveInspector({
  open,
  onClose,
  title,
  subtitle,
  actions,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const compact = useCompactInspector();

  if (!compact) {
    return <Inspector title={title} subtitle={subtitle} actions={actions}>{children}</Inspector>;
  }

  return (
    <Drawer
      className="glass-inspector-drawer"
      width="min(420px, 92vw)"
      placement="right"
      open={open}
      onClose={onClose}
      destroyOnHidden={false}
      title={<div className="drawer-inspector-title"><strong>{title}</strong>{subtitle}</div>}
      extra={actions}
    >
      <div className="inspector-body">{children}</div>
    </Drawer>
  );
}

export function InspectorField({
  label,
  children,
  mono = false,
}: {
  label: string;
  children: ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="inspector-field">
      <span>{label}</span>
      <div className={mono ? "mono" : undefined}>{children}</div>
    </div>
  );
}
