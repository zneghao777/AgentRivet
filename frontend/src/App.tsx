// 路由入口: 全站统一外壳 (左导航) + 按页面切分业务代码
import { lazy, Suspense } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import AppShell from "@/components/layout/AppShell";
import { GlassEmptyState } from "@/components/common/Glass";

const ChatPage = lazy(() => import("@/pages/ChatPage"));
const KnowledgeCenterPage = lazy(() => import("@/pages/KnowledgeCenterPage"));
const KnowledgeDetailPage = lazy(() => import("@/pages/KnowledgeDetailPage"));
const MemoryPage = lazy(() => import("@/pages/MemoryPage"));
const SkillsPage = lazy(() => import("@/pages/SkillsPage"));
const SettingsPage = lazy(() => import("@/pages/SettingsPage"));
const ObservabilityPage = lazy(() => import("@/pages/observability/ObservabilityPage"));
const TraceDetailPage = lazy(() => import("@/pages/observability/TraceDetailPage"));

function RouteFallback() {
  return (
    <div className="route-fallback">
      <GlassEmptyState icon="progress_activity" title="正在加载工作区" description="正在准备页面组件与数据…" />
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <Suspense fallback={<RouteFallback />}>
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
      </Suspense>
    </BrowserRouter>
  );
}
