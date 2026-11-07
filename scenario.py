"""Part 4 scenario: 3 nodes, a fork, and consensus.

Run:   python3 scenario_part4.py          (starts 3 fresh nodes, runs steps 1-7, stops them)
       python3 scenario_part4.py --keep   (leaves the nodes running so you can use curl / take screenshots)
"""
import argparse
import subprocess
import sys
import time

import requests

import core

PORTS = [5001, 5002, 5003]
URL = {p: f"http://localhost:{p}" for p in PORTS}


def tx(id, type, sender, parent, amount, memo=""):
    return {"id": id, "type": type, "sender": sender, "parent": parent,
            "amount": amount, "memo": memo}


def submit(port, t):
    r = requests.post(URL[port] + "/transactions/new", json=t)
    assert r.status_code == 201, r.text
    print(f"   {port}: submitted {t['id']}")


def mine(port):
    r = requests.get(URL[port] + "/mine")
    assert r.status_code == 200, r.text
    print(f"   {port}: {r.json()['message']}")


def lengths():
    return {p: requests.get(URL[p] + "/chain").json()["length"] for p in PORTS}


def chain_of(port):
    return requests.get(URL[port] + "/chain").json()["chain"]


parser = argparse.ArgumentParser()
parser.add_argument("--keep", action="store_true", help="leave the nodes running at the end")
args = parser.parse_args()

procs = [subprocess.Popen([sys.executable, "node.py", "--port", str(p)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for p in PORTS]
try:
    for p in PORTS:
        for _ in range(50):
            try:
                requests.get(URL[p] + "/chain", timeout=1)
                break
            except requests.RequestException:
                time.sleep(0.2)

    print("Step 1: register the peers on every node")
    for p in PORTS:
        r = requests.post(URL[p] + "/nodes/register", json={"nodes": list(URL.values())})
        print(f"   {p}: peers = {r.json()['peers']}")

    print("\nStep 2: on 5001, submit d1 and a1, mine, submit e1, mine")
    submit(5001, tx("d1", "DONATION", "donor-ali", "charity-hope", 1000))
    submit(5001, tx("a1", "ALLOCATION", "charity-hope", "d1", 600, "food-packages"))
    mine(5001)
    submit(5001, tx("e1", "EXPENSE", "charity-hope", "a1", 250, core.sha256_hex("receipt-001")))
    mine(5001)

    print("\nStep 3: on 5002, submit a different donation d2 and mine once")
    submit(5002, tx("d2", "DONATION", "donor-sara", "charity-hope", 500))
    mine(5002)

    print("\nStep 4: chain lengths (expected 3, 2, 1)")
    print("  ", lengths())
    print("   block 1 hash on 5001:", chain_of(5001)[1]["hash"])
    print("   block 1 hash on 5002:", chain_of(5002)[1]["hash"], "  <- a different block 1: this is a fork")
    d2_before = requests.get(URL[5002] + "/trace/d2")
    print("   /trace/d2 on 5002 before resolving:", d2_before.status_code,
          "(the donation is recorded on 5002's own chain)")

    print("\nStep 5: resolve on 5002 and 5003")
    for p in (5002, 5003):
        print(f"   {p}:", requests.get(URL[p] + "/nodes/resolve").json())

    print("\nStep 6: are the chains identical? is /trace/d1 the same everywhere?")
    chains = [chain_of(p) for p in PORTS]
    print("   lengths:", lengths())
    print("   identical chains:", chains[0] == chains[1] == chains[2])
    traces = [requests.get(URL[p] + "/trace/d1").json() for p in PORTS]
    print("   identical /trace/d1:", traces[0] == traces[1] == traces[2])
    t = traces[0]
    print(f"   d1: amount {t['donation']['amount']}, allocated {t['allocated']}, "
          f"spent {t['spent_total']}, unallocated {t['unallocated']}")

    print("\nStep 7: /trace/d2 on 5002 after resolving")
    r = requests.get(URL[5002] + "/trace/d2")
    print("  ", r.status_code, r.json())
    print("   -> donation d2 was in the block that 5002 discarded, so it is gone from the ledger.")

    if args.keep:
        print("\nNodes are still running on ports 5001-5003. Press Ctrl+C to stop them.")
        while True:
            time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    for pr in procs:
        pr.terminate()