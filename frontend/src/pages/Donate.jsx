import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth.jsx";
import { usePoll } from "../hooks.jsx";
import { money, toCents } from "../format";
import { ErrorNote, Loading } from "../components/Notice.jsx";

// Only reachable by signed-in donors (see App.jsx). The donation is made under the donor's own account.
export default function Donate() {
  const navigate = useNavigate();
  const { user } = useAuth();
  const [params] = useSearchParams();
  const { data, error: loadError } = usePoll(() => api.campaigns(), { deps: [] });
  const [campaignId, setCampaignId] = useState(params.get("campaign") || "");
  const [amount, setAmount] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  if (loadError && !data) return <ErrorNote error={loadError} />;
  if (!data) return <Loading />;
  const active = data.filter((c) => c.status === "ACTIVE");
  const campaign = active.find((c) => c.campaign_id === (campaignId || active[0]?.campaign_id));

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    const cents = toCents(amount);
    if (!campaign) return setError(new Error("Choose a campaign."));
    if (!(cents > 0)) return setError(new Error("Enter an amount greater than zero."));
    if (!user.donor_id) return setError(new Error("This account has no donor profile."));
    setBusy(true);
    try {
      const tx = await api.createTx({
        tx_type: "DONATION",
        amount: cents,
        currency: campaign.currency,
        donor_id: user.donor_id,
        charity_id: campaign.charity_id,
        campaign_id: campaign.campaign_id,
      });
      navigate(`/trace/${tx.tx_id}`);
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  };

  return (
    <>
      <h1 className="page-title">Make a donation</h1>
      <p className="muted lead">
        You are donating as {user.full_name}. The donation is signed and written to the ledger, and you get a page that follows it.
      </p>
      {active.length === 0 ? (
        <p className="empty">There are no active campaigns to donate to.</p>
      ) : (
        <form className="form" onSubmit={submit}>
          <label>Campaign
            <select value={campaign?.campaign_id || ""} onChange={(e) => setCampaignId(e.target.value)}>
              {active.map((c) => <option key={c.campaign_id} value={c.campaign_id}>{c.title}</option>)}
            </select>
          </label>
          <label>Amount ({campaign?.currency})
            <input inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="50.00" />
          </label>
          <ErrorNote error={error} />
          <button className="btn btn-primary" disabled={busy}>
            {busy ? "Submitting" : amount && toCents(amount) > 0 ? `Donate ${money(toCents(amount), campaign?.currency)}` : "Donate"}
          </button>
        </form>
      )}
    </>
  );
}
