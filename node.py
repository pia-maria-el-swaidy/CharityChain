"""TraceChain - Part 3: a node as a web service (Flask).

Run:   python3 node.py --port 5001
Needs: python -m pip install flask requests cryptography

Mining (Exercise 5): GET /mine starts Proof of Work in a background thread and answers at once (202);
GET /mine/status reports the result; GET /mine?wait=1 waits for the result (the old behaviour);
--auto-mine SECONDS mines by itself whenever transactions are pending.
"""
import argparse
import os
import threading
import time
import uuid

import requests
from flask import Flask, jsonify, request

import core
import rules

app = Flask(__name__)
lock = threading.Lock()          # one request at a time may change the ledger

node_id = uuid.uuid4().hex[:8]   # random id chosen at startup
blockchain = core.Blockchain()
peers = set()                    # URLs such as "http://127.0.0.1:5002"
own_urls = set()                 # this node's own URLs (filled in at startup)
broadcast_enabled = True         # Exercise 1; turn off with --no-broadcast to reproduce Parts 4 and 5
check_rules = True                # Exercise 2; turn off with --hash-only to reproduce Attack B
self_heal = True                  # Exercise 3; turn off with --no-heal to reproduce Q11
require_signatures = True         # Exercise 4; turn off with --no-signatures for the unsigned experiments
mining_delay = 0.0                # lab only (--mining-delay): pretend that Proof of Work is slow
requeue_orphans = True            # Exercise 5; turn off with --no-requeue to reproduce the loss of d2 in Part 4
mining = {"job": 0, "state": "idle"}   # Exercise 5: state of the background mining job


