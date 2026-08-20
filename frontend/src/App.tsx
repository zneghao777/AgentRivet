// 路由入口: 全站统一外壳 (左导航) + 各页面内容
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import AppShell from "@/components/layout/AppShell";
import ChatPage from "@/pages/ChatPage";
import KnowledgeCenterPage from "@/pages/KnowledgeCenterPage";
import KnowledgeDetailPage from "@/pages/KnowledgeDetailPage";
import MemoryPage from "@/pages/MemoryPage";
import SkillsPage from "@/pages/SkillsPage";
import SettingsPage from "@/pages/SettingsPage";
import ObservabilityPage from "@/pages/observability/ObservabilityPage";
import TraceDetailPage from "@/pages/observability/TraceDetailPage";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/" element={<ChatPage />} />
          <Route path="/observability" element={<ObservabilityPage />} />
          {/* 历史观测子页面统一重定向到观测台对应 tab，避免 404 与重复入口 */}
          <Route path="/observability/traces" element={<Navigate to="/observability?tab=traces" replace />} />
          <Route path="/observability/calls" element={<Navigate to="/observability?tab=calls" replace />} />
          <Route path="/observability/logs" element={<Navigate to="/observability?tab=logs" replace />} />
          <Route path="/observability/costs" element={<Navigate to="/observability?tab=costs" replace />} />
          <Route path="/observability/quality" element={<Navigate to="/observability?tab=quality" replace />} />
          <Route path="/observability/alerts" element={<Navigate to="/observability?tab=alerts" replace />} />
          <Route path="/observability/tool-logs" element={<Navigate to="/observability?tab=calls" replace />} />
          {/* Trace 详情保留为独立路由（从运行记录跳转进入） */}
          <Route path="/observability/traces/:id" element={<TraceDetailPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/knowledge" element={<KnowledgeCenterPage />} />
          <Route path="/knowledge/:kbId" element={<KnowledgeDetailPage />} />
          <Route path="/memory" element={<MemoryPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
