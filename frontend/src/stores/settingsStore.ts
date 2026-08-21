// settingsStore：API Key（持久化 localStorage）、模型、Base URL
import { create } from "zustand";
import {
  API_KEY_STORAGE_KEY,
  BASE_URL_STORAGE_KEY,
  DEFAULT_API_KEY,
  DEFAULT_CHAT_MODEL,
  MODEL_STORAGE_KEY,
} from "@/constants";

// 读取本地存储中的 API Key，无则使用本地开发默认 key（与 backend/.env 对应）
function loadApiKey(): string {
  return localStorage.getItem(API_KEY_STORAGE_KEY) || DEFAULT_API_KEY;
}

function loadModel(): string {
  const stored = localStorage.getItem(MODEL_STORAGE_KEY);
  if (!stored || ["deepseek-chat", "deepseek-reasoner", "deepseek-v4-flash"].includes(stored)) {
    localStorage.setItem(MODEL_STORAGE_KEY, DEFAULT_CHAT_MODEL);
    return DEFAULT_CHAT_MODEL;
  }
  return stored;
}

interface SettingsState {
  apiKey: string;
  model: string;
  baseUrl: string;
  setApiKey: (k: string) => void;
  setModel: (m: string) => void;
  setBaseUrl: (b: string) => void;
}

export const useSettingsStore = create<SettingsState>((set) => ({
  apiKey: loadApiKey(),
  model: loadModel(),
  baseUrl: localStorage.getItem(BASE_URL_STORAGE_KEY) ?? "https://api.deepseek.com",
  setApiKey: (k) => {
    localStorage.setItem(API_KEY_STORAGE_KEY, k);
    set({ apiKey: k });
  },
  setModel: (m) => {
    localStorage.setItem(MODEL_STORAGE_KEY, m);
    set({ model: m });
  },
  setBaseUrl: (b) => {
    localStorage.setItem(BASE_URL_STORAGE_KEY, b);
    set({ baseUrl: b });
  },
}));
