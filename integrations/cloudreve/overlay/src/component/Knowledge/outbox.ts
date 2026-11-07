export interface FileNotice { request_id: string; operation: string; targets: string[]; new_name: string; }
const key = (userId: string) => "zhixu-file-outbox:" + userId;
export function pendingNotices(userId: string): FileNotice[] {
  try { const value = JSON.parse(localStorage.getItem(key(userId)) ?? "[]"); return Array.isArray(value) ? value : []; } catch { return []; }
}
export function removeNotice(userId: string, requestId: string) {
  localStorage.setItem(key(userId), JSON.stringify(pendingNotices(userId).filter(item => item.request_id !== requestId)));
}
export function queueFileOperation(userId: string, route: string, method: string, payload: unknown) {
  let data: any = payload;
  if (typeof data === "string") { try { data = JSON.parse(data); } catch { data = {}; } }
  if (!data || typeof data !== "object") data = {};
  const candidates = [data.uri, data.src, data.dst, ...(Array.isArray(data.uris) ? data.uris : [])];
  const targets = candidates.filter(value => typeof value === "string" && value.startsWith("cloudreve://my") && value.length <= 2048).slice(0, 100);
  const operation = method === "DELETE" ? "delete" : route.includes("rename") ? "rename" : route.includes("move") ? "move" : route.includes("restore") ? "restore" : route.includes("content") ? "content_update" : route.includes("upload") ? "upload" : route.includes("create") ? "create" : "metadata";
  const notice: FileNotice = { request_id: crypto.randomUUID(), operation, targets, new_name: typeof data.new_name === "string" ? data.new_name.slice(0,255) : "" };
  localStorage.setItem(key(userId), JSON.stringify([...pendingNotices(userId), notice]));
  window.dispatchEvent(new Event("kb-file-changed"));
}
