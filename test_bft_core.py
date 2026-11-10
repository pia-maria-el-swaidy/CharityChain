"""Stage 2 (BFT) unit checks: thresholds, blocks, commit certificates, messages, chains.   Run:  python3 test_bft_core.py"""
import copy
import sys

import bft
import core
import wallet

results = []


def check(name, got, expected):
    ok = got == expected
    print(("OK   " if ok else "FAIL ") + name)
    if not ok:
        print("   got:     ", got)
        print("   expected:", expected)
    results.append(ok)


def has(text, part):
    return text is not None and part in text


KEYS = [wallet.generate_keypair() for _ in range(4)]
V = [k[1] for k in KEYS]
DONOR, CHARITY, EVE = wallet.generate_keypair(), wallet.generate_keypair(), wallet.generate_keypair()
RECEIPT = core.sha256_hex("receipt-001")


def stx(id, type, who, parent, amount, memo="", signer=None):
    t = {"id": id, "type": type, "sender": who[1], "parent": parent, "amount": amount, "memo": memo}
    return wallet.sign_transaction(t, (signer or who)[0])


def certify(block, round_=0, signers=(0, 1, 2), keys=KEYS):
    """Attach a commit certificate signed by the given validators."""
    votes = [bft.make_vote(bft.PRECOMMIT, block["index"], round_, block["hash"], keys[i][1], keys[i][0]) for i in signers]
    return dict(block, commit=bft.make_certificate(round_, votes, V))


def add(chain, txs, **kw):
    prev = chain[-1]
    chain.append(certify(bft.make_block(len(chain), prev["timestamp"] + 1, prev["hash"], txs), **kw))
    return chain


d1 = stx("d1", "DONATION", DONOR, CHARITY[1], 1000)
a1 = stx("a1", "ALLOCATION", CHARITY, "d1", 600, "food-packages")
e1 = stx("e1", "EXPENSE", CHARITY, "a1", 250, RECEIPT)

print("--- thresholds ---")
check("quorum: 3 of 4, 5 of 7, 7 of 10", [bft.quorum(n) for n in (4, 7, 10)], [3, 5, 7])
check("more than 1/3: 2 of 4, 3 of 7", [bft.skip_threshold(n) for n in (4, 7)], [2, 3])
check("faulty validators tolerated: 1 of 4, 2 of 7", [bft.max_faulty(n) for n in (4, 7)], [1, 2])
check("any two quorums of 4 share at least 2 validators (so at least one honest)", 2 * bft.quorum(4) - 4 >= 2, True)

print("\n--- who proposes ---")
check("height 1: round 0 -> validator 0, round 1 -> 1, round 2 -> 2",
      [V.index(bft.proposer_for(1, r, V)) for r in range(3)], [0, 1, 2])
check("height 2 starts with validator 1", V.index(bft.proposer_for(2, 0, V)), 1)
check("it wraps around", V.index(bft.proposer_for(2, 3, V)), 0)

print("\n--- genesis and blocks ---")
g = bft.make_genesis(V)
check("genesis is deterministic", g, bft.make_genesis(list(V)))
check("the validator order is part of the genesis", g["hash"] != bft.make_genesis(V[::-1])["hash"], True)
b = bft.make_block(1, 5, g["hash"], [d1, a1])
check("a good block body", bft.body_error(b, g), None)
check("wrong index", has(bft.body_error(bft.make_block(2, 5, g["hash"], [d1]), g), "wrong index"), True)
check("wrong previous hash", has(bft.body_error(bft.make_block(1, 5, "f" * 64, [d1]), g), "previous_hash"), True)
check("timestamp going backwards", has(bft.body_error(bft.make_block(1, 5, g["hash"], [d1]), dict(g, timestamp=9)), "timestamp"), True)
check("empty block", has(bft.body_error(bft.make_block(1, 5, g["hash"], []), g), "between 1 and"), True)
many = [stx(f"m{i}", "DONATION", DONOR, CHARITY[1], 1) for i in range(bft.MAX_TXS_PER_BLOCK + 1)]
check("too many transactions", has(bft.body_error(bft.make_block(1, 5, g["hash"], many), g), "between 1 and"), True)
check("edited content", has(bft.body_error(dict(b, transactions=[dict(d1, amount=5), a1]), g), "hash does not match"), True)
check("extra field", has(bft.body_error(dict(b, nonce=1), g), "exactly the fields"), True)
check("no proposer, no signature in the block: the certificate is what counts", set(b) == set(bft.BODY_FIELDS), True)

