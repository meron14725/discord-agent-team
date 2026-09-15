#!/usr/bin/env python3
"""Prepare a private, pinned macOS supervisor installation; does not start it.

Run only during the human-approved bootstrap, using a reviewed checkout.
Starting launchd and enabling the controller are separate explicit steps.
"""

import argparse
import json
import os
import plistlib
import shutil
import sys
from pathlib import Path


def prepare(config, target, python):
    target = Path(target).resolve()
    project = Path(config["project_root"]).resolve()
    if target.is_relative_to(project):
        raise ValueError("Supervisor must be outside the project checkout")
    if target.exists():
        raise ValueError("Refuse to overwrite an existing trusted supervisor installation")
    if not Path(python).is_absolute() or not Path(python).is_file():
        raise ValueError("Use an absolute path to a trusted Python 3.12+ interpreter")
    target.mkdir(mode=0o700, parents=True)
    shutil.copyfile(Path(__file__).with_name("deploy_supervisor.py"), target / "supervisor.py")
    (target / "supervisor.py").chmod(0o500)
    installed_config = {**config, "state_dir": str(target / "state")}
    (target / "config.json").write_text(json.dumps(installed_config, indent=2))
    (target / "config.json").chmod(0o600)
    launch = {
        "Label": "com.meron14725.discord-agent-deployer",
        "ProgramArguments": [python, str(target / "supervisor.py"), "--config", str(target / "config.json")],
        "WorkingDirectory": str(target),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 15,
        "StandardOutPath": str(target / "supervisor.log"),
        "StandardErrorPath": str(target / "supervisor.error.log"),
    }
    (target / "com.meron14725.discord-agent-deployer.plist").write_bytes(plistlib.dumps(launch))
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    os.umask(0o077)
    target = prepare(json.loads(Path(args.config).read_text()), args.install_dir, args.python)
    print(f"Prepared pinned supervisor in {target}; not started")


if __name__ == "__main__":
    main()
