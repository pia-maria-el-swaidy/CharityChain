"""Coherence check: the gateway's index (Postgres, read through the gateway API) against the chain.

HTTP only: reads GET /api/transactions and GET /api/donations/<id>/trace from the gateway and
GET /chain from every chain node. Imports nothing from the gateway or the chain code and needs
no database access. The chain is the truth; the gateway DB is only an index.

Env (all optional):
  GATEWAY_URL               default http://localhost:5000
  CHAIN_NODES               default http://localhost:5001,...,http://localhost:5004
  COHERENCE_PENDING_MINUTES a PENDING row older than this is reported as stale (default 5)
  COHERENCE_MAX_TRACES      how many donations to cross-check against /trace (default 200)

Exit code 0 = coherent, 1 = at least one check failed.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:5000").rstrip("/")
NODES = [u.strip().rstrip("/") for u in os.environ.get(
    "CHAIN_NODES",
    "http://localhost:5001,http://localhost:5002,http://localhost:5003,http://localhost:5004",
).split(",") if u.strip()]
PENDING_MINUTES = int(os.environ.get("COHERENCE_PENDING_MINUTES", "5"))
MAX_TRACES = int(os.environ.get("COHERENCE_MAX_TRACES", "200"))

CHAIN_TYPE = {"DONATION": "DONATION", "ALLOCATION": "ALLOCATION", "DISBURSEMENT": "EXPENSE"}

failures = []


def get_json(url, timeout=15):
    """Return (http_status or None, parsed_json or error text). Never raises."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, None
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, str(e)


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        failures.append(name)
        if detail:
            print(f"       {detail}")
    return ok


def info(message):
    print(f"[INFO] {message}")


def first(problems, n=5):
    shown = "; ".join(problems[:n])
    return shown + (f"; ... and {len(problems) - n} more" if len(problems) > n else "")


# ---------- chain side ----------
def fetch_chain(node):
    status, data = get_json(f"{node}/chain?from=0")
    if status != 200:
        return None
    if isinstance(data, dict):
        data = data.get("chain", data.get("blocks", []))
    return data if isinstance(data, list) else None


def block_tx_ids(block):
    txs = block.get("transactions", block.get("txs", []))
    return [str(t.get("id", t.get("tx_id"))) if isinstance(t, dict) else str(t) for t in txs]


def block_txs(block):
    return block.get("transactions", block.get("txs", []))


def check_chain_nodes():
    chains = {}
    for node in NODES:
        chain = fetch_chain(node)
        if chain is None:
            info(f"node {node} did not answer GET /chain (down or unreachable)")
        else:
            chains[node] = chain
    if not check("at least one chain node answers GET /chain", bool(chains), f"tried {NODES}"):
        return None
    reference_node = max(chains, key=lambda n: len(chains[n]))
    reference = chains[reference_node]
    info(f"reference chain: {reference_node}, {len(reference)} blocks")

    ref_ids = [block_tx_ids(b) for b in reference]
    problems = []
    for node, chain in chains.items():
        ids = [block_tx_ids(b) for b in chain]
        if ids != ref_ids[:len(ids)]:
            problems.append(f"{node} has blocks that differ from {reference_node}")
        elif len(ids) < len(ref_ids):
            info(f"node {node} is {len(ref_ids) - len(ids)} block(s) behind")
    check("all answering nodes hold the same blocks (a shorter chain must be a prefix)", not problems,
          first(problems))
    return reference


def index_chain(reference):
    index, problems = {}, []
    for i, block in enumerate(reference):
        block_no = block.get("index", block.get("block_index", i))
        for pos, t in enumerate(block_txs(block)):
            if not isinstance(t, dict):
                continue
            tx_id = str(t.get("id", t.get("tx_id")))
            if tx_id in index:
                problems.append(f"tx {tx_id} appears twice on the chain")
            index[tx_id] = {"block": block_no, "pos": pos, "tx": t}
    check("no transaction id appears twice on the chain", not problems, first(problems))
    return index


