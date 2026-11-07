let csrf = "";
export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
  }
}
export const setCsrf = (value: string) => {
  csrf = value;
};
export async function api<T = any>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (
    options.body &&
    !(options.body instanceof FormData) &&
    !(options.body instanceof Blob)
  )
    headers.set("Content-Type", "application/json");
  if (options.method && !["GET", "HEAD"].includes(options.method))
    headers.set("X-CSRF-Token", csrf);
  const response = await fetch("/v1" + path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new ApiError(
      typeof data.detail === "string"
        ? data.detail
        : `请求未完成（${response.status}）`,
      response.status,
    );
  }
  if (response.status === 204) return undefined as T;
  if (response.headers.get("Content-Type")?.includes("application/vnd.openxmlformats-officedocument")
      || response.headers.get("Content-Type")?.includes("application/zip"))
    return response.blob() as Promise<T>;
  return response.json();
}
