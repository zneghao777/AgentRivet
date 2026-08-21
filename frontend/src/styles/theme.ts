import type { ThemeConfig } from "antd";

export const designTokens = {
  brand50: "#edf6ff",
  brand100: "#d9ebff",
  brand200: "#b8d8fb",
  brand300: "#8bbdf4",
  brand400: "#5c9eec",
  brand500: "#2f7fe6",
  brand600: "#1d62c3",
  text: "#172b3f",
  textSecondary: "#60778e",
  border: "rgba(151, 190, 226, 0.56)",
  canvas: "#eaf4ff",
  success: "#169b91",
  warning: "#d98b27",
  danger: "#d9505d",
} as const;

export const appTheme: ThemeConfig = {
  token: {
    colorPrimary: designTokens.brand500,
    colorInfo: designTokens.brand500,
    colorSuccess: designTokens.success,
    colorWarning: designTokens.warning,
    colorError: designTokens.danger,
    colorText: designTokens.text,
    colorTextSecondary: designTokens.textSecondary,
    colorBorder: "#bfd7ec",
    colorBgContainer: "rgba(255,255,255,0.82)",
    colorBgElevated: "rgba(249,252,255,0.96)",
    borderRadius: 14,
    borderRadiusLG: 18,
    fontFamily: 'Inter, "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif',
    controlHeight: 38,
    controlOutline: "rgba(47,127,230,.18)",
  },
  components: {
    Button: { primaryShadow: "0 8px 20px rgba(47,127,230,.22)", defaultShadow: "none" },
    Drawer: { colorBgElevated: "rgba(242,249,255,.94)" },
    Input: { activeShadow: "0 0 0 3px rgba(47,127,230,.12)" },
    Select: { activeOutlineColor: "rgba(47,127,230,.12)" },
    Table: {
      headerBg: "rgba(221,238,255,.84)",
      headerColor: "#496177",
      rowHoverBg: "rgba(237,247,255,.9)",
      borderColor: "rgba(174,205,233,.48)",
      cellPaddingBlockSM: 11,
      cellPaddingInlineSM: 12,
    },
    Tabs: { itemSelectedColor: designTokens.brand500, inkBarColor: designTokens.brand500 },
  },
};

export function installCssDesignTokens() {
  const root = document.documentElement;
  const values: Record<string, string> = {
    "--brand-50": designTokens.brand50,
    "--brand-100": designTokens.brand100,
    "--brand-200": designTokens.brand200,
    "--brand-300": designTokens.brand300,
    "--brand-400": designTokens.brand400,
    "--brand-500": designTokens.brand500,
    "--brand-600": designTokens.brand600,
    "--bg-canvas": designTokens.canvas,
    "--success": designTokens.success,
    "--warning": designTokens.warning,
    "--danger": designTokens.danger,
  };
  Object.entries(values).forEach(([name, value]) => root.style.setProperty(name, value));
}
