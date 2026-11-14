"""TraceChain - Stage 1 node: Proof of Authority (round-robin validators, signed blocks).

Run one validator (normally Docker Compose does this, see Dockerfile-compose.yml and make_validators.py):

    python node_poa.py --port 5001 --validators PK1,PK2,PK3,PK4 --validator-key SK1 \\
                       --peers http://127.0.0.1:5002,http://127.0.0.1:5003,http://127.0.0.1:5004

Every setting can also come from an environment variable: PORT, HOST, PEERS, SELF_URL, VALIDATORS,
VALIDATOR_KEY, BLOCK_INTERVAL, SYNC_INTERVAL, NO_SIGNATURES, LAB.
A node without VALIDATOR_KEY is an observer: it checks and stores every block but never writes one.

How a transaction travels:
    client -> any node (POST /transactions/new) -> relayed to all the other nodes -> the validator whose turn it is
    puts the pending transactions into a block, signs it, and sends it to everybody (POST /blocks/new)
    -> every node checks the block (proposer, signature, hash, rules) and appends it.
"""
import argparse
import copy
import os
import queue
import sys
import threading
import time
import types
import uuid

import requests
from flask import Flask, jsonify, request

import core
import poa
import rules
import wallet

app = Flask(__name__)
lock = threading.RLock()                  # protects the ledger
sync_lock = threading.Lock()              # one synchronization with the peers at a time
relay_queue = queue.Queue()               # transactions waiting to be forwarded to the peers, in order

MAX_FUTURE_SECONDS = 300                  # a block dated further in the future than this is refused

node_id = uuid.uuid4().hex[:8]
validators = []                           # public keys, in turn order
my_key = None                             # this node's private key (None: observer)
my_pub = None
peers = set()
own_urls = set()
require_signatures = True
lab_mode = False

S = types.SimpleNamespace(
    chain=[],                 # the blocks
    committed=None,           # rules state after all blocks
    pending=[],               # accepted transactions not yet in a block
    pending_state=None,       # committed state + pending transactions (to check new transactions quickly)
    healthy=True,             # False if the audit found our own chain invalid
    last_block_time=time.time(),
    evidence=[],              # proofs that a validator signed two different blocks for one height
    dropped=[],               # pending transactions that became invalid (with the reason)
)


def env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def get_json_body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


def idx_of(public_key):
    return validators.index(public_key) if public_key in validators else None


# ---------- ledger state ----------

def rebuild_pending(extra=()):
    """Re-check the candidate transactions (`extra` first, then the current pending ones) against the
    committed state. Those already in a block are skipped, those no longer valid are dropped."""
    state = copy.deepcopy(S.committed)
    kept, dropped = [], []
    for t in list(extra) + list(S.pending):
        tid = t.get("id") if isinstance(t, dict) else None
        if not isinstance(tid, str) or tid in state["ids"]:
            continue
        problem = rules.apply(t, state, require_signatures)
        if problem is None:
            kept.append(t)
        else:
            dropped.append({"id": tid, "reason": problem})
    S.pending, S.pending_state = kept, state
    S.dropped = (S.dropped + dropped)[-50:]


def rebuild_all(extra=()):
    """Recompute the committed state from the chain (which must be valid), then the pending list."""
    state, error = rules.replay(S.chain, require_signatures)
    if error:
        raise RuntimeError(error)
    S.committed = state
    rebuild_pending(extra)


def note_conflict(block, existing):
    """Two different blocks, both signed by the validator whose turn it was, for the same height."""
    evidence = {"height": block["index"], "validator": idx_of(block["proposer"]),
                "hashes": sorted([existing["hash"], block["hash"]])}
    if evidence not in S.evidence:
        S.evidence.append(evidence)


# ---------- receiving and writing blocks ----------

def import_block(block):
    """Check a block and append it to the chain. Returns (http status, body)."""
    with lock:
        index = block.get("index") if isinstance(block, dict) else None
        if not isinstance(index, int) or isinstance(index, bool):
            return 400, {"error": "this is not a block"}
        height = len(S.chain)

        if index < height:                                        # we already have a block at this height
            if index >= 1 and S.chain[index]["hash"] == block.get("hash"):
                return 200, {"message": "already have this block"}
            error = poa.check_signed_header(block, validators)
            if error:
                return 422, {"error": f"invalid block: {error}"}
            note_conflict(block, S.chain[index])
            return 409, {"error": "a different block already exists at this height: possible double signing "
                                  f"by validator {idx_of(block['proposer'])}", "height": index}

        if index > height:                                        # we are behind: fetch the missing blocks
            threading.Thread(target=sync_with_peers, daemon=True).start()
            return 202, {"message": f"this node is at height {height}: synchronizing with its peers"}

        error = poa.check_block(block, S.chain[-1], validators)
        if error:
            return 422, {"error": f"invalid block: {error}"}
        if block["timestamp"] > time.time() + MAX_FUTURE_SECONDS:
            return 422, {"error": "invalid block: the timestamp is too far in the future"}
        trial = copy.deepcopy(S.committed)                        # all or nothing: the state changes only if
        for t in block["transactions"]:                           # every transaction of the block is accepted
            problem = rules.apply(t, trial, require_signatures)
            if problem:
                tid = t.get("id") if isinstance(t, dict) else None
                return 422, {"error": f"invalid block: transaction {tid}: {problem}"}
        S.chain.append(block)
        S.committed = trial
        S.last_block_time = time.time()
        rebuild_pending()
        return 201, {"message": f"block {index} appended", "height": len(S.chain)}


