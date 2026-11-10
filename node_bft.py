"""TraceChain - Stage 2 node: BFT consensus with quorum voting (see bft.py for the protocol).

Run one validator (normally Docker Compose does this, see docker-compose.yml and make_validators.py):

    python node_bft.py --port 5001 --validators PK1,PK2,PK3,PK4 --validator-key SK1 \\
                       --peers http://127.0.0.1:5002,http://127.0.0.1:5003,http://127.0.0.1:5004 --data-dir data1

Every setting can also come from an environment variable: PORT, HOST, PEERS, SELF_URL, VALIDATORS, VALIDATOR_KEY,
DATA_DIR, TIMEOUT_PROPOSE, TIMEOUT_PREVOTE, TIMEOUT_PRECOMMIT, TIMEOUT_DELTA, RESEND_INTERVAL, SYNC_INTERVAL,
NO_SIGNATURES, LAB.
A node without VALIDATOR_KEY is an observer: it never votes, but it checks every block's commit certificate itself.

How a transaction travels:
    client -> any node (POST /transactions/new) -> relayed to all nodes
    -> the validators run the voting rounds (POST /consensus/message): proposal, prevotes, precommits
    -> with 3 of 4 precommits the block is final; the precommit signatures are its certificate
    -> the block is sent to everybody (POST /blocks/new) and every node verifies the certificate itself.
A node that was away fetches the missing final blocks from a peer (GET /chain?from=N); it does not have to trust the peer,
because every block proves itself with its certificate.
"""
import argparse
import copy
import json
import os
import queue
import sys
import threading
import time
import types
import uuid

import requests
from flask import Flask, jsonify, request

import bft
import core
import rules
import wallet
import chain_storage
app = Flask(__name__)
lock = threading.RLock()                  # protects the ledger and the consensus engine
sync_lock = threading.Lock()              # one synchronization with the peers at a time
sync_event = threading.Event()            # wakes the synchronization loop up (we saw a message from a later height)
relay_queue = queue.Queue()               # transactions waiting to be forwarded to the peers, in order
senders = {}
senders_lock = threading.Lock()

node_id = uuid.uuid4().hex[:8]
validators = []                           # public keys, in turn order
my_key = None                             # None: observer
my_pub = None
peers = set()
own_urls = set()
require_signatures = True
lab_mode = False
data_dir = ""
use_db = False
db_node = ""
engine = None                             # the consensus engine (None for an observer)
timeouts = bft.Timeouts()

S = types.SimpleNamespace(
    chain=[],                 # final blocks, each with its commit certificate
    committed=None,           # rules state after all blocks
    pending=[],               # accepted transactions not yet in a block
    pending_state=None,       # committed state + pending transactions
    healthy=True,             # False while our own chain is found invalid
    last_block_time=time.time(),
    dropped=[],               # pending transactions that became invalid (with the reason)
    commits=[],               # blocks decided here, to be sent to the peers
)


def env_flag(name):
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def get_json_body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


def idx_of(public_key):
    return validators.index(public_key) if public_key in validators else None


# ---------------------------------------------------------------- sending to the peers

class PeerSender:
    """One queue and one thread per peer, so that a slow or dead peer never delays the others."""

    def __init__(self, url):
        self.url = url
        self.q = queue.Queue(maxsize=1000)
        threading.Thread(target=self.run, daemon=True).start()

    def put(self, path, payload):
        try:
            self.q.put_nowait((path, payload))
        except queue.Full:
            pass                                          # the periodic resend and the synchronization make up for it

    def run(self):
        session = requests.Session()
        while True:
            path, payload = self.q.get()
            try:
                session.post(self.url + path, json=payload, timeout=2)
            except requests.RequestException:
                pass


def send_to_peers(path, payload):
    for peer in sorted(peers):
        if peer in own_urls:
            continue
        with senders_lock:
            if peer not in senders:
                senders[peer] = PeerSender(peer)
            sender = senders[peer]
        sender.put(path, payload)


