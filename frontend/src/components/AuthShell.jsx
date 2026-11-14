import { Link } from "react-router-dom";
import MoneyFlow from "./MoneyFlow.jsx";

// Two-column frame shared by the sign-in and sign-up pages.
export default function AuthShell({ children }) {
  return (
    <div className="auth">
      <aside className="auth-aside">
        <Link to="/" className="auth-brand">CharityChain</Link>
        <div className="auth-pitch">
          <h1>See where every donation went.</h1>
          <p>
            Donors, charities and auditors all read the same ledger. Once a donation, an allocation or an expense is
            written to it, nobody can change it afterwards.
          </p>
          <div className="auth-sample" aria-label="Example of a donation">
            <p className="muted small">Example: a $1,000 donation</p>
            <MoneyFlow total={100000} allocated={85000} spent={62000} />
          </div>
        </div>
      </aside>
      <main className="auth-main">{children}</main>
    </div>
  );
}
