"""Stage 1 (PoA) integration checks with real nodes: 4 validators on one network.
Run:  python3 test_poa_network.py          (takes about one minute)"""
import os
import subprocess
import sys
import time

import requests

import core
import poa
import wallet

results = []


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


def stx(id, type, who, parent, amount, memo="", signer=None):
    tx = {"id": id, "type": type, "sender": who[1], "parent": parent, "amount": amount, "memo": memo}
    return wallet.sign_transaction(tx, (signer or who)[0])


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
    """Four validator nodes as local processes. env_mode starts them with environment variables, like Docker Compose."""

    def __init__(self, base_port, block_interval=0, sync_interval=0, env_mode=False):
        self.urls = [f"http://127.0.0.1:{base_port + i}" for i in range(4)]
        self.ports = [base_port + i for i in range(4)]
        self.block_interval, self.sync_interval, self.env_mode = block_interval, sync_interval, env_mode
        self.procs = {}

    def start(self, i):
        peers = ",".join(self.urls)
        if self.env_mode:
            env = dict(os.environ, PORT=str(self.ports[i]), HOST="127.0.0.1", PEERS=peers, SELF_URL=self.urls[i],
                       VALIDATORS=",".join(V), VALIDATOR_KEY=KEYS[i][0], BLOCK_INTERVAL=str(self.block_interval),
                       SYNC_INTERVAL=str(self.sync_interval))
            cmd = [sys.executable, "node_poa.py"]
        else:
            env = None
            cmd = [sys.executable, "node_poa.py", "--port", str(self.ports[i]), "--peers", peers,
                   "--self-url", self.urls[i], "--validators", ",".join(V), "--validator-key", KEYS[i][0],
                   "--block-interval", str(self.block_interval), "--sync-interval", str(self.sync_interval), "--lab"]
        self.procs[i] = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert wait_for(lambda: requests.get(self.urls[i] + "/status", timeout=1).ok, 15), f"node {i} did not start"

    def start_all(self):
        for i in range(4):
            self.start(i)

    def stop(self, i):
        self.procs[i].terminate()
        self.procs[i].wait()

    def stop_all(self):
        for pr in self.procs.values():
            pr.terminate()
        for pr in self.procs.values():
            pr.wait()
        self.procs.clear()

    # --- small helpers ---
    def status(self, i):
        return requests.get(self.urls[i] + "/status", timeout=5).json()

    def heights(self, which=range(4)):
        return [self.status(i)["height"] for i in which]

    def tips(self, which=range(4)):
        return [self.status(i)["tip"] for i in which]

    def pending_ids(self, i):
        return {t["id"] for t in requests.get(self.urls[i] + "/pending", timeout=5).json()["pending"]}

    def chain(self, i):
        return requests.get(self.urls[i] + "/chain", timeout=5).json()["chain"]

    def submit(self, i, tx):
        return requests.post(self.urls[i] + "/transactions/new", json=tx, timeout=10)

    def submit_and_spread(self, i, tx, which=range(4)):
        """Submit to node i and wait until the other nodes have the transaction too (it is relayed)."""
        assert self.submit(i, tx).status_code == 201
        return wait_for(lambda: all(tx["id"] in self.pending_ids(j) for j in which))

    def produce(self, i):
        return requests.get(self.urls[i] + "/produce", timeout=15)

    def send_block(self, i, block):
        return requests.post(self.urls[i] + "/blocks/new", json=block, timeout=10)

    def block_on_top(self, txs, by, i=0, ts=None, keys=KEYS):
        """A block on top of node i's chain, written by validator `by` (the attacker may hold any validator's key)."""
        chain = self.chain(i)
        prev = chain[-1]
        ts = max(int(time.time()), prev["timestamp"]) if ts is None else ts
        return poa.make_block(len(chain), ts, prev["hash"], txs, keys[by][1], keys[by][0])


