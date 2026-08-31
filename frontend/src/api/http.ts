// axios 实例：注入 X-API-Key，统一响应处理
import axios from "axios";
import type { AxiosRequestConfig } from "axios";
import type { ApiResponse } from "@/types/api";
import { API_KEY_STORAGE_KEY, DEFAULT_API_KEY } from "@/constants";

export const http = axios.create({
  baseURL: import.meta.env.VITE_API_BASE ?? "/api/v1",
  timeout: 120000,
});

// 获取当前 API Key：优先用户配置，缺省用本地开发默认 key
function getApiKey(): string {
  return localStorage.getItem(API_KEY_STORAGE_KEY) || DEFAULT_API_KEY;
}

// 请求拦截：注入 X-API-Key
http.interceptors.request.use((config) => {
  const apiKey = getApiKey();
  if (apiKey) {
    config.headers["X-API-Key"] = apiKey;
  }
  return config;
});

// 响应拦截：code !== 0 抛出带 code 的错误；401 给出明确提示
http.interceptors.response.use(
  (response) => {
    const body = response.data as ApiResponse;
    if (body && typeof body.code === "number" && body.code !== 0) {
      return Promise.reject(new Error(body.message || "请求失败"));
    }
    return response;
  },
  (error) => {
    const status = error?.response?.status;
    const bodyMessage = error?.response?.data?.message;
    const detail = error?.response?.data?.detail;
    const detailMessage = typeof detail === "string" ? detail : detail?.message;
    if (status === 401) {
      return Promise.reject(
        new Error("鉴权失败(401)：请确认 API Key 已配置且与后端一致（设置页可修改）")
      );
    }
    if (status === 503) {
      return Promise.reject(new Error(
        bodyMessage || detailMessage || "服务未就绪(503)：请检查后端是否启动"
      ));
    }
    if (bodyMessage || detailMessage) {
      return Promise.reject(new Error(bodyMessage || detailMessage));
    }
    if (!error?.response) {
      return Promise.reject(
        new Error("无法连接后端服务，请确认后端已启动且端口正确")
      );
    }
    return Promise.reject(error);
  }
);

export async function getData<T>(
  url: string,
  config?: AxiosRequestConfig
): Promise<T> {
  const resp = await http.get<ApiResponse<T>>(url, config);
  return (resp.data.data ?? null) as T;
}

export async function postData<T>(
  url: string,
  body?: unknown
): Promise<T> {
  const resp = await http.post<ApiResponse<T>>(url, body);
  return (resp.data.data ?? null) as T;
}

export async function putData<T>(url: string, body?: unknown): Promise<T> {
  const resp = await http.put<ApiResponse<T>>(url, body);
  return (resp.data.data ?? null) as T;
}

export async function patchData<T>(url: string, body?: unknown): Promise<T> {
  const resp = await http.patch<ApiResponse<T>>(url, body);
  return (resp.data.data ?? null) as T;
}

export async function deleteData<T>(url: string): Promise<T> {
  const resp = await http.delete<ApiResponse<T>>(url);
  return (resp.data.data ?? null) as T;
}
