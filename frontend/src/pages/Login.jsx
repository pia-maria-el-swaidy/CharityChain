import { useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { useAuth, HOME_BY_ROLE } from "../auth.jsx";
import AuthShell from "../components/AuthShell.jsx";
import PasswordField from "../components/PasswordField.jsx";
import { ErrorNote } from "../components/Notice.jsx";

export default function Login() {
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = location.state?.from;
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  if (user) return <Navigate to={from || HOME_BY_ROLE[user.role] || "/"} replace />;

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    if (!email.trim() || !password) return setError(new Error("Enter your email and password."));
    setBusy(true);
    try {
      const signedIn = await login(email.trim(), password);
      navigate(from || HOME_BY_ROLE[signedIn.role] || "/", { replace: true });
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  };

  return (
    <AuthShell>
      <form className="auth-form" onSubmit={submit} noValidate>
        <h2 className="auth-title">Sign in</h2>
        {from && <p className="muted">Sign in to continue.</p>}
        <div className="field">
          <label htmlFor="email">Email</label>
          <input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" autoFocus />
        </div>
        <PasswordField id="password" label="Password" value={password} onChange={setPassword} autoComplete="current-password" />
        <ErrorNote error={error} />
        <button className="btn btn-primary btn-wide" disabled={busy}>{busy ? "Signing in" : "Sign in"}</button>
        <p className="muted">New here? <Link to="/register" state={location.state}>Create a donor account</Link></p>
      </form>
    </AuthShell>
  );
}
