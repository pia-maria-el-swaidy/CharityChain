"""Exercise 5 check: background mining and orphan recovery.   Run:  python3 test_exercise5.py"""
import subprocess
import sys
import time

import requests

results = []
procs = []


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


def start(ports, *extra):
    """Start nodes (unsigned experiments), register them with each other, return their URLs."""
    urls = {p: f"http://127.0.0.1:{p}" for p in ports}
    for p in ports:
        procs.append(subprocess.Popen([sys.executable, "node.py", "--no-signatures", "--port", str(p), *extra],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for p in ports:
        for _ in range(50):
            try:
                requests.get(urls[p] + "/chain", timeout=1)
                break
            except requests.RequestException:
                time.sleep(0.2)
    for p in ports:
        requests.post(urls[p] + "/nodes/register", json={"nodes": list(urls.values())})
    return urls


def stop_all():
    for pr in procs:
        pr.terminate()
    for pr in procs:
        pr.wait()
    procs.clear()


def length(url):
    return requests.get(url + "/chain").json()["length"]


def pending_ids(url):
    return [t["id"] for t in requests.get(url + "/pending").json()["pending"]]


def wait_for(condition, seconds=15):
    end = time.time() + seconds
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.1)
    return False


def mine_wait(url):
    return requests.get(url + "/mine?wait=1")


def sync_state(urls):
    """5xx1 builds d1, a1, e1 (3 blocks); the others adopt it."""
    first, *others = list(urls.values())
    requests.post(first + "/transactions/new", json=tx("d1", "DONATION", "donor-ali", "charity-hope", 1000))
    requests.post(first + "/transactions/new", json=tx("a1", "ALLOCATION", "charity-hope", "d1", 600, "food"))
    mine_wait(first)
    requests.post(first + "/transactions/new", json=tx("e1", "EXPENSE", "charity-hope", "a1", 250, "r1"))
    mine_wait(first)
    for u in others:
        requests.get(u + "/nodes/resolve")


try:
    # ---------------------------------------------------------------- 1
    print("--- 1. /mine returns at once, the node stays responsive, /mine/status reports the result ---")
    n = start([5801], "--no-broadcast", "--mining-delay", "2")[5801]
    t0 = time.time()
    requests.get(n + "/chain")
    base = time.time() - t0
    print(f"     (a plain request takes {base:.2f} s)")
    if base > 0.5:
        print("     WARNING: every request is slow, so the timing checks below are meaningless. On Windows use\n"
              "     127.0.0.1 instead of localhost (localhost tries IPv6 first, which costs about 2 s per request).")
    check("d1 accepted", requests.post(n + "/transactions/new",
          json=tx("d1", "DONATION", "donor-ali", "charity-hope", 1000)).status_code, 201)
    t0 = time.time()
    r = requests.get(n + "/mine")
    check(f"/mine answers at once with 202 ({time.time() - t0:.2f} s)", (r.status_code, time.time() - t0 < 1.0), (202, True))
    check("status says 'mining'", requests.get(n + "/mine/status").json()["state"], "mining")
    t0 = time.time()
    check("/chain answers during mining", (length(n), time.time() - t0 < 1.0), (1, True))
    check("a new transaction is accepted during mining", requests.post(n + "/transactions/new",
          json=tx("d2", "DONATION", "donor-sara", "charity-hope", 500)).status_code, 201)
    r = requests.get(n + "/mine")
    check(f"a second /mine is refused while one is running [{r.json().get('error')}]", r.status_code, 409)
    check("the job finishes", wait_for(lambda: requests.get(n + "/mine/status").json()["state"] == "done"), True)
    st = requests.get(n + "/mine/status").json()
    check("the block holds only the transaction that was pending at the start", [t["id"] for t in st["block"]["transactions"]], ["d1"])
    check("chain length is now 2", length(n), 2)
    check("d2 (submitted during mining) is still pending", pending_ids(n), ["d2"])
    r = mine_wait(n)
    check("/mine?wait=1 still waits and answers 200 with the block", (r.status_code, r.json()["block"]["index"]), (200, 2))
    check("nothing pending -> 409", requests.get(n + "/mine").status_code, 409)
    stop_all()

    # ---------------------------------------------------------------- 2
    print("\n--- 2. a job whose chain was replaced meanwhile is discarded, its transactions stay pending ---")
    urls = start([5802, 5803], "--no-broadcast", "--mining-delay", "3")
    x, y = urls[5802], urls[5803]
    requests.post(y + "/transactions/new", json=tx("y1", "DONATION", "donor-y", "charity-hope", 10))
    mine_wait(y)                                                   # y: length 2 (waits 3 s)
    requests.post(x + "/transactions/new", json=tx("x1", "DONATION", "donor-x", "charity-hope", 20))
    check("x starts mining", requests.get(x + "/mine").status_code, 202)
    r = requests.get(x + "/nodes/resolve").json()
    check("meanwhile x adopts y's longer chain", (r["replaced"], r["length"]), (True, 2))
    check("the job ends as 'stale'", wait_for(lambda: requests.get(x + "/mine/status").json()["state"] == "stale"), True)
    check("x1 is still pending and the chain was not extended by the stale block", (pending_ids(x), length(x)), (["x1"], 2))
    r = mine_wait(x)
    check("x mines x1 on top of the new chain", (r.status_code, length(x)), (200, 3))
    stop_all()

    # ---------------------------------------------------------------- 3
    print("\n--- 3. orphan recovery: donation d2 of Part 4 is not lost any more ---")
    urls = start([5811, 5812, 5813], "--no-broadcast")
    a, b, c = urls[5811], urls[5812], urls[5813]
    requests.post(a + "/transactions/new", json=tx("d1", "DONATION", "donor-ali", "charity-hope", 1000))
    requests.post(a + "/transactions/new", json=tx("a1", "ALLOCATION", "charity-hope", "d1", 600, "food"))
    mine_wait(a)
    requests.post(a + "/transactions/new", json=tx("e1", "EXPENSE", "charity-hope", "a1", 250, "r1"))
    mine_wait(a)
    requests.post(b + "/transactions/new", json=tx("d2", "DONATION", "donor-sara", "charity-hope", 500))
    mine_wait(b)
    check("lengths 3, 2, 1 (the fork of Part 4)", [length(u) for u in (a, b, c)], [3, 2, 1])
    check("/trace/d2 works on 5812 before resolving", requests.get(b + "/trace/d2").status_code, 200)
    r = requests.get(b + "/nodes/resolve").json()
    print("     resolve on 5812:", r)
    check("5812 adopts the longer chain and reports d2 as requeued", (r["replaced"], r.get("requeued")), (True, ["d2"]))
    check("d2 is back in the pending list", pending_ids(b), ["d2"])
    check("/trace/d2 is 404 on 5812 until it is mined again", requests.get(b + "/trace/d2").status_code, 404)
    mine_wait(b)
    check("5812 mines d2 into the new chain (length 4)", length(b), 4)
    requests.get(a + "/nodes/resolve")
    requests.get(c + "/nodes/resolve")
    check("everybody has the donation d2 in the ledger",
          [requests.get(u + "/trace/d2").json()["donation"]["amount"] for u in (a, b, c)], [500, 500, 500])
    stop_all()

    # ---------------------------------------------------------------- 4
    print("\n--- 4. an orphan that is no longer valid is dropped, with the reason ---")
    urls = start([5821, 5822, 5823], "--no-broadcast")
    a, b, c = urls[5821], urls[5822], urls[5823]
    sync_state(urls)
    check("all three nodes are synchronized (length 3)", [length(u) for u in (a, b, c)], [3, 3, 3])
    requests.post(a + "/transactions/new", json=tx("a2", "ALLOCATION", "charity-hope", "d1", 300, "school"))
    mine_wait(a)
    requests.post(a + "/transactions/new", json=tx("d9", "DONATION", "donor-z", "charity-hope", 5))
    mine_wait(a)                                                   # a: length 5
    requests.post(b + "/transactions/new", json=tx("a3", "ALLOCATION", "charity-hope", "d1", 300, "health"))
    mine_wait(b)                                                   # b: length 4, competing allocation
    r = requests.get(b + "/nodes/resolve").json()
    print("     resolve on 5822:", r)
    check("5822 adopts the longer chain", (r["replaced"], r["length"]), (True, 5))
    check("a3 is NOT requeued", r.get("requeued"), None)
    reasons = {d["id"]: d["reason"] for d in r.get("dropped", [])}
    check(f"a3 is dropped: over-allocated [{reasons.get('a3')}]", "over-allocated" in reasons.get("a3", ""), True)
    check("the pending list of 5822 is empty", pending_ids(b), [])
    stop_all()

    # ---------------------------------------------------------------- 5
    print("\n--- 5. --auto-mine: nobody calls /mine ---")
    p1 = start([5831], "--auto-mine", "1")[5831]
    p2 = start([5832])[5832]
    for u in (p1, p2):
        requests.post(u + "/nodes/register", json={"nodes": [p1, p2]})
    requests.post(p1 + "/transactions/new", json=tx("d1", "DONATION", "donor-ali", "charity-hope", 1000))
    check("the block appears by itself", wait_for(lambda: length(p1) == 2, 10), True)
    check("the peer follows (broadcast) without /nodes/resolve", wait_for(lambda: length(p2) == 2, 10), True)
finally:
    stop_all()

print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")