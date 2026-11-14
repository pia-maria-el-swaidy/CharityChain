import { NavLink, Outlet } from "react-router-dom";
import { useNodes } from "../hooks.jsx";

const LINKS = [
  { to: "/", label: "Campaigns", end: true },
  { to: "/donate", label: "Donate" },
  { to: "/trace", label: "Trace a donation" },
  { to: "/charity", label: "Charity desk" },
  { to: "/ledger", label: "Ledger" },
  { to: "/network", label: "Network" },
];

function NetworkPill() {
  const { nodes } = useNodes();
  const validators = nodes.filter((n) => n.n <= 4);
  if (validators.every((n) => n.online === null)) return <span className="pill pill-wait">Checking network</span>;
  const up = validators.filter((n) => n.online).length;
  const quorum = 3; // 3 of 4 validators must sign a block
  const cls = up === 4 ? "pill-ok" : up >= quorum ? "pill-warn" : "pill-bad";
  const text = up >= quorum ? `${up} of 4 validators online` : `${up} of 4 online, blocks paused`;
  return <span className={`pill ${cls}`}>{text}</span>;
}

export default function Layout() {
  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <NavLink to="/" className="brand">CharityChain</NavLink>
          <nav aria-label="Main">
            {LINKS.map((l) => (
              <NavLink key={l.to} to={l.to} end={l.end} className={({ isActive }) => (isActive ? "active" : "")}>
                {l.label}
              </NavLink>
            ))}
          </nav>
          <NetworkPill />
        </div>
      </header>
      <main className="page">
        <Outlet />
      </main>
      <footer className="footer">
        Every donation, allocation and expense is written to a blockchain run by four independent validators.
      </footer>
    </>
  );
}
