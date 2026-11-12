import hashlib
import json
from datetime import datetime, timezone

# ONLY these 11 fields ever go into the hash.
# status, block_index, signature, tx_hash are MUTABLE - they change after commit,
# so if you hash them, the hash changes and the chain thinks the tx was tampered.
FIELDS = ("tx_id", "tx_type", "donor_id", "charity_id", "campaign_id",
          "recipient_ref", "receipt_hash", "amount", "currency", "memo", "created_at")

def format_ts(v) -> str:
    """Always UTC, always ...T09:30:00Z format, so datetime and string inputs give same hash."""
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(v, str):
        return v.replace("+00:00", "Z")
    return str(v)

def compute_tx_hash(tx: dict) -> str:
    data = {}
    for f in FIELDS:
        v = tx.get(f)
        if f == "currency" and isinstance(v, str):
            v = v.upper()  # "usd" == "USD"
        elif f == "amount" and v is not None:
            v = int(v)  # "1000" == 1000
        elif f == "created_at" and v is not None:
            v = format_ts(v)
        elif v is not None:
            v = str(v)  # UUID -> string
        data[f] = v
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()