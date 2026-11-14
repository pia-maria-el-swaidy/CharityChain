"""Gateway validation tests (B3/B4) and actor rules (route protection). HTTP only: nothing is imported from the gateway.

Needs the gateway (GATEWAY_URL, default http://localhost:5000), the chain nodes and ADMIN_EMAIL / ADMIN_PASSWORD
(environment or local .env). If the gateway is not reachable, the module is skipped.
Every run creates its own charity, donor, staff and campaigns, so runs do not collide.
"""
import os
import secrets
import sys
import threading
import time
import urllib.error
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from auth_helpers import http, make_actors  # noqa: E402

BASE = os.environ.get("GATEWAY_URL", "http://localhost:5000").rstrip("/")
WAIT_SECONDS = int(os.environ.get("E2E_WAIT_SECONDS", "90"))
TOKEN_FOR = {"DONATION": "donor_token", "ALLOCATION": "charity_token", "DISBURSEMENT": "finance_token"}


def call(method, path, body=None, token=None):
    """Return (http_status, parsed_json). Never raises on 4xx/5xx."""
    return http(BASE, method, path, body, token)


def post(body, actors):
    """POST a transaction with the token of the role that may create this tx_type."""
    return call("POST", "/api/transactions", body, actors[TOKEN_FOR[body["tx_type"]]])


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


def committed(body, actors):
    code, tx = post(body, actors)
    assert code == 201, tx
    assert wait_committed(tx["tx_id"]), "transaction never became COMMITTED"
    return tx["tx_id"]


def require_gateway():
    try:
        call("GET", "/api/transactions?limit=1")
    except (urllib.error.URLError, OSError):
        pytest.skip(f"gateway not reachable at {BASE}")


def setup_charity(tag):
    """New charity plus its donor, charity admin and finance officer. Returns (charity_id, actors)."""
    code, charity = call("POST", "/api/charities", {
        "name": f"VAL Charity {tag}", "description": "validation test", "wallet_ref": f"wallet-val-{tag}"})
    assert code == 201, charity
    cid = charity["charity_id"]
    return cid, make_actors(BASE, cid, tag)


@pytest.fixture(scope="module")
def world():
    require_gateway()
    tag = secrets.token_hex(4)
    cid, actors = setup_charity(tag)
    did = actors["donor_id"]
    camp, other_camp = new_campaign(cid, tag + "-a"), new_campaign(cid, tag + "-b")
    common = {"currency": "USD", "charity_id": cid, "campaign_id": camp}
    don = committed({**common, "tx_type": "DONATION", "amount": 10000, "donor_id": did}, actors)
    alloc = committed({**common, "tx_type": "ALLOCATION", "amount": 6000, "parent_tx_id": don}, actors)
    receipt = secrets.token_hex(32)
    disb_body = {**common, "tx_type": "DISBURSEMENT", "amount": 2500, "parent_tx_id": alloc,
                 "recipient_ref": f"recipient-val-{tag}", "receipt_hash": receipt}
    disb = committed(disb_body, actors)
    return {**actors, "tag": tag, "cid": cid, "did": did, "camp": camp, "other_camp": other_camp,
            "common": common, "don": don, "alloc": alloc, "disb": disb, "disb_body": disb_body}


def test_over_allocation_refused(world):
    w = world
    code, body = post({**w["common"], "tx_type": "ALLOCATION", "amount": 5000, "parent_tx_id": w["don"]}, w)
    assert code == 400, body
    assert "4000" in body["error"]          # 10000 - 6000 left


def test_over_spend_refused(world):
    w = world
    code, body = post({**w["disb_body"], "amount": 4000}, w)
    assert code == 400, body
    assert "3500" in body["error"]          # 6000 - 2500 left


