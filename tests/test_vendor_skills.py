import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
VENDOR = ROOT / "vendor" / "agent-skills"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_vendored_skill_manifest_pins_and_covers_every_file():
    manifest = json.loads((VENDOR / "manifest.json").read_text())

    assert manifest["schema_version"] == 1
    assert manifest["runtime_execution_allowed"] is False
    assert {snapshot["id"]: snapshot["commit"] for snapshot in manifest["snapshots"]} == {
        "mattpocock-skills": "3cca18b368ae95cdbdebbff572ccafa662551015",
        "keitakn-engineering-skills": "b785a355ff83a10515a246026a0595a30ef41827",
    }

    listed = set()
    for snapshot in manifest["snapshots"]:
        license_path = VENDOR / snapshot["license_path"]
        assert not license_path.is_symlink()
        assert not license_path.stat().st_mode & 0o111
        assert sha256(license_path) == snapshot["license_sha256"]
        listed.add(license_path.relative_to(VENDOR).as_posix())
        for entry in snapshot["files"]:
            path = VENDOR / entry["path"]
            assert path.is_file()
            assert not path.is_symlink()
            assert not path.stat().st_mode & 0o111
            assert sha256(path) == entry["sha256"]
            listed.add(path.relative_to(VENDOR).as_posix())

    actual = {
        path.relative_to(VENDOR).as_posix()
        for path in VENDOR.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    assert actual == listed


def test_third_party_notice_names_both_sources_and_pinned_commits():
    notice = (ROOT / "THIRD_PARTY-LICENSES.md").read_text()

    assert "Copyright (c) 2026 Matt Pocock" in notice
    assert "Copyright (c) 2026 keita-koga" in notice
    assert "3cca18b368ae95cdbdebbff572ccafa662551015" in notice
    assert "b785a355ff83a10515a246026a0595a30ef41827" in notice
