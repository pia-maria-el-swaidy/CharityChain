"""Exercise 4 demo: how a client signs transactions, and what the node does with bad ones.
Run:  python3 demo_signed.py"""
import subprocess
import sys
import time

import requests

import core
import wallet

PORT = 5601
NODE = f"http://localhost:{PORT}"

# Three people, each with a key pair. The public key is the identity on the ledger.
people = {name: wallet.generate_keypair() for name in ("ali (donor)", "hope (charity)", "mallory (attacker)")}
ali, hope, mallory = people.values()
names = {keys[1]: name for name, keys in people.items()}          # directory: public key -> display name
receipt = core.sha256_hex("receipt-001")


def tx(id, type, who, parent, amount, memo="", signed_by=None):
    base = {"id": id, "type": type, "sender": who[1], "parent": parent, "amount": amount, "memo": memo}
    return wallet.sign_transaction(base, (signed_by or who)[0])


def send(label, t):
    r = requests.post(NODE + "/transactions/new", json=t)
    detail = "" if r.status_code == 201 else f"  -> {r.json()['error']}"
    print(f"   {label:62} {r.status_code}{detail}")


proc = subprocess.Popen([sys.executable, "node.py", "--port", str(PORT)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(50):
        try:
            requests.get(NODE + "/chain", timeout=1)
            break
        except requests.RequestException:
            time.sleep(0.2)

    print("Public keys (what the ledger sees):")
    for name, keys in people.items():
        print(f"   {name:20} {keys[1][:16]}...")

    print("\nHonest activity:")
    send("ali donates 1000 to hope (signed by ali)", tx("d1", "DONATION", ali, hope[1], 1000))
    send("hope allocates 600 to food-packages (signed by hope)", tx("a1", "ALLOCATION", hope, "d1", 600, "food-packages"))
    requests.get(NODE + "/mine")

    print("\nAttacks:")
    send("mallory pretends to be hope, signs with her own key",
         tx("e1", "EXPENSE", hope, "a1", 500, receipt, signed_by=mallory))
    send("mallory sends an expense with no signature at all",
         {"id": "e1", "type": "EXPENSE", "sender": hope[1], "parent": "a1", "amount": 500, "memo": receipt})
    genuine = tx("e1", "EXPENSE", hope, "a1", 250, receipt)
    send("mallory edits the amount of hope's signed expense (250 -> 500)", {**genuine, "amount": 500})
    send("mallory signs as herself and spends from hope's allocation", tx("e1", "EXPENSE", mallory, "a1", 500, receipt))
    send("mallory names herself as the charity of a donation: a name, not a key",
         {**tx("d2", "DONATION", ali, hope[1], 10), "parent": "hope"})

    print("\nHonest activity again:")
    send("hope records the real expense of 250 (signed by hope)", genuine)
    requests.get(NODE + "/mine")

    t = requests.get(NODE + "/trace/d1").json()
    print("\nTrace of d1 (public keys replaced by display names):")
    print(f"   donor {names[t['donation']['donor']]} gave {t['donation']['amount']} to {names[t['donation']['charity']]}")
    for a in t["allocations"]:
        print(f"   allocation {a['id']} '{a['project']}': {a['amount']}, spent {a['spent']}, remaining {a['remaining']}")
        for e in a["expenses"]:
            print(f"      expense {e['id']}: {e['amount']}  receipt {e['receipt_hash'][:16]}...")
    print("\n/validate:", requests.get(NODE + "/validate").json())
finally:
    proc.terminate()
    proc.wait()