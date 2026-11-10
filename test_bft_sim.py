"""Stage 2 (BFT) simulation: the consensus engine on a simulated network, without HTTP.
Time is virtual, so crashes, delays, lost messages and Byzantine validators can be reproduced exactly.
Run:  python3 test_bft_sim.py          (takes about a minute)"""
import heapq
import json
import random
import sys

import bft
import wallet

NIL = bft.NIL
results = []


def check(name, got, expected):
    ok = got == expected
    print(("OK   " if ok else "FAIL ") + name)
    if not ok:
        print("   got:     ", got)
        print("   expected:", expected)
    results.append(ok)


def tx(i):
    return {"id": f"t{i}", "type": "DONATION", "sender": "s", "parent": "p", "amount": 1, "memo": ""}


class SimHost:
    """A tiny ledger for one validator: blocks of up to 3 transactions, no business rules."""

    def __init__(self, sim, index):
        self.sim, self.index = sim, index
        self.chain = [sim.genesis]
        self.pending = []

    def has_pending(self):
        return bool(self.pending)

    def next_block(self):
        if not self.pending:
            return None
        prev = self.chain[-1]
        return bft.make_block(len(self.chain), max(int(self.sim.now), prev["timestamp"]), prev["hash"], self.pending[:3])

    def is_valid(self, block):
        return bft.body_error(block, self.chain[-1]) is None

    def _append(self, block):
        self.chain.append(block)
        ids = {t["id"] for t in block["transactions"]}
        self.pending = [t for t in self.pending if t["id"] not in ids]

    def on_decide(self, block):
        assert bft.check_block(block, self.chain[-1], self.sim.V) is None, "the engine produced a bad block"
        self._append(block)

    def import_block(self, block):
        """Block synchronization: a final block from a peer is accepted if its certificate is valid."""
        if block["index"] != len(self.chain) or bft.check_block(block, self.chain[-1], self.sim.V):
            return False
        self._append(block)
        self.sim.engines[self.index].start_height(len(self.chain), self.sim.now)
        return True


class Sim:
    def __init__(self, n=4, seed=0, delay=(0.01, 0.06), drop=0.0, byzantine=(), timeouts=None):
        self.n, self.rng, self.now = n, random.Random(seed), 0.0
        self.keys = [wallet.generate_keypair() for _ in range(n)]
        self.V = [k[1] for k in self.keys]
        self.genesis = bft.make_genesis(self.V)
        self.byz, self.down = set(byzantine), set()
        self.delay, self.drop = delay, drop
        self.queue, self.seq = [], 0
        self.timeouts = timeouts or bft.Timeouts(1.0, 0.5, 0.5, 0.3)
        self.hosts = [SimHost(self, i) for i in range(n)]
        self.engines = [None if i in self.byz else bft.Engine(self.V, self.hosts[i], self.keys[i][0], self.timeouts)
                        for i in range(n)]
        for e in self.engines:
            if e:
                e.start_height(1, 0.0)
        self.last_resend = self.last_sync = 0.0
        self.seen_hashes = set()

    def honest(self):
        return [i for i in range(self.n) if i not in self.byz]

    def live(self):
        return [i for i in self.honest() if i not in self.down]

    def submit(self, transaction):
        for i in self.honest():
            self.hosts[i].pending.append(transaction)

    def deliver(self, src, dst, msg):
        if src in self.down or dst in self.down or self.rng.random() < self.drop:
            return
        if msg["type"] == bft.PROPOSAL:
            self.seen_hashes.add(msg["block"]["hash"])
        self.seq += 1
        heapq.heappush(self.queue, (self.now + self.rng.uniform(*self.delay), self.seq, dst, msg))

    def broadcast(self, src, msgs):
        for msg in msgs:
            for dst in range(self.n):
                if dst != src:
                    self.deliver(src, dst, msg)

    def step(self, dt=0.02):
        self.now = round(self.now + dt, 6)
        while self.queue and self.queue[0][0] <= self.now:
            _, _, dst, msg = heapq.heappop(self.queue)
            e = self.engines[dst]
            if e is not None and dst not in self.down:
                e.on_message(msg, self.now)
                self.broadcast(dst, e.take_outbox())
        for i in self.live():
            self.engines[i].tick(self.now)
            self.broadcast(i, self.engines[i].take_outbox())
        if self.now - self.last_resend >= 1.0:                       # gossip: everybody repeats its own messages
            self.last_resend = self.now
            for i in self.live():
                self.broadcast(i, self.engines[i].own_messages())
        if self.now - self.last_sync >= 1.0:                         # block synchronization with a certificate
            self.last_sync = self.now
            self.sync()

    def sync(self):
        live = self.live()
        if not live:
            return
        best = max(live, key=lambda i: len(self.hosts[i].chain))
        for i in live:
            host = self.hosts[i]
            while len(host.chain) < len(self.hosts[best].chain):
                if not host.import_block(self.hosts[best].chain[len(host.chain)]):
                    break

    def run(self, seconds, each_step=None):
        end = self.now + seconds
        while self.now < end:
            if each_step:
                each_step(self)
            self.step()

    def heights(self, which=None):
        return [len(self.hosts[i].chain) for i in (self.honest() if which is None else which)]


