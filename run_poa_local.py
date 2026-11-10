"""Start the four PoA validators on this computer, without Docker (ports 5001-5004).

    python make_validators.py        once: creates validators.json
    python run_poa_local.py          starts node1 ... node4 in this terminal; Ctrl+C stops them all

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
    urls = [f"http://127.0.0.1:{BASE_PORT + i}" for i in range(len(people))]
    procs = []
    for i, person in enumerate(people):
        procs.append(subprocess.Popen(
            [sys.executable, "node_poa.py", "--port", str(BASE_PORT + i), "--peers", ",".join(urls), "--self-url", urls[i],
             "--validators", ",".join(p["public"] for p in people), "--validator-key", person["private"],
             "--block-interval", "2", "--sync-interval", "5"], cwd=HERE))
    print(f"{len(procs)} validators started: {', '.join(urls)}\nCtrl+C stops them.")

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