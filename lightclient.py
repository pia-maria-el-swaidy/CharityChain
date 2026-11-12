"""A light client for the BFT chain: it asks nodes for blocks but believes NONE of them.

Every block is checked here, from scratch:
    - it links to the previous block and its hash matches its content,
    - its commit certificate holds the signatures of more than 2/3 of the validators (so it really is final),
    - every transaction in it is signed and respects rules R1-R4.
So a node (or somebody on the network path) that lies can only make the client refuse its blocks; it cannot make the
client show a wrong balance. The client keeps the verified chain and the resulting state in memory and rebuilds them
from the genesis block when it starts. The gateway answers every question about money from this state.
"""
import copy
import threading

import requests

import bft
import rules


class _Refused(Exception):
    pass


class LightClient:
    def __init__(self, validators, urls, require_signatures=True, on_blocks=None):
        self.validators = list(validators)
        self.urls = list(urls)
        self.require_signatures = require_signatures
        self.on_blocks = on_blocks                 # called with the list of new verified blocks
        self.lock = threading.RLock()
        self.error = None                          # the last problem seen while synchronizing (None: all fine)
        self.reachable = False
        self.reset()

    def reset(self):
        with self.lock:
            self.chain = [bft.make_genesis(self.validators)]     # the genesis block is built here, never downloaded
            self.state = rules.new_state()
            self.tx_block = {}                                   # transaction id -> block index
            self.donation_memo = {}                              # donation id -> campaign slug

    # ---------------------------------------------------------------- reading the chain from the nodes

    def _append(self, block):
        """Verify a block and add it. Returns an error message or None."""
        with self.lock:
            error = bft.check_block(block, self.chain[-1], self.validators)
            if error:
                return error
            trial = copy.deepcopy(self.state)
            for t in block["transactions"]:
                problem = rules.apply(t, trial, self.require_signatures)
                if problem:
                    return f"transaction {t.get('id')}: {problem}"
            self.chain.append(block)
            self.state = trial
            for t in block["transactions"]:
                self.tx_block[t["id"]] = block["index"]
                if t["type"] == "DONATION":
                    self.donation_memo[t["id"]] = t["memo"]
            return None

    def sync(self):
        """Fetch and verify the blocks we are missing. Returns the list of blocks added."""
        added, problems, worked = [], [], False
        for url in self.urls:
            try:
                status = requests.get(url + "/status", timeout=3).json()
                worked = True
                while True:
                    with self.lock:
                        height = len(self.chain)
                    if status["height"] <= height:
                        break
                    blocks = requests.get(url + "/chain", params={"from": height}, timeout=10).json()["chain"]
                    if not blocks:
                        break
                    for block in blocks:
                        error = self._append(block)
                        if error:
                            problems.append(f"{url} sent a block that was refused (block {block.get('index')}: {error})")
                            raise _Refused()
                        added.append(block)
                break                                                # this node was good enough
            except _Refused:
                continue                                             # try the next node
            except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
                problems.append(f"{url} is not reachable or answered nonsense")
        self.reachable = worked
        self.error = problems[-1] if problems and (not worked or any("refused" in p for p in problems)) else None
        if added and self.on_blocks:
            self.on_blocks(added)
        return added

    # ---------------------------------------------------------------- questions (all answered from the verified state)

    def height(self):
        with self.lock:
            return len(self.chain)

    def status(self):
        with self.lock:
            return {"verified_height": len(self.chain), "tip": self.chain[-1]["hash"],
                    "reachable": self.reachable, "error": self.error}

    def block(self, index):
        with self.lock:
            return copy.deepcopy(self.chain[index]) if 0 <= index < len(self.chain) else None

    def trace(self, donation_id):
        with self.lock:
            return rules.trace(self.state, donation_id)

    def location(self, tx_id):
        """Where a transaction is final: block, round, number of signatures. None if it is not on the chain."""
        with self.lock:
            index = self.tx_block.get(tx_id)
            if index is None:
                return None
            block = self.chain[index]
            return {"block": index, "block_hash": block["hash"], "round": block["commit"]["round"],
                    "signatures": len(block["commit"]["signatures"]), "timestamp": block["timestamp"],
                    "blocks_since": len(self.chain) - 1 - index}

    def campaign_of(self, donation_id):
        with self.lock:
            return self.donation_memo.get(donation_id) or None

    def donations(self, charity=None, donor=None):
        """Confirmed donations, newest first, optionally of one charity or one donor (public keys)."""
        with self.lock:
            out = []
            for donation_id, d in self.state["donations"].items():
                if (charity is None or d["charity"] == charity) and (donor is None or d["donor"] == donor):
                    out.append({"id": donation_id, "donor": d["donor"], "charity": d["charity"], "amount": d["amount"],
                                "allocated": d["allocated"], "campaign": self.donation_memo.get(donation_id) or None,
                                "block": self.tx_block.get(donation_id)})
            return sorted(out, key=lambda x: -(x["block"] or 0))

    def totals(self, charity):
        """Money that came to a charity, was set aside for projects, and was spent (all confirmed)."""
        with self.lock:
            donated = allocated = spent = 0
            for d in self.state["donations"].values():
                if d["charity"] == charity:
                    donated += d["amount"]
                    allocated += d["allocated"]
            for a in self.state["allocations"].values():
                if self.state["donations"][a["donation"]]["charity"] == charity:
                    spent += a["spent"]
            return {"donated": donated, "allocated": allocated, "spent": spent,
                    "unallocated": donated - allocated, "left_to_spend": allocated - spent}

    def raised(self, charity):
        """Confirmed money per campaign slug (donations without a campaign are not listed)."""
        with self.lock:
            out = {}
            for donation_id, d in self.state["donations"].items():
                slug = self.donation_memo.get(donation_id)
                if d["charity"] == charity and slug:
                    out[slug] = out.get(slug, 0) + d["amount"]
            return out

    def allocation(self, allocation_id):
        with self.lock:
            a = self.state["allocations"].get(allocation_id)
            return dict(a, id=allocation_id) if a else None

    def expenses_with_receipt(self, receipt_hash):
        """Every expense on the chain that points to this receipt (by its hash)."""
        with self.lock:
            out = []
            for e in self.state["expenses"]:
                if e["receipt_hash"] == receipt_hash:
                    allocation = self.state["allocations"][e["allocation"]]
                    out.append({"expense": e["id"], "amount": e["amount"], "allocation": e["allocation"],
                                "project": allocation["project"], "donation": allocation["donation"],
                                "block": self.tx_block.get(e["id"])})
            return out