# =====================================================================================================
print("=== 1. four validators write blocks in turn ===")
c = Cluster(6001)
try:
    c.start_all()
    st = [c.status(i) for i in range(4)]
    check("all nodes start at height 1 (genesis only)", [s["height"] for s in st], [1, 1, 1, 1])
    check("every node knows which validator it is", [s["validator_index"] for s in st], [0, 1, 2, 3])
    check("validator 0 writes block 1", [s["next_proposer"] for s in st], [0, 0, 0, 0])
    check("only node 0 has the turn", [s["is_my_turn"] for s in st], [True, False, False, False])
    check("identical genesis block (same tip) everywhere", len(set(c.tips())), 1)

    d1 = stx("d1", "DONATION", DONOR, CHARITY[1], 1000)
    a1 = stx("a1", "ALLOCATION", CHARITY, "d1", 600, "food-packages")
    check("d1 submitted to node 2 reaches all nodes", c.submit_and_spread(2, d1), True)
    check("a1 submitted to node 3 reaches all nodes", c.submit_and_spread(3, a1), True)

    r = c.produce(1)
    check(f"node 1 refuses to write block 1, it is not its turn [{r.json().get('error')}]", r.status_code, 409)
    r = c.produce(0)
    check("node 0 writes block 1", r.status_code, 201)
    check("every other node accepted it (201)", sorted(r.json()["deliveries"].values()), [201, 201, 201])
    check("all nodes are at height 2 with the same tip", wait_for(lambda: c.heights() == [2] * 4 and len(set(c.tips())) == 1), True)
    check("the transactions left the pending lists", wait_for(lambda: all(not c.pending_ids(i) for i in range(4))), True)

    r = c.produce(1)
    check(f"node 1 has the turn but nothing to write [{r.json().get('error')}]", r.status_code, 409)
    e1 = stx("e1", "EXPENSE", CHARITY, "a1", 250, RECEIPT)
    c.submit_and_spread(0, e1)
    check("node 1 writes block 2", c.produce(1).status_code, 201)
    c.submit_and_spread(1, stx("d2", "DONATION", DONOR, CHARITY[1], 500))
    check("node 2 writes block 3", c.produce(2).status_code, 201)
    c.submit_and_spread(2, stx("d3", "DONATION", DONOR, CHARITY[1], 20))
    check("node 3 writes block 4", c.produce(3).status_code, 201)
    c.submit_and_spread(3, stx("d4", "DONATION", DONOR, CHARITY[1], 30))
    check("node 0 writes block 5: the rotation starts again", c.produce(0).status_code, 201)
    check("all nodes are at height 6", wait_for(lambda: c.heights() == [6] * 4), True)
    check("the blocks were written by validators 0,1,2,3,0",
          [V.index(b["proposer"]) for b in c.chain(2)[1:]], [0, 1, 2, 3, 0])
    chains = [c.chain(i) for i in range(4)]
    check("the four chains are identical", chains[0] == chains[1] == chains[2] == chains[3], True)
    check("every node says the chain is valid",
          [requests.get(u + "/validate").json()["valid"] for u in c.urls], [True] * 4)
    traces = [requests.get(u + "/trace/d1").json() for u in c.urls]
    check("/trace/d1 is identical on all nodes", traces[0] == traces[1] == traces[2] == traces[3], True)
    check("d1: 1000 donated, 600 allocated, 250 spent", (traces[0]["allocated"], traces[0]["spent_total"]), (600, 250))

    # ================================================================================================
    print("\n=== 2. attacks through the public interface (POST /blocks/new on node 0) ===")
    h, tip = c.status(0)["height"], c.status(0)["tip"]
    turn = c.status(0)["next_proposer"]                                  # validator whose turn it is for block 6
    other = (turn + 1) % 4
    good_tx = stx("x1", "DONATION", DONOR, CHARITY[1], 5)

    r = c.send_block(0, c.block_on_top([good_tx], by=other))
    check(f"a validator writing out of turn [{r.json().get('error')}]", (r.status_code, has(r.json()["error"], "wrong proposer")), (422, True))
    outsider = poa.make_block(h, int(time.time()), tip, [good_tx], EVE[1], EVE[0])
    r = c.send_block(0, outsider)
    check("a non-validator writing a block", (r.status_code, has(r.json()["error"], "not a validator")), (422, True))
    forged = c.block_on_top([good_tx], by=turn)
    forged["signature"] = wallet.sign(EVE[0], forged["hash"])
    r = c.send_block(0, forged)
    check("the right proposer, but a signature made with another key", (r.status_code, has(r.json()["error"], "signature")), (422, True))
    overspend = stx("x2", "EXPENSE", CHARITY, "a1", 5000, RECEIPT)
    r = c.send_block(0, c.block_on_top([overspend], by=turn))
    check(f"right validator, charity signature, but overspending [{r.json().get('error')}]", (r.status_code, has(r.json()["error"], "over-spent")), (422, True))
    r = c.send_block(0, c.block_on_top([stx("x3", "EXPENSE", CHARITY, "a1", 10, RECEIPT, signer=EVE)], by=turn))
    check("an expense signed by Eve in the charity's name", (r.status_code, has(r.json()["error"], "invalid signature")), (422, True))
    r = c.send_block(0, c.block_on_top([stx("x4", "EXPENSE", EVE, "a1", 10, RECEIPT)], by=turn))
    check("an expense signed by Eve in her own name", (r.status_code, has(r.json()["error"], "wrong sender")), (422, True))
    unsigned = {k: v for k, v in good_tx.items() if k != "signature"}
    r = c.send_block(0, c.block_on_top([unsigned], by=turn))
    check("an unsigned transaction", (r.status_code, has(r.json()["error"], "missing signature")), (422, True))
    r = c.send_block(0, c.block_on_top([good_tx], by=turn, ts=int(time.time()) + 100000))
    check("a timestamp far in the future", (r.status_code, has(r.json()["error"], "future")), (422, True))
    bad_link = poa.make_block(h, int(time.time()), "f" * 64, [good_tx], V[turn], KEYS[turn][0])
    r = c.send_block(0, bad_link)
    check("a block that does not link to our tip", (r.status_code, has(r.json()["error"], "previous_hash")), (422, True))
    old = c.chain(0)[3]
    r = c.send_block(0, old)
    check("a block we already have is simply acknowledged", (r.status_code, r.json()["message"]), (200, "already have this block"))
    far = poa.make_block(h + 40, int(time.time()), tip, [good_tx], poa.proposer_for(h + 40, V),
                         KEYS[poa.proposer_index(h + 40, 4)][0])
    r = c.send_block(0, far)
    check("a block far ahead makes the node synchronize, it is not appended", (r.status_code, c.status(0)["height"]), (202, h))
    r = requests.post(c.urls[0] + "/blocks/new", json=["not", "a", "block"])
    check("garbage in the body", r.status_code, 400)
    r = requests.post(c.urls[0] + "/blocks/new", json={"hello": 1})
    check("a JSON object that is not a block", r.status_code, 400)
    different = poa.make_block(3, int(time.time()), c.chain(0)[2]["hash"], [stx("y1", "DONATION", DONOR, CHARITY[1], 1)],
                               V[poa.proposer_index(3, 4)], KEYS[poa.proposer_index(3, 4)][0])
    r = c.send_block(0, different)
    check("a different block for height 3 signed by the right validator: conflict", r.status_code, 409)
    ev = c.status(0)["evidence"]
    check(f"the evidence of double signing is kept [{ev}]", (len(ev), ev[0]["validator"], ev[0]["height"]), (1, 2, 3))
    check("after all these attacks node 0 is unchanged", (c.status(0)["height"], c.status(0)["tip"]), (h, tip))
    check("... and still valid", requests.get(c.urls[0] + "/validate").json()["valid"], True)
    check("the other nodes were never affected", c.heights([1, 2, 3]), [h] * 3)

    # ================================================================================================
    print("\n=== 3. an administrator edits a stored amount; the node finds out and heals itself ===")
    r = requests.post(c.urls[3] + "/tamper", json={"block": 2, "tx": 0, "amount": 10})
    check("tamper accepted (lab endpoint)", r.status_code, 200)
    v = requests.get(c.urls[3] + "/validate").json()
    check(f"node 3 detects it [{v['error']}]", (v["valid"], has(v["error"], "block 2")), (False, True))
    check("node 3 stops answering /trace with data it cannot trust", requests.get(c.urls[3] + "/trace/d1").status_code, 503)
    check("the other nodes are fine", [requests.get(c.urls[i] + "/validate").json()["valid"] for i in range(3)], [True] * 3)
    r = requests.get(c.urls[3] + "/nodes/resolve").json()
    check("node 3 resolves: it adopts a valid chain, even of the same length", (r["replaced"], r["length"]), (True, h))
    check("node 3 is valid again", requests.get(c.urls[3] + "/validate").json()["valid"], True)
    check("and /trace/d1 works and agrees with the others", requests.get(c.urls[3] + "/trace/d1").json(), traces[0])

    # ================================================================================================
    print("\n=== 4. KNOWN LIMITATION: the validator whose turn it is is down ===")
    h = c.status(0)["height"]
    down = c.status(0)["next_proposer"]
    live = [i for i in range(4) if i != down]
    c.stop(down)
    print(f"     validator {down} stopped (it is the one that must write block {h})")
    z = stx("z1", "DONATION", DONOR, CHARITY[1], 7)
    check("a transaction is still accepted and spread to the live nodes", c.submit_and_spread(live[0], z, which=live), True)
    answers = [c.produce(i).status_code for i in live]
    check("no other validator may write that block: the chain waits", answers, [409] * 3)
    check("the height did not change", c.heights(live), [h] * 3)

    c.start(down)                                                         # restarts empty, like a restarted container
    check("the restarted node starts with an empty ledger", c.status(down)["height"], 1)
    r = requests.get(c.urls[down] + "/nodes/resolve").json()
    check("it catches up with /nodes/resolve", (r["replaced"], r["length"]), (True, h))
    check("and it learned the pending transaction it had missed", z["id"] in c.pending_ids(down), True)
    check("now it writes its block", c.produce(down).status_code, 201)
    check("everyone is at the next height", wait_for(lambda: c.heights() == [h + 1] * 4), True)
    check("the donation z1 is now in the ledger: nothing was lost",
          requests.get(c.urls[0] + "/trace/z1").json()["donation"]["amount"], 7)