print("\n--- commit certificates ---")
good = certify(b)
check("3 of 4 signatures: final", bft.check_block(good, g, V), None)
check("4 of 4 signatures: final", bft.check_block(certify(b, signers=(0, 1, 2, 3)), g, V), None)
check("any 3 validators will do", bft.check_block(certify(b, signers=(1, 2, 3)), g, V), None)
check("2 of 4 signatures: not final", has(bft.check_block(certify(b, signers=(0, 1)), g, V), "2 signatures, 3 are needed"), True)
check("no certificate at all", has(bft.check_block(b, g, V), "exactly the fields"), True)
twice = dict(good, commit={"round": 0, "signatures": good["commit"]["signatures"][:2] + [good["commit"]["signatures"][0]]})
check("one validator counted twice does not make a quorum", has(bft.check_block(twice, g, V), "twice"), True)
outsider = certify(b, signers=(0, 1))
outsider["commit"]["signatures"].append({"validator": EVE[1], "signature": wallet.sign(EVE[0], bft.vote_text(bft.PRECOMMIT, 1, 0, b["hash"]))})
check("a signature of someone who is not a validator", has(bft.check_block(outsider, g, V), "not a validator"), True)
wrong_round = dict(good, commit=dict(good["commit"], round=1))
check("the certificate says another round than the one signed", has(bft.check_block(wrong_round, g, V), "invalid signature"), True)
other = bft.make_block(1, 5, g["hash"], [d1])
borrowed = dict(other, commit=good["commit"])
check("a certificate taken from another block", has(bft.check_block(borrowed, g, V), "invalid signature"), True)
prevotes = [bft.make_vote(bft.PREVOTE, 1, 0, b["hash"], V[i], KEYS[i][0]) for i in range(3)]
check("prevotes are not precommits: they cannot make a certificate",
      has(bft.check_block(dict(b, commit=bft.make_certificate(0, prevotes, V)), g, V), "invalid signature"), True)
nil_votes = [bft.make_vote(bft.PRECOMMIT, 1, 0, bft.NIL, V[i], KEYS[i][0]) for i in range(3)]
check("precommits for nil are not a certificate for the block",
      has(bft.check_block(dict(b, commit=bft.make_certificate(0, nil_votes, V)), g, V), "invalid signature"), True)
STRANGERS = [wallet.generate_keypair()[1] for _ in range(4)]
check("a network with other validators does not accept this certificate",
      has(bft.check_block(good, g, STRANGERS), "not a validator"), True)
check("the same validators in another order accept it (a certificate does not depend on the order)",
      bft.check_block(good, g, V[::-1]), None)
check("garbage in the certificate", has(bft.check_block(dict(b, commit="yes"), g, V), "certificate"), True)
check("who proposed the round", bft.block_proposer(certify(b, round_=2), V), 2)

print("\n--- messages ---")
vote = bft.make_vote(bft.PREVOTE, 1, 0, b["hash"], V[1], KEYS[1][0])
check("a good prevote", bft.message_error(vote, V), None)
check("a good prevote for nil", bft.message_error(bft.make_vote(bft.PREVOTE, 1, 0, bft.NIL, V[1], KEYS[1][0]), V), None)
check("a good proposal", bft.message_error(bft.make_proposal(1, 0, -1, b, V[0], KEYS[0][0]), V), None)
check("a proposal again, with proof of an earlier round", bft.message_error(bft.make_proposal(1, 2, 1, b, V[2], KEYS[2][0]), V), None)
check("a vote from a stranger", has(bft.message_error(bft.make_vote(bft.PREVOTE, 1, 0, b["hash"], EVE[1], EVE[0]), V), "not a validator"), True)
check("a vote signed with another key", has(bft.message_error(dict(vote, signature=wallet.sign(EVE[0], "x")), V), "invalid signature"), True)
check("a vote whose content was changed", has(bft.message_error(dict(vote, hash=bft.NIL), V), "invalid signature"), True)
check("the same vote at another round", has(bft.message_error(dict(vote, round=1), V), "invalid signature"), True)
check("a proposal from the wrong proposer", has(bft.message_error(bft.make_proposal(1, 0, -1, b, V[1], KEYS[1][0]), V), "not the proposer"), True)
tampered = bft.make_proposal(1, 0, -1, dict(b, transactions=[dict(d1, amount=5), a1]), V[0], KEYS[0][0])
check("a proposal with a block whose content does not match its hash", has(bft.message_error(tampered, V), "hash does not match"), True)
check("a proposal for another height than its block", has(bft.message_error(bft.make_proposal(2, 1, -1, b, V[2], KEYS[2][0]), V), "not for this height"), True)
check("valid_round must be older than the round", has(bft.message_error(bft.make_proposal(1, 1, 1, b, V[1], KEYS[1][0]), V), "valid_round"), True)
check("a vote for something that is neither a hash nor nil",
      has(bft.message_error(bft.make_vote(bft.PREVOTE, 1, 0, "hello", V[1], KEYS[1][0]), V), "block hash or for nil"), True)
