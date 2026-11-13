const MAP = {
  COMMITTED: { cls: "ok", text: "Confirmed" },
  PENDING: { cls: "wait", text: "Awaiting confirmation" },
  REJECTED: { cls: "bad", text: "Rejected" },
};

export default function StatusBadge({ status }) {
  const s = MAP[status] || { cls: "wait", text: status || "Unknown" };
  return <span className={`badge badge-${s.cls}`}>{s.text}</span>;
}