# ---------- gateway side ----------
def fetch_rows():
    rows, offset = [], 0
    while True:
        status, page = get_json(f"{GATEWAY}/api/transactions?limit=200&offset={offset}")
        if status != 200 or not isinstance(page, list):
            check("gateway answers GET /api/transactions", False, f"status={status} body={page}")
            return None
        rows.extend(page)
        if len(page) < 200:
            return rows
        offset += 200


def parse_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def check_rows_against_chain(rows, chain_index, chain_height):
    problems = []
    for r in rows:
        if r.get("status") != "COMMITTED":
            continue
        tx_id = r["tx_id"]
        c = chain_index.get(tx_id)
        if not c:
            problems.append(f"{tx_id} is COMMITTED in the DB but not on the chain")
            continue
        t = c["tx"]
        if c["block"] != r.get("block_index") or c["pos"] != r.get("tx_position"):
            problems.append(f"{tx_id}: DB says block {r.get('block_index')}/pos {r.get('tx_position')}, "
                            f"chain says block {c['block']}/pos {c['pos']}")
        if int(t.get("amount", -1)) != int(r["amount"]):
            problems.append(f"{tx_id}: amount DB {r['amount']} vs chain {t.get('amount')}")
        want_type = CHAIN_TYPE.get(r["tx_type"])
        if t.get("type") != want_type:
            problems.append(f"{tx_id}: type DB {r['tx_type']} vs chain {t.get('type')}")
        if r["tx_type"] in ("ALLOCATION", "DISBURSEMENT") and str(t.get("parent")) != str(r.get("parent_tx_id")):
            problems.append(f"{tx_id}: parent DB {r.get('parent_tx_id')} vs chain {t.get('parent')}")
        if r["tx_type"] == "DISBURSEMENT":
            want_memo = r.get("receipt_hash")
        else:
            want_memo = r.get("campaign_id") or ""
        if str(t.get("memo", "")) != str(want_memo or ""):
            problems.append(f"{tx_id}: memo DB {want_memo} vs chain {t.get('memo')}")
    check("every COMMITTED row sits on the chain with the same block, position, type, amount, parent and memo",
          not problems, first(problems))

    highest = max((r["block_index"] for r in rows if r.get("block_index") is not None), default=-1)
    check("no COMMITTED row points beyond the end of the chain", highest < chain_height,
          f"highest block_index in DB is {highest}, chain has {chain_height} blocks")

    problems = [f"{tx_id} is on the chain but the DB says {row.get('status')}"
                for tx_id, row in ((r["tx_id"], r) for r in rows)
                if tx_id in chain_index and row.get("status") != "COMMITTED"]
    check("every gateway transaction that is on the chain is COMMITTED in the DB", not problems, first(problems))

    known = {r["tx_id"] for r in rows}
    foreign = [i for i in chain_index if i not in known]
    if foreign:
        info(f"{len(foreign)} transaction(s) on the chain are not in the gateway DB "
             f"(made outside the gateway, or the DB was reset)")

    now = datetime.now(timezone.utc)
    limit = timedelta(minutes=PENDING_MINUTES)
    stale = []
    for r in rows:
        if r.get("status") == "PENDING":
            ts = parse_ts(r.get("created_at"))
            if ts and now - ts > limit:
                stale.append(f"{r['tx_id']} pending since {r['created_at']}")
    check(f"no PENDING row older than {PENDING_MINUTES} minutes", not stale, first(stale))
    pending = sum(1 for r in rows if r.get("status") == "PENDING")
    if pending:
        info(f"{pending} PENDING row(s) in total")