check("unknown type", has(bft.message_error(dict(vote, type="HELLO"), V), "unknown"), True)
check("an extra field", has(bft.message_error(dict(vote, extra=1), V), "wrong fields"), True)
check("a negative round", has(bft.message_error(dict(vote, round=-1), V), "height or round"), True)
check("not an object", has(bft.message_error("hello", V), "object"), True)

print("\n--- a whole chain ---")
chain = [g]
add(chain, [d1, a1])
add(chain, [e1], round_=1, signers=(1, 2, 3))
add(chain, [stx("d2", "DONATION", DONOR, CHARITY[1], 500)], signers=(0, 1, 2, 3))
check("3 blocks with certificates of different sizes and rounds", bft.chain_error(chain, V), None)

bad = copy.deepcopy(chain)
bad[2]["transactions"][0]["amount"] = 1
check("editing a transaction", has(bft.chain_error(bad, V), "block 2"), True)
bad = copy.deepcopy(chain)
del bad[2]["commit"]
check("a block without certificate", has(bft.chain_error(bad, V), "block 2"), True)
bad = copy.deepcopy(chain)
bad[2]["commit"]["signatures"].pop()
check("a certificate with too few signatures", has(bft.chain_error(bad, V), "needed"), True)
check("another validator list: the genesis differs", has(bft.chain_error(chain, V[::-1]), "genesis"), True)
check("swapping two blocks", bft.chain_error([chain[0], chain[2], chain[1], chain[3]], V) is not None, True)

over = [g]
add(over, [d1, a1])
add(over, [stx("e5", "EXPENSE", CHARITY, "a1", 5000, RECEIPT)], signers=(0, 1, 2, 3))
check("even a certificate of ALL validators cannot make an overspending valid", has(bft.chain_error(over, V), "over-spent"), True)
wrong_sender = [g]
add(wrong_sender, [d1, a1])
add(wrong_sender, [stx("e6", "EXPENSE", EVE, "a1", 10, RECEIPT)])
check("... nor someone else spending the charity's money", has(bft.chain_error(wrong_sender, V), "wrong sender"), True)
unsigned = [g]
add(unsigned, [d1, {k: v for k, v in a1.items() if k != "signature"}])
check("... nor an unsigned transaction", has(bft.chain_error(unsigned, V), "missing signature"), True)

print("\n--- how much of a damaged chain is still good ---")
check("an intact chain", bft.valid_prefix(chain, V), 4)
bad = copy.deepcopy(chain)
bad[3]["transactions"][0]["amount"] = 1
check("damage in the last block: 3 blocks are kept (genesis included)", bft.valid_prefix(bad, V), 3)
bad = copy.deepcopy(chain)
bad[1]["transactions"][0]["amount"] = 1
check("damage in the first block: only the genesis is kept", bft.valid_prefix(bad, V), 1)
check("a wrong genesis: nothing is kept", bft.valid_prefix([dict(g, timestamp=5)] + chain[1:], V), 0)

print("\n--- the proof of double signing ---")
c1 = bft.make_vote(bft.PREVOTE, 1, 0, b["hash"], V[3], KEYS[3][0])
c2 = bft.make_vote(bft.PREVOTE, 1, 0, bft.NIL, V[3], KEYS[3][0])
item = {"kind": "double-signing", "validator": 3, "validator_key": V[3], "height": 1, "round": 0, "type": bft.PREVOTE,
        "signed": [{"hash": c1["hash"], "valid_round": None, "signature": c1["signature"]},
                   {"hash": c2["hash"], "valid_round": None, "signature": c2["signature"]}]}
check("two different signed prevotes of the same validator, same round: a genuine proof", bft.evidence_error(item), None)
fake = copy.deepcopy(item)
fake["signed"][1]["signature"] = wallet.sign(EVE[0], "x")
check("a forged proof is recognized", has(bft.evidence_error(fake), "signature"), True)
same = copy.deepcopy(item)
same["signed"][1] = same["signed"][0]
check("the same message twice is no proof", has(bft.evidence_error(same), "same"), True)
check("garbage is no proof", has(bft.evidence_error({"x": 1}), "malformed"), True)

print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")
sys.exit(0 if all(results) else 1)