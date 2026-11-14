import { createContext, useContext, useEffect, useRef, useState, useCallback } from "react";
import { api, NODES } from "./api";

// Runs `fn` now and then every `ms` milliseconds. Stops when `until(data)` is true.
export function usePoll(fn, { ms = 0, until, deps = [] } = {}) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  const [tick, setTick] = useState(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const untilRef = useRef(until);
  untilRef.current = until;

  useEffect(() => {
    let stopped = false;
    let timer;
    const run = async () => {
      let done = false;
      try {
        const data = await fnRef.current();
        if (stopped) return;
        setState({ data, error: null, loading: false });
        done = untilRef.current ? untilRef.current(data) : false;
      } catch (error) {
        if (stopped) return;
        setState((s) => ({ data: s.data, error, loading: false }));
      }
      if (!stopped && ms && !done) timer = setTimeout(run, ms);
    };
    setState((s) => ({ ...s, loading: true }));
    run();
    return () => { stopped = true; clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, tick, ...deps]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...state, reload };
}

// ----- live status of the 5 blockchain nodes, shared by the header and the Network page -----
const NodesContext = createContext({ nodes: [], reload: () => {} });
export const useNodes = () => useContext(NodesContext);

export function NodesProvider({ children }) {
  const [nodes, setNodes] = useState(NODES.map((n) => ({ ...n, online: null, status: null })));

  const reload = useCallback(async () => {
    const next = await Promise.all(
      NODES.map(async (n) => {
        try {
          return { ...n, online: true, status: await api.node(n.n, "/status") };
        } catch {
          return { ...n, online: false, status: null };
        }
      })
    );
    setNodes(next);
  }, []);

  useEffect(() => {
    reload();
    const id = setInterval(reload, 3000);
    return () => clearInterval(id);
  }, [reload]);

  return <NodesContext.Provider value={{ nodes, reload }}>{children}</NodesContext.Provider>;
}