def check_totals(rows):
    committed = [r for r in rows if r.get("status") == "COMMITTED"]
    donations = {r["tx_id"]: r for r in committed if r["tx_type"] == "DONATION"}
    allocs = {r["tx_id"]: r for r in committed if r["tx_type"] == "ALLOCATION"}
    alloc_by_don, disb_by_alloc = defaultdict(list), defaultdict(list)
    for r in allocs.values():
        alloc_by_don[r.get("parent_tx_id")].append(r)
    for r in committed:
        if r["tx_type"] == "DISBURSEMENT":
            disb_by_alloc[r.get("parent_tx_id")].append(r)

    problems = []
    for a in allocs.values():
        if a.get("parent_tx_id") not in donations:
            problems.append(f"allocation {a['tx_id']} has no committed DONATION parent")
    for d in committed:
        if d["tx_type"] == "DISBURSEMENT" and d.get("parent_tx_id") not in allocs:
            problems.append(f"disbursement {d['tx_id']} has no committed ALLOCATION parent")
    for did, d in donations.items():
        total = sum(int(a["amount"]) for a in alloc_by_don[did])
        if total > int(d["amount"]):
            problems.append(f"donation {did}: allocated {total} > donated {d['amount']}")
    for aid, a in allocs.items():
        total = sum(int(x["amount"]) for x in disb_by_alloc[aid])
        if total > int(a["amount"]):
            problems.append(f"allocation {aid}: spent {total} > allocated {a['amount']}")
    check("DB totals: no over-allocation, no over-spending, every parent is committed", not problems,
          first(problems))

    problems = []
    ids = list(donations)[:MAX_TRACES]
    if len(donations) > MAX_TRACES:
        info(f"checking {MAX_TRACES} of {len(donations)} donations against /trace")
    for did in ids:
        status, tr = get_json(f"{GATEWAY}/api/donations/{did}/trace")
        if status != 200 or not isinstance(tr, dict):
            problems.append(f"donation {did}: trace answered {status}")
            continue
        d = donations[did]
        my_allocs = alloc_by_don[did]
        want_allocated = sum(int(a["amount"]) for a in my_allocs)
        want_spent = sum(int(x["amount"]) for a in my_allocs for x in disb_by_alloc[a["tx_id"]])
        if tr.get("donation", {}).get("amount") != int(d["amount"]):
            problems.append(f"donation {did}: trace amount {tr.get('donation', {}).get('amount')} vs DB {d['amount']}")
        if tr.get("allocated") != want_allocated:
            problems.append(f"donation {did}: trace allocated {tr.get('allocated')} vs DB {want_allocated}")
        if tr.get("unallocated") != int(d["amount"]) - want_allocated:
            problems.append(f"donation {did}: trace unallocated {tr.get('unallocated')} "
                            f"vs DB {int(d['amount']) - want_allocated}")
        if tr.get("spent_total") != want_spent:
            problems.append(f"donation {did}: trace spent_total {tr.get('spent_total')} vs DB {want_spent}")
        trace_allocs = {a.get("id"): a for a in tr.get("allocations", [])}
        for a in my_allocs:
            ta = trace_allocs.get(a["tx_id"])
            spent = sum(int(x["amount"]) for x in disb_by_alloc[a["tx_id"]])
            if not ta:
                problems.append(f"donation {did}: allocation {a['tx_id']} missing in trace")
            elif ta.get("spent") != spent or ta.get("remaining") != int(a["amount"]) - spent:
                problems.append(f"allocation {a['tx_id']}: trace spent/remaining {ta.get('spent')}/{ta.get('remaining')}"
                                f" vs DB {spent}/{int(a['amount']) - spent}")
    check(f"trace totals match the DB for {len(ids)} committed donation(s)", not problems, first(problems))


def main():
    print(f"Gateway: {GATEWAY}\nNodes:   {', '.join(NODES)}\n")
    reference = check_chain_nodes()
    if reference is None:
        sys.exit(1)
    chain_index = index_chain(reference)
    rows = fetch_rows()
    if rows is None:
        sys.exit(1)
    info(f"{len(rows)} gateway transaction(s), {len(chain_index)} transaction(s) on the chain")
    check_rows_against_chain(rows, chain_index, len(reference))
    check_totals(rows)

    print()
    if failures:
        print(f"INCOHERENT: {len(failures)} check(s) failed")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("COHERENT: the gateway index matches the chain")


if __name__ == "__main__":
    main()