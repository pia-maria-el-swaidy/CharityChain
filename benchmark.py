"""TraceChain - Exercise 6: measurements for the report.

    python benchmark.py --docker     uses `docker compose` (starts the three containers of docker-compose.yml,
                                     reachable on ports 5001-5003)
    python benchmark.py --local      starts three local node processes by itself (no Docker needed)

Options:  --only 1 2 3   run only some experiments        --samples 10   blocks mined in experiment 1
          --tx 600       transactions sent in experiment 2   --time-limit 40  seconds allowed for experiment 2
          --prefill 5    blocks mined before the failure test of experiment 3
          --no-up        (docker) the containers are already running
          --down         (docker) run `docker compose down` at the end

Experiment 1: time to mine one block (difficulty 4) and time for it to reach all nodes
Experiment 2: transactions per minute that can be submitted before the node slows down
Experiment 3: stop one node, mine two blocks on another, restart it, time until it has caught up

Writes results.md (to paste into the report) and results.json.
"""
import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
import uuid

import requests

import core
import wallet

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = uuid.uuid4().hex[:6]                       # makes the transaction ids of this run unique
DONOR = wallet.generate_keypair()
CHARITY = wallet.generate_keypair()
signatures = True                                # set from the node's answer at the start


# ------------------------------------------------------------------ clusters

