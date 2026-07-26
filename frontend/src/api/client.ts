export const API_BASE =
  (import.meta as any).env?.VITE_API_BASE_URL || "http://localhost:8000";

const TOKEN_KEY = "aidocs_token";

export const tokenStore = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (t: string) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

async function handle(res: Response) {
  if (res.status === 401) {
    tokenStore.clear();
    if (!location.pathname.startsWith("/login")) location.href = "/login";
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch {
      /* ignore */
    }
    throw new Error(typeof detail === "string" ? detail : "Request failed");
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

function authHeaders(): Record<string, string> {
  const t = tokenStore.get();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

export const api = {
  get: (path: string) =>
    fetch(`${API_BASE}${path}`, { headers: { ...authHeaders() } }).then(handle),

  post: (path: string, body?: unknown) =>
    fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }).then(handle),

  patch: (path: string, body?: unknown) =>
    fetch(`${API_BASE}${path}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    }).then(handle),

  put: (path: string, body?: unknown) =>
    fetch(`${API_BASE}${path}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    }).then(handle),

  del: (path: string) =>
    fetch(`${API_BASE}${path}`, { method: "DELETE", headers: { ...authHeaders() } }).then(handle),

  upload: (path: string, form: FormData) =>
    fetch(`${API_BASE}${path}`, { method: "POST", headers: { ...authHeaders() }, body: form }).then(handle),

  /** Authenticated binary fetch -> object URL (caller revokes when done). */
  blobUrl: async (path: string): Promise<string> => {
    const res = await fetch(`${API_BASE}${path}`, { headers: { ...authHeaders() } });
    if (!res.ok) throw new Error(`fetch failed: ${res.status}`);
    return URL.createObjectURL(await res.blob());
  },
};

/** Stream a POST SSE endpoint (chat). Calls onEvent for each parsed event. */
export async function streamPost(
  path: string,
  body: unknown,
  onEvent: (event: string, data: any) => void,
  signal?: AbortSignal
) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream", ...authHeaders() },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    throw new Error(`Stream failed: ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    // Normalize CRLF (sse-starlette emits \r\n) so event framing splits correctly.
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() || "";
    for (const chunk of chunks) {
      let ev = "message";
      let data = "";
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) ev = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) {
        try {
          onEvent(ev, JSON.parse(data));
        } catch {
          onEvent(ev, data);
        }
      }
    }
  }
}
