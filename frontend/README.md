# CharityChain frontend

React + Vite. No UI library, plain CSS.

## Run in development
1. Start the backend: `docker compose up -d --build` (gateway on :8000, nodes on :5001-5005).
2. In this folder: `npm install` then `npm run dev`.
3. Open http://localhost:5173

Vite forwards `/api/*` to the gateway and `/node/N/*` to blockchain node N, because the nodes send no CORS headers.

## Run in Docker
Add the `frontend` service from the instructions to `docker-compose.yaml`, then `docker compose up -d --build`
and open http://localhost:3000. nginx does the same forwarding as Vite.

## Pages
- `/` campaigns, latest donations, trace search
- `/campaigns/:id` one campaign with its donations
- `/donate` make a donation
- `/trace/:id` donation -> allocations -> expenses, read from the chain
- `/charity` allocate donations and record expenses (receipt is hashed in the browser)
- `/ledger` blocks, signatures, whole-chain verification
- `/network` live node status for the fault-tolerance demo
