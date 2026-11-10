"""TraceChain - Stage 1 consensus: Proof of Authority.

A fixed list of validators (their public keys) takes turns, in order, to write blocks:

    block 1 -> validator 0,  block 2 -> validator 1,  block 3 -> validator 2, ...  (then it starts again)

There is no Proof of Work and no nonce. A block carries the public key of its proposer and the proposer's
signature of the block hash. A block is valid only if
    - the proposer is exactly the validator whose turn it is,
    - the hash matches the content, and the signature matches the proposer,
    - it links to the previous block,
    - every transaction in it respects the business rules R1-R4 and is signed (rules.py).

The validator list is written into the genesis block, so a node that was configured with a different
validator list has a different genesis block and rejects the chain (compare Q1 of the lab).

Known limits of this stage (they are exactly what the BFT stage fixes):
    - if the validator whose turn it is is down, nobody else may write the block: the chain waits;
    - a validator that signs two different blocks for the same height (double signing) splits the network.
      The nodes can detect it and keep the evidence, but they cannot decide between the two blocks.
"""
import core
import rules
import wallet

MAX_TXS_PER_BLOCK = 500
BLOCK_FIELDS = ("index", "timestamp", "transactions", "previous_hash", "proposer", "hash", "signature")


# ---------- who writes which block ----------

def proposer_index(height: int, count: int) -> int:
    """Block 1 belongs to validator 0, block 2 to validator 1, and so on, round-robin."""
    return (height - 1) % count


def proposer_for(height: int, validators: list) -> str:
    return validators[proposer_index(height, len(validators))]


def validators_error(validators: list):
    if not validators:
        return "the validator list is empty"
    for v in validators:
        if not wallet.is_public_key(v):
            return f"'{v}' is not a public key (64 hex characters)"
    if len(set(validators)) != len(validators):
        return "the validator list contains the same key twice"
    return None


# ---------- hashing, genesis, signing ----------

def block_header(block: dict) -> str:
    """index|timestamp|previous_hash|tx_data|proposer     (the genesis block has the validator list instead of a proposer)"""
    tx_data = ",".join(core.tx_to_string(t) for t in block["transactions"])
    last = ",".join(block["validators"]) if "validators" in block else block["proposer"]
    return f"{block['index']}|{block['timestamp']}|{block['previous_hash']}|{tx_data}|{last}"


def hash_block(block: dict) -> str:
    return core.sha256_hex(block_header(block))


def make_genesis(validators: list) -> dict:
    block = {"index": 0, "timestamp": 0, "transactions": [], "previous_hash": "0" * 64,
             "validators": list(validators)}
    block["hash"] = hash_block(block)
    return block


def make_block(index: int, timestamp: int, previous_hash: str, transactions: list,
               proposer_public: str, proposer_private: str) -> dict:
    """Build a block and sign its hash with the proposer's private key."""
    block = {"index": index, "timestamp": timestamp, "transactions": list(transactions),
             "previous_hash": previous_hash, "proposer": proposer_public}
    block["hash"] = hash_block(block)
    block["signature"] = wallet.sign(proposer_private, block["hash"])
    return block


# ---------- checking blocks and chains ----------

def check_signed_header(block, validators: list):
    """Is this block, taken alone, properly written and signed by the validator whose turn it is?
    (Used for the evidence of double signing, where the previous block does not matter.)"""
    if not isinstance(block, dict) or set(block.keys()) != set(BLOCK_FIELDS):
        return f"a block must have exactly the fields {list(BLOCK_FIELDS)}"
    index = block["index"]
    if not isinstance(index, int) or isinstance(index, bool) or index < 1:
        return "invalid index"
    if not isinstance(block["timestamp"], int) or isinstance(block["timestamp"], bool):
        return "timestamp must be an integer"
    if not isinstance(block["transactions"], list):
        return "transactions must be a list"
    for name in ("previous_hash", "proposer", "hash", "signature"):
        if not isinstance(block[name], str):
            return f"{name} must be a string"
    expected = proposer_for(index, validators)
    if block["proposer"] != expected:
        if block["proposer"] not in validators:
            return "the proposer is not a validator"
        return (f"wrong proposer: block {index} belongs to validator {validators.index(expected)}, "
                f"not validator {validators.index(block['proposer'])}")
    try:
        computed = hash_block(block)
    except (KeyError, TypeError, AttributeError):
        return "a transaction in the block is malformed"
    if computed != block["hash"]:
        return "the hash does not match the content of the block"
    if not wallet.verify(block["proposer"], block["hash"], block["signature"]):
        return "invalid proposer signature"
    return None


def check_block(block, previous: dict, validators: list):
    """Full check of a block that should follow `previous`. Returns an error message or None.
    (The business rules of the transactions are checked separately, by replaying them.)"""
    error = check_signed_header(block, validators)
    if error:
        return error
    if block["index"] != previous["index"] + 1:
        return f"wrong index: expected {previous['index'] + 1}"
    if block["previous_hash"] != previous["hash"]:
        return "previous_hash does not match the previous block"
    if block["timestamp"] < previous["timestamp"]:
        return "the timestamp goes backwards"
    count = len(block["transactions"])
    if count < 1 or count > MAX_TXS_PER_BLOCK:
        return f"a block must hold between 1 and {MAX_TXS_PER_BLOCK} transactions"
    return None


def chain_error(chain, validators: list, require_signatures: bool = True):
    """None if the whole chain is valid: genesis, every block, and a replay of all the rules."""
    try:
        if not isinstance(chain, list) or not chain:
            return "the chain is empty"
        if chain[0] != make_genesis(validators):
            return "the genesis block differs from ours (a different validator list?)"
        for i in range(1, len(chain)):
            error = check_block(chain[i], chain[i - 1], validators)
            if error:
                return f"block {i}: {error}"
        _, error = rules.replay(chain, require_signatures)
        return error
    except (KeyError, TypeError, AttributeError, IndexError, ValueError):
        return "the chain is malformed"