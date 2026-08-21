import { createElement, type ElementType, type ReactNode } from "react";

type GlassTone = "chrome" | "panel" | "data" | "floating";

export function GlassSurface({
  as = "div",
  tone = "panel",
  className = "",
  children,
  ...props
}: {
  as?: ElementType;
  tone?: GlassTone;
  className?: string;
  children: ReactNode;
  [key: string]: unknown;
}) {
  return createElement(
    as,
    {
      ...props,
      className: `glass-surface glass-${tone}${className ? ` ${className}` : ""}`,
    },
    children,
  );
}

export function GlassEmptyState({
  icon = "inbox",
  title,
  description,
  action,
}: {
  icon?: string;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="glass-empty-state" role="status">
      <span className="glass-empty-icon material-symbols-rounded" aria-hidden="true">{icon}</span>
      <strong>{title}</strong>
      {description ? <p>{description}</p> : null}
      {action ? <div className="glass-empty-action">{action}</div> : null}
    </div>
  );
}
