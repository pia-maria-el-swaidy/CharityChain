import { Navigate, useLocation } from "react-router-dom";
import { useAuth, ROLE_LABEL } from "../auth.jsx";
import { Loading } from "./Notice.jsx";

// <RequireAuth roles={["DONOR"]}>...</RequireAuth>   no roles = any signed-in user
export default function RequireAuth({ roles, children }) {
  const { user, ready } = useAuth();
  const location = useLocation();
  if (!ready) return <Loading text="Checking your session" />;
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  if (roles && !roles.includes(user.role)) {
    return <p className="note note-bad">Your account ({ROLE_LABEL[user.role]}) cannot open this page.</p>;
  }
  return children;
}
