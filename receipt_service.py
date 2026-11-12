"""TraceChain receipt service: stores receipt files (invoices, photos) under the SHA-256 hash of their content.

    POST /receipts              body = the file's bytes (header X-Api-Key if RECEIPT_API_KEY is set)
                                -> {"hash", "size"}   (201 new, 200 already stored)
    GET  /receipts/<hash>       the file; the service hashes it again before sending, so a damaged or replaced
                                file is never served as if it were the original (500 instead)
    GET  /receipts/<hash>/info  {"hash", "size", "intact": true|false}
    GET  /health

Why content addressing: the expense on the blockchain holds only the hash. Anybody who gets the file can hash it and
compare. Changing one byte of the file gives another hash, so the receipt cannot be swapped after the expense was recorded.

Settings (environment variables or options): PORT, HOST, RECEIPT_DIR (where the files are kept), RECEIPT_API_KEY.
"""
import argparse
import hashlib
import os
import re

from flask import Flask, Response, jsonify, request

app = Flask(__name__)
MAX_BYTES = 5 * 1024 * 1024
HASH = re.compile(r"^[0-9a-f]{64}$")
config = {"dir": "receipts_data", "key": ""}


def path_of(receipt_hash):
    return os.path.join(config["dir"], receipt_hash[:2], receipt_hash)


def read_checked(receipt_hash):
    """(bytes, None) if the stored file still has this hash, (None, reason) otherwise."""
    try:
        with open(path_of(receipt_hash), "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return None, "not found"
    if hashlib.sha256(data).hexdigest() != receipt_hash:
        return None, "corrupted"
    return data, None


@app.post("/receipts")
def store():
    if config["key"] and request.headers.get("X-Api-Key") != config["key"]:
        return jsonify({"error": "missing or wrong X-Api-Key"}), 401
    data = request.get_data()
    if not data:
        return jsonify({"error": "the body is empty"}), 400
    if len(data) > MAX_BYTES:
        return jsonify({"error": f"a receipt can be at most {MAX_BYTES // (1024 * 1024)} MB"}), 413
    receipt_hash = hashlib.sha256(data).hexdigest()
    path = path_of(receipt_hash)
    if os.path.exists(path):
        return jsonify({"hash": receipt_hash, "size": len(data)}), 200
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    return jsonify({"hash": receipt_hash, "size": len(data)}), 201


@app.get("/receipts/<receipt_hash>")
def fetch(receipt_hash):
    if not HASH.match(receipt_hash):
        return jsonify({"error": "a receipt hash is 64 lowercase hex characters"}), 400
    data, problem = read_checked(receipt_hash)
    if problem == "not found":
        return jsonify({"error": "unknown receipt"}), 404
    if problem:
        return jsonify({"error": "the stored file is corrupted: its hash no longer matches"}), 500
    return Response(data, mimetype="application/octet-stream", headers={"X-Receipt-Hash": receipt_hash})


@app.get("/receipts/<receipt_hash>/info")
def info(receipt_hash):
    if not HASH.match(receipt_hash):
        return jsonify({"error": "a receipt hash is 64 lowercase hex characters"}), 400
    data, problem = read_checked(receipt_hash)
    if problem == "not found":
        return jsonify({"error": "unknown receipt"}), 404
    size = len(data) if data is not None else os.path.getsize(path_of(receipt_hash))
    return jsonify({"hash": receipt_hash, "size": size, "intact": problem is None})


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5100)))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--dir", default=os.environ.get("RECEIPT_DIR", "receipts_data"))
    parser.add_argument("--api-key", default=os.environ.get("RECEIPT_API_KEY", ""))
    args = parser.parse_args()
    config.update(dir=args.dir, key=args.api_key)
    os.makedirs(args.dir, exist_ok=True)
    print(f"receipt service on port {args.port}, files in {args.dir}, api key {'required' if args.api_key else 'NOT set'}")
    app.run(host=args.host, port=args.port, threaded=True)