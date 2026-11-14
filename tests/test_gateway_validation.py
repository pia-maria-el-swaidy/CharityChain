"""Gateway validation tests (B3/B4). HTTP only: nothing is imported from the gateway or the chain.

Needs the gateway (GATEWAY_URL, default http://localhost:5000) and the chain nodes running.
If the gateway is not reachable, the whole module is skipped.
Every run creates its own charity, donor and campaigns, so runs do not collide.
"""
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

import pytest

BASE = os.environ.get("GATEWAY_URL", "http://localhost:5000").rstrip("/")
WAIT_SECONDS = int(os.environ.get("E2E_WAIT_SECONDS", "90"))


def call(method, path, body=None):
    """Return (http_status, parsed_json). Never raises on 4xx/5xx."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw.decode(errors="replace")}


def wait_committed(tx_id):
    deadline = time.time() + WAIT_SECONDS
    polls = 0
    while time.time() < deadline:
        code, row = call("GET", f"/api/transactions/{tx_id}")
        if code == 200 and row.get("status") == "COMMITTED":
            return True
        polls += 1
        if polls % 5 == 0:
            call("POST", "/api/chain/sync")
        time.sleep(2)
    return False


def new_campaign(charity_id, tag, currency="USD"):
    code, camp = call("POST", "/api/campaigns", {
        "charity_id": charity_id, "title": f"VAL campaign {tag}", "goal_amount": 100000, "currency": currency})
    assert code == 201, camp
    return camp["campaign_id"]


def committed(body):
    code, tx = call("POST", "/api/transactions", body)
    assert code == 201, tx
    assert wait_committed(tx["tx_id"]), "transaction never became COMMITTED"
    return tx["tx_id"]


@pytest.fixture(scope="module")
def world():
    try:
        call("GET", "/api/transactions?limit=1")
    except (urllib.error.URLError, OSError):
        pytest.skip(f"gateway not reachable at {BASE}")
    tag = secrets.token_hex(4)
    code, charity = call("POST", "/api/charities", {
        "name": f"VAL Charity {tag}", "description": "validation test", "wallet_ref": f"wallet-val-{tag}"})
    assert code == 201, charity
    code, donor = call("POST", "/api/donors", {
        "display_name": f"VAL Donor {tag}", "email": f"val-{tag}@example.com"})
    assert code == 201, donor
    cid, did = charity["charity_id"], donor["donor_id"]
    camp, other_camp = new_campaign(cid, tag + "-a"), new_campaign(cid, tag + "-b")
    common = {"currency": "USD", "charity_id": cid, "campaign_id": camp}
    don = committed({**common, "tx_type": "DONATION", "amount": 10000, "donor_id": did})
    alloc = committed({**common, "tx_type": "ALLOCATION", "amount": 6000, "parent_tx_id": don})
    receipt = secrets.token_hex(32)
    disb_body = {**common, "tx_type": "DISBURSEMENT", "amount": 2500, "parent_tx_id": alloc,
                 "recipient_ref": f"recipient-val-{tag}", "receipt_hash": receipt}
    disb = committed(disb_body)
    return {"tag": tag, "cid": cid, "did": did, "camp": camp, "other_camp": other_camp,
            "common": common, "don": don, "alloc": alloc, "disb": disb, "disb_body": disb_body}


def post(body):
    code, resp = call("POST", "/api/transactions", body)
    return code, resp


def test_over_allocation_refused(world):
    w = world
    code, body = post({**w["common"], "tx_type": "ALLOCATION", "amount": 5000, "parent_tx_id": w["don"]})
    assert code == 400, body
    assert "4000" in body["error"]          # 10000 - 6000 left


def test_over_spend_refused(world):
    w = world
    code, body = post({**w["disb_body"], "amount": 4000})
    assert code == 400, body
    assert "3500" in body["error"]          # 6000 - 2500 left


def test_wrong_parent_type_refused(world):
    w = world
    code, body = post({**w["common"], "tx_type": "ALLOCATION", "amount": 100, "parent_tx_id": w["alloc"]})
    assert code == 400, body
    code, body = post({**w["disb_body"], "amount": 100, "parent_tx_id": w["don"]})
    assert code == 400, body


def test_missing_parent_refused(world):
    w = world
    code, body = post({**w["common"], "tx_type": "ALLOCATION", "amount": 100})
    assert code == 400, body
    code, body = post({**w["common"], "tx_type": "ALLOCATION", "amount": 100, "parent_tx_id": "does-not-exist"})
    assert code == 400, body


def test_wrong_currency_refused(world):
    w = world
    code, body = post({**w["common"], "currency": "EUR", "tx_type": "ALLOCATION", "amount": 100,
                       "parent_tx_id": w["don"]})
    assert code == 400, body
    assert "currency" in body["error"].lower()


def test_wrong_campaign_refused(world):
    w = world
    code, body = post({**w["common"], "campaign_id": w["other_camp"], "tx_type": "ALLOCATION", "amount": 100,
                       "parent_tx_id": w["don"]})
    assert code == 400, body
    assert "campaign" in body["error"].lower()


def test_pending_parent_refused():
    """A parent that is not COMMITTED yet must be refused. Racy by nature: skipped if the sync won the race."""
    try:
        call("GET", "/api/transactions?limit=1")
    except (urllib.error.URLError, OSError):
        pytest.skip(f"gateway not reachable at {BASE}")
    tag = secrets.token_hex(4)
    code, charity = call("POST", "/api/charities", {"name": f"VAL P {tag}", "description": "x", "wallet_ref": f"w-p-{tag}"})
    code, donor = call("POST", "/api/donors", {"display_name": f"VAL P {tag}", "email": f"p-{tag}@example.com"})
    cid = charity["charity_id"]
    camp = new_campaign(cid, tag)
    common = {"currency": "USD", "charity_id": cid, "campaign_id": camp}
    code, don = call("POST", "/api/transactions", {**common, "tx_type": "DONATION", "amount": 1000,
                                                   "donor_id": donor["donor_id"]})
    assert code == 201, don
    code, body = post({**common, "tx_type": "ALLOCATION", "amount": 500, "parent_tx_id": don["tx_id"]})
    if code == 201:
        pytest.skip("donation was already COMMITTED, the pending window was missed")
    assert code == 400 and "not confirmed" in body["error"], body


def test_concurrent_over_allocation_only_one_wins():
    """Two parallel ALLOCATIONs of 6000 against a 10000 donation: the lock lets exactly one through."""
    try:
        call("GET", "/api/transactions?limit=1")
    except (urllib.error.URLError, OSError):
        pytest.skip(f"gateway not reachable at {BASE}")
    tag = secrets.token_hex(4)
    code, charity = call("POST", "/api/charities", {"name": f"VAL C {tag}", "description": "x", "wallet_ref": f"w-c-{tag}"})
    code, donor = call("POST", "/api/donors", {"display_name": f"VAL C {tag}", "email": f"c-{tag}@example.com"})
    cid = charity["charity_id"]
    camp = new_campaign(cid, tag)
    common = {"currency": "USD", "charity_id": cid, "campaign_id": camp}
    don = committed({**common, "tx_type": "DONATION", "amount": 10000, "donor_id": donor["donor_id"]})
    results = []

    def go():
        results.append(post({**common, "tx_type": "ALLOCATION", "amount": 6000, "parent_tx_id": don})[0])

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [201, 400], results


def test_rejections_left_nothing_behind(world):
    w = world
    code, rows = call("GET", f"/api/transactions?campaign_id={w['camp']}&limit=50")
    assert code == 200
    assert len(rows) == 3, [(r.get("tx_type"), r.get("status")) for r in rows]
    assert all(r.get("status") == "COMMITTED" for r in rows)
    code, trace = call("GET", f"/api/donations/{w['don']}/trace")
    assert code == 200, trace
    assert trace["allocated"] == 6000 and trace["unallocated"] == 4000 and trace["spent_total"] == 2500
