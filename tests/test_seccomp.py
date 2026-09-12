import copy
import hashlib
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHANGED = {"clone", "clone3", "setns", "unshare"}


def rules_for(profile, syscall):
    rules = []
    for rule in profile["syscalls"]:
        if syscall in rule["names"]:
            rules.append({key: value for key, value in rule.items() if key != "names"})
    return rules


def test_profile_origin_is_pinned_and_checksum_matches():
    metadata = json.loads((ROOT / "docker/seccomp-source.json").read_text())
    assert len(metadata["commit"]) == 40
    assert "/" + metadata["commit"] + "/" in metadata["url"]
    assert (
        hashlib.sha256((ROOT / "docker/seccomp-default.json").read_bytes()).hexdigest() == metadata["sha256"]
    )


def test_no_permission_changes_outside_four_approved_syscalls():
    original = json.loads((ROOT / "docker/seccomp-default.json").read_text())
    custom = json.loads((ROOT / "docker/seccomp-codex-four-syscalls.json").read_text())
    original_top, custom_top = copy.deepcopy(original), copy.deepcopy(custom)
    original_top.pop("syscalls")
    custom_top.pop("syscalls")
    assert original_top == custom_top
    names = {n for p in (original, custom) for r in p["syscalls"] for n in r["names"]}
    differences = {name for name in names if rules_for(original, name) != rules_for(custom, name)}
    assert differences == CHANGED
    for name in CHANGED:
        assert rules_for(custom, name) == [{"action": "SCMP_ACT_ALLOW"}]
    # Mount is NOT silently granted when the four-syscall experiment fails there.
    assert rules_for(custom, "mount") == rules_for(original, "mount")


def test_experiment_is_opt_in_and_only_affects_workers():
    base = yaml.safe_load((ROOT / "compose.yaml").read_text())
    overlay = yaml.safe_load((ROOT / "compose.sandbox-test.yaml").read_text())
    assert set(overlay["services"]) == {"upstream-worker", "downstream-worker"}
    for service in overlay["services"].values():
        assert set(service) == {"security_opt"}
        assert service["security_opt"] == ["seccomp=./docker/seccomp-codex-four-syscalls.json"]
    for role in overlay["services"]:
        settings = base["services"][role]
        assert settings["security_opt"] == ["no-new-privileges:true"]
        assert settings["cap_drop"] == ["ALL"]
        assert settings["read_only"] is True
        assert not settings.get("privileged", False)
