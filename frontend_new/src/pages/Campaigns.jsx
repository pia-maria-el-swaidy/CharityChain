import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { usePoll } from "../hooks.jsx";
import { money, summarize, dateTime } from "../format";
import { ErrorNote, Loading } from "../components/Notice.jsx";
import StatusBadge from "../components/StatusBadge.jsx";

export async function loadOverview() {
  const [campaigns, charities, txs] = await Promise.all([api.campaigns(), api.charities(), api.transactions()]);
  return { campaigns, charities, txs };
}

export default function Campaigns() {
  const navigate = useNavigate();
  const [id, setId] = useState("");
  const { data, error, loading } = usePoll(loadOverview, { ms: 8000 });

  const go = (e) => {
    e.preventDefault();
    if (id.trim()) navigate(`/trace/${encodeURIComponent(id.trim())}`);
  };

  const recent = (data?.txs || []).filter((t) => t.tx_type === "DONATION" && t.status !== "REJECTED").slice(0, 5);

  return (
    <>
      <section className="hero">
        <h1>Follow every donation to the expense it paid for.</h1>
        <p>
          Charities record each donation, how it was allocated and every expense on a shared ledger that no single
          party can edit. Paste a donation ID to see where that money went.
        </p>
        <form className="search" onSubmit={go}>
          <label htmlFor="donation-id" className="sr-only">Donation ID</label>
          <input id="donation-id" value={id} onChange={(e) => setId(e.target.value)} placeholder="Donation ID" autoComplete="off" />
          <button className="btn btn-primary" type="submit">Trace donation</button>
        </form>
      </section>

      <ErrorNote error={error} />
      {loading && !data && <Loading text="Loading campaigns" />}

      {data && (
        <>
          <section>
            <h2>Campaigns</h2>
            {data.campaigns.length === 0 ? (
              <p className="empty">No campaigns yet. Create a charity and a campaign through the API, or run the demo seed script.</p>
            ) : (
              <ul className="rows">
                {data.campaigns.map((c) => {
                  const s = summarize(data.txs.filter((t) => t.campaign_id === c.campaign_id));
                  const charity = data.charities.find((x) => x.charity_id === c.charity_id);
                  const pct = c.goal_amount ? Math.min((s.donated / c.goal_amount) * 100, 100) : 0;
                  return (
                    <li key={c.campaign_id} className="row campaign-row">
                      <div>
                        <Link className="row-title" to={`/campaigns/${c.campaign_id}`}>{c.title}</Link>
                        <p className="muted">{charity?.name || "Unknown charity"}</p>
                      </div>
                      <div className="progress-block">
                        <div className="progress" aria-hidden="true"><span style={{ width: `${pct}%` }} /></div>
                        <p className="muted">
                          <strong>{money(s.donated, c.currency)}</strong> raised of {money(c.goal_amount, c.currency)}
                        </p>
                      </div>
                      <div className="figure">
                        <strong>{money(s.spent, c.currency)}</strong>
                        <span className="muted">spent on expenses</span>
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>

          <section>
            <h2>Latest donations</h2>
            {recent.length === 0 ? (
              <p className="empty">No donations yet. Make the first one from the Donate page.</p>
            ) : (
              <ul className="rows">
                {recent.map((t) => (
                  <li key={t.tx_id} className="row donation-row">
                    <span className="strong">{money(t.amount, t.currency)}</span>
                    <span className="muted">{dateTime(t.created_at)}</span>
                    <StatusBadge status={t.status} />
                    <Link to={`/trace/${t.tx_id}`}>See where it went</Link>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </>
  );
}
