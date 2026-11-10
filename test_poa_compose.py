"""Checks Dockerfile-compose.yml without Docker: every node gets its own key, the same validator list, the right peers.
Run:  python3 test_poa_compose.py"""
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
compose = yaml.safe_load(open(os.path.join(here, "Dockerfile-compose.yml")))
with tempfile.TemporaryDirectory() as tmp:
    subprocess.run([sys.executable, os.path.join(here, "make_validators.py"), "--dir", tmp], check=True, capture_output=True)
    env = dict(line.split("=", 1) for line in open(os.path.join(tmp, ".env")).read().split())


def substitute(text):
    """What Dockerfile compose does with ${VAR:?message}."""
    def one(match):
        if match.group(1) not in env:
            raise KeyError(match.group(1))
        return env[match.group(1)]
    return re.sub(r"\$\{(\w+):\?[^}]*\}", one, text)


services = compose["services"]
check("four services node1 ... node4", sorted(services), ["node1", "node2", "node3", "node4"])
envs = {name: {k: substitute(str(v)) for k, v in svc["environment"].items()} for name, svc in services.items()}
validators = env["VALIDATORS"].split(",")
check("every service gets the same validator list", {e["VALIDATORS"] for e in envs.values()}, {env["VALIDATORS"]})
check("every service has its own private key", len({e["VALIDATOR_KEY"] for e in envs.values()}), 4)
check("node k holds the key of validator k-1 (turn order = node order)",
      [wallet.public_key_of(envs[f"node{k}"]["VALIDATOR_KEY"]) for k in range(1, 5)], validators)
check("the peer list names all four nodes", {tuple(e["PEERS"].split(",")) for e in envs.values()},
      {tuple(f"http://node{k}:5000" for k in range(1, 5))})
check("SELF_URL is the node's own address",
      [envs[f"node{k}"]["SELF_URL"] for k in range(1, 5)], [f"http://node{k}:5000" for k in range(1, 5)])
check("host ports 5001-5004 map to the container port 5000",
      [services[f"node{k}"]["ports"] for k in range(1, 5)], [[f"{5000 + k}:5000"] for k in range(1, 5)])
check("the nodes run node_poa.py", {tuple(s["command"]) for s in services.values()}, {("python", "node_poa.py")})
check("the Dockerfile copies the PoA files",
      all(f in open(os.path.join(here, "Dockerfile")).read() for f in ("poa.py", "node_poa.py", "rules.py", "wallet.py", "core.py")), True)
check("the Dockerfile does not copy the secrets",
      all(f in open(os.path.join(here, ".dockerignore")).read() for f in (".env", "validators.json")), True)
check("the old Proof-of-Work compose file is kept",
      os.path.exists(os.path.join(here, "Dockerfile-compose.pow.yml")), True)

print("\nALL PASSED" if all(results) else "\nSOME CHECKS FAILED")