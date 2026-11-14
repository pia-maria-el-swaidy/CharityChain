"""Exercise 3 check: self-healing node.   Run:  python3 test_exercise3.py"""
import subprocess
import sys
import time

import requests

import core

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


def start(ports, extra):
    procs = [subprocess.Popen([sys.executable, "node.py", "--port", str(p), "--no-broadcast", *extra],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for p in ports]
    for p in ports:
        for _ in range(50):
            try:
                requests.get(f"http://localhost:{p}/chain", timeout=1)
                break
            except requests.RequestException:
                time.sleep(0.2)
    return procs


def run_attack_a(base, extra):
    ports = [base, base + 1, base + 2]
    url = {p: f"http://localhost:{p}" for p in ports}
    procs = start(ports, extra)
    try:
        for p in ports:
            requests.post(url[p] + "/nodes/register", json={"nodes": list(url.values())})
        n1, n2, n3 = (url[p] for p in ports)
        requests.post(n1 + "/transactions/new", json=tx("d1", "DONATION", "donor-ali", "charity-hope", 1000))
        requests.post(n1 + "/transactions/new", json=tx("a1", "ALLOCATION", "charity-hope", "d1", 600, "food-packages"))
        requests.get(n1 + "/mine")
        requests.post(n1 + "/transactions/new", json=tx("e1", "EXPENSE", "charity-hope", "a1", 250, core.sha256_hex("receipt-001")))
        requests.get(n1 + "/mine")
        requests.get(n2 + "/nodes/resolve")
        requests.get(n3 + "/nodes/resolve")

        requests.post(n3 + "/tamper", json={"block": 2, "tx": 0, "amount": 20})
        valid_after_tamper = requests.get(n3 + "/validate").json()["valid"]
        for i in (3, 4):
            requests.post(n3 + "/transactions/new", json=tx(f"d{i}", "DONATION", "donor-eve", "charity-hope", 100))
            requests.get(n3 + "/mine")
        len_before = requests.get(n3 + "/chain").json()["length"]
        refused = requests.get(n1 + "/nodes/resolve").json()["replaced"]
        resolve = requests.get(n3 + "/nodes/resolve").json()
        out = {"valid_after_tamper": valid_after_tamper, "len_before": len_before,
               "5001_replaced": refused, "resolve": resolve,
               "valid_after_resolve": requests.get(n3 + "/validate").json()["valid"],
               "same_chain_as_5001": requests.get(n3 + "/chain").json()["chain"] == requests.get(n1 + "/chain").json()["chain"],
               "trace_status": requests.get(n3 + "/trace/d1").status_code}
        # a healed node is a normal node again: it can take and mine a new transaction
        r = requests.post(n3 + "/transactions/new", json=tx("d9", "DONATION", "donor-new", "charity-hope", 10))
        out["new_tx_status"] = r.status_code
        return out
    finally:
        for pr in procs:
            pr.terminate()
            pr.wait()


print("--- Attack A before Exercise 3 (--no-heal) ---")
old = run_attack_a(5401, ["--no-heal"])
check("tampering is detected", old["valid_after_tamper"], False)
check("5003 has the longest chain (5)", old["len_before"], 5)
check("5001 refuses 5003's chain", old["5001_replaced"], False)
check("5003 keeps its corrupted chain", old["resolve"], {"replaced": False, "length": 5})
check("/validate on 5003 is still false", old["valid_after_resolve"], False)

print("\n--- Attack A after Exercise 3 (default) ---")
new = run_attack_a(5411, [])
check("tampering is detected", new["valid_after_tamper"], False)
check("5001 still refuses 5003's chain", new["5001_replaced"], False)
check("resolve on 5003 replaces its chain (with a shorter, valid one)", new["resolve"], {"replaced": True, "length": 3})
check("/validate on 5003 must return true", new["valid_after_resolve"], True)
check("5003 now has exactly the chain of 5001", new["same_chain_as_5001"], True)
check("/trace/d1 works again on 5003", new["trace_status"], 200)
check("healed node accepts new transactions", new["new_tx_status"], 201)

print("\n--- Edge case: no peer has a valid chain ---")
ports = [5421, 5422]
url = {p: f"http://localhost:{p}" for p in ports}
procs = start(ports, [])
try:
    for p in ports:
        requests.post(url[p] + "/nodes/register", json={"nodes": list(url.values())})
        requests.post(url[p] + "/transactions/new", json=tx("d1", "DONATION", "donor-ali", "charity-hope", 1000))
        requests.get(url[p] + "/mine")
        requests.post(url[p] + "/tamper", json={"block": 1, "tx": 0, "amount": 1})
    r = requests.get(url[5421] + "/nodes/resolve").json()
    check("nothing valid to adopt -> replaced is false", r["replaced"], False)
    check("node stays invalid (and does not crash)", requests.get(url[5421] + "/validate").json()["valid"], False)
finally:
    for pr in procs:
        pr.terminate()
        pr.wait()

print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")