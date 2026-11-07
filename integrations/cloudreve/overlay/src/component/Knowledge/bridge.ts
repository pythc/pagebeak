import SessionManager from "../../session";
import { getAccessToken } from "../../api/request.ts";
import { store } from "../../redux/store.ts";

export const knowledgeOrigin = () => {
  const url = new URL(window.location.origin);
  // Production routes /v1 through the same HTTPS origin. The separate port is
  // solely the existing localhost development layout.
  if (url.port === "5878" && (["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)
      || url.hostname.endsWith(".localhost"))) url.port = "5876";
  return url.origin;
};

export async function knowledgeRequest(path: string, body?: unknown) {
  const userId = SessionManager.currentLoginOrNull()?.user.id;
  if (!userId) throw new Error("请先登录文件空间");
  const token = await store.dispatch(getAccessToken());
  if (!token) throw new Error("文件空间登录已失效");
  if (SessionManager.currentLoginOrNull()?.user.id !== userId) throw new Error("账号已变化，请重新打开页面");
  const response = await fetch(knowledgeOrigin() + "/v1/native/" + path, {
    method: body === undefined ? "GET" : "POST",
    credentials: "omit",
    headers: { "Content-Type": "application/json", "X-Cloudreve-Token": token.replace(/^Bearer /, ""), "X-AKB-File-User": userId },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (SessionManager.currentLoginOrNull()?.user.id !== userId) throw new Error("账号已变化，请重新打开页面");
  const result = response.ok && /application\/(zip|pdf)/.test(response.headers.get("Content-Type") || "")
    ? await response.blob() : await response.json();
  if (SessionManager.currentLoginOrNull()?.user.id !== userId) throw new Error("账号已变化，已丢弃原账号的响应");
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "请求未完成，请重试");
  return result;
}