def broadcast_block(block):
    """Send the block to every peer at the same time. Returns {peer: http status or None}."""
    results = {}

    def send(peer):
        try:
            results[peer] = requests.post(peer + "/blocks/new", json=block, timeout=5).status_code
        except requests.RequestException:
            results[peer] = None

    threads = [threading.Thread(target=send, args=(p,)) for p in sorted(peers) if p not in own_urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def produce_block():
    """Write the next block, if it is this validator's turn and transactions are pending."""
    with lock:
        if my_key is None:
            return 403, {"error": "this node is not a validator"}
        height = len(S.chain)
        expected = poa.proposer_for(height, validators)
        if expected != my_pub:
            return 409, {"error": f"not my turn: validator {idx_of(expected)} writes block {height}",
                         "next_proposer": idx_of(expected)}
        if not S.pending:
            return 409, {"error": "nothing to write: no pending transactions"}
        block = poa.make_block(height, max(int(time.time()), S.chain[-1]["timestamp"]), S.chain[-1]["hash"],
                               S.pending[:poa.MAX_TXS_PER_BLOCK], my_pub, my_key)
        status, body = import_block(block)
        if status != 201:
            return 500, {"error": f"our own block was refused: {body}"}
    deliveries = broadcast_block(block)                          # outside the lock: peers answer on their own
    return 201, {"message": f"block {block['index']} written by validator {idx_of(my_pub)}",
                 "block": block, "deliveries": deliveries}


def producer_loop(interval):
    """Write a block as soon as it is our turn, something is pending and `interval` seconds have passed."""
    while True:
        time.sleep(0.2)
        with lock:
            ready = (my_key is not None and S.pending and poa.proposer_for(len(S.chain), validators) == my_pub
                     and time.time() - S.last_block_time >= interval)
        if ready:
            produce_block()


# ---------- transactions between the nodes ----------

def relay_worker():
    """Forward every accepted transaction to the other nodes, in the order it was accepted."""
    while True:
        tx = relay_queue.get()
        for peer in sorted(peers):
            if peer in own_urls:
                continue
            try:
                requests.post(peer + "/transactions/new", json=tx, headers={"X-Relayed": "1"}, timeout=3)
            except requests.RequestException:
                pass


# ---------- synchronization with the peers ----------

def sync_with_peers(wait=False):
    """Audit our own chain, adopt a longer valid chain from a peer, and collect the peers' pending
    transactions. A node whose own chain is invalid accepts any valid chain (it heals itself)."""
    got_it = sync_lock.acquire(True, 30) if wait else sync_lock.acquire(False)
    if not got_it:
        return {"replaced": False, "length": len(S.chain), "note": "a synchronization is already running"}
    try:
        with lock:
            S.healthy = poa.chain_error(S.chain, validators, require_signatures) is None
            best_length = len(S.chain) if S.healthy else 0
            my_height, my_tip = len(S.chain), S.chain[-1]["hash"]
        best, peer_pending = None, []
        for peer in sorted(peers):
            if peer in own_urls:
                continue
            try:
                status = requests.get(peer + "/status", timeout=3).json()
                if status.get("pending", 0) > 0:
                    peer_pending += requests.get(peer + "/pending", timeout=5).json()["pending"]
                ahead = (status["height"] > my_height or not S.healthy
                         or (status["height"] == my_height and status["tip"] != my_tip))
                if not ahead:
                    continue
                peer_chain = requests.get(peer + "/chain", timeout=10).json()["chain"]
            except (requests.RequestException, ValueError, KeyError, TypeError):
                continue
            if poa.chain_error(peer_chain, validators, require_signatures) is not None:
                continue                                          # a chain that is not valid is ignored
            with lock:
                if S.healthy:                                     # same height, different block: double signing?
                    for i in range(1, min(len(peer_chain), len(S.chain))):
                        if peer_chain[i]["hash"] != S.chain[i]["hash"]:
                            note_conflict(peer_chain[i], S.chain[i])
                            break
            if len(peer_chain) > best_length:
                best, best_length = peer_chain, len(peer_chain)
        replaced, requeued = False, []
        with lock:
            if best is not None and (len(best) > len(S.chain) or not S.healthy):
                old_chain, S.chain = S.chain, best
                orphans = [t for b in old_chain[1:] if isinstance(b, dict) for t in b.get("transactions", [])]
                rebuild_all(extra=orphans)                        # valid transactions of discarded blocks come back
                orphan_ids = {id(t) for t in orphans}
                requeued = [t["id"] for t in S.pending if id(t) in orphan_ids]
                S.last_block_time = time.time()
                S.healthy, replaced = True, True
            for t in peer_pending:                                # transactions we missed
                if (isinstance(t, dict) and isinstance(t.get("id"), str) and t["id"] not in S.pending_state["ids"]
                        and core.check_transaction_shape(t) is None
                        and rules.apply(t, S.pending_state, require_signatures) is None):
                    S.pending.append(t)
            result = {"replaced": replaced, "length": len(S.chain)}
            if requeued:
                result["requeued"] = requeued
            return result
    finally:
        sync_lock.release()


def sync_loop(interval):
    time.sleep(1)                                                # first synchronization shortly after the start
    while True:
        try:
            sync_with_peers()
        except Exception as error:                                # the loop must never die
            print("sync error:", error, file=sys.stderr)
        time.sleep(interval)


# ---------- HTTP interface ----------

@app.get("/")
def home():
    return jsonify({"node_id": node_id, "consensus": "poa", "validator_index": idx_of(my_pub),
                    "validators": len(validators), "signatures_required": require_signatures,
                    "try_in_browser": ["/status", "/chain", "/pending", "/validators", "/validate",
                                       "/trace/d1", "/produce", "/nodes/resolve"],
                    "need_curl_or_a_script": ["POST /transactions/new", "POST /blocks/new", "POST /nodes/register"]})


@app.get("/status")
def status():
    with lock:
        height = len(S.chain)
        next_validator = poa.proposer_for(height, validators)
        return jsonify({"height": height, "tip": S.chain[-1]["hash"], "validator_index": idx_of(my_pub),
                        "next_proposer": idx_of(next_validator), "is_my_turn": next_validator == my_pub,
                        "pending": len(S.pending), "healthy": S.healthy, "evidence": list(S.evidence),
                        "peers": sorted(peers), "validators": len(validators),
                        "seconds_since_last_block": round(time.time() - S.last_block_time, 1)})


@app.get("/validators")
def get_validators():
    return jsonify({"validators": [{"index": i, "public": v} for i, v in enumerate(validators)]})


@app.get("/chain")
def get_chain():
    with lock:
        chain = list(S.chain)
    return jsonify({"chain": chain, "length": len(chain)})


@app.get("/pending")
def get_pending():
    with lock:
        pending, dropped = list(S.pending), list(S.dropped)
    return jsonify({"pending": pending, "count": len(pending), "dropped": dropped})


@app.post("/transactions/new")
def new_transaction():
    tx = get_json_body()
    if tx is None:
        return jsonify({"error": "body must be a JSON object"}), 400
    shape_error = core.check_transaction_shape(tx)
    if shape_error:
        return jsonify({"error": shape_error}), 400
    with lock:
        problem = rules.apply(tx, S.pending_state, require_signatures)      # on success this also updates the state
        if problem:
            return jsonify({"error": problem}), 422
        S.pending.append(tx)
    if request.headers.get("X-Relayed") != "1":                             # a relayed transaction is not forwarded again
        relay_queue.put(tx)
    return jsonify({"message": f"transaction {tx['id']} accepted, it will be in the next block"}), 201


@app.post("/blocks/new")
def new_block():
    block = get_json_body()
    if block is None:
        return jsonify({"error": "body must be a JSON object"}), 400
    code, body = import_block(block)
    return jsonify(body), code


@app.get("/produce")
def produce():
    """Write a block now (only if it is this validator's turn). The automatic loop does the same by itself."""
    code, body = produce_block()
    return jsonify(body), code


@app.get("/trace/<donation_id>")
def get_trace(donation_id):
    with lock:
        if not S.healthy:
            return jsonify({"error": "this node's chain is invalid: it is synchronizing with its peers"}), 503
        result = rules.trace(S.committed, donation_id)
    if result is None:
        return jsonify({"error": f"unknown donation '{donation_id}'"}), 404
    return jsonify(result)


@app.get("/validate")
def validate():
    with lock:
        error = poa.chain_error(S.chain, validators, require_signatures)
        S.healthy = error is None
    return jsonify({"valid": error is None, "error": error})


@app.post("/nodes/register")
def register_nodes():
    body = get_json_body()
    nodes = body.get("nodes") if body else None
    if not isinstance(nodes, list) or not all(isinstance(n, str) and n for n in nodes):
        return jsonify({"error": "body must be {\"nodes\": [\"http://host:port\", ...]}"}), 400
    for url in nodes:
        url = url.rstrip("/")
        if url not in own_urls:
            peers.add(url)
    return jsonify({"message": "peers registered", "peers": sorted(peers)}), 201


@app.get("/nodes/resolve")
def resolve():
    return jsonify(sync_with_peers(wait=True))


@app.post("/tamper")
def tamper():
    """Lab only (--lab): change a stored amount without updating any hash, as a dishonest administrator could."""
    if not lab_mode:
        return jsonify({"error": "not found: start the node with --lab"}), 404
    body = get_json_body() or {}
    try:
        i, j, amount = int(body["block"]), int(body["tx"]), int(body["amount"])
        with lock:
            S.chain[i]["transactions"][j]["amount"] = amount
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "body must be {\"block\": i, \"tx\": j, \"amount\": a}"}), 400
    except IndexError:
        return jsonify({"error": "no such block or transaction"}), 404
    return jsonify({"message": f"amount of transaction {j} in block {i} set to {amount}"})


