import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the browser talks only to Vite (port 5173). Vite forwards:
//   /api/*      -> the gateway          (docker compose maps it to localhost:8000)
//   /node/N/*   -> blockchain node N    (localhost:5001 ... 5004, observer = 5005)
// The chain nodes do not send CORS headers, so this proxy is what lets the browser read them.
const nodePorts = { 1: 5001, 2: 5002, 3: 5003, 4: 5004, 5: 5005 };
const proxy = { "/api": { target: "http://localhost:8000", changeOrigin: true } };
for (const [n, port] of Object.entries(nodePorts)) {
  proxy[`/node/${n}`] = {
    target: `http://localhost:${port}`,
    changeOrigin: true,
    rewrite: (path) => path.replace(`/node/${n}`, ""),
  };
}

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy },
});
