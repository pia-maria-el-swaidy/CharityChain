"""Checks docker-compose.yml (BFT) without Docker: every node gets its own key, the same validator list, a volume.
Run:  python3 test_bft_compose.py"""
import os
import re
import subprocess
import sys
import tempfile

import yaml

import wallet

results = []


def check(name, got, expected):
    ok = got == expected
    print(("OK   " if ok else "FAIL ") + name)
    if not ok:
        print("   got:     ", got)
        print("   expected:", expected)
    results.append(ok)


here = os.path.dirname(os.path.abspath(__file__))
compose = yaml.safe_load(open(os.path.join(here, "docker-compose.yml")))
with tempfile.TemporaryDirectory() as tmp:
    subprocess.run([sys.executable, os.path.join(here, "make_validators.py"), "--dir", tmp], check=True, capture_output=True)
    env = dict(line.split("=", 1) for line in open(os.path.join(tmp, ".env")).read().split())


def substitute(text):
    def one(match):
        return env[match.group(1)]            # a missing variable raises KeyError: docker compose would refuse too
    return re.sub(r"\$\{(\w+):\?[^}]*\}", one, text)


services = compose["services"]
check("four validators and an observer", sorted(services), ["node1", "node2", "node3", "node4", "observer"])
envs = {name: {k: substitute(str(v)) for k, v in svc["environment"].items()} for name, svc in services.items()}
validators = env["VALIDATORS"].split(",")
nodes = [f"node{k}" for k in range(1, 5)]
check("every service gets the same validator list", {e["VALIDATORS"] for e in envs.values()}, {env["VALIDATORS"]})
check("every validator has its own private key", len({envs[n]["VALIDATOR_KEY"] for n in nodes}), 4)
check("node k holds the key of validator k-1", [wallet.public_key_of(envs[n]["VALIDATOR_KEY"]) for n in nodes], validators)
check("the observer has no key: it cannot vote", "VALIDATOR_KEY" in envs["observer"], False)
check("every node lists all five nodes as peers", {tuple(e["PEERS"].split(",")) for e in envs.values()},
      {tuple([f"http://node{k}:5000" for k in range(1, 5)] + ["http://observer:5000"])})
check("SELF_URL is the node's own address", [envs[n]["SELF_URL"] for n in nodes + ["observer"]],
      [f"http://{n}:5000" for n in nodes + ["observer"]])
check("host ports 5001-5005 map to the container port 5000",
      [services[n]["ports"] for n in nodes + ["observer"]], [[f"{5000 + k}:5000"] for k in range(1, 6)])
check("every node saves its ledger in its own volume", [services[n]["volumes"] for n in nodes + ["observer"]],
      [[f"{n}-data:/data"] for n in nodes + ["observer"]])
check("the volumes are declared", sorted(compose["volumes"]), sorted(f"{n}-data" for n in nodes + ["observer"]))
check("DATA_DIR points to the volume", {e["DATA_DIR"] for e in envs.values()}, {"/data"})
check("the nodes run node_bft.py", {tuple(s["command"]) for s in services.values()}, {("python", "node_bft.py")})
check("the timeouts are set", all(k in envs["node1"] for k in ("TIMEOUT_PROPOSE", "TIMEOUT_PREVOTE", "TIMEOUT_PRECOMMIT", "TIMEOUT_DELTA")), True)
dockerfile = open(os.path.join(here, "Dockerfile")).read()
check("the Dockerfile copies the BFT files and starts node_bft.py by default",
      all(f in dockerfile for f in ("bft.py", "node_bft.py", "rules.py", "wallet.py", "core.py")) and 'CMD ["python", "node_bft.py"]' in dockerfile, True)
check("the Dockerfile does not copy the secrets",
      all(f in open(os.path.join(here, ".dockerignore")).read() for f in (".env", "validators.json")), True)
check("the older setups are kept", all(os.path.exists(os.path.join(here, f)) for f in ("docker-compose.poa.yml", "docker-compose.pow.yml")), True)

print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")
sys.exit(0 if all(results) else 1)