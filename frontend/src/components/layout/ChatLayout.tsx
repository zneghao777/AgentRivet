// 三栏布局外壳: 左侧导航 + 中部内容区 + 右侧上下文
import type { ReactNode } from "react";
import Sidebar from "./Sidebar";
import ContextPanel from "./ContextPanel";

interface Props {
  children: ReactNode;
  showContext?: boolean;
}

export default function ChatLayout({ children, showContext = true }: Props) {
  return (
    <div
      style={{
        display: "flex",
        height: "100vh",
        width: "100vw",
        background: "var(--bg-canvas)",
        overflow: "hidden",
      }}
    >
      <Sidebar />
      <main
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          minWidth: 0,
          position: "relative",
        }}
      >
        {children}
      </main>
      {showContext && <ContextPanel />}
    </div>
  );
}
