import { useState } from "react";
import { shortHash } from "../format";

export default function HashChip({ value, label }) {
  const [copied, setCopied] = useState(false);
  if (!value) return null;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    } catch { /* clipboard blocked: ignore */ }
  };
  return (
    <button type="button" className="hash" onClick={copy} title={`${value}\nClick to copy`}>
      {label && <span className="hash-label">{label}</span>}
      <code>{shortHash(value)}</code>
      <span className="hash-copy">{copied ? "Copied" : "Copy"}</span>
    </button>
  );
}