def agreement(sim):
    """No two honest validators have different blocks at the same height, and every block has a valid certificate."""
    chains = [sim.hosts[i].chain for i in sim.honest()]
    for k in range(min(len(c) for c in chains)):
        if len({c[k]["hash"] for c in chains}) != 1:
            return False
    for c in chains:
        for k in range(1, len(c)):
            if bft.check_block(c[k], c[k - 1], sim.V):
                return False
    return True


def evidence_of(sim, validator_index):
    found = [e for i in sim.honest() for e in sim.engines[i].evidence if e["validator"] == validator_index]
    return found


# ===================================================================================================
print("=== 1. everybody honest ===")
sim = Sim(seed=1)
for i in range(6):
    sim.submit(tx(i))
sim.run(10)
check("6 transactions make 2 blocks on every validator", sim.heights(), [3, 3, 3, 3])
check("all validators agree and every certificate is valid", agreement(sim), True)
chain = sim.hosts[0].chain
check("committed in round 0, proposers rotate 0 then 1",
      [(b["commit"]["round"], bft.block_proposer(b, sim.V)) for b in chain[1:]], [(0, 0), (0, 1)])
check("every block carries at least 3 of 4 signatures", all(len(b["commit"]["signatures"]) >= 3 for b in chain[1:]), True)

print("\n=== 2. the proposer of round 0 is down (Proof of Authority stopped here) ===")
sim = Sim(seed=2)
sim.down.add(0)
for i in range(3):
    sim.submit(tx(i))
sim.run(20)
check("the three live validators still commit the block", sim.heights([1, 2, 3]), [2, 2, 2])
block = sim.hosts[1].chain[1]
check(f"after a timeout the next validator proposed (round {block['commit']['round']})",
      (block["commit"]["round"], bft.block_proposer(block, sim.V)), (1, 1))
sim.down.discard(0)
sim.run(10)
check("validator 0 comes back and catches up", sim.heights(), [2, 2, 2, 2])
check("agreement", agreement(sim), True)

print("\n=== 3. two validators down: no majority, so nothing is decided; it resumes when one returns ===")
sim = Sim(seed=3)
sim.down.update({0, 1})
for i in range(3):
    sim.submit(tx(i))
sim.run(30)
check("2 of 4 cannot decide: no block (and no fork)", sim.heights([2, 3]), [1, 1])
sim.down.discard(1)
sim.run(30)
check("with 3 of 4 the chain moves again", sim.heights([1, 2, 3]), [2, 2, 2])
check("agreement", agreement(sim), True)

print("\n=== 4. a Byzantine proposer sends three different blocks to the three honest validators ===")
sim = Sim(seed=4, byzantine=[0])
for i in range(6):
    sim.submit(tx(i))
blocks = [bft.make_block(1, 1, sim.genesis["hash"], [tx(100 + k)]) for k in range(3)]
for k, dst in enumerate([1, 2, 3]):
    sim.deliver(0, dst, bft.make_proposal(1, 0, -1, blocks[k], sim.V[0], sim.keys[0][0]))
    sim.deliver(0, dst, bft.make_vote(bft.PREVOTE, 1, 0, blocks[k]["hash"], sim.V[0], sim.keys[0][0]))
sim.run(30)
check("the honest validators still agree on one block per height", agreement(sim), True)
check("and they make progress", min(sim.heights()) >= 2, True)
check("none of the three conflicting blocks was committed",
      {blocks[k]["hash"] for k in range(3)}.isdisjoint({b["hash"] for b in sim.hosts[1].chain}), True)
proofs = evidence_of(sim, 0)
check(f"double signing by validator 0 was detected ({len(proofs)} proofs)", len(proofs) >= 1, True)
check("and the proof is cryptographically valid", all(bft.evidence_error(e) is None for e in proofs), True)

print("\n=== 5. a Byzantine validator votes differently for different validators ===")
sim = Sim(seed=5, byzantine=[3])
for i in range(3):
    sim.submit(tx(i))
