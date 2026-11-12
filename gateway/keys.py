"""Gateway-side wallets: ed25519 keys + signing. Reproduces Michele's signing contract
(wallet.py + core.tx_signing_string) WITHOUT importing her code. tests/test_signing.py checks they match."""
import os

from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat


def _fernet() -> Fernet:
    secret = os.environ.get("KEY_ENCRYPTION_SECRET")
    if not secret:
        raise RuntimeError("KEY_ENCRYPTION_SECRET is not set (see .env.example)")
    return Fernet(secret.encode())


def generate_keypair():
    """(private_hex, public_hex): raw 32-byte keys as 64 hex characters, same as wallet.py."""
    sk = Ed25519PrivateKey.generate()
    private = sk.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
    public = sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    return private, public


def encrypt_private(private_hex: str) -> str:
    return _fernet().encrypt(private_hex.encode()).decode()


def decrypt_private(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def tx_signing_string(tx: dict) -> str:
    """Must stay identical to core.tx_signing_string: id:type:sender:parent:amount:memo"""
    return f"{tx['id']}:{tx['type']}:{tx['sender']}:{tx['parent']}:{tx['amount']}:{tx['memo']}"


def sign(private_hex: str, message: str) -> str:
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    return sk.sign(message.encode("utf-8")).hex()


def verify(public_hex: str, message: str, signature_hex: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(
            bytes.fromhex(signature_hex), message.encode("utf-8"))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False