# ---------- start ----------

def setup(args):
    global validators, my_key, my_pub, require_signatures, lab_mode
    validators = [v.strip() for v in args.validators.split(",") if v.strip()]
    error = poa.validators_error(validators)
    if error:
        sys.exit(f"VALIDATORS: {error}. Run  python make_validators.py  to create the keys.")
    my_key = args.validator_key.strip() or None
    if my_key:
        try:
            my_pub = wallet.public_key_of(my_key)
        except ValueError:
            sys.exit("VALIDATOR_KEY is not a valid private key (64 hex characters)")
        if my_pub not in validators:
            sys.exit("the public key of VALIDATOR_KEY is not in the VALIDATORS list")
    require_signatures = not args.no_signatures
    lab_mode = args.lab
    own_urls.update({f"http://localhost:{args.port}", f"http://127.0.0.1:{args.port}"})
    if args.self_url:
        own_urls.add(args.self_url.rstrip("/"))
    for url in (u.strip().rstrip("/") for u in args.peers.split(",")):
        if url and url not in own_urls:
            peers.add(url)
    S.chain = [poa.make_genesis(validators)]
    S.committed = rules.new_state()
    S.pending_state = copy.deepcopy(S.committed)
    S.last_block_time = time.time()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5001)))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"), help="use 0.0.0.0 inside Docker")
    parser.add_argument("--peers", default=os.environ.get("PEERS", ""), help="comma-separated peer URLs")
    parser.add_argument("--self-url", default=os.environ.get("SELF_URL", ""), help="this node's own URL for the others")
    parser.add_argument("--validators", default=os.environ.get("VALIDATORS", ""),
                        help="comma-separated public keys of all validators, in turn order")
    parser.add_argument("--validator-key", default=os.environ.get("VALIDATOR_KEY", ""),
                        help="this node's private key (without it the node is an observer)")
    parser.add_argument("--block-interval", type=float, default=float(os.environ.get("BLOCK_INTERVAL", 2) or 0),
                        help="seconds between blocks; 0 = no automatic writing (use /produce)")
    parser.add_argument("--sync-interval", type=float, default=float(os.environ.get("SYNC_INTERVAL", 5) or 0),
                        help="seconds between synchronizations with the peers; 0 = never (use /nodes/resolve)")
    parser.add_argument("--no-signatures", action="store_true", default=env_flag("NO_SIGNATURES"),
                        help="accept unsigned transactions with plain names")
    parser.add_argument("--lab", action="store_true", default=env_flag("LAB"),
                        help="enable the lab-only /tamper endpoint")
    args = parser.parse_args()
    setup(args)

    threading.Thread(target=relay_worker, daemon=True).start()
    if my_key and args.block_interval > 0:
        threading.Thread(target=producer_loop, args=(args.block_interval,), daemon=True).start()
    if args.sync_interval > 0:
        threading.Thread(target=sync_loop, args=(args.sync_interval,), daemon=True).start()
    role = f"validator {idx_of(my_pub)} of {len(validators)}" if my_pub else f"observer of {len(validators)} validators"
    print(f"node {node_id}: {role}, port {args.port}, peers: {sorted(peers) or 'none'}")
    app.run(host=args.host, port=args.port, threaded=True)