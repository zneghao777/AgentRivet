const AGENT_COLORS = [
  "var(--agent-c1)",
  "var(--agent-c2)",
  "var(--agent-c3)",
  "var(--agent-c4)",
  "var(--agent-c5)",
  "var(--agent-c6)",
] as const;

export function agentColor(name: string): string {
  let hash = 0;
  for (let index = 0; index < name.length; index += 1) {
    hash = ((hash << 5) - hash + name.charCodeAt(index)) | 0;
  }
  return AGENT_COLORS[Math.abs(hash) % AGENT_COLORS.length];
}
