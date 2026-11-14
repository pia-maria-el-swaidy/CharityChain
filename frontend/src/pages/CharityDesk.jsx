import { useState } from "react";
import { api } from "../api";
import { useAuth } from "../auth.jsx";
import { usePoll } from "../hooks.jsx";
import { money, toCents, sha256File, shortId, summarize } from "../format";
import { ErrorNote, Loading } from "../components/Notice.jsx";
import StatusBadge from "../components/StatusBadge.jsx";
import { loadOverview } from "./Campaigns.jsx";

const sumBy = (list, parent) => list.filter((t) => t.parent_tx_id === parent).reduce((s, t) => s + Number(t.amount), 0);

function AllocateForm({ charity, donations, campaigns, onDone }) {
  const open = donations.filter((d) => d.left > 0);
  const [donationId, setDonationId] = useState("");
  const [amount, setAmount] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const donation = open.find((d) => d.tx_id === (donationId || open[0]?.tx_id));

  if (open.length === 0) return <p className="empty">Every confirmed donation is fully allocated.</p>;

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    const cents = toCents(amount);
    if (!(cents > 0)) return setError(new Error("Enter an amount greater than zero."));
    if (cents > donation.left) return setError(new Error(`Only ${money(donation.left, donation.currency)} of this donation is unallocated.`));
    setBusy(true);
    try {
      await api.createTx({
        tx_type: "ALLOCATION", amount: cents, currency: donation.currency, charity_id: charity.charity_id,
        campaign_id: donation.campaign_id, parent_tx_id: donation.tx_id,
      });
      setAmount("");
      onDone("Allocation recorded. It will show as confirmed in a few seconds.");
    } catch (err) { setError(err); }
    setBusy(false);
  };

  return (
    <form className="form" onSubmit={submit}>
      <label>Donation
        <select value={donation.tx_id} onChange={(e) => setDonationId(e.target.value)}>
          {open.map((d) => (
            <option key={d.tx_id} value={d.tx_id}>
              {money(d.amount, d.currency)} for {campaigns.find((c) => c.campaign_id === d.campaign_id)?.title || "a campaign"} ({money(d.left, d.currency)} left, {shortId(d.tx_id)})
            </option>
          ))}
        </select>
      </label>
      <label>Amount to allocate ({donation.currency})
        <input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="25.00" />
      </label>
      <ErrorNote error={error} />
      <button className="btn btn-primary" disabled={busy}>{busy ? "Recording" : "Record allocation"}</button>
    </form>
  );
}

function ExpenseForm({ charity, allocations, campaigns, onDone }) {
  const open = allocations.filter((a) => a.left > 0);
  const [allocId, setAllocId] = useState("");
  const [amount, setAmount] = useState("");
  const [recipient, setRecipient] = useState("");
  const [hash, setHash] = useState("");
  const [fileName, setFileName] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const alloc = open.find((a) => a.tx_id === (allocId || open[0]?.tx_id));

  if (open.length === 0) return <p className="empty">No confirmed allocation has money left to spend.</p>;

  const pickFile = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setError(null);
    try { setHash(await sha256File(file)); setFileName(file.name); } catch (err) { setError(err); }
  };

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    const cents = toCents(amount);
    if (!(cents > 0)) return setError(new Error("Enter an amount greater than zero."));
    if (cents > alloc.left) return setError(new Error(`Only ${money(alloc.left, alloc.currency)} is left in this allocation.`));
    if (!recipient.trim()) return setError(new Error("Say who was paid."));
    if (!/^[0-9a-f]{64}$/.test(hash.trim().toLowerCase())) return setError(new Error("Attach the receipt file, or paste its SHA-256 hash (64 hex characters)."));
    setBusy(true);
    try {
      await api.createTx({
        tx_type: "DISBURSEMENT", amount: cents, currency: alloc.currency, charity_id: charity.charity_id,
        campaign_id: alloc.campaign_id, parent_tx_id: alloc.tx_id, recipient_ref: recipient.trim(),
        receipt_hash: hash.trim().toLowerCase(),
      });
      setAmount(""); setRecipient(""); setHash(""); setFileName("");
      onDone("Expense recorded. It will show as confirmed in a few seconds.");
    } catch (err) { setError(err); }
    setBusy(false);
  };

  return (
    <form className="form" onSubmit={submit}>
      <label>Allocation to spend from
        <select value={alloc.tx_id} onChange={(e) => setAllocId(e.target.value)}>
          {open.map((a) => (
            <option key={a.tx_id} value={a.tx_id}>
              {money(a.amount, a.currency)} for {campaigns.find((c) => c.campaign_id === a.campaign_id)?.title || "a campaign"} ({money(a.left, a.currency)} left, {shortId(a.tx_id)})
            </option>
          ))}
        </select>
      </label>
      <label>Amount spent ({alloc.currency})
        <input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="12.50" />
      </label>
      <label>Paid to
        <input value={recipient} onChange={(e) => setRecipient(e.target.value)} placeholder="Supplier or beneficiary name" />
      </label>
      <label>Receipt file
        <input type="file" onChange={pickFile} />
        <span className="hint">{fileName ? `${fileName} was fingerprinted in your browser. The file itself is not uploaded.` : "The file stays on your computer. Only its SHA-256 fingerprint goes on the ledger."}</span>
      </label>
      <label>Receipt fingerprint
        <input value={hash} onChange={(e) => setHash(e.target.value)} placeholder="Filled in when you attach a file" spellCheck="false" />
      </label>
      <ErrorNote error={error} />
      <button className="btn btn-primary" disabled={busy}>{busy ? "Recording" : "Record expense"}</button>
    </form>
  );
}

