"""Part 1 check (section 1.5). Run:  python3 test_core.py"""
import copy
import core

D1 = {"id": "d1", "type": "DONATION", "sender": "donor-ali",
      "parent": "charity-hope", "amount": 1000, "memo": ""}
A1 = {"id": "a1", "type": "ALLOCATION", "sender": "charity-hope",
      "parent": "d1", "amount": 600, "memo": "food-packages"}
RECEIPT = core.sha256_hex("receipt-001")
E1 = {"id": "e1", "type": "EXPENSE", "sender": "charity-hope",
      "parent": "a1", "amount": 250, "memo": RECEIPT}


def check(name, got, expected):
    ok = got == expected
    print(("OK   " if ok else "FAIL ") + name)
    if not ok:
        print("   got:     ", got)
        print("   expected:", expected)
    return ok


results = []
results.append(check("sha256('hello')", core.sha256_hex("hello"),
    "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"))

genesis = core.make_genesis()
results.append(check("genesis hash", genesis["hash"],
    "825105cae079b2f06320d6a4e63c512f2567014869a9d3f37e7016d0f13bec54"))
results.append(check("receipt hash", RECEIPT,
    "0d0c6594dc01742802e5f5b447ba828a013fea78e7362528553da0db9c4b0118"))

b1 = {"index": 1, "timestamp": 1760000000, "transactions": [D1, A1],
      "previous_hash": genesis["hash"], "nonce": 0}
results.append(check("block 1, nonce 0", core.hash_block(b1),
    "ff6b7017e54bb6ef37aa5fcbb459d087abeb79d52b8574cb84b68774a918102c"))
b1["hash"] = core.proof_of_work(b1)
results.append(check("block 1 after PoW (nonce)", b1["nonce"], 191708))
results.append(check("block 1 after PoW (hash)", b1["hash"],
    "000055f1d629a51a0711aa4458168cb58328bfc7045b23e0b56d1827665820de"))

b2 = {"index": 2, "timestamp": 1760000100, "transactions": [E1],
      "previous_hash": b1["hash"], "nonce": 0}
b2["hash"] = core.proof_of_work(b2)
results.append(check("block 2 after PoW (nonce)", b2["nonce"], 32251))
results.append(check("block 2 after PoW (hash)", b2["hash"],
    "00007f81cd89212c673d2d939d74875350fd6790cc9cd77578e5b4d65f426ae6"))

chain = [genesis, b1, b2]
results.append(check("valid_chain on honest chain", core.valid_chain(chain), True))
tampered = copy.deepcopy(chain)
tampered[1]["transactions"][0]["amount"] = 5000
results.append(check("valid_chain after tampering", core.valid_chain(tampered), False))

bc = core.Blockchain()
results.append(check("reject bad type",
    bc.add_transaction({**D1, "type": "GIFT"}) is not None, True))
results.append(check("reject forbidden char",
    bc.add_transaction({**D1, "memo": "a:b"}) is not None, True))
results.append(check("reject missing field",
    bc.add_transaction({k: v for k, v in D1.items() if k != "memo"}) is not None, True))
results.append(check("accept good transaction", bc.add_transaction(D1), None))
block = bc.mine()
results.append(check("mine produces valid chain", core.valid_chain(bc.chain), True))

print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")