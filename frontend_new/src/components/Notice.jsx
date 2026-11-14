export function ErrorNote({ error }) {
  if (!error) return null;
  return <p className="note note-bad" role="alert">{error.message || String(error)}</p>;
}

export function Loading({ text = "Loading" }) {
  return <p className="empty">{text}…</p>;
}
