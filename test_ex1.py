"""Exercise 1 check: mining on one node updates the others without calling /nodes/resolve.
Run:  python3 test_exercise1.py"""
import subprocess
import sys
import time

import requests

PORTS = [5201, 5202, 5203]
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


def length(p):
    return requests.get(URL[p] + "/chain").json()["length"]


procs = {p: subprocess.Popen([sys.executable, "node.py", "--port", str(p)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for p in PORTS}
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

    n1 = URL[5201]
    requests.post(n1 + "/transactions/new", json=tx("d1", "DONATION", "donor-ali", "charity-hope", 1000))
    requests.post(n1 + "/transactions/new", json=tx("a1", "ALLOCATION", "charity-hope", "d1", 600, "food-packages"))
    r = requests.get(n1 + "/mine").json()
    print("   /mine response keys:", sorted(r.keys()))
    check("response lists the notified peers", r["notified_peers"], [URL[5202], URL[5203]])
    check("the node did not notify itself", URL[5201] in r["notified_peers"], False)
    check("lengths after mining on 5201 (no manual resolve)", [length(p) for p in PORTS], [2, 2, 2])
    chains = [requests.get(URL[p] + "/chain").json()["chain"] for p in PORTS]
    check("chains are identical", chains[0] == chains[1] == chains[2], True)
    check("/trace/d1 works on 5203 without any manual resolve",
          requests.get(URL[5203] + "/trace/d1").json()["allocated"], 600)

    # a different node can mine too, and everybody follows
    requests.post(URL[5202] + "/transactions/new", json=tx("d2", "DONATION", "donor-sara", "charity-hope", 500))
    requests.get(URL[5202] + "/mine")
    check("mining on 5202 updates 5201 and 5203", [length(p) for p in PORTS], [3, 3, 3])

    # fault tolerance: a dead peer must not break mining
    procs[5203].terminate()
    procs[5203].wait()
    requests.post(n1 + "/transactions/new", json=tx("d3", "DONATION", "donor-eve", "charity-hope", 50))
    r = requests.get(n1 + "/mine")
    check("mining still works with a dead peer", r.status_code, 200)
    check("dead peer is reported as unreachable", r.json()["unreachable_peers"], [URL[5203]])
    check("live peer was still notified", r.json()["notified_peers"], [URL[5202]])
    check("5202 has the new block", length(5202), 4)
finally:
    for pr in procs.values():
        pr.terminate()

print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")