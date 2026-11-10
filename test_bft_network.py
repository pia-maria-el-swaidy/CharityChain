"""Stage 2 (BFT) integration checks with real nodes: 4 validators (and an observer) on one network.
Run:  python3 test_bft_network.py            (all sections, about 3 minutes)
      python3 test_bft_network.py 1 2 4      (only some sections)"""
import json
import os
import subprocess
import sys
import tempfile
import time

import requests

import bft
import core
import wallet

results = []
SECTIONS = {int(a) for a in sys.argv[1:]} or set(range(1, 8))


def check(name, got, expected):
    ok = got == expected
    print(("OK   " if ok else "FAIL ") + name)
    if not ok:
        print("   got:     ", got)
        print("   expected:", expected)
    results.append(ok)


def has(text, part):
    return text is not None and part in text


KEYS = [wallet.generate_keypair() for _ in range(4)]
V = [k[1] for k in KEYS]
DONOR, CHARITY, EVE = wallet.generate_keypair(), wallet.generate_keypair(), wallet.generate_keypair()
RECEIPT = core.sha256_hex("receipt-001")
GENESIS = bft.make_genesis(V)


def stx(id, type, who, parent, amount, memo="", signer=None):
    t = {"id": id, "type": type, "sender": who[1], "parent": parent, "amount": amount, "memo": memo}
    return wallet.sign_transaction(t, (signer or who)[0])


def wait_for(condition, seconds=20):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if condition():
                return True
        except requests.RequestException:
            pass
        time.sleep(0.1)
    return False


class Cluster:
    """Four validators (and optionally an observer) as local processes."""

    def __init__(self, base_port, sync_interval=1, observer=False, env_mode=False, data_root=None):
        self.count = 5 if observer else 4
        self.ports = [base_port + i for i in range(self.count)]
        self.urls = [f"http://127.0.0.1:{p}" for p in self.ports]
        self.sync_interval, self.env_mode, self.data_root = sync_interval, env_mode, data_root
        self.procs = {}

    def data_dir(self, i):
        return os.path.join(self.data_root, f"node{i + 1}") if self.data_root else ""

    def start(self, i):
        peers, validator = ",".join(self.urls), i < 4
        timing = {"propose": "0.6", "prevote": "0.3", "precommit": "0.3", "delta": "0.2"}
        if self.env_mode:                                              # like Docker Compose
            env = dict(os.environ, PORT=str(self.ports[i]), HOST="127.0.0.1", PEERS=peers, SELF_URL=self.urls[i],
                       VALIDATORS=",".join(V), TIMEOUT_PROPOSE=timing["propose"], TIMEOUT_PREVOTE=timing["prevote"],
                       TIMEOUT_PRECOMMIT=timing["precommit"], TIMEOUT_DELTA=timing["delta"],
                       SYNC_INTERVAL=str(self.sync_interval), DATA_DIR=self.data_dir(i), LAB="1")
            env.pop("VALIDATOR_KEY", None)
            if validator:
                env["VALIDATOR_KEY"] = KEYS[i][0]
            cmd = [sys.executable, "node_bft.py"]
        else:
            env = None
            cmd = [sys.executable, "node_bft.py", "--port", str(self.ports[i]), "--peers", peers, "--self-url", self.urls[i],
                   "--validators", ",".join(V), "--timeout-propose", timing["propose"], "--timeout-prevote", timing["prevote"],
                   "--timeout-precommit", timing["precommit"], "--timeout-delta", timing["delta"],
                   "--sync-interval", str(self.sync_interval), "--lab"]
            if validator:
                cmd += ["--validator-key", KEYS[i][0]]
            if self.data_root:
                cmd += ["--data-dir", self.data_dir(i)]
        self.procs[i] = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert wait_for(lambda: requests.get(self.urls[i] + "/status", timeout=1).ok, 15), f"node {i} did not start"

    def start_all(self):
        for i in range(self.count):
            self.start(i)

    def stop(self, i):
        proc = self.procs.pop(i, None)
        if proc:
            proc.terminate()
            proc.wait()

    def stop_all(self):
        for i in list(self.procs):
            self.stop(i)

    # --- helpers
    def status(self, i):
        return requests.get(self.urls[i] + "/status", timeout=5).json()

    def heights(self, which=None):
        return [self.status(i)["height"] for i in (range(self.count) if which is None else which)]

    def chain(self, i):
        return requests.get(self.urls[i] + "/chain", timeout=5).json()["chain"]

    def hashes(self, i):
        return [b["hash"] for b in self.chain(i)]

    def submit(self, i, tx):
        return requests.post(self.urls[i] + "/transactions/new", json=tx, timeout=10)

    def valid(self, i):
        return requests.get(self.urls[i] + "/validate", timeout=10).json()["valid"]

    def messages(self, i, msgs):
        return requests.post(self.urls[i] + "/consensus/message", json=msgs, timeout=10).json()["results"]

    def send_block(self, i, block):
        return requests.post(self.urls[i] + "/blocks/new", json=block, timeout=10)

    def tip(self, i=0):
        return self.chain(i)[-1]

    def forge(self, txs, signers, i=0, round_=0, previous=None, ts=None):
        """A block on top of node i's chain with a commit certificate signed by the given validators."""
        prev = previous or self.tip(i)
        ts = prev["timestamp"] + 1 if ts is None else ts
        block = bft.make_block(prev["index"] + 1, ts, prev["hash"], txs)
        votes = [bft.make_vote(bft.PRECOMMIT, block["index"], round_, block["hash"], V[k], KEYS[k][0]) for k in signers]
        return dict(block, commit=bft.make_certificate(round_, votes, V))


