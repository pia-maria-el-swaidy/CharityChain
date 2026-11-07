"""Part 3 check. Starts 3 nodes, runs the curl scenario of Part 3 plus a small
register/resolve test, then stops the nodes.   Run:  python3 test_node.py"""
import subprocess
import sys
import time

import requests

PORTS = [5101, 5102, 5103]          # different from 5001-5003 so it never clashes with your own nodes
URL = {p: f"http://localhost:{p}" for p in PORTS}
results = []


def check(name, got, expected):
    ok = got == expected
    print(("OK   " if ok else "FAIL ") + name)
    if not ok:
        print("   got:     ", got)
        print("   expected:", expected)
    results.append(ok)


def tx(id, type, sender, parent, amount, memo=""):
    return {"id": id, "type": type, "sender": sender, "parent": parent,
            "amount": amount, "memo": memo}


procs = [subprocess.Popen([sys.executable, "node.py", "--port", str(p)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for p in PORTS]
try:
    for p in PORTS:                  # wait until the nodes answer
        for _ in range(50):
            try:
                requests.get(URL[p] + "/chain", timeout=1)
                break
            except requests.RequestException:
                time.sleep(0.2)

    n1 = URL[PORTS[0]]
    d1 = tx("d1", "DONATION", "donor-ali", "charity-hope", 1000)
    a1 = tx("a1", "ALLOCATION", "charity-hope", "d1", 600, "food-packages")

    # --- the scenario of Part 3 ---
    r = requests.get(n1 + "/chain").json()
    check("fresh node: chain has only the genesis block", r["length"], 1)
    check("POST d1 -> 201", requests.post(n1 + "/transactions/new", json=d1).status_code, 201)
    check("POST a1 -> 201", requests.post(n1 + "/transactions/new", json=a1).status_code, 201)
    m = requests.get(n1 + "/mine")
    check("GET /mine -> 200", m.status_code, 200)
    check("chain now has 2 blocks", requests.get(n1 + "/chain").json()["length"], 2)
    t = requests.get(n1 + "/trace/d1").json()
    check("trace d1: allocated 600", t["allocated"], 600)
    check("trace d1: unallocated 400", t["unallocated"], 400)

    # --- error codes ---
    check("missing field -> 400", requests.post(n1 + "/transactions/new",
          json={"id": "x1", "type": "DONATION"}).status_code, 400)
    check("not JSON -> 400", requests.post(n1 + "/transactions/new", data="hello").status_code, 400)
    over = requests.post(n1 + "/transactions/new", json=tx("a2", "ALLOCATION", "charity-hope", "d1", 500, "school"))
    check("rule broken -> 422", over.status_code, 422)
    print("     reason:", over.json()["error"])
    check("nothing pending -> 409", requests.get(n1 + "/mine").status_code, 409)
    check("unknown donation -> 404", requests.get(n1 + "/trace/zzz").status_code, 404)
    check("/validate -> true", requests.get(n1 + "/validate").json(), {"valid": True})

    # --- peers and consensus ---
    peers = {"nodes": list(URL.values())}
    for p in PORTS:
        r = requests.post(URL[p] + "/nodes/register", json=peers)
        check(f"register on {p} -> 201", r.status_code, 201)
    check("a node does not list itself as a peer", len(r.json()["peers"]), 2)
    n2, n3 = URL[PORTS[1]], URL[PORTS[2]]
    r = requests.get(n2 + "/nodes/resolve").json()
    check("resolve on node 2 adopts the longer chain", r, {"replaced": True, "length": 2})
    r = requests.get(n2 + "/nodes/resolve").json()
    check("resolve again -> nothing to replace", r["replaced"], False)
    check("node 3 resolves too", requests.get(n3 + "/nodes/resolve").json()["replaced"], True)
    same = [requests.get(URL[p] + "/chain").json()["chain"] for p in PORTS]
    check("all three chains are identical", same[0] == same[1] == same[2], True)
    check("trace is the same on node 3", requests.get(n3 + "/trace/d1").json() == t, True)

    # --- lab-only endpoints ---
    check("tamper -> 200", requests.post(n3 + "/tamper", json={"block": 1, "tx": 0, "amount": 5000}).status_code, 200)
    check("validate after tamper -> false", requests.get(n3 + "/validate").json(), {"valid": False})
    check("tamper bad block -> 404", requests.post(n3 + "/tamper", json={"block": 9, "tx": 0, "amount": 1}).status_code, 404)
    check("forge -> 200", requests.post(n2 + "/forge", json=tx("e5", "EXPENSE", "charity-hope", "a1", 5000, "ab")).status_code, 200)
    check("hash-only validate accepts the forged chain", requests.get(n2 + "/validate").json(), {"valid": True})
finally:
    for pr in procs:
        pr.terminate()

print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")