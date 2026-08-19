// 路由入口: 全站统一外壳 (左导航) + 各页面内容
import { BrowserRouter, Route, Routes } from "react-router-dom";
import AppShell from "@/components/layout/AppShell";
import ChatPage from "@/pages/ChatPage";
import KnowledgePage from "@/pages/KnowledgePage";
import SessionsPage from "@/pages/SessionsPage";
import SkillsPage from "@/pages/SkillsPage";
import SettingsPage from "@/pages/SettingsPage";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppShell />}>
          <Route path="/" element={<ChatPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/skills" element={<SkillsPage />} />
          <Route path="/knowledge" element={<KnowledgePage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