def same_ledger(c, which):
    chains = [c.hashes(i) for i in which]
    return all(h == chains[0] for h in chains)


# =====================================================================================================
c = Cluster(6201)
try:
    if SECTIONS & {1, 2, 3, 4}:
        c.start_all()

    if 1 in SECTIONS:
        print("=== 1. four validators, a block is final with 3 signatures ===")
        st = [c.status(i) for i in range(4)]
        check("all nodes start at height 1", [s["height"] for s in st], [1, 1, 1, 1])
        check("every node knows its validator number and the quorum (3 of 4)",
              ([s["validator_index"] for s in st], {s["quorum"] for s in st}), ([0, 1, 2, 3], {3}))
        d1 = stx("d1", "DONATION", DONOR, CHARITY[1], 1000)
        a1 = stx("a1", "ALLOCATION", CHARITY, "d1", 600, "food-packages")
        e1 = stx("e1", "EXPENSE", CHARITY, "a1", 250, RECEIPT)
        t0 = time.time()
        check("d1 submitted to node 2 is accepted", c.submit(2, d1).status_code, 201)
        ok = wait_for(lambda: c.heights() == [2] * 4)
        check(f"all four nodes have the block {time.time() - t0:.2f} s later, nobody called /mine or /produce", ok, True)
        block = c.chain(0)[1]
        check("it was decided in round 0 with at least 3 signatures",
              (block["commit"]["round"], len(block["commit"]["signatures"]) >= 3), (0, True))
        check("anybody can verify the certificate alone, without trusting a node", bft.check_block(block, GENESIS, V), None)
        c.submit(3, a1)
        check("a1 via node 3", wait_for(lambda: c.heights() == [3] * 4), True)
        c.submit(0, e1)
        check("e1 via node 0", wait_for(lambda: c.heights() == [4] * 4), True)
        chain = c.chain(1)
        check("the proposers of blocks 1, 2, 3 are validators 0, 1, 2",
              [bft.block_proposer(b, V) for b in chain[1:]], [0, 1, 2])
        check("the four ledgers are identical", same_ledger(c, range(4)), True)
        check("every node says its chain is valid", [c.valid(i) for i in range(4)], [True] * 4)
        traces = [requests.get(u + "/trace/d1").json() for u in c.urls]
        check("/trace/d1 is identical on all nodes", traces[0] == traces[1] == traces[2] == traces[3], True)
        check("d1: 1000 donated, 600 allocated, 250 spent", (traces[0]["allocated"], traces[0]["spent_total"]), (600, 250))
        for k, who in enumerate([0, 1, 2]):
            c.submit(who, stx(f"x{k}", "DONATION", DONOR, CHARITY[1], 10 + k))
        check("three transactions at once all end up in the ledger",
              wait_for(lambda: all(requests.get(c.urls[3] + f"/trace/x{k}").status_code == 200 for k in range(3))), True)
        check("everybody still agrees", wait_for(lambda: len(set(c.heights())) == 1 and same_ledger(c, range(4))), True)

    if 2 in SECTIONS:
        print("\n=== 2. the validator whose turn it is is down: the chain goes on (Proof of Authority stopped here) ===")
        H = c.status(0)["height"]
        down = (H - 1) % 4                                        # proposer of round 0 for the next block
        live = [i for i in range(4) if i != down]
        c.stop(down)
        print(f"     validator {down} stopped: it is the proposer of block {H} in round 0")
        t0 = time.time()
        check("a transaction is accepted by a live node", c.submit(live[0], stx("c1", "DONATION", DONOR, CHARITY[1], 7)).status_code, 201)
        ok = wait_for(lambda: c.heights(live) == [H + 1] * 3, 20)
        block = c.chain(live[0])[H]
        check(f"after a timeout the next validator proposed: block decided in round {block['commit']['round']} ({time.time() - t0:.1f} s)",
              (ok, block["commit"]["round"] >= 1, bft.block_proposer(block, V) != down), (True, True, True))
        c.submit(live[1], stx("c2", "DONATION", DONOR, CHARITY[1], 8))
        wait_for(lambda: c.heights(live) == [H + 2] * 3)
        block = c.chain(live[0])[H + 1]
        check("the next block (a live proposer) is decided in round 0 again", (block["commit"]["round"], len(block["commit"]["signatures"])), (0, 3))
        c.start(down)
        check("the restarted validator (empty memory) catches up by itself", wait_for(lambda: c.status(down)["height"] == H + 2, 20), True)
        check("it has exactly the same chain, and a valid one", (same_ledger(c, range(4)), c.valid(down)), (True, True))
        c.submit(down, stx("c3", "DONATION", DONOR, CHARITY[1], 9))
        check("and it takes part again", wait_for(lambda: c.heights() == [H + 3] * 4), True)

    if 3 in SECTIONS:
        print("\n=== 3. two of four validators down: no majority, nothing is decided, nothing is lost ===")
        H = c.status(0)["height"]
        c.stop(0)
        c.stop(1)
        c.submit(2, stx("h1", "DONATION", DONOR, CHARITY[1], 5))
        time.sleep(4)
        check("2 live validators cannot decide: no block, no fork", c.heights([2, 3]), [H, H])
        check("the transaction waits in the pending list", c.status(2)["pending"] >= 1, True)
        c.start(0)
        ok = wait_for(lambda: c.heights([0, 2, 3]) == [H + 1] * 3, 30)
        check("as soon as 3 validators are up, the block is decided", ok, True)
        c.start(1)
        check("the last validator catches up", wait_for(lambda: c.heights() == [H + 1] * 4, 20), True)
        chain = c.chain(0)
        check("the final chain is valid, certificates included", bft.chain_error(chain, V), None)
        check("no node holds a different block anywhere", same_ledger(c, range(4)), True)

    if 4 in SECTIONS:
        print("\n=== 4. attacks through the public interface ===")
        H, tip = c.status(0)["height"], c.status(0)["tip"]
        vote = lambda who, kind=bft.PREVOTE, h=H, r=0, value=bft.NIL: bft.make_vote(kind, h, r, value, V[who], KEYS[who][0])
        block = bft.make_block(H, int(time.time()), tip, [stx("z1", "DONATION", DONOR, CHARITY[1], 1)])
        proposer_round0 = (H - 1) % 4
        wrong_one = (proposer_round0 + 1) % 4
        r = c.messages(0, [bft.make_vote(bft.PREVOTE, H, 0, bft.NIL, EVE[1], EVE[0])])
        check(f"a vote from a stranger {r}", has(r[0], "not a validator"), True)
        r = c.messages(0, [dict(vote(1), signature=wallet.sign(EVE[0], "x"))])
        check(f"a vote with a forged signature {r}", has(r[0], "invalid signature"), True)
        r = c.messages(0, [bft.make_proposal(H, 0, -1, block, V[wrong_one], KEYS[wrong_one][0])])
        check(f"a proposal from a validator whose turn it is not {r}", has(r[0], "not the proposer"), True)
        bad_block = dict(block, transactions=[stx("z2", "DONATION", DONOR, CHARITY[1], 99)])
        r = c.messages(0, [bft.make_proposal(H, 0, -1, bad_block, V[proposer_round0], KEYS[proposer_round0][0])])
        check(f"a proposal whose block does not match its hash {r}", has(r[0], "hash does not match"), True)
        r = c.messages(0, [vote(1, h=H + 10)])
        check(f"a vote for a much later height makes the node fetch blocks, nothing else {r}", r, ["future height"])
        r = c.messages(0, [vote(1, h=1)])
        check(f"a vote for an old height {r}", r, ["old height"])
        r = c.messages(0, ["hello", {"type": "PREVOTE"}, None, dict(vote(1), type="HELLO")])
        check(f"garbage messages are refused one by one, no crash {r}", all(x is not None for x in r) and len(r) == 4, True)
        check("the validators did not move", (c.heights(), len(set(c.hashes(i)[-1] for i in range(4)))), ([H] * 4, 1))

        good_tx = stx("z3", "DONATION", DONOR, CHARITY[1], 3)
        r = c.send_block(0, c.forge([good_tx], signers=(0, 1)))
        check(f"a block with only 2 signatures [{r.json().get('error')}]", (r.status_code, has(r.json()["error"], "2 signatures")), (422, True))
        forged = c.forge([good_tx], signers=(0, 1))
        forged["commit"]["signatures"].append({"validator": EVE[1], "signature": wallet.sign(
            EVE[0], bft.vote_text(bft.PRECOMMIT, forged["index"], 0, forged["hash"]))})
        r = c.send_block(0, forged)
        check("... with a stranger's signature added to reach 3", (r.status_code, has(r.json()["error"], "not a validator")), (422, True))
        twice = c.forge([good_tx], signers=(0, 1))
        twice["commit"]["signatures"].append(twice["commit"]["signatures"][0])
        r = c.send_block(0, twice)
        check("... with one signature counted twice", (r.status_code, has(r.json()["error"], "twice")), (422, True))
        borrowed = dict(c.forge([good_tx], signers=(0, 1, 2)), commit=c.chain(0)[2]["commit"])
        r = c.send_block(0, borrowed)
        check("a certificate borrowed from another block", (r.status_code, has(r.json()["error"], "invalid signature")), (422, True))
        legit = c.forge([good_tx], signers=(0, 1, 2))
        r = c.send_block(0, dict(legit, transactions=[stx("z4", "DONATION", DONOR, CHARITY[1], 1000000)]))
        check("a real certificate, but the transactions were changed afterwards", (r.status_code, has(r.json()["error"], "hash")), (422, True))
        r = c.send_block(0, c.forge([good_tx], signers=(0, 1, 2), previous=dict(c.tip(0), hash="f" * 64)))
        check("a block that does not link to our chain", (r.status_code, has(r.json()["error"], "previous_hash")), (422, True))
        over = c.forge([stx("z5", "EXPENSE", CHARITY, "a1", 5000, RECEIPT)], signers=(0, 1, 2, 3))
        r = c.send_block(0, over)
        check(f"even ALL 4 validators signing cannot make an overspending valid [{r.json().get('error')}]",
              (r.status_code, has(r.json()["error"], "over-spent")), (422, True))
        r = c.send_block(0, c.forge([stx("z6", "EXPENSE", EVE, "a1", 5, RECEIPT)], signers=(0, 1, 2)))
        check("... nor a valid signature of someone who is not the charity", (r.status_code, has(r.json()["error"], "wrong sender")), (422, True))
        r = c.send_block(0, c.chain(0)[1])
        check("a block we already have is simply acknowledged", (r.status_code, r.json()["message"]), (200, "already have this block"))
        check("garbage in the body of /blocks/new", (requests.post(c.urls[0] + "/blocks/new", json=["x"]).status_code,
                                                       requests.post(c.urls[0] + "/blocks/new", json={"a": 1}).status_code), (400, 400))
        check("after all these attacks nothing changed and everything is valid",
              (c.heights(), [c.valid(i) for i in range(4)]), ([H] * 4, [True] * 4))