class LocalCluster:
    """Three node processes on this computer (what Docker Compose does, without containers)."""
    name = "local processes"

    def __init__(self):
        self.ports = [5901, 5902, 5903]
        self.urls = [f"http://127.0.0.1:{p}" for p in self.ports]
        self.procs = {}

    def _spawn(self, i):
        self.procs[i] = subprocess.Popen(
            [sys.executable, "node.py", "--port", str(self.ports[i]), "--peers", ",".join(self.urls),
             "--self-url", self.urls[i]], cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def up(self):
        for i in range(3):
            self._spawn(i)

    def stop_node(self, i):
        self.procs[i].terminate()
        self.procs[i].wait()

    def start_node(self, i):
        self._spawn(i)

    def shutdown(self, down=False):
        for pr in self.procs.values():
            pr.terminate()
        for pr in self.procs.values():
            pr.wait()


class DockerCluster:
    name = "Docker Compose (3 containers)"

    def __init__(self):
        self.urls = [f"http://127.0.0.1:{p}" for p in (5001, 5002, 5003)]

    def _compose(self, *args):
        subprocess.run(["docker", "compose", "-f", os.path.join(HERE, "docker-compose.yml"), *args],
                       cwd=HERE, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def up(self):
        self._compose("up", "-d", "--build")

    def stop_node(self, i):
        self._compose("stop", f"node{i + 1}")

    def start_node(self, i):
        self._compose("start", f"node{i + 1}")

    def shutdown(self, down=False):
        if down:
            self._compose("down")


# ------------------------------------------------------------------ helpers

def wait_up(url, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        try:
            requests.get(url + "/chain", timeout=1)
            return True
        except requests.RequestException:
            time.sleep(0.1)
    return False


def chain_length(url):
    return requests.get(url + "/chain", timeout=10).json()["length"]


def donation(index):
    """A donation of 1 unit; signed with the donor's key if the nodes require signatures."""
    tx = {"id": f"bench-{RUN}-{index}", "type": "DONATION", "sender": DONOR[1] if signatures else "donor-bench",
          "parent": CHARITY[1] if signatures else "charity-bench", "amount": 1, "memo": ""}
    return wallet.sign_transaction(tx, DONOR[0]) if signatures else tx


counter = [0]


def next_donation():
    counter[0] += 1
    return donation(counter[0])


def submit(session, url, tx):
    r = session.post(url + "/transactions/new", json=tx, timeout=60)
    if r.status_code != 201:
        raise RuntimeError(f"transaction refused: {r.status_code} {r.text}")


def wait_all_have(urls, length, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        if all(chain_length(u) >= length for u in urls):
            return True
        time.sleep(0.01)
    return False


def stats(values):
    return {"min": min(values), "mean": statistics.mean(values), "median": statistics.median(values), "max": max(values)}


# ------------------------------------------------------------------ experiment 1

def experiment_1(urls, samples):
    print(f"\nExperiment 1: mine {samples} blocks on node 1, difficulty {core.DIFFICULTY}")
    s = requests.Session()
    rows = []
    for i in range(samples):
        submit(s, urls[0], next_donation())
        t0 = time.time()
        assert s.get(urls[0] + "/mine", timeout=10).status_code == 202
        while True:
            st = s.get(urls[0] + "/mine/status", timeout=10).json()
            if st["state"] != "mining":
                break
            time.sleep(0.01)
        t_done = time.time()
        if st["state"] != "done":
            print("   job did not finish:", st.get("message"))
            continue
        target = st["block"]["index"] + 1
        ok = wait_all_have(urls[1:], target)
        t_all = time.time()
        rows.append({"mining_s": st["seconds"], "attempts": st["block"]["nonce"] + 1,
                     "propagation_s": (t_all - t_done) if ok else None, "total_s": t_all - t0})
        print(f"   block {st['block']['index']:>3}: mined in {st['seconds']:.3f} s "
              f"({st['block']['nonce'] + 1:>7} attempts), reached all nodes {1000 * (t_all - t_done):.0f} ms later")
    prop = [r["propagation_s"] for r in rows if r["propagation_s"] is not None]
    return {"samples": len(rows), "rows": rows, "mining": stats([r["mining_s"] for r in rows]),
            "propagation": stats(prop) if prop else None,
            "hash_rate": sum(r["attempts"] for r in rows) / sum(r["mining_s"] for r in rows),
            "expected_attempts": 16 ** core.DIFFICULTY}


# ------------------------------------------------------------------ experiment 2

def experiment_2(urls, max_tx, time_limit, bucket=50):
    print(f"\nExperiment 2: submit up to {max_tx} transactions to node 1 (limit {time_limit} s), without mining")
    txs = [next_donation() for _ in range(max_tx)]                 # signed beforehand: signing is not measured
    s = requests.Session()
    buckets = []
    t_start = t_bucket = time.time()
    sent = 0
    for tx in txs:
        submit(s, urls[0], tx)
        sent += 1
        if sent % bucket == 0:
            now = time.time()
            buckets.append({"up_to": sent, "seconds": now - t_bucket, "tx_per_min": bucket / (now - t_bucket) * 60,
                            "ms_per_tx": 1000 * (now - t_bucket) / bucket})
            t_bucket = now
            print(f"   transactions {sent - bucket + 1:>4}-{sent:>4}: {buckets[-1]['tx_per_min']:>8.0f} per minute "
                  f"({buckets[-1]['ms_per_tx']:.1f} ms each)")
        if time.time() - t_start > time_limit:
            break
    elapsed = time.time() - t_start
    slow_at = next((b["up_to"] for b in buckets if b["tx_per_min"] < 0.5 * buckets[0]["tx_per_min"]), None)
    t0 = time.time()                                               # all of them go into one block
    r = s.get(urls[0] + "/mine?wait=1", timeout=600)
    mine_seconds = time.time() - t0 if r.status_code == 200 else None
    return {"sent": sent, "seconds": elapsed, "average_tx_per_min": sent / elapsed * 60, "buckets": buckets,
            "bucket_size": bucket, "slows_down_at": slow_at, "mine_all_seconds": mine_seconds}


# ------------------------------------------------------------------ experiment 3

def experiment_3(cluster, prefill):
    urls = cluster.urls
    print(f"\nExperiment 3: stop node 3, mine two blocks on node 1, restart node 3 ({prefill} blocks before the failure)")
    s = requests.Session()
    for _ in range(prefill):
        submit(s, urls[0], next_donation())
        s.get(urls[0] + "/mine?wait=1", timeout=600)
    wait_all_have(urls, chain_length(urls[0]))
    before = chain_length(urls[0])

    t0 = time.time()
    cluster.stop_node(2)
    stop_s = time.time() - t0
    print(f"   node 3 stopped ({stop_s:.1f} s). Chain length: {before}")

    outage = []
    for _ in range(2):
        submit(s, urls[0], next_donation())
        t0 = time.time()
        r = s.get(urls[0] + "/mine?wait=1", timeout=600).json()
        outage.append({"seconds": time.time() - t0, "unreachable": r.get("unreachable_peers", []),
                       "notified": r.get("notified_peers", [])})
        print(f"   block {r['block']['index']} mined in {outage[-1]['seconds']:.2f} s; "
              f"notified {len(outage[-1]['notified'])}, unreachable {outage[-1]['unreachable']}")
    wait_all_have(urls[:2], before + 2)
    node2_len = chain_length(urls[1])

    t0 = time.time()
    cluster.start_node(2)
    started_s = time.time() - t0
    up = wait_up(urls[2])
    up_s = time.time() - t0
    len_at_start = chain_length(urls[2]) if up else None
    t1 = time.time()
    r = requests.get(urls[2] + "/nodes/resolve", timeout=120).json()
    resolve_s = time.time() - t1
    total_s = time.time() - t0
    chains = [requests.get(u + "/chain", timeout=30).json()["chain"] for u in urls]
    result = {"blocks_before": before, "blocks_after": chain_length(urls[0]), "stop_seconds": stop_s,
              "outage_mining": outage, "node2_followed": node2_len == before + 2,
              "restart_command_s": started_s, "ready_s": up_s, "length_when_ready": len_at_start,
              "resolve_s": resolve_s, "resolve_answer": r, "total_catch_up_s": total_s,
              "identical_chains": chains[0] == chains[1] == chains[2],
              "valid": all(requests.get(u + "/validate", timeout=30).json()["valid"] for u in urls)}
    print(f"   node 2 followed without help: {result['node2_followed']}")
    print(f"   node 3 restarted: ready after {up_s:.1f} s, ledger length at that moment: {len_at_start}")
    print(f"   /nodes/resolve on node 3: {r}, took {resolve_s:.2f} s")
    print(f"   total from restart to caught up: {total_s:.1f} s | identical chains: {result['identical_chains']} "
          f"| valid: {result['valid']}")
    return result


# ------------------------------------------------------------------ report

def fmt(x, digits=3):
    return "-" if x is None else f"{x:.{digits}f}"


def write_report(res, cluster, outdir):
    lines = ["# TraceChain measurements", "",
             f"- Deployment: {cluster.name}, 3 nodes, Proof of Work difficulty {core.DIFFICULTY}, "
             f"signatures {'required' if signatures else 'not required'}",
             f"- Machine: {platform.platform()}, Python {platform.python_version()}, {os.cpu_count()} CPU threads",
             f"- Date: {time.strftime('%Y-%m-%d %H:%M')}", ""]
    if "1" in res:
        r = res["1"]
        m, p = r["mining"], r["propagation"]
        lines += ["## 1. Mining time and propagation", "",
                  f"{r['samples']} blocks mined on node 1 (one transaction each). The expected number of attempts at "
                  f"difficulty {core.DIFFICULTY} is {r['expected_attempts']:,}, but the real number varies a lot, because "
                  "finding a nonce is a game of chance.", "",
                  "| | min | mean | median | max |", "|---|---|---|---|---|",
                  f"| Time to mine one block (s) | {fmt(m['min'])} | {fmt(m['mean'])} | {fmt(m['median'])} | {fmt(m['max'])} |"]
        if p:
            lines.append(f"| Time until all nodes have it (s) | {fmt(p['min'])} | {fmt(p['mean'])} | {fmt(p['median'])} | {fmt(p['max'])} |")
        lines += ["", f"Hash rate of one node: about {r['hash_rate']:,.0f} hashes per second (single thread).", ""]
    if "2" in res:
        r = res["2"]
        lines += ["## 2. Transactions per minute", "",
                  f"{r['sent']} transactions submitted to node 1 in {r['seconds']:.1f} s, without mining, "
                  f"one client: **{r['average_tx_per_min']:,.0f} transactions per minute on average**. ",
                  (f"The rate fell below half of the initial rate after {r['slows_down_at']} pending transactions."
                   if r["slows_down_at"] else "The rate did not fall below half of the initial rate in this run."), "",
                  "| Transactions | per minute | ms per transaction |", "|---|---|---|"]
        for b in r["buckets"]:
            lines.append(f"| {b['up_to'] - r['bucket_size'] + 1}-{b['up_to']} | {b['tx_per_min']:,.0f} | {b['ms_per_tx']:.1f} |")
        lines += ["", "Why it slows down: for every submitted transaction the node replays the whole chain and all pending "
                  "transactions to check rules R1-R4 and the signatures, so the cost grows with the ledger size. "
                  "A cached state, updated incrementally, would remove this cost.", "",
                  f"Mining one block with all of them took {fmt(r['mine_all_seconds'], 2)} s.", ""]
    if "3" in res:
        r = res["3"]
        times = ", ".join(f"{o['seconds']:.2f} s" for o in r["outage_mining"])
        down = all(o["unreachable"] for o in r["outage_mining"])
        lines += ["## 3. Failure and recovery of one node", "",
                  f"Chain length before the failure: {r['blocks_before']}. Node 3 stopped, two blocks mined on node 1, node 3 restarted.", "",
                  "| Step | Result |", "|---|---|",
                  f"| Mining while node 3 was down | {times}; node 3 reported as unreachable: {down} |",
                  f"| Node 2 followed without any call | {r['node2_followed']} |",
                  f"| Node 3 ready again after | {r['ready_s']:.1f} s (its ledger had {r['length_when_ready']} block: it starts empty) |",
                  f"| `/nodes/resolve` on node 3 | {r['resolve_s']:.2f} s -> {r['resolve_answer']} |",
                  f"| **Total from restart to caught up** | **{r['total_catch_up_s']:.1f} s** |",
                  f"| Chains identical on all nodes, all valid | {r['identical_chains']}, {r['valid']} |", ""]
    path = os.path.join(outdir, "results.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(outdir, "results.json"), "w") as f:
        json.dump(res, f, indent=2)
    return path


def main():
    global signatures
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--docker", action="store_true")
    mode.add_argument("--local", action="store_true")
    ap.add_argument("--only", nargs="+", choices=["1", "2", "3"], default=["1", "2", "3"])
    ap.add_argument("--samples", type=int, default=10)
    ap.add_argument("--tx", type=int, default=600)
    ap.add_argument("--time-limit", type=float, default=40)
    ap.add_argument("--prefill", type=int, default=5)
    ap.add_argument("--no-up", action="store_true", help="(docker) the containers are already running")
    ap.add_argument("--down", action="store_true", help="(docker) docker compose down at the end")
    ap.add_argument("--output-dir", default=HERE)
    args = ap.parse_args()

    cluster = DockerCluster() if args.docker else LocalCluster()
    try:
        if not (args.docker and args.no_up):
            print(f"starting 3 nodes: {cluster.name} ...")
            cluster.up()
        for u in cluster.urls:
            if not wait_up(u):
                sys.exit(f"node {u} does not answer")
        signatures = requests.get(cluster.urls[0] + "/").json().get("signatures_required", True)
        res = {}
        if "1" in args.only:
            res["1"] = experiment_1(cluster.urls, args.samples)
        if "2" in args.only:
            res["2"] = experiment_2(cluster.urls, args.tx, args.time_limit)
        if "3" in args.only:
            res["3"] = experiment_3(cluster, args.prefill)
        print("\nreport written to", write_report(res, cluster, args.output_dir))
    finally:
        cluster.shutdown(down=args.down)


if __name__ == "__main__":
    main()