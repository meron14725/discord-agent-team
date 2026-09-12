"""Derive the approved four-syscall experiment from a pinned, verified Moby profile."""

import copy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = frozenset({"clone", "clone3", "unshare", "setns"})


def build(original):
    derived = copy.deepcopy(original)
    retained = []
    for rule in derived["syscalls"]:
        rule["names"] = [name for name in rule["names"] if name not in ALLOWED]
        if rule["names"]:
            retained.append(rule)
    retained.append({"names": sorted(ALLOWED), "action": "SCMP_ACT_ALLOW"})
    derived["syscalls"] = retained
    return derived


def main():
    raw = (ROOT / "docker/seccomp-default.json").read_bytes()
    metadata = json.loads((ROOT / "docker/seccomp-source.json").read_text())
    if hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
        raise RuntimeError("Pinned Moby profile checksum mismatch")
    result = build(json.loads(raw))
    target = ROOT / "docker/seccomp-codex-four-syscalls.json"
    target.write_text(json.dumps(result, indent=2) + "\n")
    print("Verified source checksum; changed only: " + ", ".join(sorted(ALLOWED)))


if __name__ == "__main__":
    main()