finally:
    c.stop_all()

# =====================================================================================================
c = Cluster(6211, sync_interval=0)
try:
    if SECTIONS & {5, 6}:
        c.start_all()
        for i in range(4):
            requests.post(c.urls[i] + "/nodes/register", json={"nodes": c.urls})
        c.submit(1, stx("base", "DONATION", DONOR, CHARITY[1], 1))
        assert wait_for(lambda: c.heights() == [2] * 4)

    if 5 in SECTIONS:
        print("\n=== 5. a validator signs three different blocks for the same height (Proof of Authority split here) ===")
        H = c.status(0)["height"]
        cheat = (H - 1) % 4                                       # the proposer of round 0
        others = [i for i in range(4) if i != cheat]
        c.stop(cheat)                                             # we play this validator: its node must not interfere
        prev = c.tip(others[0])
        blocks = [bft.make_block(H, prev["timestamp"] + 1, prev["hash"], [stx(f"eq{k}", "DONATION", DONOR, CHARITY[1], 100 + k)])
                  for k in range(3)]
        honest_tx = stx("ok1", "DONATION", DONOR, CHARITY[1], 5)
        c.submit(others[0], honest_tx)
        for k, dst in enumerate(others):
            c.messages(dst, [bft.make_proposal(H, 0, -1, blocks[k], V[cheat], KEYS[cheat][0]),
                             bft.make_vote(bft.PREVOTE, H, 0, blocks[k]["hash"], V[cheat], KEYS[cheat][0])])
        check("two different proposals were signed for height %d" % H, len({b["hash"] for b in blocks}), 3)
        ok = wait_for(lambda: c.heights(others) == [H + 1] * 3, 30)
        check("the three honest validators still decide ONE block, and it is the same one", (ok, len({c.chain(i)[H]["hash"] for i in others})), (True, 1))
        check("their ledgers become identical (the next block may still be in flight) and are valid",
              (wait_for(lambda: len({c.status(i)["height"] for i in others}) == 1 and same_ledger(c, others), 20),
               [c.valid(i) for i in others]), (True, [True] * 3))
        check("the transaction of an honest client is in the ledger on all of them",
              wait_for(lambda: all(requests.get(c.urls[i] + "/trace/ok1").status_code == 200 for i in others), 30), True)
        proofs = [e for i in others for e in c.status(i)["evidence"] if e["validator"] == cheat]
        check(f"the double signing was detected and recorded ({len(proofs)} proofs)", len(proofs) >= 1, True)
        check("and every proof is valid on its own: two signatures of that validator, same round, different content",
              all(bft.evidence_error(e) is None for e in proofs), True)
        c.start(cheat)
        requests.get(c.urls[cheat] + "/nodes/resolve", timeout=30)
        check("the cheating validator's node restarts and catches up", wait_for(lambda: same_ledger(c, range(4)), 20), True)

    if 6 in SECTIONS:
        print("\n=== 6. an administrator edits a stored amount: the node finds out and repairs itself ===")
        H = c.status(3)["height"]
        r = requests.post(c.urls[3] + "/tamper", json={"block": 1, "tx": 0, "amount": 999999})
        check("tamper accepted (lab endpoint)", r.status_code, 200)
        v = requests.get(c.urls[3] + "/validate").json()
        check(f"node 3 detects it [{v['error']}]", (v["valid"], has(v["error"], "block 1")), (False, True))
        check("it refuses to answer /trace with data it cannot trust", requests.get(c.urls[3] + "/trace/base").status_code, 503)
        check("the other nodes are fine", [c.valid(i) for i in range(3)], [True] * 3)
        r = requests.get(c.urls[3] + "/nodes/resolve", timeout=30).json()
        check(f"/nodes/resolve repairs it: it keeps the valid beginning and fetches the rest {r}", (r["replaced"], r["length"]), (True, H))
        check("valid again, same ledger as the others, and /trace works",
              (c.valid(3), same_ledger(c, range(4)), requests.get(c.urls[3] + "/trace/base").status_code), (True, True, 200))