def test_wrong_parent_type_refused(world):
    w = world
    code, body = post({**w["common"], "tx_type": "ALLOCATION", "amount": 100, "parent_tx_id": w["alloc"]}, w)
    assert code == 400, body
    code, body = post({**w["disb_body"], "amount": 100, "parent_tx_id": w["don"]}, w)
    assert code == 400, body


def test_missing_parent_refused(world):
    w = world
    code, body = post({**w["common"], "tx_type": "ALLOCATION", "amount": 100}, w)
    assert code == 400, body
    code, body = post({**w["common"], "tx_type": "ALLOCATION", "amount": 100, "parent_tx_id": "does-not-exist"}, w)
    assert code == 400, body


def test_wrong_currency_refused(world):
    w = world
    code, body = post({**w["common"], "currency": "EUR", "tx_type": "ALLOCATION", "amount": 100,
                       "parent_tx_id": w["don"]}, w)
    assert code == 400, body
    assert "currency" in body["error"].lower()


def test_wrong_campaign_refused(world):
    w = world
    code, body = post({**w["common"], "campaign_id": w["other_camp"], "tx_type": "ALLOCATION", "amount": 100,
                       "parent_tx_id": w["don"]}, w)
    assert code == 400, body
    assert "campaign" in body["error"].lower()


def test_actor_rules(world):
    """Route protection: no token = 401; wrong role or someone else's id = 403; nothing is stored."""
    w = world
    alloc = {**w["common"], "tx_type": "ALLOCATION", "amount": 100, "parent_tx_id": w["don"]}
    assert call("POST", "/api/transactions", alloc)[0] == 401                                  # no token
    assert call("POST", "/api/transactions", alloc, w["donor_token"])[0] == 403                # donor cannot allocate
    assert call("POST", "/api/transactions", alloc, w["finance_token"])[0] == 403              # finance cannot allocate
    disb = {**w["disb_body"], "amount": 100}
    assert call("POST", "/api/transactions", disb, w["charity_token"])[0] == 403               # admin cannot disburse
    assert call("POST", "/api/transactions", disb, w["donor_token"])[0] == 403
    other_donor = {**w["common"], "tx_type": "DONATION", "amount": 100, "donor_id": str(uuid.uuid4())}
    code, body = call("POST", "/api/transactions", other_donor, w["donor_token"])
    assert code == 403 and "donor_id" in body["error"], body                                   # not your donor_id
    other_charity = {**alloc, "charity_id": str(uuid.uuid4())}
    assert call("POST", "/api/transactions", other_charity, w["charity_token"])[0] == 403      # not your charity


def test_pending_parent_refused():
    """A parent that is not COMMITTED yet must be refused. Racy by nature: skipped if the sync won the race."""
    require_gateway()
    tag = secrets.token_hex(4)
    cid, actors = setup_charity(tag)
    camp = new_campaign(cid, tag)
    common = {"currency": "USD", "charity_id": cid, "campaign_id": camp}
    code, don = post({**common, "tx_type": "DONATION", "amount": 1000, "donor_id": actors["donor_id"]}, actors)
    assert code == 201, don
    code, body = post({**common, "tx_type": "ALLOCATION", "amount": 500, "parent_tx_id": don["tx_id"]}, actors)
    if code == 201:
        pytest.skip("donation was already COMMITTED, the pending window was missed")
    assert code == 400 and "not confirmed" in body["error"], body


def test_concurrent_over_allocation_only_one_wins():
    """Two parallel ALLOCATIONs of 6000 against a 10000 donation: the lock lets exactly one through."""
    require_gateway()
    tag = secrets.token_hex(4)
    cid, actors = setup_charity(tag)
    camp = new_campaign(cid, tag)
    common = {"currency": "USD", "charity_id": cid, "campaign_id": camp}
    don = committed({**common, "tx_type": "DONATION", "amount": 10000, "donor_id": actors["donor_id"]}, actors)
    results = []

    def go():
        results.append(post({**common, "tx_type": "ALLOCATION", "amount": 6000, "parent_tx_id": don}, actors)[0])

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
