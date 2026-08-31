// settingsStore：API Key（持久化 localStorage）、模型、Base URL
import { create } from "zustand";
import {
  API_KEY_STORAGE_KEY,
  APPROVAL_MODE_STORAGE_KEY,
  BASE_URL_STORAGE_KEY,
  CUSTOM_MODELS_STORAGE_KEY,
  DEFAULT_API_KEY,
  DEFAULT_CHAT_MODEL,
  MODEL_STORAGE_KEY,
} from "@/constants";

export interface CustomChatModel {
  id: string;
  name: string;
  modelId: string;
  description: string;
  baseUrl: string;
  apiKey: string;
  createdAt: string;
}

export type CustomChatModelInput = Pick<CustomChatModel, "name" | "modelId" | "description" | "baseUrl" | "apiKey">;
export type ApprovalMode = "manual" | "auto";

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

function loadCustomModels(): CustomChatModel[] {
  try {
    const value = JSON.parse(localStorage.getItem(CUSTOM_MODELS_STORAGE_KEY) || "[]");
    if (!Array.isArray(value)) return [];
    const seen = new Set<string>();
    const models: CustomChatModel[] = [];
    for (const item of value) {
      if (
        !item || typeof item.id !== "string" || typeof item.name !== "string"
        || typeof item.modelId !== "string" || typeof item.description !== "string"
        || typeof item.createdAt !== "string" || seen.has(item.modelId)
      ) continue;
      seen.add(item.modelId);
      models.push({
        ...item,
        baseUrl: typeof item.baseUrl === "string"
          ? item.baseUrl
          : localStorage.getItem(BASE_URL_STORAGE_KEY) ?? "https://api.deepseek.com",
        apiKey: typeof item.apiKey === "string" ? item.apiKey : "",
      });
    }
    return models;
  } catch {
    return [];
  }
}

function loadApprovalMode(): ApprovalMode {
  return localStorage.getItem(APPROVAL_MODE_STORAGE_KEY) === "manual" ? "manual" : "auto";
}

function persistCustomModels(models: CustomChatModel[]) {
  localStorage.setItem(CUSTOM_MODELS_STORAGE_KEY, JSON.stringify(models));
}

function customModelRecordId(): string {
  return typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `model-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

interface SettingsState {
  apiKey: string;
  model: string;
  baseUrl: string;
  customModels: CustomChatModel[];
  approvalMode: ApprovalMode;
  setApiKey: (k: string) => void;
  setModel: (m: string) => void;
  setBaseUrl: (b: string) => void;
  setApprovalMode: (mode: ApprovalMode) => void;
  addCustomModel: (input: CustomChatModelInput) => CustomChatModel;
  updateCustomModel: (id: string, input: CustomChatModelInput) => void;
  removeCustomModel: (id: string) => void;
}

export const useSettingsStore = create<SettingsState>((set) => ({
  apiKey: loadApiKey(),
  model: loadModel(),
  baseUrl: localStorage.getItem(BASE_URL_STORAGE_KEY) ?? "https://api.deepseek.com",
  customModels: loadCustomModels(),
  approvalMode: loadApprovalMode(),
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
  setApprovalMode: (approvalMode) => {
    localStorage.setItem(APPROVAL_MODE_STORAGE_KEY, approvalMode);
    set({ approvalMode });
  },
  addCustomModel: (input) => {
    const customModel: CustomChatModel = {
      id: customModelRecordId(),
      name: input.name.trim(),
      modelId: input.modelId.trim(),
      description: input.description.trim(),
      baseUrl: input.baseUrl.trim().replace(/\/+$/, ""),
      apiKey: input.apiKey.trim(),
      createdAt: new Date().toISOString(),
    };
    set((state) => {
      const customModels = [...state.customModels, customModel];
      persistCustomModels(customModels);
      return { customModels };
    });
    return customModel;
  },
  updateCustomModel: (id, input) => {
    set((state) => {
      const previous = state.customModels.find((item) => item.id === id);
      if (!previous) return state;
      const updated = {
        ...previous,
        name: input.name.trim(),
        modelId: input.modelId.trim(),
        description: input.description.trim(),
        baseUrl: input.baseUrl.trim().replace(/\/+$/, ""),
        apiKey: input.apiKey.trim(),
      };
      const customModels = state.customModels.map((item) => item.id === id ? updated : item);
      persistCustomModels(customModels);
      if (state.model === previous.modelId) {
        localStorage.setItem(MODEL_STORAGE_KEY, updated.modelId);
        return { customModels, model: updated.modelId };
      }
      return { customModels };
    });
  },
  removeCustomModel: (id) => {
    set((state) => {
      const removed = state.customModels.find((item) => item.id === id);
      if (!removed) return state;
      const customModels = state.customModels.filter((item) => item.id !== id);
      persistCustomModels(customModels);
      if (state.model === removed.modelId) {
        localStorage.setItem(MODEL_STORAGE_KEY, DEFAULT_CHAT_MODEL);
        return { customModels, model: DEFAULT_CHAT_MODEL };
      }
      return { customModels };
    });
  },
}));
