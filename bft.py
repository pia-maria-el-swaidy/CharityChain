"""TraceChain - Stage 2 consensus: BFT with quorum voting (a simplified Tendermint).

Validators: a fixed list N = 3f+1 (4 validators tolerate f = 1 faulty or malicious one).
A block is final when it carries a COMMIT CERTIFICATE: the signatures of more than 2/3 of the validators
(3 of 4) on the vote "PRECOMMIT | height | round | block hash". There are no forks: two different blocks
cannot both get 3 of 4 signatures unless two validators sign twice, and an honest validator never does.
Anybody can check a certificate without trusting the node that gave it to them.

For every height the validators run rounds. In round r the validator number (height - 1 + r) mod N proposes a block.

    PROPOSE    the proposer sends its block (signed)
    PREVOTE    everybody votes for the block if it is valid (and compatible with its lock), otherwise for "nil"
    PRECOMMIT  when more than 2/3 prevoted the same block, everybody locks on it and precommits it
    COMMIT     when more than 2/3 precommitted the same block, it is final; the precommits are its certificate

If nothing happens before a timeout (the proposer is down or slow, votes are missing) the validators vote "nil"
and move to round r+1 with the next proposer. The lock rule (a validator that precommitted a block only prevotes that
block later, unless it sees a newer majority for another one) is what keeps the network safe across rounds.
The engine in this file does no network and no disk access: the node (node_bft.py) feeds it messages and time.

What this fixes compared with Proof of Authority:
    - a crashed proposer no longer stops the chain: after the timeout the next validator proposes;
    - a validator that signs two different blocks cannot split the network, and its double signing
      is recorded together with the two signatures as proof.
"""
import copy
import re

import core
import rules
import wallet

PROPOSAL, PREVOTE, PRECOMMIT = "PROPOSAL", "PREVOTE", "PRECOMMIT"
NIL = "nil"
PROPOSE = "propose"                       # steps inside a round (PREVOTE and PRECOMMIT are also step names)
MAX_TXS_PER_BLOCK = 500
MAX_FUTURE_SECONDS = 300
BODY_FIELDS = ("index", "timestamp", "transactions", "previous_hash", "hash")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


# ---------------------------------------------------------------- thresholds

def quorum(n: int) -> int:
    """More than 2/3 of the validators: 3 of 4, 5 of 7."""
    return 2 * n // 3 + 1


def skip_threshold(n: int) -> int:
    """More than 1/3 of the validators: at least one of them is honest."""
    return n // 3 + 1


def max_faulty(n: int) -> int:
    return (n - 1) // 3


def proposer_for(height: int, round_: int, validators: list) -> str:
    return validators[(height - 1 + round_) % len(validators)]


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


# ---------------------------------------------------------------- blocks

def block_header(block: dict) -> str:
    """index|timestamp|previous_hash|tx_data|extra   (extra: the validator list in the genesis block, 'bft' otherwise)"""
    tx_data = ",".join(core.tx_to_string(t) for t in block["transactions"])
    extra = ",".join(block["validators"]) if "validators" in block else "bft"
    return f"{block['index']}|{block['timestamp']}|{block['previous_hash']}|{tx_data}|{extra}"


def hash_block(block: dict) -> str:
    return core.sha256_hex(block_header(block))


def make_genesis(validators: list) -> dict:
    block = {"index": 0, "timestamp": 0, "transactions": [], "previous_hash": "0" * 64,
             "validators": list(validators)}
    block["hash"] = hash_block(block)
    return block


def make_block(index: int, timestamp: int, previous_hash: str, transactions: list) -> dict:
    """The body of a block: what a proposer proposes. The commit certificate is added when it is final."""
    block = {"index": index, "timestamp": timestamp, "transactions": list(transactions),
             "previous_hash": previous_hash}
    block["hash"] = hash_block(block)
    return block


def body_shape_error(block, with_commit=False):
    fields = set(BODY_FIELDS) | ({"commit"} if with_commit else set())
    if not isinstance(block, dict) or set(block.keys()) != fields:
        return f"a block must have exactly the fields {sorted(fields)}"
    if not _is_int(block["index"]) or block["index"] < 1:
        return "invalid index"
    if not _is_int(block["timestamp"]):
        return "timestamp must be an integer"
    if not isinstance(block["transactions"], list) or not all(isinstance(t, dict) for t in block["transactions"]):
        return "transactions must be a list of objects"
    for name in ("previous_hash", "hash"):
        if not isinstance(block[name], str):
            return f"{name} must be a string"
    return None


