import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core      # Michele's modules: used ONLY here, to verify the contract
import wallet

from gateway import keys

TX = {"id": "d1", "type": "DONATION", "sender": "", "parent": "ab" * 32, "amount": 1000, "memo": ""}


def test_signing_string_is_identical():
    assert keys.tx_signing_string(TX) == core.tx_signing_string(TX)


def test_gateway_signature_verifies_with_michele_wallet():
    priv, pub = keys.generate_keypair()
    tx = {**TX, "sender": pub}
    sig = keys.sign(priv, keys.tx_signing_string(tx))
    assert wallet.verify(pub, core.tx_signing_string(tx), sig)


def test_michele_signature_verifies_with_gateway():
    priv, pub = wallet.generate_keypair()
    tx = {**TX, "sender": pub}
    signed = wallet.sign_transaction(tx, priv)
    assert keys.verify(pub, keys.tx_signing_string(tx), signed["signature"])