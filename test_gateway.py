"""End-to-end tests of the API gateway with real processes: 4 validators + 1 observer, the receipt service, the gateway.
Run:  python3 test_gateway.py            (about 2 minutes)"""
import copy
import hashlib
import http.server
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

import requests

import wallet

HERE = os.path.dirname(os.path.abspath(__file__))
results = []


def check(name, got, expected):
    ok = got == expected
    print(("OK   " if ok else "FAIL ") + name)
    if not ok:
        print("   got:     ", got)
        print("   expected:", expected)
    results.append(ok)


def has(text, part):
    return text is not None and part in str(text)


def wait_for(condition, seconds=20):
    end = time.time() + seconds
    while time.time() < end:
        try:
            if condition():
                return True
        except requests.RequestException:
            pass
        time.sleep(0.1)
    return False


KEYS = [wallet.generate_keypair() for _ in range(4)]
V = [k[1] for k in KEYS]
procs = {}


def launch(name, cmd, env=None):
    full = dict(os.environ, **(env or {}))
    procs[name] = subprocess.Popen(cmd, cwd=HERE, env=full, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(name):
    p = procs.pop(name, None)
    if p:
        p.terminate()
        p.wait()


def up(url, path="/status"):
    return wait_for(lambda: requests.get(url + path, timeout=1).ok, 20)


NODE_PORTS = [6501 + i for i in range(5)]
NODE_URLS = [f"http://127.0.0.1:{p}" for p in NODE_PORTS]
RECEIPTS = "http://127.0.0.1:6510"
GATEWAY = "http://127.0.0.1:6520"
ADMIN = ("admin@example.org", "admin-password-1")


def start_node(i):
    env = {"PORT": str(NODE_PORTS[i]), "HOST": "127.0.0.1", "PEERS": ",".join(NODE_URLS), "SELF_URL": NODE_URLS[i],
           "VALIDATORS": ",".join(V), "TIMEOUT_PROPOSE": "0.6", "TIMEOUT_PREVOTE": "0.3", "TIMEOUT_PRECOMMIT": "0.3",
           "TIMEOUT_DELTA": "0.2", "SYNC_INTERVAL": "1"}
    if i < 4:
        env["VALIDATOR_KEY"] = KEYS[i][0]
    launch(f"node{i}", [sys.executable, "node_bft.py"], env)
    assert up(NODE_URLS[i]), f"node {i} did not start"


def start_gateway(name, port, db_path, data_dir, chain_urls, node_urls=None):
    env = {"PORT": str(port), "HOST": "127.0.0.1", "DATABASE_URL": f"sqlite:///{db_path}", "GATEWAY_DATA_DIR": data_dir,
           "VALIDATORS": ",".join(V), "NODE_URLS": ",".join(node_urls or NODE_URLS[:4]), "CHAIN_URLS": ",".join(chain_urls),
           "RECEIPTS_URL": RECEIPTS, "RECEIPT_API_KEY": "test-key", "ADMIN_EMAIL": ADMIN[0], "ADMIN_PASSWORD": ADMIN[1],
           "INDEX_INTERVAL": "0.3"}
    env.pop("DATABASE_URL_NODE", None)
    launch(name, [sys.executable, "gateway.py"], env)
    assert up(f"http://127.0.0.1:{port}", "/api/health"), f"{name} did not start"


def api(method, path, token=None, base=GATEWAY, **kw):
    headers = dict(kw.pop("headers", {}))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.request(method, base + path, headers=headers, timeout=20, **kw)


def register(email, name, role, **extra):
    return api("POST", "/api/auth/register", json=dict(email=email, name=name, role=role, password="correct-horse-1", **extra))


def login(email, password="correct-horse-1"):
    r = api("POST", "/api/auth/login", json={"email": email, "password": password})
    return r.json().get("token") if r.status_code == 200 else None


def confirmed(tx_id, seconds=20):
    return wait_for(lambda: api("GET", f"/api/transactions/{tx_id}").json().get("status") == "confirmed", seconds)


def fake_pdf(text):
    return b"%PDF-1.4\n% fake receipt for the tests: " + text.encode() + b"\n%%EOF\n"


with tempfile.TemporaryDirectory() as tmp:
    db_path, data_dir = os.path.join(tmp, "gateway.db"), os.path.join(tmp, "gw")
    receipt_dir = os.path.join(tmp, "receipts")
    try:
        for i in range(5):
            start_node(i)
        launch("receipts", [sys.executable, "receipt_service.py"], {"PORT": "6510", "RECEIPT_DIR": receipt_dir, "RECEIPT_API_KEY": "test-key"})
        assert up(RECEIPTS, "/health")
        start_gateway("gateway", 6520, db_path, data_dir, [NODE_URLS[4]])

        # =============================================================================================
        print("=== 1. accounts and security ===")
        r = register("ali@example.org", "Ali", "donor")
        check("a donor registers", (r.status_code, r.json()["user"]["role"], len(r.json()["user"]["public_key"])), (201, "donor", 64))
        r = register("hope@example.org", "Hope Foundation", "charity", description="Flood relief and food packages")
        check("a charity registers and is told it must be verified", (r.status_code, has(r.json()["note"], "verify")), (201, True))
        r = register("other@example.org", "Other Aid", "charity", description="Another charity")
        check("a second charity registers", r.status_code, 201)
        check("a weak password is refused", api("POST", "/api/auth/register", json={"email": "x@y.org", "name": "X", "role": "donor", "password": "short"}).status_code, 400)
        check("a bad email is refused", register("not-an-email", "X", "donor").status_code, 400)
        check("nobody can register as admin", register("evil@example.org", "Evil", "admin").status_code, 400)
        check("a charity needs a description", register("nd@example.org", "ND", "charity").status_code, 400)
        check("a second account with the same email is refused", register("ALI@example.org", "Ali 2", "donor").status_code, 409)
        check("a wrong password", api("POST", "/api/auth/login", json={"email": "ali@example.org", "password": "wrong-password"}).status_code, 401)
        check("an unknown email gives the same answer", api("POST", "/api/auth/login", json={"email": "nobody@example.org", "password": "x"}).status_code, 401)
        register("lock@example.org", "Lock", "donor")
        for _ in range(5):
            api("POST", "/api/auth/login", json={"email": "lock@example.org", "password": "wrong"})
        check("after 5 wrong passwords the account is locked for a minute", api("POST", "/api/auth/login", json={"email": "lock@example.org", "password": "correct-horse-1"}).status_code, 429)
        donor, hope = login("ali@example.org"), login("hope@example.org")
        admin = login(*ADMIN)
        check("the three logins work", all([donor, hope, admin]), True)
        check("no token: 401", api("GET", "/api/me").status_code, 401)
        check("a made-up token: 401", api("GET", "/api/me", token="x" * 40).status_code, 401)
        me = api("GET", "/api/me", token=donor).json()
        check("/api/me shows the account and no secret", ("private_key_enc" in me, "password_hash" in me, "private" in json.dumps(me)), (False, False, False))
        check("a donor cannot use the admin interface", api("GET", "/api/admin/charities", token=donor).status_code, 403)
        check("an anonymous visitor cannot either", api("GET", "/api/admin/charities").status_code, 401)
        con = sqlite3.connect(db_path)
        stored = con.execute("SELECT password_hash, private_key_enc FROM users WHERE email = 'ali@example.org'").fetchone()
        con.close()
        check("the password is stored hashed, the private key encrypted",
              ("correct-horse-1" in stored[0], len(stored[1]) != 64 and stored[1].startswith("gAAAA")), (False, True))
        out = api("POST", "/api/auth/logout", token=donor)
        check("logout ends the session", (out.status_code, api("GET", "/api/me", token=donor).status_code), (200, 401))
        donor = login("ali@example.org")

        # =============================================================================================
        print("\n=== 2. the directory: a charity must be verified ===")
        hope_id = api("GET", "/api/me", token=hope).json()["id"]
        check("an unverified charity is not listed", api("GET", "/api/charities").json()["charities"], [])
        check("it cannot create a campaign yet", api("POST", "/api/campaigns", token=hope, json={"slug": "flood-relief", "title": "Flood relief", "description": "x"}).status_code, 403)
        r = api("POST", "/api/donations", token=donor, json={"charity_id": hope_id, "amount": 100})
        check(f"nobody can donate to it yet [{r.json().get('error')}]", r.status_code, 409)
        check("only an admin can verify", api("POST", f"/api/admin/charities/{hope_id}/verify", token=hope).status_code, 403)
        pending = api("GET", "/api/admin/charities", token=admin).json()["charities"]
        check("the admin sees both charities, unverified", sorted((c["name"], c["verified"]) for c in pending), [("Hope Foundation", False), ("Other Aid", False)])
        check("the admin verifies Hope", api("POST", f"/api/admin/charities/{hope_id}/verify", token=admin).status_code, 200)
        listed = api("GET", "/api/charities").json()["charities"]
        check("now it is listed, with empty totals", [(c["name"], c["totals"]["donated"]) for c in listed], [("Hope Foundation", 0)])
        r = api("POST", "/api/campaigns", token=hope, json={"slug": "flood-relief", "title": "Flood relief 2026", "description": "Food and shelter", "goal": 5000})
        check("it creates a campaign", r.status_code, 201)
        check("a bad slug is refused", api("POST", "/api/campaigns", token=hope, json={"slug": "Flood Relief!", "title": "x", "description": "x"}).status_code, 400)
        check("a slug can be used once", api("POST", "/api/campaigns", token=hope, json={"slug": "flood-relief", "title": "x", "description": "x"}).status_code, 409)
        check("the campaign is public", [c["slug"] for c in api("GET", "/api/campaigns").json()["campaigns"]], ["flood-relief"])
        check("a donation to a campaign that does not exist is refused", api("POST", "/api/donations", token=donor, json={"charity_id": hope_id, "amount": 5, "campaign": "nothing"}).status_code, 400)

        # =============================================================================================
        print("\n=== 3. the money flow: donation -> allocation -> expense with a receipt ===")
        t0 = time.time()
        r = api("POST", "/api/donations", token=donor, json={"charity_id": hope_id, "amount": 1000, "campaign": "flood-relief"})
        check("the donation is accepted as pending", (r.status_code, r.json()["status"]), (202, "pending"))
        d1 = r.json()["tx_id"]
        ok = confirmed(d1)
        check(f"and final {time.time() - t0:.2f} s after the click (validators' voting + the gateway reading and verifying the block)", ok, True)
        status = api("GET", f"/api/transactions/{d1}").json()
        check("finality: signed by at least 3 validators, decided in round 0", (status["location"]["signatures"] >= 3, status["location"]["round"]), (True, 0))

        r = api("POST", "/api/allocations", token=hope, json={"donation_id": d1, "amount": 600, "project": "food-packages"})
        check("the charity sets 600 aside for food packages", r.status_code, 202)
        a1 = r.json()["tx_id"]
        check("the allocation is final", confirmed(a1), True)

        pdf = fake_pdf("rice 50 kg, Market Co")
        r = api("POST", "/api/expenses", token=hope, data={"allocation_id": a1, "amount": "250", "description": "Rice, 50 kg, Market Co"},
                files={"file": ("rice-invoice.pdf", pdf, "application/pdf")})
        check(f"the charity records an expense of 250 with its receipt [{r.text[:80]}]", r.status_code, 202)
        e1 = r.json()["tx_id"]
        check("the expense is final", confirmed(e1), True)

        trace = api("GET", f"/api/trace/{d1}").json()
        receipt_hash = hashlib.sha256(pdf).hexdigest()
        d, a = trace["donation"], trace["allocations"][0]
        check("the trace names donor and charity and the campaign", (d["donor_name"], d["charity_name"], d["campaign"], d["amount"]), ("Ali", "Hope Foundation", "flood-relief", 1000))
        check("1000 donated, 600 allocated, 250 spent, 400 not yet allocated",
              (trace["allocated"], trace["spent_total"], trace["unallocated"], a["remaining"]), (600, 250, 400, 350))
        e = a["expenses"][0]
        check("the expense carries the receipt hash and the charity's description",
              (e["receipt"]["hash"], e["receipt"]["filename"], e["description"]), (receipt_hash, "rice-invoice.pdf", "Rice, 50 kg, Market Co"))
        check("every record says in which block it is final", all(x["location"] and x["location"]["signatures"] >= 3 for x in (d, a, e)), True)
        check("the trace says who checked it: the gateway itself", has(trace["verified"]["by"], "gateway"), True)

        card = api("GET", f"/api/charities/{hope_id}").json()
        check("the charity page: totals and the campaign's progress", (card["totals"]["donated"], card["totals"]["spent"], card["campaigns"][0]["raised"]), (1000, 250, 1000))
        check("the charity page lists the donation with the donor's name", [(x["donor_name"], x["amount"], x["unallocated"]) for x in card["recent_donations"]], [("Ali", 1000, 400)])
        check("the campaign list shows the same progress", api("GET", "/api/campaigns").json()["campaigns"][0]["raised"], 1000)
        mine = api("GET", "/api/me/transactions", token=donor).json()["transactions"]
        check("the donor's history shows the donation as confirmed", [(t["type"], t["amount"], t["status"]) for t in mine], [("DONATION", 1000, "confirmed")])

        r = api("GET", f"/api/receipts/{receipt_hash}")
        check("anybody can download the receipt, byte for byte", (r.status_code, r.content, r.headers.get("X-Receipt-Hash")), (200, pdf, receipt_hash))
        v = api("GET", f"/api/receipts/{receipt_hash}/verify").json()
        check("verify: it is on the chain, in expense e1, and the stored file matches its hash",
              (v["on_chain"], v["expenses"][0]["expense"], v["file_matches_hash"]), (True, e1, True))
        check("an unknown receipt hash", api("GET", f"/api/receipts/{'0' * 64}").status_code, 404)
        check("a malformed receipt hash", api("GET", "/api/receipts/abc").status_code, 400)
        b = api("GET", "/api/blocks/1").json()
        check("a block with its certificate", (b["verified"], len(b["signed_by_validators"]) >= 3, b["needed"]), (True, True, 3))
        check("no such block", api("GET", "/api/blocks/999").status_code, 404)
        check("unknown donation", api("GET", "/api/trace/nothing").status_code, 404)

        path = os.path.join(receipt_dir, receipt_hash[:2], receipt_hash)
        with open(path, "ab") as f:
            f.write(b"edited later")
        v = api("GET", f"/api/receipts/{receipt_hash}/verify").json()
        check("somebody edits the stored receipt file: verify says it no longer matches", (v["on_chain"], v["file_matches_hash"]), (True, False))
        check("and the gateway refuses to hand out the changed file", api("GET", f"/api/receipts/{receipt_hash}").status_code, 502)
        with open(path, "wb") as f:
            f.write(pdf)

        # =============================================================================================
        print("\n=== 4. the chain's rules and the input checks ===")
        height = api("GET", "/api/health").json()["chain"]["verified_height"]

        def send_expense(token, allocation, amount, content=None, filename="r.pdf", description="x"):
            files = {"file": (filename, content if content is not None else fake_pdf(f"r{amount}{time.time()}"), "application/pdf")} if content != "nofile" else None
            return api("POST", "/api/expenses", token=token, data={"allocation_id": allocation, "amount": str(amount), "description": description}, files=files)

        r = send_expense(hope, a1, 500)
        check(f"spending 500 when 350 is left [{r.json().get('error')}]", (r.status_code, has(r.json()["error"], "over-spent")), (422, True))
        r = api("POST", "/api/allocations", token=hope, json={"donation_id": d1, "amount": 500, "project": "shelter"})
        check(f"allocating 500 when 400 is left [{r.json().get('error')}]", (r.status_code, has(r.json()["error"], "over-allocated")), (422, True))
        other_id = [c["id"] for c in api("GET", "/api/admin/charities", token=admin).json()["charities"] if c["name"] == "Other Aid"][0]
        api("POST", f"/api/admin/charities/{other_id}/verify", token=admin)
        other = login("other@example.org")
        r = api("POST", "/api/allocations", token=other, json={"donation_id": d1, "amount": 5, "project": "mine-now"})
        check(f"another (verified) charity cannot touch Hope's donation [{r.json().get('error')}]", (r.status_code, has(r.json()["error"], "wrong sender")), (422, True))
        check("a donor cannot allocate", api("POST", "/api/allocations", token=donor, json={"donation_id": d1, "amount": 5, "project": "x"}).status_code, 403)
        check("a charity cannot donate", api("POST", "/api/donations", token=hope, json={"charity_id": hope_id, "amount": 5}).status_code, 403)
        for bad in (0, -5, "10", True, 10 ** 13, 1.5, None):
            r = api("POST", "/api/donations", token=donor, json={"charity_id": hope_id, "amount": bad})
            if r.status_code != 400:
                check(f"amount {bad!r} must be refused", r.status_code, 400)
        check("amounts 0, -5, '10', true, 10^13, 1.5 and null are all refused", True, True)
        check("a donation needs a charity", api("POST", "/api/donations", token=donor, json={"amount": 5}).status_code, 400)
        check("an unknown charity", api("POST", "/api/donations", token=donor, json={"charity_id": "nope", "amount": 5}).status_code, 404)
        check("a bad project name", api("POST", "/api/allocations", token=hope, json={"donation_id": d1, "amount": 5, "project": "Bad Name"}).status_code, 400)
        check("an expense without a file", send_expense(hope, a1, 5, content="nofile").status_code, 400)
        check("a file that is not a PDF, PNG or JPEG", send_expense(hope, a1, 5, content=b"just some text").status_code, 415)
        check("an expense without a description", send_expense(hope, a1, 5, description="   ").status_code, 400)
        check("an amount that is not a number", api("POST", "/api/expenses", token=hope, data={"allocation_id": a1, "amount": "ten", "description": "x"},
                                                     files={"file": ("r.pdf", fake_pdf("q"), "application/pdf")}).status_code, 400)
        check("a receipt of more than 5 MB", send_expense(hope, a1, 5, content=b"%PDF-1.4" + b"0" * (5 * 1024 * 1024 + 100)).status_code, 413)
        check("an expense without logging in", api("POST", "/api/expenses", data={"allocation_id": a1}).status_code, 401)
        check("an expense against an allocation that does not exist (the chain says no)", send_expense(hope, "a-0000000000", 5).status_code, 422)
        check("nothing wrong reached the chain",
              (api("GET", "/api/health").json()["chain"]["verified_height"], api("GET", f"/api/trace/{d1}").json()["spent_total"]), (height, 250))

        # =============================================================================================
        print("\n=== 5. the gateway does not trust the nodes it reads from ===")
        real = requests.get(NODE_URLS[4] + "/chain").json()["chain"]
        mode = {"value": "short"}

        class Liar(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, payload):
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith("/status"):
                    return self.reply({"height": len(real), "pending": 0})
                start = int(self.path.split("from=")[1]) if "from=" in self.path else 0
                blocks = copy.deepcopy(real[start:start + 200])
                for blk in blocks:
                    if mode["value"] == "short":
                        blk["commit"]["signatures"] = blk["commit"]["signatures"][:2]
                    elif mode["value"] == "tamper":
                        blk["transactions"][0]["amount"] = 1
                self.reply({"chain": blocks, "length": len(real)})

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 6530), Liar)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        LIAR = "http://127.0.0.1:6530"
        start_gateway("gw_liar", 6521, os.path.join(tmp, "liar.db"), os.path.join(tmp, "gw2"), [LIAR])
        G2 = "http://127.0.0.1:6521"
        time.sleep(2)
        h = api("GET", "/api/health", base=G2).json()["chain"]
        check(f"a node sends blocks with only 2 signatures: refused [{h['error']}]", (h["verified_height"], has(h["error"], "2 signatures")), (1, True))
        mode["value"] = "tamper"
        check("a node sends a block with a changed amount: refused",
              wait_for(lambda: has(api("GET", "/api/health", base=G2).json()["chain"]["error"], "hash does not match"), 10), True)
        check("the gateway shows nothing it could not verify", (api("GET", f"/api/trace/{d1}", base=G2).status_code,
                                                                   api("GET", "/api/charities", base=G2).json()["charities"]), (404, []))
        start_gateway("gw_mixed", 6522, os.path.join(tmp, "mixed.db"), os.path.join(tmp, "gw3"), [LIAR, NODE_URLS[4]])
        G3 = "http://127.0.0.1:6522"
        check("with the liar listed first and an honest node second, it still reads the true chain",
              wait_for(lambda: api("GET", "/api/health", base=G3).json()["chain"]["verified_height"] == height, 15), True)
        h = api("GET", "/api/health", base=G3).json()["chain"]
        check("and it reports that one node sent bad blocks", has(h["error"], "refused"), True)
        check("its figures are the true ones", api("GET", f"/api/trace/{d1}", base=G3).json()["spent_total"], 250)
        stop("gw_liar")
        stop("gw_mixed")
        server.shutdown()

        # =============================================================================================
        print("\n=== 6. a validator is down, the gateway restarts, a full re-read ===")
        stop("node0")                                           # the first node of NODE_URLS
        r = api("POST", "/api/donations", token=donor, json={"charity_id": hope_id, "amount": 77})
        check("a donation still goes through when the first node is down", r.status_code, 202)
        d2 = r.json()["tx_id"]
        check("and is final (3 of 4 validators are enough)", confirmed(d2, 25), True)
        before = api("GET", f"/api/trace/{d1}").json()
        mine_before = api("GET", "/api/me/transactions", token=donor).json()["transactions"]
        stop("gateway")
        start_gateway("gateway", 6520, db_path, data_dir, [NODE_URLS[4]])
        check("after a restart the old session still works", api("GET", "/api/me", token=donor).status_code, 200)
        check("the verified chain is rebuilt from the genesis block", wait_for(lambda: api("GET", "/api/health").json()["chain"]["verified_height"] >= 5, 20), True)
        check("the trace is identical", api("GET", f"/api/trace/{d1}").json(), before)
        check("the history is intact", api("GET", "/api/me/transactions", token=donor).json()["transactions"], mine_before)
        r = api("POST", "/api/donations", token=donor, json={"charity_id": hope_id, "amount": 5})
        check("and the wallets still open: a new donation works (the encryption key was kept)", r.status_code, 202)
        confirmed(r.json()["tx_id"], 25)
        counts = lambda: sorted(api("GET", "/api/me/transactions", token=donor).json()["transactions"], key=lambda t: t["tx_id"])
        before_rows = [(t["tx_id"], t["status"], t["block_height"]) for t in counts()]
        r = api("POST", "/api/admin/reindex", token=admin)
        h = api("GET", "/api/health").json()["chain"]
        check("the admin re-reads and re-verifies the whole chain", (r.status_code, r.json()["verified_blocks"], r.json()["error"]), (200, h["verified_height"] - 1, None))
        check("the database holds the same records afterwards", [(t["tx_id"], t["status"], t["block_height"]) for t in counts()], before_rows)

        for i in range(4):
            stop(f"node{i}")
        r = api("POST", "/api/donations", token=donor, json={"charity_id": hope_id, "amount": 1})
        check(f"with every validator down the gateway says so [{r.json().get('error')}]", (r.status_code, has(r.json()["error"], "not reachable")), (503, True))
        nodes = api("GET", "/api/health").json()["nodes"]
        check("the health page shows which nodes are down", list(nodes.values()), [False] * 4)
        check("reading still works from what it already verified", api("GET", f"/api/trace/{d1}").json()["spent_total"], 250)
    finally:
        for name in list(procs):
            stop(name)

print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")
sys.exit(0 if all(results) else 1)