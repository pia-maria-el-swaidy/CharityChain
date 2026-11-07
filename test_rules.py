"""Part 2 check (sections 2.2 and 2.3). Run:  python3 test_rules.py"""
import core
import rules
from test_core import D1, A1, E1, RECEIPT   # reuses the 1.5 transactions (and re-runs Part 1 checks)

print("\n--- Part 2 ---")


def check(name, got, expected):
    ok = got == expected
    print(("OK   " if ok else "FAIL ") + name)
    if not ok:
        print("   got:     ", got)
        print("   expected:", expected)
    return ok


def mine_block(prev, index, ts, txs):
    b = {"index": index, "timestamp": ts, "transactions": txs,
         "previous_hash": prev["hash"], "nonce": 0}
    b["hash"] = core.proof_of_work(b)
    return b


genesis = core.make_genesis()
b1 = mine_block(genesis, 1, 1760000000, [D1, A1])
b2 = mine_block(b1, 2, 1760000100, [E1])
chain = [genesis, b1, b2]
results = []

# 2.2 trace
state, err = rules.replay(chain)
results.append(check("replay of the 1.5 chain succeeds", err, None))
expected_trace = {
    "donation": {"id": "d1", "donor": "donor-ali", "charity": "charity-hope", "amount": 1000},
    "allocated": 600, "unallocated": 400, "spent_total": 250,
    "allocations": [{"id": "a1", "project": "food-packages", "amount": 600, "spent": 250,
                     "remaining": 350,
                     "expenses": [{"id": "e1", "amount": 250, "receipt_hash": RECEIPT}]}]}
results.append(check("trace(d1) matches section 2.2", rules.trace(state, "d1"), expected_trace))
results.append(check("trace of unknown donation is None", rules.trace(state, "nope"), None))

# 2.3 rules table (each case starts from the chain of 1.5)
def submit(tx):
    return rules.validate_new_transaction(chain, [], tx)

def tx(id, type, sender, parent, amount, memo=""):
    return {"id": id, "type": type, "sender": sender, "parent": parent,
            "amount": amount, "memo": memo}

cases = [
    ("a2 amount 500 -> over-allocated", tx("a2", "ALLOCATION", "charity-hope", "d1", 500, "school"), "over-allocated (600 + 500 > 1000)"),
    ("a2 amount 400 -> accepted",       tx("a2", "ALLOCATION", "charity-hope", "d1", 400, "school"), None),
    ("e2 amount 400 -> over-spent",     tx("e2", "EXPENSE", "charity-hope", "a1", 400, RECEIPT), "over-spent (250 + 400 > 600)"),
    ("e3 wrong sender",                 tx("e3", "EXPENSE", "charity-other", "a1", 100, RECEIPT), "wrong sender"),
    ("e4 unknown parent",               tx("e4", "EXPENSE", "charity-hope", "a99", 100, RECEIPT), "unknown parent"),
    ("d1 again -> duplicate id",        D1, "duplicate id"),
    ("d9 amount 0 -> not positive",     tx("d9", "DONATION", "donor-ali", "charity-hope", 0), "amount must be positive"),
]
for name, t, expected in cases:
    got = submit(t)
    if expected is None:
        results.append(check(name, got, None))
    else:
        results.append(check(name + "  [" + str(got) + "]", got is not None and expected in got, True))

# pending transactions count too
bc = core.Blockchain()
results.append(check("pending: d1 accepted", bc.add_transaction(D1), None))
results.append(check("pending: a1 accepted", bc.add_transaction(A1), None))
err = bc.add_transaction(tx("a2", "ALLOCATION", "charity-hope", "d1", 500, "school"))
results.append(check("pending: over-allocation seen before mining [" + str(err) + "]",
                     err is not None and "over-allocated" in err, True))
results.append(check("pending: an expense on a still-pending allocation is accepted (pending counts)",
                     bc.add_transaction(tx("e1", "EXPENSE", "charity-hope", "a1", 250, RECEIPT)) is None, True))

# preview of Attack B: a correctly mined block that breaks the rules
forged = mine_block(b2, 3, 1760000200, [tx("e5", "EXPENSE", "charity-hope", "a1", 5000, RECEIPT)])
bad_chain = chain + [forged]
results.append(check("hash-only valid_chain accepts the forged chain (the problem!)",
                     core.valid_chain(bad_chain), True))
_, err = rules.replay(bad_chain)
results.append(check("replay rejects the forged chain [" + str(err) + "]", err is not None, True))

print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")