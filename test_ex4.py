"""Exercise 4 check: signed transactions.   Run:  python3 test_exercise4.py"""
import subprocess
import sys
import time

import requests

import core
import rules
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


# ---------------- unit checks ----------------
print("--- keys and signatures ---")
priv_a, pub_a = wallet.generate_keypair()
priv_b, pub_b = wallet.generate_keypair()
check("public key is 64 hex characters", wallet.is_public_key(pub_a), True)
check("a name is not a public key", wallet.is_public_key("charity-hope"), False)
sig = wallet.sign(priv_a, "hello")
check("signature is 128 hex characters", len(sig), 128)
check("verify with the right key", wallet.verify(pub_a, "hello", sig), True)
check("verify fails for another message", wallet.verify(pub_a, "hello!", sig), False)
check("verify fails with another key", wallet.verify(pub_b, "hello", sig), False)
check("verify does not crash on garbage", wallet.verify("zz", "hello", "xx"), False)


def make(id, type, sender_keys, parent, amount, memo="", sign_with=None):
    """Build a signed transaction. sender_keys=(private, public); sign_with overrides the signing key."""
    priv, pub = sender_keys
    tx = {"id": id, "type": type, "sender": pub, "parent": parent, "amount": amount, "memo": memo}
    return wallet.sign_transaction(tx, sign_with or priv)


ali, hope, eve = (priv_a, pub_a), wallet.generate_keypair(), wallet.generate_keypair()
hope_pub, eve_pub = hope[1], eve[1]
receipt = core.sha256_hex("receipt-001")

print("\n--- rules with require_signatures=True ---")
state = rules.new_state()


def apply(tx):
    return rules.apply(tx, state, require_signatures=True)


check("signed donation is accepted", apply(make("d1", "DONATION", ali, hope_pub, 1000)), None)
check("signed allocation by the charity is accepted", apply(make("a1", "ALLOCATION", hope, "d1", 600, "food")), None)
err = apply({"id": "e1", "type": "EXPENSE", "sender": hope_pub, "parent": "a1", "amount": 100, "memo": receipt})
check(f"unsigned transaction is rejected [{err}]", has(err, "missing signature"), True)
err = apply(make("e1", "EXPENSE", hope, "a1", 100, receipt, sign_with=eve[0]))
check(f"expense claiming to be the charity but signed with another key [{err}]", has(err, "invalid signature"), True)
good = make("e1", "EXPENSE", hope, "a1", 100, receipt)
tampered = {**good, "amount": 500}
err = apply(tampered)
check(f"changing the amount after signing breaks the signature [{err}]", has(err, "invalid signature"), True)
err = apply(make("e2", "EXPENSE", eve, "a1", 100, receipt))
check(f"valid signature, but not the charity of this donation [{err}]", has(err, "wrong sender"), True)
err = apply(make("d2", "DONATION", ali, "charity-hope", 10))
check(f"donation must name the charity by public key [{err}]", has(err, "public key"), True)
err = apply({**make("d3", "DONATION", ali, hope_pub, 10), "sender": "donor-ali"})
check(f"a plain name is not accepted as sender [{err}]", has(err, "public key"), True)
check("the genuine expense is accepted", apply(good), None)

print("\n--- signatures and the block hash ---")
b = {"index": 1, "timestamp": 1, "transactions": [good], "previous_hash": "0" * 64, "nonce": 0}
b2 = {**b, "transactions": [{**good, "signature": "0" * 128}]}
check("the block hash commits to the signature", core.hash_block(b) != core.hash_block(b2), True)
check("unsigned transaction text is unchanged (old test vectors stay valid)",
      core.tx_to_string({"id": "d1", "type": "DONATION", "sender": "donor-ali", "parent": "charity-hope",
                         "amount": 1000, "memo": ""}), "d1:DONATION:donor-ali:charity-hope:1000:")

print("\n--- the Blockchain class ---")
bc = core.Blockchain(require_signatures=True)
check("unsigned donation refused", bc.add_transaction(
    {"id": "d1", "type": "DONATION", "sender": ali[1], "parent": hope_pub, "amount": 5, "memo": ""}) is not None, True)
check("signed donation accepted", bc.add_transaction(make("d1", "DONATION", ali, hope_pub, 5)), None)
bc.mine()
check("mined chain is valid with signatures required", core.valid_chain(bc.chain, require_signatures=True), True)

# ---------------- two real nodes ----------------
print("\n--- two nodes, default settings (signatures required) ---")
PORTS = [5501, 5502]
URL = {p: f"http://localhost:{p}" for p in PORTS}
procs = [subprocess.Popen([sys.executable, "node.py", "--port", str(p), "--no-broadcast"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for p in PORTS]
try:
    for p in PORTS:
        for _ in range(50):
            try:
                requests.get(URL[p] + "/chain", timeout=1)
                break
            except requests.RequestException:
                time.sleep(0.2)
    for p in PORTS:
        requests.post(URL[p] + "/nodes/register", json={"nodes": list(URL.values())})
    a, b_ = URL[5501], URL[5502]

    def post(url, tx):
        return requests.post(url + "/transactions/new", json=tx)

    check("/ says signatures are required", requests.get(a + "/").json()["signatures_required"], True)
    check("signed donation -> 201", post(a, make("d1", "DONATION", ali, hope_pub, 1000)).status_code, 201)
    check("signed allocation -> 201", post(a, make("a1", "ALLOCATION", hope, "d1", 600, "food")).status_code, 201)
    requests.get(a + "/mine")
    r = post(a, make("e1", "EXPENSE", hope, "a1", 250, receipt, sign_with=eve[0]))
    check(f"expense signed with the WRONG key -> 422 [{r.json().get('error')}]", r.status_code, 422)
    r = post(a, {"id": "e1", "type": "EXPENSE", "sender": hope_pub, "parent": "a1", "amount": 250, "memo": receipt})
    check(f"unsigned expense -> 422 [{r.json().get('error')}]", r.status_code, 422)
    check("expense signed with the right key -> 201",
          post(a, make("e1", "EXPENSE", hope, "a1", 250, receipt)).status_code, 201)
    requests.get(a + "/mine")
    check("node B resolves and validates",
          (requests.get(b_ + "/nodes/resolve").json()["replaced"], requests.get(b_ + "/validate").json()["valid"]),
          (True, True))
    t = requests.get(b_ + "/trace/d1").json()
    check("trace on B: 600 allocated, 250 spent", (t["allocated"], t["spent_total"]), (600, 250))
    check("the charity in the trace is a public key", t["donation"]["charity"], hope_pub)

    print("\n--- an attacker forges blocks with correct hashes but no valid signature ---")
    forged = {"id": "e9", "type": "EXPENSE", "sender": hope_pub, "parent": "a1", "amount": 100, "memo": receipt}
    check("forge 1 (unsigned expense in a well-mined block)", requests.post(b_ + "/forge", json=forged).status_code, 200)
    check("the attacker's own /validate says false", requests.get(b_ + "/validate").json()["valid"], False)
    stolen = make("e8", "EXPENSE", hope, "a1", 100, receipt, sign_with=eve[0])
    requests.post(b_ + "/forge", json=stolen)
    check("honest node A refuses the longer forged chain", requests.get(a + "/nodes/resolve").json()["replaced"], False)
    check("trace on A is unchanged", requests.get(a + "/trace/d1").json()["spent_total"], 250)
finally:
    for pr in procs:
        pr.terminate()
        pr.wait()

print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")