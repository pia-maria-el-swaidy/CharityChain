import { useState } from "react";
import { api } from "../api";
import { usePoll } from "../hooks.jsx";
import { ROLE_LABEL } from "../auth.jsx";
import { ErrorNote, Loading } from "../components/Notice.jsx";

const STAFF = ["CHARITY_ADMIN", "FINANCE_OFFICER", "AUDITOR", "SYSTEM_ADMIN"];
const NEEDS_CHARITY = ["CHARITY_ADMIN", "FINANCE_OFFICER"];

export default function Admin() {
  const { data, error, loading, reload } = usePoll(
    async () => { const [users, charities] = await Promise.all([api.users(), api.charities()]); return { users, charities }; }
  );
  const [form, setForm] = useState({ full_name: "", email: "", password: "", role: "CHARITY_ADMIN", charity_id: "" });
  const [fields, setFields] = useState({});
  const [formError, setFormError] = useState(null);
  const [done, setDone] = useState("");
  const [busy, setBusy] = useState(false);

  if (error && !data) return <ErrorNote error={error} />;
  if (loading && !data) return <Loading />;

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const charityName = (id) => data.charities.find((c) => c.charity_id === id)?.name || "";
  const needsCharity = NEEDS_CHARITY.includes(form.role);

  const submit = async (e) => {
    e.preventDefault();
    setFormError(null); setFields({}); setDone(""); setBusy(true);
    try {
      const created = await api.createUser({ ...form, charity_id: needsCharity ? form.charity_id || null : null });
      setDone(`Account created for ${created.full_name}. Give them the password you chose and ask them to keep it private.`);
      setForm({ full_name: "", email: "", password: "", role: form.role, charity_id: form.charity_id });
      reload();
    } catch (err) {
      setFields(err.body?.fields || {});
      setFormError(err.body?.fields ? null : err);
    }
    setBusy(false);
  };

  return (
    <>
      <h1 className="page-title">Platform administration</h1>
      <div className="two-col">
        <section>
          <h2>Create a staff account</h2>
          <form className="form" onSubmit={submit} noValidate>
            <label>Full name
              <input value={form.full_name} onChange={set("full_name")} autoComplete="off" />
              {fields.full_name && <span className="field-error">{fields.full_name}</span>}
            </label>
            <label>Email
              <input type="email" value={form.email} onChange={set("email")} autoComplete="off" />
              {fields.email && <span className="field-error">{fields.email}</span>}
            </label>
            <label>Temporary password
              <input type="text" value={form.password} onChange={set("password")} autoComplete="off" spellCheck="false" />
              {fields.password && <span className="field-error">{fields.password}</span>}
            </label>
            <label>Role
              <select value={form.role} onChange={set("role")}>
                {STAFF.map((r) => <option key={r} value={r}>{ROLE_LABEL[r]}</option>)}
              </select>
              {fields.role && <span className="field-error">{fields.role}</span>}
            </label>
            {needsCharity && (
              <label>Charity
                <select value={form.charity_id} onChange={set("charity_id")}>
                  <option value="">Choose a charity</option>
                  {data.charities.map((c) => <option key={c.charity_id} value={c.charity_id}>{c.name}</option>)}
                </select>
                {fields.charity_id && <span className="field-error">{fields.charity_id}</span>}
              </label>
            )}
            <ErrorNote error={formError} />
            {done && <p className="note note-ok" role="status">{done}</p>}
            <button className="btn btn-primary" disabled={busy}>{busy ? "Creating" : "Create account"}</button>
          </form>
        </section>

        <section>
          <h2>Accounts</h2>
          <table className="table">
            <thead><tr><th>Name</th><th>Role</th></tr></thead>
            <tbody>
              {data.users.map((u) => (
                <tr key={u.user_id}>
                  <td>{u.full_name}<br /><span className="muted small">{u.email}</span></td>
                  <td>{ROLE_LABEL[u.role]}{u.charity_id ? <><br /><span className="muted small">{charityName(u.charity_id)}</span></> : null}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </>
  );
}
