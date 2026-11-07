"""Exercise 2 check: rule-aware valid_chain.   Run:  python3 test_exercise2.py"""
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


# --- unit checks on malformed data from a peer ---
check("valid_chain([]) is False", core.valid_chain([]), False)
check("valid_chain on garbage is False (no crash)", core.valid_chain([{"x": 1}, {"y": 2}]), False)
check("valid_chain on the genesis alone is True", core.valid_chain([core.make_genesis()]), True)


def run_attack_b(base, extra):
    """Start 3 nodes, sync them, forge on the third, return what the honest nodes did."""
    ports = [base, base + 1, base + 2]
    url = {p: f"http://localhost:{p}" for p in ports}
    procs = [subprocess.Popen([sys.executable, "node.py", "--port", str(p), "--no-broadcast", *extra],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for p in ports]
    try:
        for p in ports:
            for _ in range(50):
                try:
                    requests.get(url[p] + "/chain", timeout=1)
                    break
                except requests.RequestException:
                    time.sleep(0.2)
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
        before = requests.get(n1 + "/trace/d1").json()

        requests.post(n3 + "/forge", json=tx("e5", "EXPENSE", "charity-hope", "a1", 5000, core.sha256_hex("fake")))
        valid_on_attacker = requests.get(n3 + "/validate").json()["valid"]
        requests.post(n3 + "/forge", json=tx("d5", "DONATION", "donor-eve", "charity-hope", 100))
        replaced = [requests.get(n + "/nodes/resolve").json()["replaced"] for n in (n1, n2)]
        trace_after = requests.get(n1 + "/trace/d1")
        return {"valid_on_attacker": valid_on_attacker, "replaced": replaced,
                "length_on_1": requests.get(n1 + "/chain").json()["length"],
                "trace_status": trace_after.status_code, "trace_unchanged": trace_after.json() == before}
    finally:
        for pr in procs:
            pr.terminate()
            pr.wait()


print("\n--- Attack B with the old hash-only validation (nodes started with --hash-only) ---")
old = run_attack_b(5301, ["--hash-only"])
check("hash-only: 5003's forged chain looks valid", old["valid_on_attacker"], True)
check("hash-only: 5001 and 5002 adopt it", old["replaced"], [True, True])
check("hash-only: /trace/d1 on 5001 is now broken", old["trace_status"], 500)

print("\n--- Attack B with rule-aware validation (default) ---")
new = run_attack_b(5311, [])
check("5003's chain must be invalid", new["valid_on_attacker"], False)
check("5001 and 5002 refuse it", new["replaced"], [False, False])
check("5001 keeps its length (3)", new["length_on_1"], 3)
check("/trace/d1 on 5001 is unchanged", new["trace_status"] == 200 and new["trace_unchanged"], True)

print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")