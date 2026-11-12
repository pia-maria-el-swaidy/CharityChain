import os
import threading
import uuid
from datetime import datetime, timezone

import psycopg2
from flask import Flask, jsonify, request
from flask_cors import CORS

from gateway.db import (
    init_db,
    add_campaign, get_campaigns,
    add_transaction,
    add_donor, add_charity, get_charities,
    list_transactions, get_transaction,
    create_transaction,
)
from gateway import chain_client, chain_sync
from blockchain.hashing import compute_tx_hash

app = Flask(__name__)
CORS(app)
init_db()

TX_TYPES = {"DONATION", "ALLOCATION", "DISBURSEMENT"}

# ---------- helpers ----------
def _uuid_or_none(value, name):
    if value in (None, ""):
        return None
    try:
        return str(uuid.UUID(str(value))).lower()
    except ValueError:
        raise ValueError(f"{name} is not a valid UUID")

def build_tx(data):
    if not isinstance(data, dict):
        raise ValueError("JSON body required")
    tx_type = data.get("tx_type")
    if tx_type not in TX_TYPES:
        raise ValueError(f"tx_type must be one of {sorted(TX_TYPES)}")
    try:
        amount = int(data["amount"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("amount must be an integer (cents)")
    if amount <= 0:
        raise ValueError("amount must be > 0")
    currency = str(data.get("currency", "")).upper()
    if len(currency) != 3:
        raise ValueError("currency must be 3 letters")
    charity_id = _uuid_or_none(data.get("charity_id"), "charity_id")
    if not charity_id:
        raise ValueError("charity_id is required")
    donor_id = _uuid_or_none(data.get("donor_id"), "donor_id")
    if tx_type == "DONATION" and not donor_id:
        raise ValueError("donor_id is required for DONATION")
    if tx_type == "DISBURSEMENT" and not data.get("recipient_ref"):
        raise ValueError("recipient_ref is required for DISBURSEMENT")

    tx = {
        "tx_id": str(uuid.uuid4()),
        "tx_type": tx_type,
        "donor_id": donor_id,
        "charity_id": charity_id,
        "campaign_id": _uuid_or_none(data.get("campaign_id"), "campaign_id"),
        "recipient_ref": data.get("recipient_ref") or None,
        "receipt_hash": data.get("receipt_hash") or None,
        "amount": amount,
        "currency": currency,
        "memo": data.get("memo") or None,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0),
        "signature": data.get("signature"),
    }
    tx["tx_hash"] = compute_tx_hash(tx)
    return tx

def public_tx(row):
    """JSON-safe view. Hides the donor id of anonymous donors."""
    r = dict(row)
    if r.pop("is_anonymous", False):
        r["donor_id"] = None
    for k, v in r.items():
        if hasattr(v, "isoformat"):
            r[k] = v.isoformat()
        elif k.endswith("_id") and v is not None:
            r[k] = str(v)
    return r

# ---------- health ----------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "gateway ok"})

# ---------- campaigns ----------
@app.route("/api/campaigns", methods=["GET"])
def list_campaigns():
    return jsonify([dict(r) for r in get_campaigns()])

@app.route("/api/campaigns", methods=["POST"])
def create_campaign():
    data = request.get_json(silent=True) or {}
    try:
        charity_id = _uuid_or_none(data.get("charity_id"), "charity_id")
        goal = int(data["goal_amount"])
        title = data["title"]
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"error": f"invalid input: {e}"}), 400
    try:
        new_id = add_campaign(charity_id, title, goal, data.get("currency", "USD"))
    except psycopg2.errors.ForeignKeyViolation:
        return jsonify({"error": "unknown charity_id"}), 400
    return jsonify({"campaign_id": str(new_id)}), 201

# ---------- donors ----------
@app.route("/api/donors", methods=["POST"])
def create_donor():
    d = request.get_json(silent=True) or {}
    anon = bool(d.get("is_anonymous", False))
    if not anon and not d.get("email"):
        return jsonify({"error": "email is required unless is_anonymous"}), 400
    try:
        new_id = add_donor(d.get("display_name"), d.get("email"), anon)
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "email already exists"}), 409
    return jsonify({"donor_id": str(new_id)}), 201

# ---------- charities ----------
@app.route("/api/charities", methods=["GET"])
def list_charities():
    return jsonify([{**dict(r), "charity_id": str(r["charity_id"])} for r in get_charities()])

@app.route("/api/charities", methods=["POST"])
def create_charity():
    d = request.get_json(silent=True) or {}
    if not d.get("name") or not d.get("wallet_ref"):
        return jsonify({"error": "name and wallet_ref are required"}), 400
    try:
        new_id = add_charity(d["name"], d.get("description"), d["wallet_ref"])
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "wallet_ref already exists"}), 409
    return jsonify({"charity_id": str(new_id)}), 201

# ---------- transactions ----------
@app.route("/api/transactions", methods=["POST"])
def create_tx():
    try:
        tx = build_tx(request.get_json(silent=True))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    try:
        create_transaction(tx, lambda t: chain_client.submit_transaction(chain_client.to_chain_tx(t)))
    except psycopg2.errors.ForeignKeyViolation:
        return jsonify({"error": "unknown donor_id, charity_id or campaign_id"}), 400
    except chain_client.ChainRejected as e:
        return jsonify({"error": "the chain rejected the transaction", "detail": e.body}), 422
    except chain_client.ChainUnavailable:
        return jsonify({"error": "no blockchain node reachable, nothing was saved"}), 503
    return jsonify({"tx_id": tx["tx_id"], "tx_hash": tx["tx_hash"], "status": "PENDING"}), 201

@app.route("/api/transactions", methods=["GET"])
def list_txs():
    try:
        campaign_id = _uuid_or_none(request.args.get("campaign_id"), "campaign_id")
        limit = min(int(request.args.get("limit", 50)), 200)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    status = request.args.get("status")
    if status not in (None, "PENDING", "COMMITTED", "REJECTED"):
        return jsonify({"error": "invalid status"}), 400
    return jsonify([public_tx(r) for r in list_transactions(campaign_id, status, limit, offset)])

@app.route("/api/transactions/<tx_id>", methods=["GET"])
def get_tx(tx_id):
    # tx_id is TEXT, NOT UUID - Michele's example is "d1", so do NOT validate as UUID
    if not tx_id:
        return jsonify({"error": "tx_id required"}), 400
    row = get_transaction(tx_id)
    return (jsonify(public_tx(row)), 200) if row else (jsonify({"error": "not found"}), 404)

# ---------- trace + sync (Option B: HTTP only, chain is source of truth) ----------
@app.route("/api/donations/<donation_id>/trace", methods=["GET"])
def trace(donation_id):
    try:
        code, body = chain_client.get_trace(donation_id)
    except chain_client.ChainUnavailable:
        return jsonify({"error": "no blockchain node reachable"}), 503
    return jsonify(body), code

@app.route("/api/chain/sync", methods=["POST"])
def sync_now():
    try:
        return jsonify({"blocks_scanned": chain_sync.sync_once()})
    except chain_client.ChainUnavailable:
        return jsonify({"error": "no blockchain node reachable"}), 503

if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG") == "1"
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":  # avoid two threads with reloader
        threading.Thread(target=chain_sync.run_forever, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=debug)