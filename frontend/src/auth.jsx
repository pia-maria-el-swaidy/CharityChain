import { createContext, useContext, useEffect, useState } from "react";
import { api, getToken, setToken, onUnauthorized } from "./api";

export const ROLE_LABEL = {
  DONOR: "Donor",
  CHARITY_ADMIN: "Charity administrator",
  FINANCE_OFFICER: "Finance officer",
  AUDITOR: "Auditor",
  SYSTEM_ADMIN: "System administrator",
};

// where each role lands after signing in
export const HOME_BY_ROLE = {
  DONOR: "/",
  CHARITY_ADMIN: "/charity",
  FINANCE_OFFICER: "/charity",
  AUDITOR: "/ledger",
  SYSTEM_ADMIN: "/admin",
};

const AuthContext = createContext(null);
export const useAuth = () => useContext(AuthContext);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [ready, setReady] = useState(!getToken()); // with a saved token we first ask the server who it belongs to

  useEffect(() => {
    if (!getToken()) return;
    api.me()
      .then(setUser)
      .catch(() => setToken(null))
      .finally(() => setReady(true));
  }, []);

  useEffect(() => onUnauthorized(() => setUser(null)), []);

  const accept = (result) => {
    setToken(result.token);
    setUser(result.user);
    return result.user;
  };

  const value = {
    user,
    ready,
    login: async (email, password) => accept(await api.login({ email, password })),
    register: async (fullName, email, password) => accept(await api.register({ full_name: fullName, email, password })),
    logout: () => { setToken(null); setUser(null); },
  };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
