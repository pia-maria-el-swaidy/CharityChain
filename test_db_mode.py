"""PostgreSQL mode of the validators (chain_storage.py), tested with a stand-in database (see test_support/psycopg2.py).
Checks that every node keeps its own rows, restores its ledger and voting state, and repairs damaged rows.
Run:  python3 test_db_mode.py"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

import requests

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


KEYS = [wallet.generate_keypair() for _ in range(4)]
V = [k[1] for k in KEYS]
DONOR, CHARITY = wallet.generate_keypair(), wallet.generate_keypair()
HERE = os.path.dirname(os.path.abspath(__file__))


def stx(id, amount):
    t = {"id": id, "type": "DONATION", "sender": DONOR[1], "parent": CHARITY[1], "amount": amount, "memo": ""}
    return wallet.sign_transaction(t, DONOR[0])


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


with tempfile.TemporaryDirectory() as tmp:
    db_path = os.path.join(tmp, "pg.db")
    ports = [6401 + i for i in range(4)]
    urls = [f"http://127.0.0.1:{p}" for p in ports]
    procs = {}

    def start(i):
        env = dict(os.environ, PYTHONPATH=os.path.join(HERE, "test_support"), DATABASE_URL=f"fakepg://{db_path}",
                   PORT=str(ports[i]), HOST="127.0.0.1", PEERS=",".join(urls), SELF_URL=urls[i], VALIDATORS=",".join(V),
                   VALIDATOR_KEY=KEYS[i][0], TIMEOUT_PROPOSE="0.6", TIMEOUT_PREVOTE="0.3", TIMEOUT_PRECOMMIT="0.3",
                   TIMEOUT_DELTA="0.2", SYNC_INTERVAL="1")
        env.pop("DATA_DIR", None)
        procs[i] = subprocess.Popen([sys.executable, "node_bft.py"], cwd=HERE, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert wait_for(lambda: requests.get(urls[i] + "/status", timeout=1).ok, 20), f"node {i} did not start"

    def stop(i):
        proc = procs.pop(i, None)
        if proc:
            proc.terminate()
            proc.wait()

    def heights(which=range(4)):
        return [requests.get(urls[i] + "/status", timeout=3).json()["height"] for i in which]

    def rows(sql, params=()):
        con = sqlite3.connect(db_path, timeout=30)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    try:
        for i in range(4):
            start(i)
        for k in range(1, 3):
            requests.post(urls[k % 4] + "/transactions/new", json=stx(f"d{k}", 100 * k))
            check(f"block {k} decided on all four validators", wait_for(lambda: heights() == [k + 1] * 4), True)

        nodes = [r[0] for r in rows("SELECT DISTINCT node FROM bft_blocks ORDER BY node")]
        check(f"every validator keeps its own rows ({len(nodes)} different node names, none shared)", len(nodes), 4)
        check("the default node name comes from the validator's public key", sorted(nodes), sorted(f"validator-{v[:16]}" for v in V))
        check("2 blocks saved for each validator", [r[1] for r in rows("SELECT node, COUNT(*) FROM bft_blocks GROUP BY node")], [2] * 4)
        check("every validator saved its own voting state", len(rows("SELECT node FROM bft_engine")), 4)

        for i in range(4):
            stop(i)
        start(0)                                                  # alone: nobody to ask
        check("a validator started alone has its ledger back from the database", heights([0]), [3])
        for i in (1, 2, 3):
            start(i)
        requests.post(urls[2] + "/transactions/new", json=stx("d3", 300))
        check("after restarting everything the network decides new blocks on top of the saved ledger",
              wait_for(lambda: heights() == [4] * 4), True)

        stop(2)                                                   # damage the rows of validator 2
        name = f"validator-{V[2][:16]}"
        (txs,) = rows("SELECT transactions_json FROM bft_blocks WHERE node = ? AND idx = 2", (name,))[0:1][0]
        damaged = json.loads(txs)
        damaged[0]["amount"] = 1
        con = sqlite3.connect(db_path, timeout=30)
        con.execute("UPDATE bft_blocks SET transactions_json = ? WHERE node = ? AND idx = 2", (json.dumps(damaged), name))
        con.commit()
        con.close()
        start(2)
        check("damaged rows are detected at start: only the valid beginning (1 block) is kept", heights([2]), [2])
        check("then the node fetches the rest from its peers", wait_for(lambda: heights() == [4] * 4, 25), True)
        check("and the rows in the database were repaired (3 good blocks again)",
              rows("SELECT COUNT(*) FROM bft_blocks WHERE node = ?", (name,))[0][0], 3)
        a, b = (requests.get(urls[i] + "/chain").json()["chain"] for i in (2, 0))
        check("same ledger as the others and valid", ([x["hash"] for x in a] == [x["hash"] for x in b], bft.chain_error(a, V)), (True, None))
    finally:
        for i in list(procs):
            stop(i)

print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")
sys.exit(0 if all(results) else 1)