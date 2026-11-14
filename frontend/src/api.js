// All calls go through relative URLs: Vite (dev) or nginx (Docker) forwards them.
export class ApiError extends Error {
  constructor(status, message, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

function describe(status, body) {
  if (status === 401 && body?.error) return body.error;
  if (status === 403) return "Your role is not allowed to do this.";
  if (status === 429) return body?.error || "Too many attempts. Wait a few minutes.";
  if (status === 503) return "No blockchain node is reachable. Nothing was saved.";
  if (status === 422) {
    let reason = body?.detail;
    try { const parsed = JSON.parse(body.detail); reason = parsed.error || parsed.reason || reason; } catch { /* keep text */ }
    return `The blockchain rejected this transaction${reason ? `: ${reason}` : "."}`;
  }
  return body?.error || `Request failed (${status}).`;
}

// ----- sign-in token (kept in localStorage so a refresh does not sign you out) -----
const TOKEN_KEY = "cc_token";
export const getToken = () => localStorage.getItem(TOKEN_KEY);
export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}
const unauthorizedListeners = new Set();
export function onUnauthorized(fn) {
  unauthorizedListeners.add(fn);
  return () => unauthorizedListeners.delete(fn);
}

async function request(path, options = {}) {
  const headers = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let res;
  try {
    res = await fetch(path, { ...options, headers });
  } catch {
    throw new ApiError(0, "Cannot reach the server. Check that the stack is running.");
  }
  const text = await res.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = { raw: text }; }
  if (!res.ok) {
    // an expired or revoked token on a normal request: forget it and let the app show the sign-in page
    if (res.status === 401 && token && !path.startsWith("/api/auth/login")) {
      setToken(null);
      unauthorizedListeners.forEach((fn) => fn());
    }
    throw new ApiError(res.status, describe(res.status, body), body);
  }
  return body;
}

const post = (path, data) => request(path, { method: "POST", body: JSON.stringify(data ?? {}) });

export const api = {
  charities: () => request("/api/charities"),
  campaigns: () => request("/api/campaigns"),
  transactions: (params = {}) => {
    const q = new URLSearchParams({ limit: 200, ...params });
    return request(`/api/transactions?${q}`);
  },
  tx: (id) => request(`/api/transactions/${encodeURIComponent(id)}`),
  trace: (id) => request(`/api/donations/${encodeURIComponent(id)}/trace`),
  sync: () => post("/api/chain/sync"),
  createDonor: (data) => post("/api/donors", data),
  createTx: (data) => post("/api/transactions", data),
  // Accounts
  login: (data) => post("/api/auth/login", data),
  register: (data) => post("/api/auth/register", data),
  me: () => request("/api/auth/me"),
  users: () => request("/api/admin/users"),
  createUser: (data) => post("/api/admin/users", data),
  // Blockchain nodes (1-4 = validators, 5 = observer)
  node: (n, path) => request(`/node/${n}${path}`),
};

export const NODES = [
  { n: 1, name: "Hope Foundation", role: "Validator 1" },
  { n: 2, name: "Auditor", role: "Validator 2" },
  { n: 3, name: "Ministry", role: "Validator 3" },
  { n: 4, name: "Donors Association", role: "Validator 4" },
  { n: 5, name: "Observer", role: "Reads only, never votes" },
];