finally:
    c.stop_all()

# =====================================================================================================
print("\n=== 5. KNOWN LIMITATION: a validator signs two different blocks for the same height ===")
c = Cluster(6011)
try:
    c.start_all()
    cheat = 0                                                              # validator 0 writes block 1
    block_a = c.block_on_top([stx("dA", "DONATION", DONOR, CHARITY[1], 111)], by=cheat)
    block_b = c.block_on_top([stx("dB", "DONATION", DONOR, CHARITY[1], 222)], by=cheat)
    check("two different, properly signed blocks for height 1", block_a["hash"] != block_b["hash"], True)
    check("A is accepted by nodes 0 and 1", [c.send_block(i, block_a).status_code for i in (0, 1)], [201, 201])
    check("B is accepted by nodes 2 and 3", [c.send_block(i, block_b).status_code for i in (2, 3)], [201, 201])
    tips = c.tips()
    check("the network is split in two", (tips[0] == tips[1], tips[2] == tips[3], tips[0] != tips[2]), (True, True, True))
    check("nobody has noticed yet", [len(c.status(i)["evidence"]) for i in range(4)], [0, 0, 0, 0])
    r = requests.get(c.urls[0] + "/nodes/resolve").json()
    check("on synchronization node 0 sees the other branch but cannot decide: same length, nothing replaced", r["replaced"], False)
    ev = c.status(0)["evidence"]
    check(f"the double signing is detected and recorded [{ev}]", [(e["validator"], e["height"]) for e in ev], [(cheat, 1)])
    check("the evidence holds both block hashes", ev[0]["hashes"], sorted([block_a["hash"], block_b["hash"]]))
    check("but the split remains (this is what the BFT stage fixes)", c.tips()[0] != c.tips()[2], True)
