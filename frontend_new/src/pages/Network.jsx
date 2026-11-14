import { useState } from "react";
import { api } from "../api";
import { useNodes } from "../hooks.jsx";

function VerifyButton({ n, disabled }) {
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try { setResult(await api.node(n, "/validate")); } catch (e) { setResult({ valid: false, error: e.message }); }
    setBusy(false);
  };
  return (
    <span className="verify">
      <button className="btn btn-small" onClick={run} disabled={disabled || busy}>{busy ? "Checking" : "Verify chain"}</button>
      {result && <span className={result.valid ? "ok-text" : "bad-text"}>{result.valid ? "Valid" : "Invalid"}</span>}
    </span>
  );
}

export default function Network() {
  const { nodes } = useNodes();
  const validators = nodes.filter((n) => n.n <= 4);
  const up = validators.filter((n) => n.online).length;
  const heights = nodes.filter((n) => n.online).map((n) => n.status.height);
  const behind = heights.length > 0 && Math.min(...heights) !== Math.max(...heights);

  return (
    <>
      <h1 className="page-title">Network</h1>
      <p className="muted lead">
        {up >= 3
          ? "At least three of four validators are online, so new blocks can be confirmed."
          : "Fewer than three validators are online. Transactions wait until enough validators return."}
        {behind && " Some nodes are behind and are catching up from their peers."}
      </p>
      <p className="muted small">To test failure, stop a validator with <code>docker compose stop node3</code> and watch this page. Start it again with <code>docker compose start node3</code>.</p>

      <table className="table nodes">
        <thead>
          <tr><th>Node</th><th>State</th><th className="num">Blocks</th><th className="num">Waiting</th><th>Consensus</th><th className="num">Last block</th><th>Integrity</th></tr>
        </thead>
        <tbody>
          {nodes.map((n) => {
            const s = n.status;
            return (
              <tr key={n.n} className={n.online === false ? "row-down" : ""}>
                <td><strong>{n.name}</strong><br /><span className="muted small">{n.role}</span></td>
                <td>{n.online === null ? "Checking" : n.online ? <span className="ok-text">Online</span> : <span className="bad-text">Offline</span>}</td>
                <td className="num">{s ? s.height : "-"}</td>
                <td className="num">{s ? s.pending : "-"}</td>
                <td>{s && s.step ? `Round ${s.round}, ${s.step}` : s ? "Idle" : "-"}</td>
                <td className="num">{s ? `${Math.round(s.seconds_since_last_block)} s ago` : "-"}</td>
                <td>{s ? <VerifyButton n={n.n} /> : <span className="muted">-</span>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </>
  );
}
