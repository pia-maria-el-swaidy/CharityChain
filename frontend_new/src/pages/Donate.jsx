import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { usePoll } from "../hooks.jsx";
import { money, toCents } from "../format";
import { ErrorNote, Loading } from "../components/Notice.jsx";

const DONORS_KEY = "cc_donors"; // email -> donor_id, so a returning donor keeps one identity in this browser

const readDonors = () => { try { return JSON.parse(localStorage.getItem(DONORS_KEY)) || {}; } catch { return {}; } };

export default function Donate() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const { data, error: loadError } = usePoll(() => api.campaigns(), { deps: [] });
  const [campaignId, setCampaignId] = useState(params.get("campaign") || "");
  const [amount, setAmount] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [anonymous, setAnonymous] = useState(false);
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
    setBusy(true);
    try {
      let donorId;
      const donors = readDonors();
      const key = email.trim().toLowerCase();
      if (!anonymous && donors[key]) {
        donorId = donors[key];
      } else {
        const created = await api.createDonor(
          anonymous ? { is_anonymous: true } : { display_name: name.trim() || null, email: key, is_anonymous: false }
        );
        donorId = created.donor_id;
        if (!anonymous) localStorage.setItem(DONORS_KEY, JSON.stringify({ ...donors, [key]: donorId }));
      }
      const tx = await api.createTx({
        tx_type: "DONATION",
        amount: cents,
        currency: campaign.currency,
        donor_id: donorId,
        charity_id: campaign.charity_id,
        campaign_id: campaign.campaign_id,
      });
      navigate(`/trace/${tx.tx_id}`);
    } catch (err) {
      setError(err.status === 409 ? new Error("This email is already registered from another browser. Use a different email or donate anonymously.") : err);
      setBusy(false);
    }
  };

  return (
    <>
      <h1 className="page-title">Make a donation</h1>
      <p className="muted lead">Your donation is signed and written to the ledger. You get an ID you can use to follow it later.</p>
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
          <label className="check">
            <input type="checkbox" checked={anonymous} onChange={(e) => setAnonymous(e.target.checked)} />
            Donate anonymously
          </label>
          {!anonymous && (
            <>
              <label>Name
                <input value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
              </label>
              <label>Email
                <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
              </label>
            </>
          )}
          <ErrorNote error={error} />
          <button className="btn btn-primary" disabled={busy}>
            {busy ? "Submitting" : amount && toCents(amount) > 0 ? `Donate ${money(toCents(amount), campaign?.currency)}` : "Donate"}
          </button>
        </form>
      )}
    </>
  );
}
