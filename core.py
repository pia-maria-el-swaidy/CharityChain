"""TraceChain - Part 1: ledger core (data model, hashing, genesis, PoW, validation)."""
import hashlib
import time

DIFFICULTY = 4
TX_TYPES = ("DONATION", "ALLOCATION", "EXPENSE")
TX_FIELDS = ("id", "type", "sender", "parent", "amount", "memo")
FORBIDDEN_CHARS = (":", ",", "|")


def sha256_hex(text: str) -> str:
    """SHA-256 of the UTF-8 bytes of text, as 64 lowercase hex characters."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------- transactions ----------

def check_transaction_shape(tx) -> str | None:
    """Return an error message if tx is malformed, or None if it is fine.
    (Business rules such as 'no overspending' come in Part 2.)"""
    if not isinstance(tx, dict):
        return "transaction must be an object"
    keys = set(tx.keys())
    if keys != set(TX_FIELDS) and keys != set(TX_FIELDS) | {"signature"}:
        return f"fields must be exactly {list(TX_FIELDS)}, plus an optional 'signature'"
    if tx["type"] not in TX_TYPES:
        return f"type must be one of {list(TX_TYPES)}"
    if not isinstance(tx["amount"], int) or isinstance(tx["amount"], bool):
        return "amount must be an integer"
    for name in ("id", "type", "sender", "parent", "memo") + (("signature",) if "signature" in tx else ()):
        if not isinstance(tx[name], str):
            return f"{name} must be a string"
        if any(c in tx[name] for c in FORBIDDEN_CHARS):
            return f"{name} must not contain ':' ',' or '|'"
    for name in ("id", "sender", "parent"):
        if tx[name] == "":
            return f"{name} must not be empty"
    return None


def tx_signing_string(tx: dict) -> str:
    """The text that is signed: id:type:sender:parent:amount:memo."""
    return f"{tx['id']}:{tx['type']}:{tx['sender']}:{tx['parent']}:{tx['amount']}:{tx['memo']}"


def tx_to_string(tx: dict) -> str:
    """The text that goes into the block hash. A signed transaction adds ':' + signature,
    so the block hash also commits to the signature. Unsigned transactions hash as before."""
    text = tx_signing_string(tx)
    return text + ":" + tx["signature"] if "signature" in tx else text


# ---------- blocks ----------

def block_header(block: dict) -> str:
    tx_data = ",".join(tx_to_string(t) for t in block["transactions"])
    return f"{block['index']}|{block['timestamp']}|{block['previous_hash']}|{tx_data}|{block['nonce']}"


def hash_block(block: dict) -> str:
    return sha256_hex(block_header(block))


def make_genesis() -> dict:
    block = {"index": 0, "timestamp": 0, "transactions": [],
             "previous_hash": "0" * 64, "nonce": 0}
    block["hash"] = hash_block(block)
    return block


def proof_of_work(block: dict, should_stop=None):
    """Find a nonce so that the block hash starts with DIFFICULTY zeros.
    Exercise 5: if should_stop is given it is called every 1000 attempts; when it returns True
    the search is abandoned and None is returned."""
    block["nonce"] = 0
    while not hash_block(block).startswith("0" * DIFFICULTY):
        block["nonce"] += 1
        if should_stop is not None and block["nonce"] % 1000 == 0 and should_stop():
            return None
    return hash_block(block)


def valid_chain_hashes(chain: list) -> bool:
    """Part 1 validation: genesis, hash links, block hashes and Proof of Work.
    It knows nothing about the business rules (that is the hole of Attack B)."""
    if not chain or chain[0] != make_genesis():
        return False
    for i in range(1, len(chain)):
        cur, prev = chain[i], chain[i - 1]
        if cur["previous_hash"] != prev["hash"]:
            return False
        if cur["hash"] != hash_block(cur):
            return False
        if not cur["hash"].startswith("0" * DIFFICULTY):
            return False
    return True


def valid_chain(chain: list, check_rules: bool = True, require_signatures: bool = False) -> bool:
    """Exercise 2: the hash checks AND a replay of the business rules R1-R4.
    Exercise 4: with require_signatures, the replay also verifies every signature."""
    try:
        if not valid_chain_hashes(chain):
            return False
        if not check_rules:
            return True
        from rules import replay          # imported here because rules.py itself imports core
        _, error = replay(chain, require_signatures)
        return error is None
    except (KeyError, TypeError, AttributeError, IndexError):
        return False                      # malformed data received from a peer


# ---------- the blockchain object ----------

class Blockchain:
    def __init__(self, require_signatures: bool = False):
        self.chain = [make_genesis()]
        self.pending = []
        self.require_signatures = require_signatures

    def add_transaction(self, tx: dict) -> str | None:
        error = check_transaction_shape(tx)
        if error:
            return error
        from rules import validate_new_transaction   # Part 2: business rules
        error = validate_new_transaction(self.chain, self.pending, tx, self.require_signatures)
        if error:
            return error
        self.pending.append(tx)
        return None

    def mine(self) -> dict | None:
        if not self.pending:
            return None
        block = {"index": len(self.chain), "timestamp": int(time.time()),
                 "transactions": self.pending, "previous_hash": self.chain[-1]["hash"],
                 "nonce": 0}
        block["hash"] = proof_of_work(block)
        self.chain.append(block)
        self.pending = []
        return block