def body_error(block, previous: dict, with_commit=False):
    """Everything about a block except the certificate and the business rules."""
    error = body_shape_error(block, with_commit)
    if error:
        return error
    if block["index"] != previous["index"] + 1:
        return f"wrong index: expected {previous['index'] + 1}"
    if block["previous_hash"] != previous["hash"]:
        return "previous_hash does not match the previous block"
    if block["timestamp"] < previous["timestamp"]:
        return "the timestamp goes backwards"
    if not 1 <= len(block["transactions"]) <= MAX_TXS_PER_BLOCK:
        return f"a block must hold between 1 and {MAX_TXS_PER_BLOCK} transactions"
    try:
        computed = hash_block(block)
    except (KeyError, TypeError, AttributeError):
        return "a transaction in the block is malformed"
    if computed != block["hash"]:
        return "the hash does not match the content of the block"
    return None


# ---------------------------------------------------------------- votes, proposals, certificates

def vote_text(kind: str, height: int, round_: int, value: str) -> str:
    return f"{kind}|{height}|{round_}|{value}"


def proposal_text(height: int, round_: int, valid_round: int, block_hash: str) -> str:
    return f"{PROPOSAL}|{height}|{round_}|{valid_round}|{block_hash}"


def make_vote(kind, height, round_, value, public, private) -> dict:
    return {"type": kind, "height": height, "round": round_, "hash": value, "validator": public,
            "signature": wallet.sign(private, vote_text(kind, height, round_, value))}


def make_proposal(height, round_, valid_round, block, public, private) -> dict:
    return {"type": PROPOSAL, "height": height, "round": round_, "valid_round": valid_round, "block": block,
            "validator": public, "signature": wallet.sign(private, proposal_text(height, round_, valid_round, block["hash"]))}


def message_error(msg, validators: list):
    """Is this a well-formed message, signed by a validator (and, for a proposal, by the right proposer)?"""
    if not isinstance(msg, dict):
        return "a message must be an object"
    kind = msg.get("type")
    if kind == PROPOSAL:
        fields = {"type", "height", "round", "valid_round", "block", "validator", "signature"}
    elif kind in (PREVOTE, PRECOMMIT):
        fields = {"type", "height", "round", "hash", "validator", "signature"}
    else:
        return "unknown message type"
    if set(msg.keys()) != fields:
        return "wrong fields"
    if not _is_int(msg["height"]) or msg["height"] < 1 or not _is_int(msg["round"]) or msg["round"] < 0:
        return "invalid height or round"
    if not isinstance(msg["validator"], str) or msg["validator"] not in validators:
        return "the sender is not a validator"
    if not isinstance(msg["signature"], str):
        return "signature must be a string"
    if kind == PROPOSAL:
        if not _is_int(msg["valid_round"]) or not -1 <= msg["valid_round"] < msg["round"]:
            return "invalid valid_round"
        block = msg["block"]
        error = body_shape_error(block)
        if error:
            return f"invalid block: {error}"
        if block["index"] != msg["height"]:
            return "the block is not for this height"
        if proposer_for(msg["height"], msg["round"], validators) != msg["validator"]:
            return "not the proposer of this round"
        try:
            if hash_block(block) != block["hash"]:
                return "the block hash does not match its content"
        except (KeyError, TypeError, AttributeError):
            return "a transaction in the block is malformed"
        text = proposal_text(msg["height"], msg["round"], msg["valid_round"], block["hash"])
    else:
        value = msg["hash"]
        if not isinstance(value, str) or not (value == NIL or HEX64.match(value)):
            return "a vote is for a block hash or for nil"
        text = vote_text(kind, msg["height"], msg["round"], value)
    if not wallet.verify(msg["validator"], text, msg["signature"]):
        return "invalid signature"
    return None


def make_certificate(round_: int, precommits: list, validators: list) -> dict:
    sigs = sorted(({"validator": m["validator"], "signature": m["signature"]} for m in precommits),
                  key=lambda s: validators.index(s["validator"]))
    return {"round": round_, "signatures": sigs}


