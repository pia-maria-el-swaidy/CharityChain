export function money(cents, currency = "USD") {
  const value = (Number(cents) || 0) / 100;
  try {
    return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(value);
  } catch {
    return `${value.toFixed(2)} ${currency}`;
  }
}

export const toCents = (text) => {
  const n = Number.parseFloat(String(text).replace(",", "."));
  return Number.isFinite(n) ? Math.round(n * 100) : NaN;
};

export const shortHash = (h) => (h && h.length > 16 ? `${h.slice(0, 8)}…${h.slice(-6)}` : h || "");
export const shortId = (id) => (id ? String(id).slice(0, 8) : "");

export function dateTime(value) {
  if (!value) return "";
  const d = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" });
}

export async function sha256File(file) {
  if (!window.crypto?.subtle) throw new Error("Hashing needs a secure page (localhost or https). Paste the hash instead.");
  const buf = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buf);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Totals used by several pages, computed from the gateway's transaction list.
export function summarize(txs) {
  const live = txs.filter((t) => t.status !== "REJECTED");
  const sum = (type, only) =>
    live.filter((t) => t.tx_type === type && (!only || only(t))).reduce((s, t) => s + Number(t.amount), 0);
  return {
    donated: sum("DONATION", (t) => t.status === "COMMITTED"),
    allocated: sum("ALLOCATION", (t) => t.status === "COMMITTED"),
    spent: sum("DISBURSEMENT", (t) => t.status === "COMMITTED"),
  };
}