# ---------------------------------------------------------------- the ledger

def rebuild_pending(extra=()):
    """Re-check the candidate transactions against the committed state: those already in a block are skipped,
    those no longer valid are dropped (and listed in S.dropped)."""
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
    state, error = rules.replay(S.chain, require_signatures)
    if error:
        raise RuntimeError(error)
    S.committed = state
    rebuild_pending(extra)


def append_block(block):
    """Verify a final block (certificate, link, rules) and append it. Returns an error message or None. Lock held."""
    error = bft.check_block(block, S.chain[-1], validators)
    if error:
        return error
    trial = copy.deepcopy(S.committed)
    for t in block["transactions"]:
        problem = rules.apply(t, trial, require_signatures)
        if problem:
            return f"transaction {t.get('id')}: {problem}"
    S.chain.append(block)
    S.committed = trial
    S.last_block_time = time.time()
    store_block(block)
    rebuild_pending()
    return None


class LedgerHost:
    """What the consensus engine needs from the ledger."""

    def has_pending(self):
        return bool(S.pending)

    def next_block(self):
        if not S.pending:
            return None
        prev = S.chain[-1]
        return bft.make_block(len(S.chain), max(int(time.time()), prev["timestamp"]), prev["hash"],
                              S.pending[:bft.MAX_TXS_PER_BLOCK])

    def is_valid(self, block):
        prev = S.chain[-1]
        if block["index"] != len(S.chain) or bft.body_error(block, prev):
            return False
        if block["timestamp"] > time.time() + bft.MAX_FUTURE_SECONDS:
            return False
        trial = copy.deepcopy(S.committed)
        return all(rules.apply(t, trial, require_signatures) is None for t in block["transactions"])

    def on_decide(self, block):
        error = append_block(block)
        if error:
            raise RuntimeError(f"the engine decided a block that does not fit: {error}")
        S.commits.append(block)


def import_committed(block):
    """A final block from a peer: check its certificate and append it. Returns (http status, body)."""
    with lock:
        index = block.get("index") if isinstance(block, dict) else None
        if not isinstance(index, int) or isinstance(index, bool):
            return 400, {"error": "this is not a block"}
        height = len(S.chain)
        if index < height:
            if index >= 1 and S.chain[index]["hash"] == block.get("hash"):
                return 200, {"message": "already have this block"}
            return 409, {"error": "a different block is already final at this height"}
        if index > height:
            sync_event.set()
            return 202, {"message": f"this node is at height {height}: fetching the missing blocks"}
        error = append_block(block)
        if error:
            return 422, {"error": f"invalid block: {error}"}
        if engine is not None:
            engine.start_height(len(S.chain), time.time())
        return 201, {"message": f"block {index} appended", "height": len(S.chain)}


# ---------------------------------------------------------------- saving to disk

def _path(name):
    return os.path.join(data_dir, name)


