"""TraceChain - Exercise 4: ed25519 keys and signatures.

Library: `cryptography`  (python -m pip install cryptography).  PyNaCl would work too: only
generate_keypair, sign and verify below would change.

Command line (lab use only: private keys are stored in plain text files).
Works the same on Windows, macOS and Linux: no JSON quoting needed.

    python wallet.py new hope                       create wallets/hope.json
    python wallet.py list                           show the wallets and their public keys

    python wallet.py send ali http://127.0.0.1:5001 id=d1 type=DONATION parent=@hope amount=1000
    python wallet.py send hope http://127.0.0.1:5001 id=a1 type=ALLOCATION parent=d1 amount=600 memo=food-packages
        signs the transaction with that wallet and submits it to the node.
        'sender' is filled in with the wallet's public key, 'memo' defaults to empty,
        and a value written @name is replaced by that wallet's public key (the charity of a donation).

    python wallet.py sign hope id=a1 type=ALLOCATION parent=d1 amount=600 memo=food-packages
        only prints the signed transaction as JSON (save it with > tx.json and use curl -d @tx.json)
"""
import json
import os
import re
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

import core

WALLET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wallets")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def generate_keypair():
    """Return (private_key_hex, public_key_hex); the public key is the identity on the ledger."""
    sk = Ed25519PrivateKey.generate()
    private = sk.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    public = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return private, public


def public_key_of(private_hex: str) -> str:
    """The public key (hex) that belongs to a private key (hex)."""
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    return sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def sign(private_hex: str, message: str) -> str:
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    return sk.sign(message.encode("utf-8")).hex()


def verify(public_hex: str, message: str, signature_hex: str) -> bool:
    try:
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        pk.verify(bytes.fromhex(signature_hex), message.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def is_public_key(text) -> bool:
    return isinstance(text, str) and HEX64.match(text) is not None


def sign_transaction(tx: dict, private_hex: str) -> dict:
    """Return a copy of tx with a 'signature' field: the signature of id:type:sender:parent:amount:memo."""
    unsigned = {k: v for k, v in tx.items() if k != "signature"}
    return {**unsigned, "signature": sign(private_hex, core.tx_signing_string(unsigned))}


# ---------- tiny wallet files ----------

def save_wallet(name: str) -> dict:
    os.makedirs(WALLET_DIR, exist_ok=True)
    private, public = generate_keypair()
    wallet = {"name": name, "private": private, "public": public}
    with open(os.path.join(WALLET_DIR, f"{name}.json"), "w") as f:
        json.dump(wallet, f, indent=2)
    return wallet


def load_wallet(name: str) -> dict:
    with open(os.path.join(WALLET_DIR, f"{name}.json")) as f:
        return json.load(f)


def parse_tx_args(args: list) -> dict:
    """Turn  id=a1 type=ALLOCATION parent=d1 amount=600  (or one JSON object) into a transaction dict."""
    if len(args) == 1 and args[0].lstrip().startswith("{"):
        tx = json.loads(args[0])
    else:
        tx = {}
        for arg in args:
            if "=" not in arg:
                raise ValueError(f"expected key=value, got '{arg}'")
            key, value = arg.split("=", 1)
            if value.startswith("@"):                      # @hope -> hope's public key
                value = load_wallet(value[1:])["public"]
            tx[key] = value
    if "amount" in tx:
        tx["amount"] = int(tx["amount"])
    tx.setdefault("memo", "")
    missing = [k for k in ("id", "type", "parent", "amount") if k not in tx]
    if missing:
        raise ValueError(f"missing: {', '.join(missing)}  (needed: id, type, parent, amount; memo is optional)")
    return tx


def _main(argv):
    try:
        if len(argv) >= 2 and argv[0] == "new":
            w = save_wallet(argv[1])
            print(f"wallet '{w['name']}' created. public key (use it as sender / charity): {w['public']}")
        elif argv[:1] == ["list"]:
            names = sorted(f[:-5] for f in os.listdir(WALLET_DIR)) if os.path.isdir(WALLET_DIR) else []
            for name in names:
                w = load_wallet(name)
                print(f"{w['name']:15} {w['public']}")
        elif len(argv) >= 3 and argv[0] == "sign":
            w = load_wallet(argv[1])
            tx = parse_tx_args(argv[2:])
            tx.setdefault("sender", w["public"])
            print(json.dumps(sign_transaction(tx, w["private"])))
        elif len(argv) >= 4 and argv[0] == "send":
            import requests
            w = load_wallet(argv[1])
            tx = parse_tx_args(argv[3:])
            tx.setdefault("sender", w["public"])
            try:
                r = requests.post(argv[2].rstrip("/") + "/transactions/new",
                                  json=sign_transaction(tx, w["private"]))
            except requests.exceptions.ConnectionError:
                print(f"cannot reach a node at {argv[2]}. Start one in another terminal first, e.g.:  "
                      f"python node.py --port {argv[2].rstrip('/').rsplit(':', 1)[-1]}")
                return
            print(r.status_code, r.text.strip())
        else:
            print(__doc__)
    except FileNotFoundError as e:
        print(f"wallet not found: {e.filename}  (create it with: python wallet.py new NAME)")
    except (ValueError, json.JSONDecodeError) as e:
        print(f"error: {e}")


if __name__ == "__main__":
    _main(sys.argv[1:])