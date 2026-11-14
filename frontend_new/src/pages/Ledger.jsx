import { useState } from "react";
import { api } from "../api";
import { usePoll } from "../hooks.jsx";
import { money, dateTime, shortHash, shortId } from "../format";
import { ErrorNote, Loading } from "../components/Notice.jsx";
import HashChip from "../components/HashChip.jsx";

const TYPE_NAME = { DONATION: "Donation", ALLOCATION: "Allocation", EXPENSE: "Expense" };
const SHOWN = 50;

async function loadBlocks() {
  // Asking for a start far past the end returns an empty list plus the chain length.
  const head = await api.node(5, "/chain?from=1000000000");
  const length = head.length;
  const tail = await api.node(5, `/chain?from=${Math.max(length - SHOWN, 0)}`);
  return { length, blocks: [...tail.chain].reverse() };
}

export default function Ledger() {
  const { data, error, loading } = usePoll(loadBlocks, { ms: 5000 });
  const [check, setCheck] = useState(null);
  const [checking, setChecking] = useState(false);

  const verify = async () => {
    setChecking(true);
    try { setCheck(await api.node(5, "/validate")); } catch (e) { setCheck({ valid: false, error: e.message }); }
    setChecking(false);
  };

  return (
    <>
      <h1 className="page-title">Ledger</h1>
      <p className="muted lead">
        Every block below was signed by at least three of the four validators. Each block carries the fingerprint of the one before it,
        so changing an old record would break every block after it.
      </p>

      <div className="actions">
        <button className="btn" onClick={verify} disabled={checking}>{checking ? "Checking" : "Verify the whole chain"}</button>
        {check && (
          <span className={`note ${check.valid ? "note-ok" : "note-bad"}`} role="status">
            {check.valid ? "Chain is valid: every hash link and validator signature checks out." : `Chain is NOT valid: ${check.error}`}
          </span>
        )}
      </div>

      <ErrorNote error={error} />
      {loading && !data && <Loading text="Reading blocks" />}
      {data && (
        <>
          <p className="muted">{data.length} blocks. Showing the latest {data.blocks.length}.</p>
          <ul className="blocks">
            {data.blocks.map((b) => (
              <li key={b.hash}>
                <details>
                  <summary>
                    <span className="block-no">Block {b.index}</span>
                    <code>{shortHash(b.hash)}</code>
                    <span className="muted">{b.transactions.length} {b.transactions.length === 1 ? "transaction" : "transactions"}</span>
                    <span className="muted">{b.commit ? `${b.commit.signatures.length} signatures` : "genesis"}</span>
                    <span className="muted">{b.timestamp ? dateTime(b.timestamp) : ""}</span>
                  </summary>
                  <div className="block-body">
                    <p className="proof"><HashChip value={b.hash} label="Hash" /><HashChip value={b.previous_hash} label="Previous" /></p>
                    {b.transactions.length === 0 ? <p className="muted">No transactions.</p> : (
                      <table className="table">
                        <thead><tr><th>Type</th><th className="num">Amount</th><th>ID</th><th>Parent</th></tr></thead>
                        <tbody>
                          {b.transactions.map((t) => (
                            <tr key={t.id}>
                              <td>{TYPE_NAME[t.type] || t.type}</td>
                              <td className="num">{money(t.amount)}</td>
                              <td><code>{shortId(t.id)}</code></td>
                              <td><code>{t.type === "DONATION" ? "charity wallet" : shortId(t.parent)}</code></td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    )}
                  </div>
                </details>
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}
