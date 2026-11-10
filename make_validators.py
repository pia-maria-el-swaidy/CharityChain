"""TraceChain - create the validator keys of the Proof of Authority network.

    python make_validators.py               4 validators: writes validators.json and .env
    python make_validators.py --count 7     a different number of validators
    python make_validators.py --force       replace existing files (this changes the network: old chains become invalid)

validators.json   names, private and public keys (for you: tests, benchmarks, demos)
.env              what Dockerfile compose reads: VALIDATORS (all public keys, in turn order) and one private
                  key per node: VALIDATOR_KEY_1 ... VALIDATOR_KEY_n

Lab only: private keys sit in plain text files. Never commit these two files.
"""
import argparse
import json
import os
import sys

import wallet

NAMES = ["hope-foundation", "auditor", "ministry", "donors-association"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=4)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)))
    args = ap.parse_args()
    if args.count < 1:
        sys.exit("--count must be at least 1")

    json_path = os.path.join(args.dir, "validators.json")
    env_path = os.path.join(args.dir, ".env")
    existing = [p for p in (json_path, env_path) if os.path.exists(p)]
    if existing and not args.force:
        sys.exit(f"{', '.join(os.path.basename(p) for p in existing)} already exist(s). "
                 "Use --force to replace them (this creates a NEW network).")

    people = []
    for i in range(args.count):
        private, public = wallet.generate_keypair()
        name = NAMES[i] if i < len(NAMES) else f"validator-{i + 1}"
        people.append({"index": i, "name": name, "private": private, "public": public})

    with open(json_path, "w") as f:
        json.dump(people, f, indent=2)
    with open(env_path, "w") as f:
        f.write("VALIDATORS=" + ",".join(p["public"] for p in people) + "\n")
        for p in people:
            f.write(f"VALIDATOR_KEY_{p['index'] + 1}={p['private']}\n")

    print(f"{args.count} validators created ({json_path}, {env_path}):")
    for p in people:
        print(f"   validator {p['index']}  node{p['index'] + 1}  {p['name']:20} {p['public'][:16]}...")


if __name__ == "__main__":
    main()