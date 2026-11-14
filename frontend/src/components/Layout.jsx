import { Link, NavLink, Outlet } from "react-router-dom";
import { useNodes } from "../hooks.jsx";
import { useAuth, ROLE_LABEL } from "../auth.jsx";

// roles: who sees the link once signed in.  guest: also shown to visitors who are not signed in.
const LINKS = [
  { to: "/", label: "Campaigns", end: true },
  { to: "/donate", label: "Donate", roles: ["DONOR"], guest: true },
  { to: "/trace", label: "Trace a donation" },
  { to: "/charity", label: "Charity desk", roles: ["CHARITY_ADMIN", "FINANCE_OFFICER"] },
  { to: "/ledger", label: "Ledger" },
  { to: "/network", label: "Network", roles: ["AUDITOR", "SYSTEM_ADMIN"] },
  { to: "/admin", label: "Administration", roles: ["SYSTEM_ADMIN"] },
];

const visible = (link, user) => !link.roles || (user ? link.roles.includes(user.role) : Boolean(link.guest));

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

function Account() {
  const { user, ready, logout } = useAuth();
  if (!ready) return null;
  if (!user) {
    return (
      <div className="account">
        <Link to="/login" className="account-link">Sign in</Link>
        <Link to="/register" className="btn btn-small btn-light">Create account</Link>
      </div>
    );
  }
  return (
    <div className="account">
      <span className="account-who">
        <strong>{user.full_name}</strong>
        <span>{ROLE_LABEL[user.role]}</span>
      </span>
      <button type="button" className="btn btn-small btn-light" onClick={logout}>Sign out</button>
    </div>
  );
}

export default function Layout() {
  const { user } = useAuth();
  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <NavLink to="/" className="brand">CharityChain</NavLink>
          <nav aria-label="Main">
            {LINKS.filter((l) => visible(l, user)).map((l) => (
              <NavLink key={l.to} to={l.to} end={l.end} className={({ isActive }) => (isActive ? "active" : "")}>
                {l.label}
              </NavLink>
            ))}
          </nav>
          <NetworkPill />
          <Account />
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
