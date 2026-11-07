"""TraceChain - Part 2: business rules (R1-R4), replay and traceability."""
import core
import wallet


def new_state() -> dict:
    return {"ids": set(), "donations": {}, "allocations": {}, "expenses": []}


def check_signature(tx: dict) -> str | None:
    """Exercise 4: the sender is a public key and the signature proves the sender wrote this transaction."""
    if not wallet.is_public_key(tx["sender"]):
        return "sender must be a public key (64 hex characters)"
    if tx["type"] == "DONATION" and not wallet.is_public_key(tx["parent"]):
        return "a donation must name the receiving charity by its public key"
    if "signature" not in tx:
        return "missing signature"
    if not wallet.verify(tx["sender"], core.tx_signing_string(tx), tx["signature"]):
        return "invalid signature"
    return None


def apply(tx: dict, state: dict, require_signatures: bool = False) -> str | None:
    """Check tx against the rules. If it is accepted, update state and return None.
    If a rule is broken, return an error message and leave state untouched."""
    # Format first (fields, types, forbidden characters)
    error = core.check_transaction_shape(tx)
    if error:
        return error
    # Exercise 4: who wrote it?
    if require_signatures:
        error = check_signature(tx)
        if error:
            return error

    # R1: unique id
    if tx["id"] in state["ids"]:
        return f"duplicate id '{tx['id']}'"
    # R2: positive amount
    if tx["amount"] <= 0:
        return "amount must be positive"

    if tx["type"] == "DONATION":
        state["donations"][tx["id"]] = {
            "donor": tx["sender"], "charity": tx["parent"],
            "amount": tx["amount"], "allocated": 0}

    elif tx["type"] == "ALLOCATION":                       # R3
        donation = state["donations"].get(tx["parent"])
        if donation is None:
            return f"unknown parent donation '{tx['parent']}'"
        if tx["sender"] != donation["charity"]:
            return (f"wrong sender: '{tx['sender']}' is not the charity "
                    f"'{donation['charity']}' that received the donation")
        total = donation["allocated"] + tx["amount"]
        if total > donation["amount"]:
            return (f"over-allocated ({donation['allocated']} + {tx['amount']} "
                    f"> {donation['amount']})")
        donation["allocated"] = total
        state["allocations"][tx["id"]] = {
            "donation": tx["parent"], "project": tx["memo"],
            "amount": tx["amount"], "spent": 0}

    elif tx["type"] == "EXPENSE":                          # R4
        allocation = state["allocations"].get(tx["parent"])
        if allocation is None:
            return f"unknown parent allocation '{tx['parent']}'"
        charity = state["donations"][allocation["donation"]]["charity"]
        if tx["sender"] != charity:
            return (f"wrong sender: '{tx['sender']}' is not the charity "
                    f"'{charity}' that owns this allocation")
        total = allocation["spent"] + tx["amount"]
        if total > allocation["amount"]:
            return (f"over-spent ({allocation['spent']} + {tx['amount']} "
                    f"> {allocation['amount']})")
        allocation["spent"] = total
        state["expenses"].append({"id": tx["id"], "allocation": tx["parent"],
                                  "amount": tx["amount"], "receipt_hash": tx["memo"]})

    state["ids"].add(tx["id"])
    return None


def replay(chain: list, require_signatures: bool = False):
    """Rebuild the state from all blocks. Returns (state, None) or (None, error)."""
    state = new_state()
    for block in chain[1:]:                       # skip genesis
        for tx in block["transactions"]:
            error = apply(tx, state, require_signatures)
            if error:
                return None, f"block {block['index']}, transaction {tx.get('id')}: {error}"
    return state, None


def validate_new_transaction(chain: list, pending: list, tx: dict, require_signatures: bool = False) -> str | None:
    """Check a submitted transaction against the chain PLUS the pending transactions."""
    state, error = replay(chain, require_signatures)
    if error:
        return f"local chain is invalid: {error}"
    for p in pending:
        apply(p, state, require_signatures)       # pending ones were already accepted
    return apply(tx, state, require_signatures)


def trace(state: dict, donation_id: str):
    """Follow a donation to its allocations and expenses. None if unknown."""
    donation = state["donations"].get(donation_id)
    if donation is None:
        return None
    allocations = []
    for alloc_id, a in state["allocations"].items():
        if a["donation"] != donation_id:
            continue
        expenses = [{"id": e["id"], "amount": e["amount"], "receipt_hash": e["receipt_hash"]}
                    for e in state["expenses"] if e["allocation"] == alloc_id]
        allocations.append({"id": alloc_id, "project": a["project"], "amount": a["amount"],
                            "spent": a["spent"], "remaining": a["amount"] - a["spent"],
                            "expenses": expenses})
    return {
        "donation": {"id": donation_id, "donor": donation["donor"],
                     "charity": donation["charity"], "amount": donation["amount"]},
        "allocated": donation["allocated"],
        "unallocated": donation["amount"] - donation["allocated"],
        "spent_total": sum(a["spent"] for a in allocations),
        "allocations": allocations,
    }


def trace_chain(chain: list, donation_id: str):
    """Convenience: trace from the confirmed (mined) blocks only."""
    state, error = replay(chain)
    if error:
        return None
    return trace(state, donation_id)