export default function CharityDesk() {
  const { user } = useAuth();
  const [selected, setSelected] = useState(localStorage.getItem("cc_charity") || "");
  const [message, setMessage] = useState("");
  const { data, error, loading, reload } = usePoll(loadOverview, { ms: 6000 });

  if (error && !data) return <ErrorNote error={error} />;
  if (loading && !data) return <Loading />;
  if (data.charities.length === 0) return <p className="empty">No charities exist yet. Create one through the API or the demo seed script.</p>;

  // staff are tied to one charity; the picker only appears for an account without one
  const fixedCharity = Boolean(user.charity_id);
  const charity = data.charities.find((c) => c.charity_id === (user.charity_id || selected)) || data.charities[0];
  const canAllocate = user.role === "CHARITY_ADMIN";
  const canSpend = user.role === "FINANCE_OFFICER";
  const mine = data.txs.filter((t) => t.charity_id === charity.charity_id && t.status !== "REJECTED");
  const allocsAll = mine.filter((t) => t.tx_type === "ALLOCATION");
  const spendAll = mine.filter((t) => t.tx_type === "DISBURSEMENT");
  const donations = mine
    .filter((t) => t.tx_type === "DONATION" && t.status === "COMMITTED")
    .map((d) => ({ ...d, left: Number(d.amount) - sumBy(allocsAll, d.tx_id) }));
  const allocations = allocsAll
    .filter((t) => t.status === "COMMITTED")
    .map((a) => ({ ...a, left: Number(a.amount) - sumBy(spendAll, a.tx_id) }));
  const totals = summarize(mine);

  const done = (text) => { setMessage(text); reload(); };

  return (
    <>
      <h1 className="page-title">Charity desk</h1>
      {fixedCharity ? <p className="muted">{charity.name}</p> : <label className="inline-select">Working as
        <select value={charity.charity_id} onChange={(e) => { setSelected(e.target.value); localStorage.setItem("cc_charity", e.target.value); setMessage(""); }}>
          {data.charities.map((c) => <option key={c.charity_id} value={c.charity_id}>{c.name}</option>)}
        </select>
      </label>}

      <dl className="stats">
        <div><dt>Received</dt><dd>{money(totals.donated)}</dd></div>
        <div><dt>Allocated</dt><dd>{money(totals.allocated)}</dd></div>
        <div><dt>Spent</dt><dd>{money(totals.spent)}</dd></div>
      </dl>

      {message && <p className="note note-ok" role="status">{message}</p>}

      <div>
        {canAllocate && (
          <section>
            <h2>Allocate a donation</h2>
            <AllocateForm key={`a-${charity.charity_id}`} charity={charity} donations={donations} campaigns={data.campaigns} onDone={done} />
          </section>
        )}
        {canSpend && (
          <section>
            <h2>Record an expense</h2>
            <ExpenseForm key={`e-${charity.charity_id}`} charity={charity} allocations={allocations} campaigns={data.campaigns} onDone={done} />
          </section>
        )}
      </div>

      <section>
        <h2>Recent activity</h2>
        {mine.length === 0 ? <p className="empty">Nothing recorded for this charity yet.</p> : (
          <table className="table">
            <thead><tr><th>Type</th><th className="num">Amount</th><th>Status</th><th>ID</th></tr></thead>
            <tbody>
              {mine.slice(0, 12).map((t) => (
                <tr key={t.tx_id}>
                  <td>{{ DONATION: "Donation", ALLOCATION: "Allocation", DISBURSEMENT: "Expense" }[t.tx_type]}</td>
                  <td className="num">{money(t.amount, t.currency)}</td>
                  <td><StatusBadge status={t.status} /></td>
                  <td><code>{shortId(t.tx_id)}</code></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
