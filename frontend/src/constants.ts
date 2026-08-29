// 前端常量：与 backend/.env 的 API_KEYS 对应
// 本地开发默认 API Key（后端配置的合法 key），未手动配置时兜底使用
export const DEFAULT_API_KEY = "sk-dev-key-1";
export const API_KEY_STORAGE_KEY = "harness_api_key";
export const MODEL_STORAGE_KEY = "harness_model";
export const BASE_URL_STORAGE_KEY = "harness_base_url";
export const CUSTOM_MODELS_STORAGE_KEY = "harness_custom_models";

export const CHAT_MODELS = [
  { value: "deepseek-v4-pro", label: "deepseek-v4-pro", description: "通用对话与工具调用" },
  { value: "deepseek-v4-flash-vision-exp", label: "deepseek-v4-flash-vision-exp", description: "支持图片理解" },
] as const;
export const DEFAULT_CHAT_MODEL = CHAT_MODELS[0].value;
export const VISION_CHAT_MODEL = CHAT_MODELS[1].value;