def env_flag(name):
    """True if the environment variable is set to 1, true or yes (used for Docker Compose settings)."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def get_json_body():
    """Return the JSON body as a dict, or None if it is missing or not an object."""
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


# ---------- home page ----------

@app.get("/")
def home():
    return jsonify({
        "node_id": node_id,
        "chain_length": len(blockchain.chain),
        "peers": sorted(peers),
        "signatures_required": require_signatures,
        "mining": mining.get("state"),
        "try_in_browser": ["/chain", "/pending", "/mine", "/mine/status", "/validate",
                           "/trace/d1", "/nodes/resolve"],
        "need_curl_or_a_script": ["POST /transactions/new", "POST /nodes/register",
                                  "POST /tamper", "POST /forge"],
    })


# ---------- ledger ----------

@app.get("/chain")
def get_chain():
    return jsonify({"chain": blockchain.chain, "length": len(blockchain.chain)})


@app.post("/transactions/new")
def new_transaction():
    tx = get_json_body()
    if tx is None:
        return jsonify({"error": "body must be a JSON object"}), 400
    shape_error = core.check_transaction_shape(tx)
    if shape_error:                                   # malformed -> 400
        return jsonify({"error": shape_error}), 400
    with lock:
        rule_error = blockchain.add_transaction(tx)   # rule broken -> 422
    if rule_error:
        return jsonify({"error": rule_error}), 422
    return jsonify({"message": f"transaction {tx['id']} will be added to the next block"}), 201


def notify_peers():
    """Exercise 1: ask every peer (except ourselves) to run /nodes/resolve.
    Called AFTER the lock is released, because the peers will fetch our /chain."""
    notified, unreachable = [], []
    for peer in sorted(peers):
        if peer in own_urls:
            continue
        try:
            requests.get(peer + "/nodes/resolve", timeout=5)
            notified.append(peer)
        except requests.RequestException:
            unreachable.append(peer)          # a dead peer must not stop the others
    return notified, unreachable


def chain_is_stale(index, previous_hash):
    """True if the chain no longer ends where a block being mined expects."""
    chain = blockchain.chain
    return len(chain) != index or chain[-1]["hash"] != previous_hash


def run_mining_job(template):
    """Runs in a background thread. The lock is NOT held during Proof of Work, so the node stays responsive."""
    index, previous = template["index"], template["previous_hash"]
    try:
        waited = 0.0                                      # lab only: --mining-delay
        while waited < mining_delay and not chain_is_stale(index, previous):
            time.sleep(0.05)
            waited += 0.05
        result = None
        if not chain_is_stale(index, previous):
            result = core.proof_of_work(template, should_stop=lambda: chain_is_stale(index, previous))
        appended = False
        with lock:
            if result is None or chain_is_stale(index, previous):
                mining.update(state="stale", seconds=round(time.time() - mining["started"], 3),
                              message="the chain changed while mining: the block was "
                              "discarded and its transactions stay pending")
            else:
                template["hash"] = result
                blockchain.chain.append(template)
                mined = {t["id"] for t in template["transactions"]}
                blockchain.pending = [t for t in blockchain.pending if t["id"] not in mined]
                mining.update(state="done", block=template, seconds=round(time.time() - mining["started"], 3),
                              message=f"block {template['index']} mined by node {node_id}")
                appended = True
        if appended and broadcast_enabled:
            notified, unreachable = notify_peers()        # lock released: peers fetch our /chain
            with lock:
                mining.update(notified_peers=notified, unreachable_peers=unreachable)
    except Exception as error:                            # never leave a job stuck in "mining"
        with lock:
            mining.update(state="failed", message=f"mining failed: {error}")


def start_mining():
    """Start a Proof of Work job in a background thread. Returns (job, thread, None) or (job, None, reason)."""
    with lock:
        if mining["state"] == "mining":
            return mining["job"], None, "busy"
        if not blockchain.pending:
            return None, None, "nothing"
        template = {"index": len(blockchain.chain), "timestamp": int(time.time()),
                    "transactions": list(blockchain.pending),       # snapshot of what is pending now
                    "previous_hash": blockchain.chain[-1]["hash"], "nonce": 0}
        job = mining["job"] + 1
        mining.clear()
        mining.update(job=job, state="mining", started=time.time(),
                      transactions=[t["id"] for t in template["transactions"]])
        thread = threading.Thread(target=run_mining_job, args=(template,), daemon=True)
        thread.start()
    return job, thread, None


@app.get("/mine")
def mine():
    job, thread, problem = start_mining()
    if problem == "nothing":
        return jsonify({"error": "nothing to mine: no pending transactions"}), 409
    if problem == "busy":
        return jsonify({"error": "a block is already being mined", "job": job}), 409
    if request.args.get("wait") not in ("1", "true"):
        return jsonify({"message": "mining started in the background", "job": job,
                        "status": "/mine/status"}), 202
    thread.join()                                         # ?wait=1: answer only when the job is finished
    with lock:
        result = dict(mining)
    if result.get("state") != "done":
        return jsonify({"error": result.get("message", "mining did not finish")}), 409
    response = {"message": result["message"], "block": result["block"]}
    if broadcast_enabled:
        response["notified_peers"] = result.get("notified_peers", [])
        response["unreachable_peers"] = result.get("unreachable_peers", [])
    return jsonify(response)


@app.get("/mine/status")
def mine_status():
    """state: idle | mining | done | stale | failed"""
    with lock:
        status = {k: v for k, v in mining.items() if k != "started"}
        if mining["state"] == "mining":
            status["seconds"] = round(time.time() - mining["started"], 1)
        status["pending"] = len(blockchain.pending)
    return jsonify(status)


@app.get("/pending")
def get_pending():
    with lock:
        pending = list(blockchain.pending)
    return jsonify({"pending": pending, "count": len(pending)})


def sync_on_start(port):
    """--sync-on-start: a freshly started node (empty ledger) asks its peers for the chain,
    retrying for a few seconds because the other containers may still be starting."""
    for _ in range(10):
        time.sleep(1)
        try:
            if requests.get(f"http://127.0.0.1:{port}/nodes/resolve", timeout=30).json().get("replaced"):
                return
        except (requests.RequestException, ValueError):
            pass


def auto_miner(interval):
    """--auto-mine: every `interval` seconds, mine if something is pending and no job is running."""
    while True:
        time.sleep(interval)
        start_mining()


@app.get("/trace/<donation_id>")
def get_trace(donation_id):
    with lock:
        state, error = rules.replay(blockchain.chain, require_signatures)
        result = None if error else rules.trace(state, donation_id)
    if error:                                         # the chain itself breaks the rules
        return jsonify({"error": f"this ledger breaks the rules: {error}"}), 500
    if result is None:
        return jsonify({"error": f"unknown donation '{donation_id}'"}), 404
    return jsonify(result)


@app.get("/validate")
def validate():
    return jsonify({"valid": core.valid_chain(blockchain.chain, check_rules=check_rules,
                                              require_signatures=require_signatures)})


# ---------- network ----------

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


def recover_orphans(old_chain, new_chain):
    """Exercise 5 (call with the lock held, after the chain was replaced).
    Rebuilds the pending list: every transaction of the discarded blocks, then the old pending ones,
    each kept only if it is not already in the new chain and is still valid against it."""
    orphans = [t for b in old_chain[1:] for t in b.get("transactions", [])] if requeue_orphans else []
    candidates = orphans + blockchain.pending
    state, error = rules.replay(new_chain, require_signatures)
    in_new_chain = {t["id"]: t for b in new_chain[1:] for t in b["transactions"]}
    if error:                                     # only possible with --hash-only: just remove duplicates
        blockchain.pending = [t for t in blockchain.pending if t["id"] not in in_new_chain]
        return [], []
    kept, dropped = [], []
    for t in candidates:
        tid = t.get("id") if isinstance(t, dict) else None
        if not isinstance(tid, str):
            tid = None
        if tid in in_new_chain:
            if in_new_chain[tid] != t:            # same id, different content
                dropped.append({"id": tid, "reason": "this id is used by a different transaction in the new chain"})
            continue                              # same transaction: it is already in the new chain
        problem = rules.apply(t, state, require_signatures)
        if problem:
            dropped.append({"id": tid, "reason": problem})
        else:
            kept.append(t)
    blockchain.pending = kept
    orphan_objects = {id(t) for t in orphans}
    return [t["id"] for t in kept if id(t) in orphan_objects], dropped


@app.get("/nodes/resolve")
def resolve():
    """Consensus: the longest valid chain wins."""
    with lock:
        own_chain_valid = core.valid_chain(blockchain.chain, check_rules=check_rules,
                                           require_signatures=require_signatures)
        # Exercise 3: a node whose own chain is invalid starts from length 0,
        # so it accepts ANY valid chain from a peer (even a shorter one).
        best_length = len(blockchain.chain) if (own_chain_valid or not self_heal) else 0
        best = None
        for peer in sorted(peers):
            try:
                peer_chain = requests.get(peer + "/chain", timeout=5).json()["chain"]
            except (requests.RequestException, ValueError, KeyError, TypeError):
                continue                                  # skip a peer that does not answer properly
            if len(peer_chain) > best_length and core.valid_chain(
                    peer_chain, check_rules=check_rules, require_signatures=require_signatures):
                best, best_length = peer_chain, len(peer_chain)
        requeued, dropped = [], []
        if best is not None:
            old_chain, blockchain.chain = blockchain.chain, best
            requeued, dropped = recover_orphans(old_chain, best)
        response = {"replaced": best is not None, "length": len(blockchain.chain)}
        if requeued:
            response["requeued"] = requeued       # transactions of discarded blocks, back in the pending list
        if dropped:
            response["dropped"] = dropped         # transactions that are no longer valid, with the reason
        return jsonify(response)


# ---------- lab-only endpoints (used in Part 5) ----------

@app.post("/tamper")
def tamper():
    body = get_json_body() or {}
    try:
        i, j, amount = int(body["block"]), int(body["tx"]), int(body["amount"])
        blockchain.chain[i]["transactions"][j]["amount"] = amount   # no hash is updated
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "body must be {\"block\": i, \"tx\": j, \"amount\": a}"}), 400
    except IndexError:
        return jsonify({"error": "no such block or transaction"}), 404
    return jsonify({"message": f"amount of transaction {j} in block {i} set to {amount}"})


@app.post("/forge")
def forge():
    tx = get_json_body()
    error = core.check_transaction_shape(tx) if tx is not None else "body must be a JSON object"
    if error:
        return jsonify({"error": error}), 400
    with lock:
        block = {"index": len(blockchain.chain), "timestamp": int(time.time()),
                 "transactions": [tx], "previous_hash": blockchain.chain[-1]["hash"], "nonce": 0}
        block["hash"] = core.proof_of_work(block)             # valid PoW, NO rule check
        blockchain.chain.append(block)
    return jsonify({"message": "forged block appended", "block": block})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5001)))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"), help="use 0.0.0.0 inside Docker")
    parser.add_argument("--peers", default=os.environ.get("PEERS", ""),
                        help="comma-separated peer URLs (environment variable PEERS)")
    parser.add_argument("--self-url", default=os.environ.get("SELF_URL", ""),
                        help="this node's own URL as the others see it, so it never lists itself (SELF_URL)")
    parser.add_argument("--sync-on-start", action="store_true", default=env_flag("SYNC_ON_START"),
                        help="after starting, fetch the chain from the peers (SYNC_ON_START=1)")
    parser.add_argument("--no-broadcast", action="store_true", default=env_flag("NO_BROADCAST"),
                        help="do not notify peers after mining (needed to reproduce the forks of Parts 4 and 5)")
    parser.add_argument("--hash-only", action="store_true",
                        help="validate chains with the Part 1 hash checks only (reproduces Attack B)")
    parser.add_argument("--no-heal", action="store_true",
                        help="a node with an invalid chain keeps it (reproduces Q11 / Attack A before Exercise 3)")
    parser.add_argument("--no-signatures", action="store_true", default=env_flag("NO_SIGNATURES"),
                        help="accept unsigned transactions with plain names (the experiments of Parts 3-5)")
    parser.add_argument("--no-requeue", action="store_true",
                        help="discarded transactions are lost after a chain replacement (reproduces Part 4)")
    parser.add_argument("--mining-delay", type=float, default=0.0,
                        help="lab only: seconds added to every mining job, to watch the background job")
    parser.add_argument("--auto-mine", type=float, default=float(os.environ.get("AUTO_MINE", 0) or 0),
                        metavar="SECONDS",
                        help="mine by itself every SECONDS when transactions are pending (0 = off)")
    args = parser.parse_args()
    mining_delay = args.mining_delay
    requeue_orphans = not args.no_requeue
    require_signatures = not args.no_signatures
    blockchain.require_signatures = require_signatures
    self_heal = not args.no_heal
    broadcast_enabled = not args.no_broadcast
    check_rules = not args.hash_only
    own_urls.update({f"http://localhost:{args.port}", f"http://127.0.0.1:{args.port}"})
    if args.self_url:
        own_urls.add(args.self_url.rstrip("/"))
    for url in (u.strip().rstrip("/") for u in args.peers.split(",")):
        if url and url not in own_urls:
            peers.add(url)                    # Exercise 6: the peer list comes from the PEERS variable
    if args.sync_on_start:
        threading.Thread(target=sync_on_start, args=(args.port,), daemon=True).start()
    if args.auto_mine > 0:
        threading.Thread(target=auto_miner, args=(args.auto_mine,), daemon=True).start()
    print(f"node {node_id} listening on port {args.port}, peers: {sorted(peers) or 'none'}")
    app.run(host=args.host, port=args.port, threaded=True)