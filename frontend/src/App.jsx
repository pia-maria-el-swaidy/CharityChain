import { Routes, Route } from "react-router-dom";
import { NodesProvider } from "./hooks.jsx";
import { AuthProvider } from "./auth.jsx";
import Layout from "./components/Layout.jsx";
import RequireAuth from "./components/RequireAuth.jsx";
import Campaigns from "./pages/Campaigns.jsx";
import CampaignDetail from "./pages/CampaignDetail.jsx";
import Donate from "./pages/Donate.jsx";
import Trace from "./pages/Trace.jsx";
import CharityDesk from "./pages/CharityDesk.jsx";
import Ledger from "./pages/Ledger.jsx";
import Network from "./pages/Network.jsx";
import Admin from "./pages/Admin.jsx";
import Login from "./pages/Login.jsx";
import Register from "./pages/Register.jsx";

const guard = (roles, page) => <RequireAuth roles={roles}>{page}</RequireAuth>;

export default function App() {
  return (
    <AuthProvider>
      <NodesProvider>
        <Routes>
          {/* sign in and sign up have their own full-page layout */}
          <Route path="login" element={<Login />} />
          <Route path="register" element={<Register />} />

          <Route element={<Layout />}>
            {/* public: transparency is the point, anyone can read */}
            <Route index element={<Campaigns />} />
            <Route path="campaigns/:id" element={<CampaignDetail />} />
            <Route path="trace" element={<Trace />} />
            <Route path="trace/:id" element={<Trace />} />
            <Route path="ledger" element={<Ledger />} />

            {/* signed in, specific roles */}
            <Route path="donate" element={guard(["DONOR"], <Donate />)} />
            <Route path="charity" element={guard(["CHARITY_ADMIN", "FINANCE_OFFICER"], <CharityDesk />)} />
            <Route path="network" element={guard(["AUDITOR", "SYSTEM_ADMIN"], <Network />)} />
            <Route path="admin" element={guard(["SYSTEM_ADMIN"], <Admin />)} />

            <Route path="*" element={<p className="empty">This page does not exist.</p>} />
          </Route>
        </Routes>
      </NodesProvider>
    </AuthProvider>
  );
}
