"""Stage 1 (PoA) unit checks: schedule, signed blocks, chain validation.   Run:  python3 test_poa_core.py"""
import json
import os
import subprocess
import sys
import tempfile

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


KEYS = [wallet.generate_keypair() for _ in range(4)]            # (private, public) of the 4 validators
V = [k[1] for k in KEYS]
DONOR, CHARITY, EVE = wallet.generate_keypair(), wallet.generate_keypair(), wallet.generate_keypair()
RECEIPT = core.sha256_hex("receipt-001")


def stx(id, type, who, parent, amount, memo="", signer=None):
    tx = {"id": id, "type": type, "sender": who[1], "parent": parent, "amount": amount, "memo": memo}
    return wallet.sign_transaction(tx, (signer or who)[0])


def add_block(chain, txs, ts=None, by=None, keys=KEYS):
    """Append a block written by the validator whose turn it is (or by validator `by`)."""
    height = len(chain)
    who = poa.proposer_index(height, len(keys)) if by is None else by
    ts = chain[-1]["timestamp"] + 1 if ts is None else ts
    chain.append(poa.make_block(height, ts, chain[-1]["hash"], txs, keys[who][1], keys[who][0]))
    return chain


d1 = stx("d1", "DONATION", DONOR, CHARITY[1], 1000)
a1 = stx("a1", "ALLOCATION", CHARITY, "d1", 600, "food-packages")
e1 = stx("e1", "EXPENSE", CHARITY, "a1", 250, RECEIPT)


def good_chain():
    chain = [poa.make_genesis(V)]
    add_block(chain, [d1, a1])                                   # validator 0
    add_block(chain, [e1])                                       # validator 1
    add_block(chain, [stx("d2", "DONATION", DONOR, CHARITY[1], 500)])   # validator 2
    add_block(chain, [stx("d3", "DONATION", DONOR, CHARITY[1], 20)])    # validator 3
    add_block(chain, [stx("d4", "DONATION", DONOR, CHARITY[1], 30)])    # validator 0 again
    return chain


print("--- the schedule ---")
check("blocks 1 to 8 belong to validators 0,1,2,3,0,1,2,3",
      [poa.proposer_index(h, 4) for h in range(1, 9)], [0, 1, 2, 3, 0, 1, 2, 3])
check("proposer_for gives the public key", poa.proposer_for(2, V), V[1])

print("\n--- validator list and genesis ---")
check("a good validator list", poa.validators_error(V), None)
check("empty list refused", has(poa.validators_error([]), "empty"), True)
check("a name is not a key", has(poa.validators_error(["alice"]), "not a public key"), True)
check("duplicate key refused", has(poa.validators_error([V[0], V[0]]), "twice"), True)
check("genesis is deterministic", poa.make_genesis(V), poa.make_genesis(list(V)))
check("another validator list gives another genesis",
      poa.make_genesis(V)["hash"] != poa.make_genesis(V[::-1])["hash"], True)
check("the genesis block carries the validator list", poa.make_genesis(V)["validators"], V)

print("\n--- one block ---")
g = poa.make_genesis(V)
b1 = poa.make_block(1, 10, g["hash"], [d1, a1], V[0], KEYS[0][0])
check("a block written by validator 0 at height 1 is valid", poa.check_block(b1, g, V), None)
check("it has a 64-hex hash and a 128-hex signature", (len(b1["hash"]), len(b1["signature"])), (64, 128))

wrong_turn = poa.make_block(1, 10, g["hash"], [d1, a1], V[1], KEYS[1][0])
check("validator 1 writing block 1 (not its turn) is refused",
      has(poa.check_block(wrong_turn, g, V), "wrong proposer"), True)
outsider = poa.make_block(1, 10, g["hash"], [d1, a1], EVE[1], EVE[0])
check("a non-validator is refused", has(poa.check_block(outsider, g, V), "not a validator"), True)
forged_sig = dict(b1, signature=wallet.sign(EVE[0], b1["hash"]))
check("a signature made with another key is refused",
      has(poa.check_block(forged_sig, g, V), "invalid proposer signature"), True)
edited = dict(b1, transactions=[dict(d1, amount=5000), a1])
check("changing a transaction after signing is refused",
      has(poa.check_block(edited, g, V), "hash does not match"), True)
rehashed = dict(edited)
rehashed["hash"] = poa.hash_block(rehashed)
check("recomputing the hash does not help: the signature no longer matches",
      has(poa.check_block(rehashed, g, V), "invalid proposer signature"), True)
check("wrong previous_hash is refused",
      has(poa.check_block(poa.make_block(1, 10, "0" * 63 + "1", [d1], V[0], KEYS[0][0]), g, V), "previous_hash"), True)
