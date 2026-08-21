import React from "react";
import ReactDOM from "react-dom/client";
import { App as AntdApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import App from "./App";
import "@/styles/global.css";
import "@/styles/glass-system.css";
import { appTheme, installCssDesignTokens } from "@/styles/theme";

installCssDesignTokens();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      locale={zhCN}
      theme={appTheme}
    >
      <AntdApp><App /></AntdApp>
    </ConfigProvider>
  </React.StrictMode>
);