def certificate_error(block: dict, validators: list):
    """A block is final if more than 2/3 of the validators signed 'PRECOMMIT | height | round | hash'."""
    cert = block.get("commit")
    if not isinstance(cert, dict) or set(cert.keys()) != {"round", "signatures"}:
        return "missing or malformed commit certificate"
    if not _is_int(cert["round"]) or cert["round"] < 0 or not isinstance(cert["signatures"], list):
        return "malformed commit certificate"
    text = vote_text(PRECOMMIT, block["index"], cert["round"], block["hash"])
    seen = set()
    for s in cert["signatures"]:
        if not isinstance(s, dict) or set(s.keys()) != {"validator", "signature"} \
                or not isinstance(s["validator"], str) or not isinstance(s["signature"], str):
            return "malformed commit certificate"
        if s["validator"] not in validators:
            return "the certificate is signed by someone who is not a validator"
        if s["validator"] in seen:
            return "the certificate counts a validator twice"
        if not wallet.verify(s["validator"], text, s["signature"]):
            return "the certificate holds an invalid signature"
        seen.add(s["validator"])
    needed = quorum(len(validators))
    if len(seen) < needed:
        return f"the certificate has {len(seen)} signatures, {needed} are needed"
    return None


def check_block(block, previous: dict, validators: list):
    """A final block: well formed, linked to the previous one, with a valid certificate."""
    error = body_error(block, previous, with_commit=True)
    return error or certificate_error(block, validators)


def block_proposer(block: dict, validators: list) -> int:
    """Index of the validator who proposed the round in which the block was committed."""
    return validators.index(proposer_for(block["index"], block["commit"]["round"], validators))


def chain_error(chain, validators: list, require_signatures: bool = True):
    """None if the whole chain is valid: genesis, every block with its certificate, and a replay of all rules."""
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


def valid_prefix(chain, validators: list, require_signatures: bool = True) -> int:
    """How many blocks (genesis included) at the start of the chain are valid. 0 if even the genesis is not."""
    try:
        if not isinstance(chain, list) or not chain or chain[0] != make_genesis(validators):
            return 0
    except (KeyError, TypeError, AttributeError):
        return 0
    state = rules.new_state()
    for i in range(1, len(chain)):
        try:
            if check_block(chain[i], chain[i - 1], validators):
                return i
            trial = copy.deepcopy(state)
            for t in chain[i]["transactions"]:
                if rules.apply(t, trial, require_signatures):
                    return i
            state = trial
        except (KeyError, TypeError, AttributeError, IndexError, ValueError):
            return i
    return len(chain)


def evidence_error(item: dict):
    """Check the proof of double signing found in a node's evidence list. None if the proof is genuine."""
    try:
        first, second = item["signed"]
        if first["hash"] == second["hash"] and first.get("valid_round") == second.get("valid_round"):
            return "the two signed messages are the same"
        for s in (first, second):
            if item["type"] == PROPOSAL:
                text = proposal_text(item["height"], item["round"], s["valid_round"], s["hash"])
            else:
                text = vote_text(item["type"], item["height"], item["round"], s["hash"])
            if not wallet.verify(item["validator_key"], text, s["signature"]):
                return "a signature does not match"
        return None
    except (KeyError, TypeError, ValueError):
        return "malformed evidence"


# ---------------------------------------------------------------- the consensus engine

class Timeouts:
    """How long to wait in each step; the waiting grows with the round, so that a slow network is eventually enough."""

    def __init__(self, propose=2.0, prevote=1.0, precommit=1.0, delta=0.5):
        self.base = {"propose": propose, "prevote": prevote, "precommit": precommit}
        self.delta = delta

    def duration(self, kind: str, round_: int) -> float:
        return self.base[kind] + self.delta * round_


