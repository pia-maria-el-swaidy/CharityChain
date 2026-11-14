import { useState } from "react";
import { Link, Navigate, useLocation, useNavigate } from "react-router-dom";
import { useAuth, HOME_BY_ROLE } from "../auth.jsx";
import AuthShell from "../components/AuthShell.jsx";
import PasswordField from "../components/PasswordField.jsx";
import { ErrorNote } from "../components/Notice.jsx";

const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export default function Register() {
  const { user, register } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = location.state?.from;
  const [form, setForm] = useState({ name: "", email: "", password: "", confirm: "" });
  const [fields, setFields] = useState({});
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  if (user) return <Navigate to={from || HOME_BY_ROLE[user.role] || "/"} replace />;
  const set = (key) => (value) => setForm((f) => ({ ...f, [key]: value }));

  const validate = () => {
    const problems = {};
    if (form.name.trim().length < 2) problems.name = "Enter your full name.";
    if (!EMAIL.test(form.email.trim())) problems.email = "Enter a valid email address.";
    if (form.password.length < 8) problems.password = "Use at least 8 characters.";
    if (form.confirm !== form.password) problems.confirm = "The two passwords do not match.";
    return problems;
  };

  const submit = async (e) => {
    e.preventDefault();
    setError(null);
    const problems = validate();
    setFields(problems);
    if (Object.keys(problems).length) return;
    setBusy(true);
    try {
      const created = await register(form.name.trim(), form.email.trim(), form.password);
      navigate(from || HOME_BY_ROLE[created.role] || "/", { replace: true });
    } catch (err) {
      // the server can also point at fields (for example an email that is already registered)
      const serverFields = err.body?.fields || {};
      setFields({ name: serverFields.full_name, email: serverFields.email, password: serverFields.password });
      setError(Object.keys(serverFields).length ? null : err);
      setBusy(false);
    }
  };

  return (
    <AuthShell>
      <form className="auth-form" onSubmit={submit} noValidate>
        <h2 className="auth-title">Create a donor account</h2>
        <p className="muted">Charity staff and auditors do not sign up here. A system administrator creates those accounts.</p>

        <div className="field">
          <label htmlFor="name">Full name</label>
          <input id="name" value={form.name} onChange={(e) => set("name")(e.target.value)} autoComplete="name" autoFocus
                 aria-invalid={fields.name ? "true" : undefined} aria-describedby={fields.name ? "name-error" : undefined} />
          {fields.name && <span id="name-error" className="field-error">{fields.name}</span>}
        </div>
        <div className="field">
          <label htmlFor="email">Email</label>
          <input id="email" type="email" value={form.email} onChange={(e) => set("email")(e.target.value)} autoComplete="email"
                 aria-invalid={fields.email ? "true" : undefined} aria-describedby={fields.email ? "email-error" : undefined} />
          {fields.email && <span id="email-error" className="field-error">{fields.email}</span>}
        </div>
        <PasswordField id="password" label="Password" value={form.password} onChange={set("password")}
                       autoComplete="new-password" error={fields.password} hint="At least 8 characters." />
        <PasswordField id="confirm" label="Repeat password" value={form.confirm} onChange={set("confirm")}
                       autoComplete="new-password" error={fields.confirm} />

        <ErrorNote error={error} />
        <button className="btn btn-primary btn-wide" disabled={busy}>{busy ? "Creating account" : "Create account"}</button>
        <p className="muted">Already have an account? <Link to="/login" state={location.state}>Sign in</Link></p>
      </form>
    </AuthShell>
  );
}
