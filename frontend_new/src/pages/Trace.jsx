import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { usePoll } from "../hooks.jsx";
import { money, shortId } from "../format";
import { ErrorNote, Loading } from "../components/Notice.jsx";
import StatusBadge from "../components/StatusBadge.jsx";
import HashChip from "../components/HashChip.jsx";
import MoneyFlow from "../components/MoneyFlow.jsx";

const FINAL = ["ok", "unknown", "rejected", "notdonation"];

async function loadTrace(id) {
  const meta = await api.tx(id).catch(() => null);
  if (meta && meta.tx_type !== "DONATION") return { state: "notdonation", meta };
  if (meta?.status === "PENDING") await api.sync().catch(() => {}); // ask the gateway to read new blocks now
  let trace;
  try {
    trace = await api.trace(id);
  } catch (e) {
    if (e.status === 404) return { state: meta ? (meta.status === "REJECTED" ? "rejected" : "pending") : "unknown", meta };
    throw e;
  }
  const ids = [id, ...trace.allocations.flatMap((a) => [a.id, ...a.expenses.map((x) => x.id)])];
  const [metas, campaigns, charities] = await Promise.all([
    Promise.all(ids.map((i) => api.tx(i).catch(() => null))),
    api.campaigns(),
    api.charities(),
  ]);
  const byId = Object.fromEntries(ids.map((i, k) => [i, metas[k]]));
  return { state: "ok", trace, byId, campaigns, charities };
}

function Proof({ id, meta }) {
  return (
    <p className="proof">
      {meta?.block_index != null ? <span>Recorded in block {meta.block_index}</span> : <span>Block not known yet</span>}
      <HashChip value={id} label="ID" />
    </p>
  );
}

function TraceView({ id }) {
  const { data, error } = usePoll(() => loadTrace(id), { ms: 4000, until: (d) => FINAL.includes(d?.state), deps: [id] });

  if (error && !data) return <ErrorNote error={error} />;
  if (!data) return <Loading text="Reading the ledger" />;
  if (data.state === "unknown") return <p className="note note-bad">No donation with this ID exists. Check the ID and try again.</p>;
  if (data.state === "rejected") return <p className="note note-bad">This donation was rejected by the blockchain and was not recorded.</p>;
  if (data.state === "notdonation")
    return <p className="note note-bad">This ID belongs to an {data.meta.tx_type.toLowerCase()}, not a donation. Trace starts from a donation ID.</p>;
  if (data.state === "pending")
    return <p className="note note-wait">Your donation is waiting for the validators to confirm it. This page updates by itself, usually within a few seconds.</p>;

  const { trace, byId, campaigns, charities } = data;
  const meta = byId[id];
  const d = trace.donation;
  const currency = meta?.currency || "USD";
  const charity = charities.find((c) => c.charity_id === meta?.charity_id);
  const title = (cid) => campaigns.find((c) => c.campaign_id === cid)?.title || cid;

  return (
    <>
      <section>
        <h2>{money(d.amount, currency)} donated{charity ? ` to ${charity.name}` : ""}</h2>
        <MoneyFlow total={d.amount} allocated={trace.allocated} spent={trace.spent_total} currency={currency} />
      </section>

      <section>
        <h2>Step by step</h2>
        <ol className="timeline">
          <li className="step">
            <div className="step-head"><h3>Donation received</h3><StatusBadge status={meta?.status || "COMMITTED"} /></div>
            <p className="step-amount">{money(d.amount, currency)}</p>
            <p className="muted">From {meta?.donor_id ? `donor ${shortId(meta.donor_id)}` : "an anonymous donor"}</p>
            <Proof id={id} meta={meta} />
          </li>

          {trace.allocations.map((a) => (
            <li className="step" key={a.id}>
              <div className="step-head"><h3>Allocated to {title(a.project)}</h3><StatusBadge status={byId[a.id]?.status || "COMMITTED"} /></div>
              <p className="step-amount">{money(a.amount, currency)}</p>
              <p className="muted">{money(a.spent, currency)} spent, {money(a.remaining, currency)} remaining</p>
              <Proof id={a.id} meta={byId[a.id]} />

              {a.expenses.length > 0 && (
                <ul className="expenses">
                  {a.expenses.map((x) => (
                    <li key={x.id}>
                      <div className="step-head">
                        <strong>{money(x.amount, currency)} spent{byId[x.id]?.recipient_ref ? ` on ${byId[x.id].recipient_ref}` : ""}</strong>
                        <StatusBadge status={byId[x.id]?.status || "COMMITTED"} />
                      </div>
                      <p className="proof">
                        {byId[x.id]?.block_index != null && <span>Recorded in block {byId[x.id].block_index}</span>}
                        <HashChip value={x.receipt_hash} label="Receipt" />
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}

          {trace.unallocated > 0 && (
            <li className="step step-open">
              <h3>Not yet allocated</h3>
              <p className="step-amount">{money(trace.unallocated, currency)}</p>
              <p className="muted">The charity has not assigned this part of the donation to a campaign yet.</p>
            </li>
          )}
        </ol>
      </section>
      <p className="muted small">This view is read from the blockchain, not from the charity's database.</p>
    </>
  );
}

export default function Trace() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [text, setText] = useState(id || "");
  const submit = (e) => {
    e.preventDefault();
    if (text.trim()) navigate(`/trace/${encodeURIComponent(text.trim())}`);
  };
  return (
    <>
      <h1 className="page-title">Trace a donation</h1>
      <form className="search" onSubmit={submit}>
        <label htmlFor="trace-id" className="sr-only">Donation ID</label>
        <input id="trace-id" value={text} onChange={(e) => setText(e.target.value)} placeholder="Donation ID" autoComplete="off" />
        <button className="btn btn-primary" type="submit">Trace donation</button>
      </form>
      {id ? <TraceView key={id} id={id} /> : <p className="empty">Enter a donation ID to see its allocations and expenses. You can find IDs on a <Link to="/">campaign page</Link>.</p>}
    </>
  );
}
