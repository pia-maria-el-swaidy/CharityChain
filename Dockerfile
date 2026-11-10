# TraceChain node. Build:  docker build -t tracechain-node .
FROM python:3.12-slim

WORKDIR /app

# dependencies first, so that Docker can cache this layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# the node code: core (transactions, hashing), rules (R1-R4, replay, trace), wallet (signatures),
# bft + node_bft (BFT network, the default), poa + node_poa (Proof of Authority), node (old Proof of Work node)
COPY core.py rules.py wallet.py bft.py node_bft.py poa.py node_poa.py node.py ./

# settings are read from environment variables (see docker-compose.yml)
ENV HOST=0.0.0.0 \
    PORT=5000 \
    PYTHONUNBUFFERED=1
EXPOSE 5000

CMD ["python", "node_bft.py"]