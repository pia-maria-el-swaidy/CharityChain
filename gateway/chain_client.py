import os

import requests
from gateway import db, keys

# Michele's BFT nodes. Change ports if hers are different.
# In PowerShell: $env:CHAIN_NODES = "http://localhost:5001,http://localhost:5002,http://localhost:5003,http://localhost:5004"
NODES = [u.strip() for u in os.environ.get(
    "CHAIN_NODES",
    "http://localhost:5001,http://localhost:5002,http://localhost:5003,http://localhost:5004"
).split(",") if u.strip()]

TIMEOUT = 5

class ChainUnavailable(Exception):
    pass

class ChainRejected(Exception):
    def __init__(self, body):
        self.body = body
        super().__init__(str(body))

FORBIDDEN_CHARS = (":", ",", "|")      # the chain refuses these inside id/sender/parent/memo


def _reject(message):
    raise ChainRejected({"error": message})


def to_chain_tx(tx: dict) -> dict:
    """Gateway transaction -> a real signed transaction in the chain's exact format.
    Custodial wallets: DONATION is signed by the donor's key, ALLOCATION and EXPENSE by the charity's key.
      DONATION    parent = the charity's public key           memo = campaign id
      ALLOCATION  parent = tx id of the DONATION it spends    memo = campaign id ("project" in trace)
      EXPENSE     parent = tx id of the ALLOCATION it spends  memo = receipt hash
    (the gateway calls an EXPENSE a DISBURSEMENT; recipient_ref stays off-chain)."""
    tx_type = tx["tx_type"]
    if tx_type == "DONATION":
        sender, private = db.ensure_wallet("donors", tx["donor_id"])
        parent, _ = db.ensure_wallet("charities", tx["charity_id"])
        chain_type, memo = "DONATION", str(tx.get("campaign_id") or "")
    elif tx_type == "ALLOCATION":
        sender, private = db.ensure_wallet("charities", tx["charity_id"])
        parent = tx.get("parent_tx_id")
        if not parent:
            _reject("an ALLOCATION needs parent_tx_id (the DONATION it spends)")
        chain_type, memo = "ALLOCATION", str(tx.get("campaign_id") or "")
    elif tx_type == "DISBURSEMENT":
        sender, private = db.ensure_wallet("charities", tx["charity_id"])
        parent = tx.get("parent_tx_id")
        if not parent:
            _reject("a DISBURSEMENT needs parent_tx_id (the ALLOCATION it spends)")
        memo = tx.get("receipt_hash")
        if not memo:
            _reject("a DISBURSEMENT needs receipt_hash")
        chain_type = "EXPENSE"
    else:
        _reject(f"unknown tx_type {tx_type}")

    for name, value in (("parent_tx_id", parent), ("memo", memo)):
        if any(c in str(value) for c in FORBIDDEN_CHARS):
            _reject(f"{name} must not contain ':' ',' or '|'")

    chain_tx = {"id": tx["tx_id"], "type": chain_type, "sender": sender, "parent": str(parent),
                "amount": int(tx["amount"]), "memo": memo}
    chain_tx["signature"] = keys.sign(private, keys.tx_signing_string(chain_tx))   # real ed25519
    return chain_tx

def submit_transaction(chain_tx: dict):
    """POST to first node that answers. 4xx = rejected, all down = unavailable."""
    last_error = None
    for base in NODES:
        try:
            r = requests.post(f"{base}/transactions/new", json=chain_tx, timeout=TIMEOUT)
        except requests.RequestException as e:
            last_error = e
            continue
        if r.status_code in (200, 201):
            return r.json() if r.content else {}
        if 400 <= r.status_code < 500:
            raise ChainRejected(r.text)
        last_error = r.text
    raise ChainUnavailable(f"no blockchain node reachable: {last_error}")

def get_chain(from_height: int = 0):
    """GET /chain?from=N from first node that answers."""
    last_error = None
    for base in NODES:
        try:
            r = requests.get(f"{base}/chain", params={"from": from_height}, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            # Accept either {"chain": [...]} or [...] directly
            if isinstance(data, dict):
                return data.get("chain", data.get("blocks", []))
            return data
        except requests.RequestException as e:
            last_error = e
            continue
    raise ChainUnavailable(f"no blockchain node reachable: {last_error}")

def get_trace(donation_id: str):
    """GET /trace/<id> from first node that answers. Returns (status_code, body)."""
    last_error = None
    for base in NODES:
        try:
            r = requests.get(f"{base}/trace/{donation_id}", timeout=TIMEOUT)
            return r.status_code, (r.json() if r.content else {})
        except requests.RequestException as e:
            last_error = e
            continue
    raise ChainUnavailable(f"no blockchain node reachable: {last_error}")