finally:
    c.stop_all()

# =====================================================================================================
print("\n=== 6. automatic mode, started with environment variables like Docker Compose ===")
c = Cluster(6021, block_interval=1, sync_interval=1, env_mode=True)
try:
    c.start_all()
    check("nodes started from environment variables know their role", [c.status(i)["validator_index"] for i in range(4)], [0, 1, 2, 3])
    for n, via in enumerate([3, 1, 0, 2], start=1):                        # each donation enters at a different node
        tx = stx(f"auto{n}", "DONATION", DONOR, CHARITY[1], 10 * n)
        assert c.submit(via, tx).status_code == 201
        check(f"block {n} appears by itself on all nodes", wait_for(lambda: c.heights() == [n + 1] * 4, 25), True)
    check("blocks 1 to 4 were written by validators 0,1,2,3 in turn",
          [V.index(b["proposer"]) for b in c.chain(0)[1:]], [0, 1, 2, 3])
    check("the chains are identical and valid", (len({str(c.chain(i)) for i in range(4)}), requests.get(c.urls[3] + "/validate").json()["valid"]), (1, True))
    c.stop(2)
    assert c.submit(0, stx("auto5", "DONATION", DONOR, CHARITY[1], 50)).status_code == 201   # block 5 belongs to validator 0
    check("validator 0 writes block 5 while node 2 is down", wait_for(lambda: c.heights([0, 1, 3]) == [6] * 3, 25), True)
    c.start(2)
    check("the restarted node catches up by itself (no call from us)", wait_for(lambda: c.status(2)["height"] == 6, 25), True)
    check("it has exactly the same chain", c.chain(2) == c.chain(0), True)
finally:
    c.stop_all()

print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")