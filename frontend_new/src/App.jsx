import { Routes, Route } from "react-router-dom";
import { NodesProvider } from "./hooks.jsx";
import Layout from "./components/Layout.jsx";
import Campaigns from "./pages/Campaigns.jsx";
import CampaignDetail from "./pages/CampaignDetail.jsx";
import Donate from "./pages/Donate.jsx";
import Trace from "./pages/Trace.jsx";
import CharityDesk from "./pages/CharityDesk.jsx";
import Ledger from "./pages/Ledger.jsx";
import Network from "./pages/Network.jsx";

export default function App() {
  return (
    <NodesProvider>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Campaigns />} />
          <Route path="campaigns/:id" element={<CampaignDetail />} />
          <Route path="donate" element={<Donate />} />
          <Route path="trace" element={<Trace />} />
          <Route path="trace/:id" element={<Trace />} />
          <Route path="charity" element={<CharityDesk />} />
          <Route path="ledger" element={<Ledger />} />
          <Route path="network" element={<Network />} />
          <Route path="*" element={<p className="empty">This page does not exist.</p>} />
        </Route>
      </Routes>
    </NodesProvider>
  );
}