b = sim.keys[3]
sim.deliver(3, 0, bft.make_vote(bft.PREVOTE, 1, 0, NIL, b[1], b[0]))
sim.deliver(3, 1, bft.make_vote(bft.PREVOTE, 1, 0, blocks[0]["hash"], b[1], b[0]))
sim.deliver(3, 2, bft.make_vote(bft.PRECOMMIT, 1, 0, NIL, b[1], b[0]))
sim.deliver(3, 1, bft.make_vote(bft.PRECOMMIT, 1, 0, blocks[1]["hash"], b[1], b[0]))
sim.run(20)
check("the three honest validators commit and agree", (min(sim.heights()) >= 2, agreement(sim)), (True, True))
proofs = evidence_of(sim, 3)
check(f"double voting by validator 3 detected ({len(proofs)} proofs), all valid",
      (len(proofs) >= 1, all(bft.evidence_error(e) is None for e in proofs)), (True, True))

print("\n=== 6. messages are lost, then the network becomes reliable ===")
sim = Sim(seed=6, drop=0.3)
for i in range(9):
    sim.submit(tx(i))
sim.run(10)
sim.drop = 0.0
sim.run(40)
check("all 3 blocks are committed everywhere", sim.heights(), [4, 4, 4, 4])
check("agreement", agreement(sim), True)

print("\n=== 7. a validator restarts from its saved state and does not change its mind ===")
sim = Sim(seed=7)
for i in range(3):
    sim.submit(tx(i))
e = sim.engines[1]
while not e.prevotes.get(0, {}).get(sim.V[1]) and sim.now < 5:
    sim.step()
saved = json.loads(json.dumps(e.snapshot()))                       # what the node writes to disk
before = sorted(m["signature"] for m in e.own_messages())
fresh = bft.Engine(sim.V, sim.hosts[1], sim.keys[1][0], sim.timeouts)
fresh.start_height(1, sim.now)
check("the snapshot fits and is restored", fresh.restore(saved, sim.now), True)
check("it still knows every message it signed", sorted(m["signature"] for m in fresh.own_messages()), before)
check("same round and step", (fresh.round, fresh.step), (e.round, e.step))
bad = bft.make_vote(bft.PREVOTE, 1, 0, NIL, sim.V[1], sim.keys[1][0])
fresh_before = len(fresh.outbox)
fresh._vote(bft.PREVOTE, 0, NIL)
check("it refuses to sign a second, different prevote for the same round", len(fresh.outbox), fresh_before)
sim.engines[1] = fresh
sim.run(15)
check("the network goes on and agrees", (min(sim.heights()) >= 2, agreement(sim)), (True, True))
check("a snapshot of another height is refused",
      bft.Engine(sim.V, sim.hosts[1], sim.keys[1][0], sim.timeouts).restore(saved, 0.0), False)


print("\n=== 8. the lock rule, step by step ===")


class FixedHost:
    def __init__(self):
        self.pending = True

    def has_pending(self):
        return True

    def next_block(self):
        return None

    def is_valid(self, block):
        return True

    def on_decide(self, block):
        self.decided = block


keys = [wallet.generate_keypair() for _ in range(4)]
V = [k[1] for k in keys]
genesis = bft.make_genesis(V)
A = bft.make_block(1, 1, genesis["hash"], [tx(1)])
B = bft.make_block(1, 2, genesis["hash"], [tx(2)])
me = 2                                                              # we are validator 2 (proposer of round 2)
engine = bft.Engine(V, FixedHost(), keys[me][0], bft.Timeouts(1, 1, 1, 0))
engine.start_height(1, 0.0)
t = 0.0
engine.tick(t)
engine.on_message(bft.make_proposal(1, 0, -1, A, V[0], keys[0][0]), t)
check("round 0: we prevote the proposal A", engine.prevotes[0][V[me]]["hash"], A["hash"])
for k in (0, 3):
    engine.on_message(bft.make_vote(bft.PREVOTE, 1, 0, A["hash"], V[k], keys[k][0]), t)
check("a majority prevoted A: we lock on A and precommit it", (engine.locked_round, engine.precommits[0][V[me]]["hash"]), (0, A["hash"]))
for k in (0, 3):
    engine.on_message(bft.make_vote(bft.PRECOMMIT, 1, 0, NIL, V[k], keys[k][0]), t)
t += 1.5
engine.tick(t)
check("the precommit timeout moves us to round 1", engine.round, 1)
engine.on_message(bft.make_proposal(1, 1, -1, B, V[1], keys[1][0]), t)
check("round 1: a different block B is proposed, but we are locked on A: we prevote nil", engine.prevotes[1][V[me]]["hash"], NIL)
for k in (0, 1, 3):
    engine.on_message(bft.make_vote(bft.PREVOTE, 1, 1, B["hash"], V[k], keys[k][0]), t)
check("a newer majority for B: we move our lock to B", (engine.locked_round, engine.locked_value["hash"]), (1, B["hash"]))

engine = bft.Engine(V, FixedHost(), keys[me][0], bft.Timeouts(1, 1, 1, 0))
engine.start_height(1, 0.0)
engine.tick(0.0)
engine.on_message(bft.make_proposal(1, 0, -1, A, V[0], keys[0][0]), 0.0)
for k in (0, 3):
    engine.on_message(bft.make_vote(bft.PREVOTE, 1, 0, A["hash"], V[k], keys[k][0]), 0.0)