finally:
    c.stop_all()

# =====================================================================================================
if 7 in SECTIONS:
    print("\n=== 7. configuration from environment variables, saved ledger, observer, damaged disk ===")
    with tempfile.TemporaryDirectory() as tmp:
        c = Cluster(6221, sync_interval=2, observer=True, env_mode=True, data_root=tmp)
        try:
            c.start_all()
            check("5 nodes started from environment variables only: 4 validators and 1 observer",
                  [c.status(i)["validator_index"] for i in range(5)], [0, 1, 2, 3, None])
            c.submit(4, stx("p1", "DONATION", DONOR, CHARITY[1], 100))           # the client talks to the observer
            check("a transaction submitted to the observer is decided by the validators",
                  wait_for(lambda: c.heights() == [2] * 5, 20), True)
            check("the observer verified the certificate itself: its chain is valid", (c.valid(4), bft.chain_error(c.chain(4), V)), (True, None))
            r = requests.post(c.urls[4] + "/consensus/message", json=[])
            check("the observer takes no part in the voting", r.status_code, 404)
            c.submit(0, stx("p2", "DONATION", DONOR, CHARITY[1], 200))
            wait_for(lambda: c.heights() == [3] * 5, 20)
            check("the ledger is saved on disk (one line per block)",
                  sum(1 for _ in open(os.path.join(tmp, "node2", "chain.jsonl"))), 2)
            c.stop_all()

            c.start(1)                                                           # alone: nobody to ask
            check("a validator started alone has its whole ledger back from disk", c.status(1)["height"], 3)
            check("and it can answer /trace", requests.get(c.urls[1] + "/trace/p2").json()["donation"]["amount"], 200)
            for i in (0, 2, 3, 4):
                c.start(i)
            c.submit(3, stx("p3", "DONATION", DONOR, CHARITY[1], 300))
            check("after a restart of everything the network decides new blocks on top of the saved ledger",
                  wait_for(lambda: c.heights() == [4] * 5, 25), True)

            c.stop(2)                                                            # damage the disk of validator 2
            path = os.path.join(tmp, "node3", "chain.jsonl")
            lines = open(path).read().splitlines()
            damaged = json.loads(lines[2])
            damaged["transactions"][0]["amount"] = 1
            lines[2] = json.dumps(damaged)
            open(path, "w").write("\n".join(lines) + "\n")
            c.start(2)
            check("a damaged ledger on disk is detected at start: only the valid beginning (2 blocks) is kept",
                  c.status(2)["height"], 3)
            check("then it fetches the rest from its peers and is correct again",
                  wait_for(lambda: c.status(2)["height"] == 4 and c.valid(2) and same_ledger(c, range(5)), 25), True)
            check("and the file on disk was repaired too", sum(1 for _ in open(path)), 3)
        finally:
            c.stop_all()

print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")
sys.exit(0 if all(results) else 1)