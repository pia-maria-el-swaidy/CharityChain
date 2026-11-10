"""Start the BFT network on this computer, without Docker: 4 validators (ports 5001-5004) and an observer (5005).

    python make_validators.py        once: creates validators.json
    python run_bft_local.py          starts the nodes in this terminal; Ctrl+C stops them all
    python run_bft_local.py --data   also saves every node's ledger in the folder  data/  (it survives a restart)

Then use the nodes exactly like the containers:  http://127.0.0.1:5001/status  ...
and  python wallet.py send ...  to submit signed transactions.
"""
import json
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE_PORT = 5001


def main():
    path = os.path.join(HERE, "validators.json")
    if not os.path.exists(path):
        sys.exit("validators.json not found: run  python make_validators.py  first")
    people = json.load(open(path))
    urls = [f"http://127.0.0.1:{BASE_PORT + i}" for i in range(len(people) + 1)]       # the last one is the observer
    keys = ",".join(p["public"] for p in people)
    procs = []
    for i in range(len(urls)):
        cmd = [sys.executable, "node_bft.py", "--port", str(BASE_PORT + i), "--peers", ",".join(urls), "--self-url", urls[i],
               "--validators", keys]
        if i < len(people):
            cmd += ["--validator-key", people[i]["private"]]
        if "--data" in sys.argv:
            cmd += ["--data-dir", os.path.join(HERE, "data", f"node{i + 1}")]
        procs.append(subprocess.Popen(cmd, cwd=HERE))
    print(f"{len(people)} validators and 1 observer started: {', '.join(urls)}\nCtrl+C stops them.")

    def stop(*_):
        for pr in procs:
            pr.terminate()
        for pr in procs:
            pr.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()