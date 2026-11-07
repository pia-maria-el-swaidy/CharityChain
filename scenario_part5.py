"""Part 5 scenario: attack the ledger from node 5003.

Run:   python3 scenario_part5.py                    (each attack before and after its fix)
       python3 scenario_part5.py --only A
       python3 scenario_part5.py --only B --mode after --keep    (leave the nodes running for curl/screenshots)
"""
import argparse
import subprocess
import sys
import time

import requests

import core

PORTS = [5001, 5002, 5003]
URL = {p: f"http://localhost:{p}" for p in PORTS}
procs = []


def tx(id, type, sender, parent, amount, memo=""):
    return {"id": id, "type": type, "sender": sender, "parent": parent,
            "amount": amount, "memo": memo}


def start_nodes(extra=()):
    stop_nodes()
    for p in PORTS:
        procs.append(subprocess.Popen([sys.executable, "node.py", "--port", str(p), "--no-broadcast", *extra],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for p in PORTS:
        for _ in range(50):
            try:
                requests.get(URL[p] + "/chain", timeout=1)
                break
            except requests.RequestException:
                time.sleep(0.2)


def stop_nodes():
    for pr in procs:
        pr.terminate()
    for pr in procs:
        pr.wait()
    procs.clear()


def post(port, path, body):
    return requests.post(URL[port] + path, json=body)


def get(port, path):
    return requests.get(URL[port] + path)


def lengths():
    return {p: get(p, "/chain").json()["length"] for p in PORTS}


def synchronized_state(extra=()):
    """Fresh nodes, all registered and holding the same 3-block chain (the state at the end of Part 4)."""
    start_nodes(extra)
    for p in PORTS:
        post(p, "/nodes/register", {"nodes": list(URL.values())})
    post(5001, "/transactions/new", tx("d1", "DONATION", "donor-ali", "charity-hope", 1000))
    post(5001, "/transactions/new", tx("a1", "ALLOCATION", "charity-hope", "d1", 600, "food-packages"))
    get(5001, "/mine")
    post(5001, "/transactions/new", tx("e1", "EXPENSE", "charity-hope", "a1", 250, core.sha256_hex("receipt-001")))
    get(5001, "/mine")
    get(5002, "/nodes/resolve")
    get(5003, "/nodes/resolve")
    print("   synchronized state, lengths:", lengths())


def attack_a(heal):
    title = ("AFTER Exercise 3 (self-healing node)" if heal
             else "BEFORE Exercise 3 (nodes started with --no-heal)")
    print("=" * 70 + f"\nAttack A: edit a recorded expense - {title}\n" + "=" * 70)
    synchronized_state([] if heal else ["--no-heal"])
    print("\nStep 1: on 5003, change e1 (block 2, tx 0) from 250 to 20")
    print("   tamper  :", post(5003, "/tamper", {"block": 2, "tx": 0, "amount": 20}).json())
    print("   validate:", get(5003, "/validate").json(), " <- the edit is detected")
    print("\nStep 2: on 5003, submit a new donation and mine, twice")
    for i in (3, 4):
        r = post(5003, "/transactions/new", tx(f"d{i}", "DONATION", "donor-eve", "charity-hope", 100))
        print(f"   submit d{i}:", r.status_code, " mine:", get(5003, "/mine").json()["message"])
    print("   lengths:", lengths(), " <- 5003 now has the longest chain")
    print("\nStep 3: resolve on 5001")
    print("  ", get(5001, "/nodes/resolve").json(), " <- 5003's longer chain is NOT adopted")
    print("\nStep 4: resolve, then validate, on 5003")
    print("   resolve :", get(5003, "/nodes/resolve").json())
    print("   validate:", get(5003, "/validate").json())
    if heal:
        same = get(5003, "/chain").json()["chain"] == get(5001, "/chain").json()["chain"]
        print("   lengths:", lengths(), "| 5003 now has the same chain as 5001:", same)
        print("   -> 5003 healed itself (its own chain was invalid, so it accepted a valid one, even a shorter one)")
        print("      /trace/d1 on 5003:", get(5003, "/trace/d1").status_code)
    else:
        print("   -> 5003 keeps its corrupted chain, because no peer is LONGER than its own chain")


def attack_b(hash_only):
    title = ("BEFORE Exercise 2 (nodes started with --hash-only)" if hash_only
             else "AFTER Exercise 2 (rule-aware valid_chain)")
    print("\n" + "=" * 70 + f"\nAttack B: a perfectly mined lie - {title}\n" + "=" * 70)
    synchronized_state(["--hash-only"] if hash_only else [])
    trace_before = get(5001, "/trace/d1").json()
    print("\nStep 1: on 5003, forge a block with an expense of 5000 against a 600 allocation")
    r = post(5003, "/forge", tx("e5", "EXPENSE", "charity-hope", "a1", 5000, core.sha256_hex("fake-receipt")))
    print("   forge   :", r.json()["message"], "| block hash starts with", r.json()["block"]["hash"][:8])
    print("\nStep 2: validate on 5003")
    v = get(5003, "/validate").json()
    print("   validate:", v, " <- hashes and Proof of Work are correct, but the rules are broken"
          if hash_only else " <- the replay of the rules catches the overspending")
    print("\nStep 3: make 5003's chain the longest, then resolve on 5001 and 5002")
    r = post(5003, "/forge", tx("d5", "DONATION", "donor-eve", "charity-hope", 100))
    print("   forge d5:", r.json()["message"], "| lengths:", lengths())
    for p in (5001, 5002):
        print(f"   resolve on {p}:", get(p, "/nodes/resolve").json())
    print("   lengths:", lengths())
    r = get(5001, "/trace/d1")
    if hash_only:
        print("   -> honest nodes ADOPTED the forged chain")
        print(f"   /trace/d1 on 5001: {r.status_code}", r.json())
    else:
        print("   -> honest nodes REFUSED the forged chain")
        print(f"   /trace/d1 on 5001: {r.status_code}, unchanged from before the attack: {r.json() == trace_before}")
        t = r.json()
        print(f"      d1: allocated {t['allocated']}, spent {t['spent_total']}, unallocated {t['unallocated']}")
        print("   5003 itself: /validate =", get(5003, "/validate").json(),
              "(it stays corrupted until Exercise 3)")


parser = argparse.ArgumentParser()
parser.add_argument("--only", choices=["A", "B"], help="run only one attack")
parser.add_argument("--mode", choices=["both", "before", "after"], default="both",
                    help="run the attacks before the fixes (Exercises 2 and 3), after them, or both")
parser.add_argument("--keep", action="store_true", help="leave the nodes running at the end")
args = parser.parse_args()
try:
    if args.only in (None, "A"):
        if args.mode in ("both", "before"):
            attack_a(heal=False)
        if args.mode in ("both", "after"):
            attack_a(heal=True)
    if args.only in (None, "B"):
        if args.mode in ("both", "before"):
            attack_b(hash_only=True)
        if args.mode in ("both", "after"):
            attack_b(hash_only=False)
    if args.keep:
        print("\nNodes are still running on ports 5001-5003. Press Ctrl+C to stop them.")
        while True:
            time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    stop_nodes()