def _write_atomic(path, text):
    temp = path + ".tmp"
    with open(temp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def store_block(block):
    if use_db:
        chain_storage.save_bft_block(db_node, block)
    elif data_dir:
        with open(_path("chain.jsonl"), "a") as f:
            f.write(json.dumps(block) + "\n")
            f.flush()
            os.fsync(f.fileno())


def rewrite_chain_file():
    if use_db:
        chain_storage.replace_bft_chain(db_node, S.chain[1:])
    elif data_dir:
        _write_atomic(_path("chain.jsonl"), "".join(json.dumps(b) + "\n" for b in S.chain[1:]))


def save_engine():
    if engine is None:
        return
    if use_db:
        chain_storage.save_bft_engine(db_node, engine.snapshot())
    elif data_dir:
        _write_atomic(_path("engine.json"), json.dumps(engine.snapshot()))


# ---------------------------------------------------------------- running the consensus engine

def collect():
    """Lock held: save what the engine changed (before anything is sent), then take what it wants to send."""
    if engine is None:
        return [], []
    if engine.dirty:
        save_engine()
        engine.dirty = False
    out = engine.take_outbox()
    done, S.commits = S.commits, []
    return out, done


def dispatch(out, done):
    if out:
        send_to_peers("/consensus/message", out)
    for block in done:
        send_to_peers("/blocks/new", block)


def run_engine(action):
    """Run an action on the engine under the lock, then save its state and send what it produced."""
    with lock:
        result = action()
        out, done = collect()
    dispatch(out, done)
    return result


def engine_loop():
    while True:
        time.sleep(0.05)
        try:
            run_engine(lambda: engine.tick(time.time()))
        except Exception as error:                        # the loop must never die
            print("consensus error:", repr(error), file=sys.stderr)


def resend_loop(interval):
    """Gossip: from time to time repeat our own messages of the current height, in case somebody missed them."""
    while True:
        time.sleep(interval)
        with lock:
            msgs = engine.own_messages()
        if msgs:
            send_to_peers("/consensus/message", msgs)


# ---------------------------------------------------------------- transactions between the nodes

def relay_worker():
    while True:
        tx = relay_queue.get()
        for peer in sorted(peers):
            if peer in own_urls:
                continue
            try:
                requests.post(peer + "/transactions/new", json=tx, headers={"X-Relayed": "1"}, timeout=3)
            except requests.RequestException:
                pass


# ---------------------------------------------------------------- synchronization with the peers

def audit_chain():
    """Check our own chain. If it is damaged, keep its valid beginning: the rest comes back from the peers."""
    with lock:
        if bft.chain_error(S.chain, validators, require_signatures) is None:
            S.healthy = True
            return
        S.healthy = False
        keep = bft.valid_prefix(S.chain, validators, require_signatures)
        S.chain = S.chain[:keep] if keep >= 1 else [bft.make_genesis(validators)]
        rebuild_all()
        rewrite_chain_file()
        if engine is not None:
            engine.start_height(len(S.chain), time.time())


def sync_once():
    """Fetch the final blocks we are missing from the peers (each block proves itself with its certificate)
    and the pending transactions we missed. Returns the number of blocks added."""
    if not sync_lock.acquire(True, 30):
        return 0
    added = 0
    try:
        audit_chain()
        for peer in sorted(peers):
            if peer in own_urls:
                continue
            try:
                status = requests.get(peer + "/status", timeout=3).json()
                if status.get("pending", 0) > 0:
                    pulled = requests.get(peer + "/pending", timeout=5).json()["pending"]
                    with lock:
                        for t in pulled:
                            if (isinstance(t, dict) and isinstance(t.get("id"), str)
                                    and t["id"] not in S.pending_state["ids"]
                                    and core.check_transaction_shape(t) is None
                                    and rules.apply(t, S.pending_state, require_signatures) is None):
                                S.pending.append(t)
                while True:
                    with lock:
                        height = len(S.chain)
                    if status["height"] <= height:
                        break
                    blocks = requests.get(peer + "/chain", params={"from": height}, timeout=10).json()["chain"]
                    progressed = False
                    for block in blocks:
                        code, _ = import_committed(block)
                        if code == 201:
                            added += 1
                            progressed = True
                        elif code != 200:
                            break
                    if not progressed:
                        break
            except (requests.RequestException, ValueError, KeyError, TypeError):
                continue
        with lock:
            S.healthy = bft.chain_error(S.chain, validators, require_signatures) is None
    finally:
        sync_lock.release()
    return added


def sync_loop(interval):
    time.sleep(1)
    while True:
        try:
            sync_once()
        except Exception as error:
            print("sync error:", repr(error), file=sys.stderr)
        sync_event.wait(interval)
        sync_event.clear()
        time.sleep(0.2)


# ---------------------------------------------------------------- HTTP interface

@app.get("/")
def home():
    return jsonify({"node_id": node_id, "consensus": "bft", "validator_index": idx_of(my_pub),
                    "validators": len(validators), "quorum": bft.quorum(len(validators)),
                    "signatures_required": require_signatures,
                    "try_in_browser": ["/status", "/chain", "/pending", "/validators", "/validate", "/trace/d1"],
                    "need_curl_or_a_script": ["POST /transactions/new", "POST /consensus/message", "POST /blocks/new"]})


@app.get("/status")
def status():
    with lock:
        info = {"height": len(S.chain), "tip": S.chain[-1]["hash"], "validator_index": idx_of(my_pub),
                "pending": len(S.pending), "healthy": S.healthy, "peers": sorted(peers),
                "validators": len(validators), "quorum": bft.quorum(len(validators)),
                "seconds_since_last_block": round(time.time() - S.last_block_time, 1)}
        if engine is not None:
            info.update({"round": engine.round, "step": engine.step, "active": engine.active,
                         "locked_round": engine.locked_round, "evidence": list(engine.evidence)})
        else:
            info["evidence"] = []
        return jsonify(info)


@app.get("/validators")
def get_validators():
    return jsonify({"validators": [{"index": i, "public": v} for i, v in enumerate(validators)]})


@app.get("/chain")
def get_chain():
    start = max(request.args.get("from", 0, type=int), 0)
    with lock:
        blocks = list(S.chain[start:start + 200])
        total = len(S.chain)
    return jsonify({"chain": blocks, "length": total, "from": start})


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
    if request.headers.get("X-Relayed") != "1":
        relay_queue.put(tx)
    return jsonify({"message": f"transaction {tx['id']} accepted, it will be in the next block"}), 201


@app.post("/consensus/message")
def consensus_message():
    """Votes and proposals of the other validators (one message or a list)."""
    if engine is None:
        return jsonify({"error": "this node is an observer: it does not take part in the voting"}), 404
    body = request.get_json(silent=True)
    messages = body if isinstance(body, list) else [body]
    if len(messages) > 300:
        return jsonify({"error": "too many messages"}), 400

    def handle():
        now, results = time.time(), []
        for msg in messages:
            reason = engine.on_message(msg, now)
            if reason == "future height":
                sync_event.set()
            results.append(reason)
        return results

    return jsonify({"results": run_engine(handle)})


@app.post("/blocks/new")
def new_block():
    """A final block with its certificate (sent by the validator that decided it, or by anybody)."""
    block = get_json_body()
    if block is None:
        return jsonify({"error": "body must be a JSON object"}), 400
    code, body = import_committed(block)
    return jsonify(body), code


@app.get("/trace/<donation_id>")
def get_trace(donation_id):
    with lock:
        if not S.healthy:
            return jsonify({"error": "this node's chain is invalid: it is repairing itself from its peers"}), 503
        result = rules.trace(S.committed, donation_id)
    if result is None:
        return jsonify({"error": f"unknown donation '{donation_id}'"}), 404
    return jsonify(result)


@app.get("/validate")
def validate():
    with lock:
        error = bft.chain_error(S.chain, validators, require_signatures)
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
    """Synchronize now: audit our chain and fetch the final blocks we are missing."""
    added = sync_once()
    with lock:
        length = len(S.chain)
    return jsonify({"replaced": added > 0, "added": added, "length": length})


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


# ---------------------------------------------------------------- start

def setup(args):
    global validators, my_key, my_pub, require_signatures, lab_mode, data_dir, engine, timeouts, use_db, db_node
    validators = [v.strip() for v in args.validators.split(",") if v.strip()]
    error = None
    if not validators:
        error = "the validator list is empty"
    elif any(not wallet.is_public_key(v) for v in validators):
        error = "an entry is not a public key (64 hex characters)"
    elif len(set(validators)) != len(validators):
        error = "the validator list contains the same key twice"
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
    data_dir = args.data_dir
    timeouts = bft.Timeouts(args.timeout_propose, args.timeout_prevote, args.timeout_precommit, args.timeout_delta)
    own_urls.update({f"http://localhost:{args.port}", f"http://127.0.0.1:{args.port}"})
    if args.self_url:
        own_urls.add(args.self_url.rstrip("/"))
    for url in (u.strip().rstrip("/") for u in args.peers.split(",")):
        if url and url not in own_urls:
            peers.add(url)
    use_db = bool(os.environ.get("DATABASE_URL"))
    db_node = args.node_name or f"node_{args.port}"
    S.chain = [bft.make_genesis(validators)]
    if use_db:
        chain_storage.init_bft_db()
        S.chain += chain_storage.load_bft_chain(db_node)
    elif data_dir:
        os.makedirs(data_dir, exist_ok=True)
        if os.path.exists(_path("chain.jsonl")):
            with open(_path("chain.jsonl")) as f:
                S.chain += [json.loads(line) for line in f if line.strip()]
    if len(S.chain) > 1:
        keep = bft.valid_prefix(S.chain, validators, require_signatures)
        if keep < len(S.chain):
            print(f"warning: the saved chain is damaged after block {keep - 1}: keeping the valid beginning",
                  file=sys.stderr)
            S.chain = S.chain[:max(keep, 1)]
            rewrite_chain_file()
    S.committed = rules.new_state()
    S.pending_state = copy.deepcopy(S.committed)
    rebuild_all()
    S.last_block_time = time.time()

    if my_key:
        engine = bft.Engine(validators, LedgerHost(), my_key, timeouts)
        engine.start_height(len(S.chain), time.time())
        snap = None
        if use_db:
            snap = chain_storage.load_bft_engine(db_node)
        elif data_dir and os.path.exists(_path("engine.json")):
            try:
                with open(_path("engine.json")) as f:
                    snap = json.load(f)
            except (OSError, ValueError):
                snap = None
        if snap and engine.restore(snap, time.time()):
            print("consensus state restored from", "PostgreSQL" if use_db else "disk")


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
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", ""),
                        help="folder where the chain and the voting state are saved (empty: memory only)")
    parser.add_argument("--node-name", default=os.environ.get("NODE_NAME", ""),
                        help="name of this validator's rows in PostgreSQL")
    parser.add_argument("--timeout-propose", type=float, default=float(os.environ.get("TIMEOUT_PROPOSE", 2.0)))
    parser.add_argument("--timeout-prevote", type=float, default=float(os.environ.get("TIMEOUT_PREVOTE", 1.0)))
    parser.add_argument("--timeout-precommit", type=float, default=float(os.environ.get("TIMEOUT_PRECOMMIT", 1.0)))
    parser.add_argument("--timeout-delta", type=float, default=float(os.environ.get("TIMEOUT_DELTA", 0.5)),
                        help="seconds added to every timeout for each further round")
    parser.add_argument("--resend-interval", type=float, default=float(os.environ.get("RESEND_INTERVAL", 1.0)))
    parser.add_argument("--sync-interval", type=float, default=float(os.environ.get("SYNC_INTERVAL", 5.0)),
                        help="seconds between synchronizations with the peers; 0 = never (use /nodes/resolve)")
    parser.add_argument("--no-signatures", action="store_true", default=env_flag("NO_SIGNATURES"),
                        help="accept unsigned transactions with plain names")
    parser.add_argument("--lab", action="store_true", default=env_flag("LAB"), help="enable the lab-only /tamper endpoint")
    args = parser.parse_args()
    setup(args)

    threading.Thread(target=relay_worker, daemon=True).start()
    if engine is not None:
        threading.Thread(target=engine_loop, daemon=True).start()
        if args.resend_interval > 0:
            threading.Thread(target=resend_loop, args=(args.resend_interval,), daemon=True).start()
    if args.sync_interval > 0:
        threading.Thread(target=sync_loop, args=(args.sync_interval,), daemon=True).start()
    role = f"validator {idx_of(my_pub)} of {len(validators)}" if my_pub else f"observer of {len(validators)} validators"
    print(f"node {node_id}: {role}, port {args.port}, height {len(S.chain)}, peers: {sorted(peers) or 'none'}")
    app.run(host=args.host, port=args.port, threaded=True)