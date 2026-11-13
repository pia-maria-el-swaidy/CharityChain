import { Link, useParams } from "react-router-dom";
import { usePoll } from "../hooks.jsx";
import { money, summarize, dateTime, shortId } from "../format";
import { ErrorNote, Loading } from "../components/Notice.jsx";
import StatusBadge from "../components/StatusBadge.jsx";
import MoneyFlow from "../components/MoneyFlow.jsx";
import { loadOverview } from "./Campaigns.jsx";

export default function CampaignDetail() {
  const { id } = useParams();
  const { data, error, loading } = usePoll(loadOverview, { ms: 8000, deps: [id] });
  if (error && !data) return <ErrorNote error={error} />;
  if (loading && !data) return <Loading />;
  const campaign = data?.campaigns.find((c) => c.campaign_id === id);
  if (!campaign) return <p className="empty">This campaign does not exist. <Link to="/">Back to campaigns</Link></p>;

  const charity = data.charities.find((c) => c.charity_id === campaign.charity_id);
  const txs = data.txs.filter((t) => t.campaign_id === id);
  const s = summarize(txs);
  const donations = txs.filter((t) => t.tx_type === "DONATION");

  return (
    <>
      <p className="crumb"><Link to="/">Campaigns</Link></p>
      <h1 className="page-title">{campaign.title}</h1>
      <p className="muted">{charity?.name}. Goal {money(campaign.goal_amount, campaign.currency)}.</p>

      <section>
        <h2>Where the money stands</h2>
        <MoneyFlow total={s.donated} allocated={s.allocated} spent={s.spent} currency={campaign.currency} />
        <p className="actions"><Link className="btn btn-primary" to={`/donate?campaign=${id}`}>Donate to this campaign</Link></p>
      </section>

      <section>
        <h2>Donations</h2>
        {donations.length === 0 ? (
          <p className="empty">No donations yet.</p>
        ) : (
          <table className="table">
            <thead><tr><th>Date</th><th>Donor</th><th className="num">Amount</th><th>Status</th><th><span className="sr-only">Trace</span></th></tr></thead>
            <tbody>
              {donations.map((t) => (
                <tr key={t.tx_id}>
                  <td>{dateTime(t.created_at)}</td>
                  <td>{t.donor_id ? `Donor ${shortId(t.donor_id)}` : "Anonymous"}</td>
                  <td className="num">{money(t.amount, t.currency)}</td>
                  <td><StatusBadge status={t.status} /></td>
                  <td><Link to={`/trace/${t.tx_id}`}>Trace</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
