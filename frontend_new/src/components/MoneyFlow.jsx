import { money } from "../format";

// One bar for one donation: how much was spent, how much is allocated but unspent, how much is not yet allocated.
export default function MoneyFlow({ total, allocated, spent, currency = "USD" }) {
  const t = Math.max(Number(total) || 0, 1);
  const spentPct = Math.min((spent / t) * 100, 100);
  const heldPct = Math.min(Math.max(((allocated - spent) / t) * 100, 0), 100 - spentPct);
  const idle = Math.max(total - allocated, 0);
  const held = Math.max(allocated - spent, 0);
  return (
    <div className="flow">
      <div className="flow-bar" role="img"
           aria-label={`${money(spent, currency)} spent, ${money(held, currency)} allocated, ${money(idle, currency)} not yet allocated`}>
        <span className="flow-spent" style={{ width: `${spentPct}%` }} />
        <span className="flow-held" style={{ width: `${heldPct}%` }} />
      </div>
      <dl className="flow-legend">
        <div><dt><i className="sw sw-spent" />Spent on expenses</dt><dd>{money(spent, currency)}</dd></div>
        <div><dt><i className="sw sw-held" />Allocated, not yet spent</dt><dd>{money(held, currency)}</dd></div>
        <div><dt><i className="sw sw-idle" />Not yet allocated</dt><dd>{money(idle, currency)}</dd></div>
      </dl>
    </div>
  );
}