check("a skipped height is refused",
      has(poa.check_block(poa.make_block(2, 10, g["hash"], [d1], V[1], KEYS[1][0]), g, V), "wrong index"), True)
late = poa.make_block(1, 5, g["hash"], [d1], V[0], KEYS[0][0])
check("a timestamp before the previous block is refused",
      has(poa.check_block(late, dict(g, timestamp=9), V), "timestamp"), True)
check("an empty block is refused",
      has(poa.check_block(poa.make_block(1, 10, g["hash"], [], V[0], KEYS[0][0]), g, V), "between 1 and"), True)
many = [stx(f"m{i}", "DONATION", DONOR, CHARITY[1], 1) for i in range(poa.MAX_TXS_PER_BLOCK + 1)]
check("a block with too many transactions is refused",
      has(poa.check_block(poa.make_block(1, 10, g["hash"], many, V[0], KEYS[0][0]), g, V), "between 1 and"), True)
check("an extra field is refused", has(poa.check_block(dict(b1, nonce=0), g, V), "exactly the fields"), True)
check("garbage is refused without crashing", has(poa.check_block("nonsense", g, V), "exactly the fields"), True)

print("\n--- a whole chain ---")
chain = good_chain()
check("5 blocks written by 4 validators in turn: valid", poa.chain_error(chain, V), None)
check("the proposers are 0,1,2,3,0",
      [V.index(b["proposer"]) for b in chain[1:]], [0, 1, 2, 3, 0])

bad = good_chain()
bad[2]["transactions"][0]["amount"] = 1
check("editing an old transaction breaks the chain", has(poa.chain_error(bad, V), "block 2"), True)

check("a node with another validator list rejects the chain (genesis differs)",
      has(poa.chain_error(chain, V[::-1]), "genesis"), True)
check("an empty chain is invalid", poa.chain_error([], V) is not None, True)
check("a chain missing its genesis is invalid", poa.chain_error(chain[1:], V) is not None, True)

shuffled = good_chain()
shuffled[2], shuffled[3] = shuffled[3], shuffled[2]
check("reordering blocks is detected", poa.chain_error(shuffled, V) is not None, True)

print("\n--- the right validator, but the transactions break the rules ---")
rules_chain = [poa.make_genesis(V)]
add_block(rules_chain, [d1, a1])
over = stx("e5", "EXPENSE", CHARITY, "a1", 5000, RECEIPT)         # signed by the real charity, but too much
add_block(rules_chain, [over])
error = poa.chain_error(rules_chain, V)
check(f"overspending is refused even in a block signed by the right validator [{error}]", has(error, "over-spent"), True)

stolen = [poa.make_genesis(V)]
add_block(stolen, [d1, a1])
add_block(stolen, [stx("e6", "EXPENSE", EVE, "a1", 100, RECEIPT)])  # valid signature of Eve, but she is not the charity
check("a valid signature of someone who is not the charity is refused",
      has(poa.chain_error(stolen, V), "wrong sender"), True)

fake = [poa.make_genesis(V)]
add_block(fake, [d1, a1])
add_block(fake, [stx("e7", "EXPENSE", CHARITY, "a1", 100, RECEIPT, signer=EVE)])
check("a transaction signed with the wrong key is refused", has(poa.chain_error(fake, V), "invalid signature"), True)

unsigned = [poa.make_genesis(V)]
add_block(unsigned, [d1, {k: v for k, v in a1.items() if k != "signature"}])
check("an unsigned transaction is refused", has(poa.chain_error(unsigned, V), "missing signature"), True)
check("... unless signatures are not required", poa.chain_error(unsigned, V, require_signatures=False), None)

print("\n--- make_validators.py ---")
with tempfile.TemporaryDirectory() as tmp:
    run = subprocess.run([sys.executable, "make_validators.py", "--dir", tmp], capture_output=True, text=True)
    check("the script runs", run.returncode, 0)
    people = json.load(open(os.path.join(tmp, "validators.json")))
    check("4 validators with distinct keys", (len(people), len({p["public"] for p in people})), (4, 4))
    check("every private key matches its public key", all(wallet.public_key_of(p["private"]) == p["public"] for p in people), True)
    env = dict(line.split("=", 1) for line in open(os.path.join(tmp, ".env")).read().split())
    check(".env lists the public keys in order", env["VALIDATORS"].split(","), [p["public"] for p in people])
    check(".env has one private key per node", [env[f"VALIDATOR_KEY_{i}"] for i in range(1, 5)], [p["private"] for p in people])
    again = subprocess.run([sys.executable, "make_validators.py", "--dir", tmp], capture_output=True, text=True)
    check("it refuses to overwrite without --force", again.returncode != 0, True)

print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")