class Engine:
    """The rules of the protocol for one validator. No network, no disk, no threads.

    The host (the node) provides:
        host.has_pending()   do we hold transactions that could go into a block?
        host.next_block()    a block body to propose, or None
        host.is_valid(block) is this block acceptable (links to our tip, rules R1-R4, signatures)?
        host.on_decide(block) the block is final: append it (the block already carries its certificate)
    and calls start_height(), tick(now), on_message(msg, now). What the engine wants to send is in take_outbox()."""

    def __init__(self, validators: list, host, private_key: str, timeouts=None):
        self.validators = list(validators)
        self.n = len(validators)
        self.quorum = quorum(self.n)
        self.skip = skip_threshold(self.n)
        self.host = host
        self.key = private_key
        self.me = wallet.public_key_of(private_key)
        if self.me not in self.validators:
            raise ValueError("the key is not a validator's key")
        self.timeouts = timeouts or Timeouts()
        self.outbox = []
        self.evidence = []
        self.future = {}
        self.dirty = False
        self.height = None
        self._reset(None)

    # ---- per-height state
    def _reset(self, height):
        self.height = height
        self.round, self.step, self.active = -1, None, False
        self.locked_value, self.locked_round = None, -1      # the block we precommitted, and in which round
        self.valid_value, self.valid_round = None, -1        # the last block we saw a majority of prevotes for
        self.proposals, self.prevotes, self.precommits = {}, {}, {}
        self.blocks, self.valid_cache, self.max_round = {}, {}, {}
        self.seen_signatures = set()                         # every message already handled at this height
        self.timers = []
        self.prevote_timer, self.precommit_timer, self.polka = set(), set(), set()

    def start_height(self, height: int, now: float):
        """Begin the work on a new height (after a block was finalized or learned from a peer)."""
        self._reset(height)
        for h in [h for h in self.future if h < height]:
            del self.future[h]
        for msg in self.future.pop(height, []):
            self._ingest(msg)
        self.dirty = True

    # ---- helpers
    def _valid(self, block) -> bool:
        key = block["hash"]
        if key not in self.valid_cache:
            self.valid_cache[key] = bool(self.host.is_valid(block))
        return self.valid_cache[key]

    @staticmethod
    def _count(votes, value) -> int:
        """How many validators voted for `value`. A validator that signed several different votes in one round
        (a cheater) counts for each of them: two different majorities still need an honest validator in common,
        who votes only once, so this is safe, and it is needed so that everybody can see the same majorities."""
        return sum(1 for per_validator in (votes or {}).values() if value in per_validator)

    def _schedule(self, kind, round_, now):
        self.timers.append((now + self.timeouts.duration(kind, round_), kind, self.height, round_))

    def take_outbox(self) -> list:
        out, self.outbox = self.outbox, []
        return out

    def own_messages(self) -> list:
        """Everything we signed at the current height (sent again from time to time, in case somebody missed it)."""
        out = [m for r, m in sorted(self.proposals.items()) if m["validator"] == self.me]
        for table in (self.prevotes, self.precommits):
            for r, votes in sorted(table.items()):
                out += list(votes.get(self.me, {}).values())
        return out

    # ---- messages
    def _ingest(self, msg) -> str:
        """Store a verified message. Returns 'new', 'dup' or 'conflict' (the sender already signed something different)."""
        kind, r, v = msg["type"], msg["round"], msg["validator"]
        self.seen_signatures.add(msg["signature"])
        if r > self.max_round.get(v, -1):
            self.max_round[v] = r
        if kind == PROPOSAL:
            self.blocks[msg["block"]["hash"]] = msg["block"]
            old = self.proposals.get(r)
            if old is None:
                self.proposals[r] = msg
                return "new"
            if old["block"]["hash"] != msg["block"]["hash"] or old["valid_round"] != msg["valid_round"]:
                self._record_evidence(old, msg)
                return "conflict"
            return "dup"
        signed = (self.prevotes if kind == PREVOTE else self.precommits).setdefault(r, {}).setdefault(v, {})
        if msg["hash"] in signed:
            return "dup"
        first = next(iter(signed.values()), None)
        signed[msg["hash"]] = msg                           # kept: every vote counts for its own value
        if first is None:
            return "new"
        self._record_evidence(first, msg)
        return "conflict"

    def _record_evidence(self, first, second):
        def signed(m):
            return {"hash": m["block"]["hash"] if m["type"] == PROPOSAL else m["hash"],
                    "valid_round": m.get("valid_round"), "signature": m["signature"]}
        item = {"kind": "double-signing", "validator": self.validators.index(second["validator"]),
                "validator_key": second["validator"], "height": second["height"], "round": second["round"],
                "type": second["type"], "signed": [signed(first), signed(second)]}
        same = [e for e in self.evidence if (e["validator_key"], e["height"], e["round"], e["type"]) ==
                (item["validator_key"], item["height"], item["round"], item["type"])]
        if not same:
            self.evidence = (self.evidence + [item])[-100:]
            self.dirty = True

    def _is_duplicate(self, msg) -> bool:
        """Already handled? (Also true for a conflicting message we have already passed on: no endless echo.)"""
        try:
            return msg["height"] == self.height and msg["signature"] in self.seen_signatures
        except (KeyError, TypeError):
            return False

    def on_message(self, msg, now: float):
        """Handle a message from another validator. Returns None, or the reason why it was ignored."""
        if self.height is None:
            return "not started"
        if self._is_duplicate(msg):
            return None
        error = message_error(msg, self.validators)
        if error:
            return error
        if msg["height"] < self.height:
            return "old height"
        if msg["validator"] == self.me:
            return None
        if msg["height"] > self.height:                       # we are behind: keep it, the node will fetch the blocks
            bucket = self.future.setdefault(msg["height"], [])
            if len(bucket) < 200 and msg not in bucket:
                bucket.append(msg)
            return "future height"
        status = self._ingest(msg)
        if status != "dup":
            self.dirty = True
            self.outbox.append(msg)                           # pass it on: everybody sees the same votes (and the proofs)
        self.tick(now)
        return None

    def _emit(self, msg):
        self.outbox.append(msg)
        self._ingest(msg)
        self.dirty = True

    def _vote(self, kind, round_, value):
        table = self.prevotes if kind == PREVOTE else self.precommits
        if self.me in table.get(round_, {}):                  # never sign two different votes for one round and step
            return
        self._emit(make_vote(kind, self.height, round_, value, self.me, self.key))

    # ---- rounds
    def tick(self, now: float):
        if self.height is None:
            return
        if not self.active and (self.host.has_pending() or any(self._valid(p["block"]) for p in self.proposals.values())):
            self.active = True
            self._start_round(0, now)
        due = sorted(t for t in self.timers if t[0] <= now)
        self.timers = [t for t in self.timers if t[0] > now]
        for _, kind, height, round_ in due:
            self._on_timeout(kind, height, round_, now)
        self.process(now)

    def _start_round(self, round_, now):
        self.round, self.step = round_, PROPOSE
        self.dirty = True
        if proposer_for(self.height, round_, self.validators) == self.me:
            if self.valid_value is not None:
                block, valid_round = self.valid_value, self.valid_round     # propose again what has a majority
            else:
                block, valid_round = self.host.next_block(), -1
            if block is not None:
                self._emit(make_proposal(self.height, round_, valid_round, block, self.me, self.key))
                return
        self._schedule("propose", round_, now)

    def _on_timeout(self, kind, height, round_, now):
        if height != self.height or not self.active:
            return
        if kind == "propose" and round_ == self.round and self.step == PROPOSE:
            self._vote(PREVOTE, round_, NIL)
            self.step = PREVOTE
        elif kind == "prevote" and round_ == self.round and self.step == PREVOTE:
            self._vote(PRECOMMIT, round_, NIL)
            self.step = PRECOMMIT
        elif kind == "precommit" and round_ == self.round:
            self._start_round(round_ + 1, now)

    def process(self, now):
        for _ in range(1000):
            if not self._rules(now):
                return

    def _rules(self, now) -> bool:
        if self._rule_decide(now):
            return True
        if not self.active:
            return False
        r = self.round
        return (self._rule_propose(r) or self._rule_propose_again(r) or self._rule_prevote_timer(r, now)
                or self._rule_polka(r) or self._rule_nil_polka(r) or self._rule_precommit_timer(r, now)
                or self._rule_skip(now))

    def _rule_decide(self, now) -> bool:
        """More than 2/3 precommitted the same block (in any round): it is final."""
        for r in sorted(self.precommits):
            counts = {}
            for per_validator in self.precommits[r].values():
                for value in per_validator:
                    counts[value] = counts.get(value, 0) + 1
            for value, count in counts.items():
                if value != NIL and count >= self.quorum and value in self.blocks and self._valid(self.blocks[value]):
                    certificate = make_certificate(r, [per[value] for per in self.precommits[r].values() if value in per],
                                                   self.validators)
                    block = dict(self.blocks[value], commit=certificate)
                    height = self.height
                    self.host.on_decide(block)
                    self.start_height(height + 1, now)
                    return True
        return False

    def _rule_propose(self, r) -> bool:
        """A new proposal arrives: prevote it, unless it is invalid or we are locked on another block."""
        p = self.proposals.get(r)
        if p is None or self.step != PROPOSE or p["valid_round"] != -1:
            return False
        block = p["block"]
        ok = self._valid(block) and (self.locked_round == -1 or self.locked_value["hash"] == block["hash"])
        self._vote(PREVOTE, r, block["hash"] if ok else NIL)
        self.step = PREVOTE
        return True

    def _rule_propose_again(self, r) -> bool:
        """A block proposed again, with proof (valid_round) that a majority prevoted it in an earlier round."""
        p = self.proposals.get(r)
        if p is None or self.step != PROPOSE:
            return False
        vr, block = p["valid_round"], p["block"]
        if not 0 <= vr < r or self._count(self.prevotes.get(vr), block["hash"]) < self.quorum:
            return False
        ok = self._valid(block) and (self.locked_round <= vr or self.locked_value["hash"] == block["hash"])
        self._vote(PREVOTE, r, block["hash"] if ok else NIL)
        self.step = PREVOTE
        return True

    def _rule_prevote_timer(self, r, now) -> bool:
        if self.step != PREVOTE or r in self.prevote_timer or len(self.prevotes.get(r, {})) < self.quorum:
            return False
        self.prevote_timer.add(r)
        self._schedule("prevote", r, now)
        return True

    def _rule_polka(self, r) -> bool:
        """More than 2/3 prevoted the proposal: lock on it and precommit it."""
        p = self.proposals.get(r)
        if p is None or r in self.polka or self.step == PROPOSE:
            return False
        block = p["block"]
        if self._count(self.prevotes.get(r), block["hash"]) < self.quorum or not self._valid(block):
            return False
        self.polka.add(r)
        if self.step == PREVOTE:
            self.locked_value, self.locked_round = block, r
            self._vote(PRECOMMIT, r, block["hash"])
            self.step = PRECOMMIT
        self.valid_value, self.valid_round = block, r
        return True

    def _rule_nil_polka(self, r) -> bool:
        if self.step != PREVOTE or self._count(self.prevotes.get(r), NIL) < self.quorum:
            return False
        self._vote(PRECOMMIT, r, NIL)
        self.step = PRECOMMIT
        return True

    def _rule_precommit_timer(self, r, now) -> bool:
        if r in self.precommit_timer or len(self.precommits.get(r, {})) < self.quorum:
            return False
        self.precommit_timer.add(r)
        self._schedule("precommit", r, now)
        return True

    def _rule_skip(self, now) -> bool:
        """More than 1/3 of the validators are already in a later round: join them."""
        rounds = sorted(self.max_round.values(), reverse=True)
        if len(rounds) < self.skip or rounds[self.skip - 1] <= self.round:
            return False
        self._start_round(rounds[self.skip - 1], now)
        return True

    # ---- saving and restoring (a validator must not forget its votes and its lock when it restarts)
    def snapshot(self) -> dict:
        return {"height": self.height, "round": self.round, "step": self.step, "active": self.active,
                "locked_value": self.locked_value, "locked_round": self.locked_round,
                "valid_value": self.valid_value, "valid_round": self.valid_round,
                "proposals": {str(r): m for r, m in self.proposals.items()},
                "prevotes": {str(r): v for r, v in self.prevotes.items()},
                "precommits": {str(r): v for r, v in self.precommits.items()},
                "evidence": self.evidence}

    def restore(self, snap: dict, now: float) -> bool:
        """Reload a snapshot made at the same height. Returns False (and changes nothing) if it does not fit."""
        try:
            if snap["height"] != self.height:
                return False
            self.evidence = list(snap.get("evidence", []))
            self.round, self.step, self.active = snap["round"], snap["step"], snap["active"]
            self.locked_value, self.locked_round = snap["locked_value"], snap["locked_round"]
            self.valid_value, self.valid_round = snap["valid_value"], snap["valid_round"]
            self.proposals = {int(r): m for r, m in snap["proposals"].items()}
            self.prevotes = {int(r): v for r, v in snap["prevotes"].items()}
            self.precommits = {int(r): v for r, v in snap["precommits"].items()}
        except (KeyError, TypeError, ValueError):
            return False
        for m in self.proposals.values():
            self.blocks[m["block"]["hash"]] = m["block"]
            self.seen_signatures.add(m["signature"])
        for v in (self.locked_value, self.valid_value):
            if v is not None:
                self.blocks[v["hash"]] = v
        restored = [(r, m) for r, m in self.proposals.items()]
        for table in (self.prevotes, self.precommits):
            for r, votes in table.items():
                restored += [(r, m) for per in votes.values() for m in per.values()]
        for r, m in restored:
            self.seen_signatures.add(m["signature"])
            if r > self.max_round.get(m["validator"], -1):
                self.max_round[m["validator"]] = r
        if self.active and self.step in (PROPOSE, PREVOTE, PRECOMMIT):
            self._schedule({PROPOSE: "propose", PREVOTE: "prevote", PRECOMMIT: "precommit"}[self.step], self.round, now)
        return True