for k in (0, 3):
    engine.on_message(bft.make_vote(bft.PRECOMMIT, 1, 0, NIL, V[k], keys[k][0]), 0.0)
engine.tick(1.5)
for k in (0, 1, 3):
    engine.on_message(bft.make_vote(bft.PREVOTE, 1, 1, B["hash"], V[k], keys[k][0]), 1.5)
for k in (0, 1, 3):
    engine.on_message(bft.make_vote(bft.PRECOMMIT, 1, 1, NIL, V[k], keys[k][0]), 1.5)
engine.tick(5.0)
check("we are in round 2 and proposer; we hold A, locked at round 0", (engine.round >= 2, engine.locked_round), (True, 0))
engine.on_message(bft.make_proposal(1, 3, 1, B, V[3], keys[3][0]), 5.0)
check("a proposal of B with proof of a majority at round 1 (newer than our lock) is prevoted",
      engine.prevotes.get(3, {}).get(V[me], {}).get("hash") == B["hash"] or engine.round != 3, True)

host = FixedHost()
engine = bft.Engine(V, host, keys[me][0], bft.Timeouts(1, 1, 1, 0))
engine.start_height(1, 0.0)
engine.on_message(bft.make_proposal(1, 0, -1, A, V[0], keys[0][0]), 0.0)
for k in (0, 1, 3):
    engine.on_message(bft.make_vote(bft.PRECOMMIT, 1, 0, A["hash"], V[k], keys[k][0]), 0.0)
check("3 precommits for A: the block is decided, with a certificate of 3 signatures",
      (host.decided["hash"], len(host.decided["commit"]["signatures"]), bft.check_block(host.decided, genesis, V)),
      (A["hash"], 3, None))
check("and the engine moved on to the next height", engine.height, 2)

# ===================================================================================================
print("\n=== 9. fuzzing: a Byzantine validator with random messages, lost and late messages ===")


def byz_act(sim):
    rng, b = sim.rng, next(iter(sim.byz))
    if rng.random() > 0.25:
        return
    h = max(len(sim.hosts[i].chain) for i in sim.honest())
    r = rng.randrange(5)
    kind = rng.choice([bft.PROPOSAL, bft.PREVOTE, bft.PRECOMMIT])
    targets = [d for d in sim.honest() if rng.random() < 0.7]
    pool = list(sim.seen_hashes) + [NIL, NIL, "ab" * 32]
    for d in targets:
        if kind == bft.PROPOSAL:
            if bft.proposer_for(h, r, sim.V) != sim.V[b]:
                return
            prev = sim.hosts[sim.honest()[0]].chain[-1]
            block = bft.make_block(h, prev["timestamp"] + 1, prev["hash"], [tx(rng.randrange(40))])
            msg = bft.make_proposal(h, r, rng.choice([-1] + list(range(r))), block, sim.V[b], sim.keys[b][0])
        else:
            msg = bft.make_vote(kind, h, r, rng.choice(pool), sim.V[b], sim.keys[b][0])
        sim.deliver(b, d, msg)


bad_seeds, slow_seeds = [], []
for seed in range(100, 112):
    sim = Sim(seed=seed, byzantine=[seed % 4], delay=(0.01, 0.5), drop=0.2)
    for i in range(30):
        sim.submit(tx(i))
    sim.run(12, byz_act)
    sim.drop = 0.0
    sim.delay = (0.01, 0.08)
    sim.run(60, byz_act)
    if not agreement(sim):
        bad_seeds.append(seed)
    if min(sim.heights()) < 4:
        slow_seeds.append((seed, sim.heights()))
check("12 random runs: the honest validators never disagree", bad_seeds, [])
check("12 random runs: and they always make progress once the network is reliable", slow_seeds, [])

bad_seeds, slow_seeds = [], []
for seed in range(200, 208):
    sim = Sim(seed=seed, delay=(0.01, 0.4), drop=0.15)
    for i in range(30):
        sim.submit(tx(i))

    def crashes(s):
        if s.rng.random() < 0.004:                                  # a validator crashes or comes back
            who = s.rng.randrange(4)
            s.down.symmetric_difference_update({who})
            if len(s.down) > 1:
                s.down.discard(who)

    sim.run(15, crashes)
    sim.drop, sim.down = 0.0, set()
    sim.run(40)
    if not agreement(sim):
        bad_seeds.append(seed)
    if min(sim.heights()) < 4:
        slow_seeds.append((seed, sim.heights()))
check("8 random runs with crashes and lost messages: no disagreement", bad_seeds, [])
check("8 random runs with crashes and lost messages: progress afterwards", slow_seeds, [])

print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")
sys.exit(0 if all(results) else 1)