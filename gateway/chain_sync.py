import time

from gateway import chain_client
from gateway.db import get_synced_height, set_synced_height, mark_committed

def sync_once() -> int:
    """Read new blocks from the chain, mark our PENDING rows COMMITTED.
    Returns number of blocks scanned. Safe to run twice."""
    start = get_synced_height()
    blocks = chain_client.get_chain(from_height=start)
    scanned = 0
    for block in blocks:
        # Michele's block: {"index": N, "transactions": [{"id": ...}, ...]}
        idx = block.get("index", block.get("block_index", start + scanned))
        txs = block.get("transactions", block.get("txs", []))
        tx_ids = []
        for t in txs:
            if isinstance(t, dict):
                tx_ids.append(t.get("id", t.get("tx_id")))
            else:
                tx_ids.append(str(t))
        if tx_ids:
            mark_committed(tx_ids, idx)
        set_synced_height(idx + 1)
        scanned += 1
    return scanned

def run_forever(interval_seconds: int = 10):
    while True:
        try:
            sync_once()
        except Exception:
            pass  # chain down - retry next round, don't kill the gateway
        time.sleep(interval_seconds)