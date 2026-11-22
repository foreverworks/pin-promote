"""Offline fixtures: a bare remote and a clone of it, with a config and pin files on main."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from pin_promote import Config, parse

FP_A, FP_B, FP_C = "a" * 64, "b" * 64, "c" * 64
DIGEST_A = "sha256:" + "1" * 64

CONFIG: dict[str, object] = {
    "schema": 1,
    "shared_sources": ["lock.json"],
    "journal": ["CHANGELOG.md"],
    "channels": {
        "vm": {
            "pin": "images/vm/pin.json",
            "fields": {"fingerprint": "[0-9a-f]{64}"},
            "sources": ["images/", ":!images/host/"],
            "checks": ["boot"],
            "subject": "vm: pin the verified image",
        },
        "host": {
            "pin": "images/host/pin.json",
            "fields": {"os": "[0-9a-f]{16}", "fingerprint": "[0-9a-f]{64}"},
            "sources": ["images/host/", "install/"],
            "checks": ["smoke", "host-test"],
            "note": "what the next install installs",
        },
    },
}


def run_git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, check=True)
    return done.stdout.strip()


class Repository(unittest.TestCase):
    """`self.remote` (bare) and `self.clone`, main holding the config, two pins and their sources."""

    def setUp(self) -> None:
        held = tempfile.TemporaryDirectory()
        self.addCleanup(held.cleanup)
        self.root = Path(held.name)
        self.remote = self.root / "remote.git"
        self.clone = self.root / "clone"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.clone)], check=True)
        # A machine-wide hooks path (a pre-push guard, say) is not this fixture's: hooks live in the repos.
        run_git(self.remote, "config", "core.hooksPath", str(self.remote / "hooks"))
        settings = (
            ("user.name", "Test"),
            ("user.email", "test@example.com"),
            ("commit.gpgsign", "false"),
            ("core.hooksPath", str(self.root / "no-hooks")),
        )
        for key, value in settings:
            run_git(self.clone, "config", key, value)
        run_git(self.clone, "remote", "add", "origin", str(self.remote))
        files = {
            "pin-promote.json": json.dumps(CONFIG, indent=2) + "\n",
            "lock.json": '{"tool": "1.0"}\n',
            "CHANGELOG.md": "# Changes\n",
            "images/vm/build.sh": "build\n",
            "images/vm/pin.json": json.dumps({"fingerprint": FP_A}) + "\n",
            "images/host/spec.yml": "spec\n",
            "images/host/pin.json": json.dumps({"os": "0" * 16, "fingerprint": FP_A}) + "\n",
            "install/driver.py": "driver\n",
            "docs/readme.md": "docs\n",
        }
        self.build = self.commit(files, "fixture")
        self.config: Config = parse(CONFIG)

    def commit(self, files: dict[str, str], message: str) -> str:
        """Commit these files on top of origin/main (or as the first commit) and push; the new tip."""
        if run_git(self.clone, "ls-remote", "origin", "main"):
            run_git(self.clone, "fetch", "-q", "origin", "main")
            run_git(self.clone, "checkout", "-q", "--detach", "origin/main")
        for name, text in files.items():
            path = self.clone / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        run_git(self.clone, "add", "-A")
        run_git(self.clone, "commit", "-q", "-m", message)
        run_git(self.clone, "push", "-q", "origin", "HEAD:refs/heads/main")
        run_git(self.clone, "fetch", "-q", "origin", "main")
        return run_git(self.clone, "rev-parse", "HEAD")

    def tip(self) -> str:
        return run_git(self.remote, "rev-parse", "main")

    def on_main(self, path: str) -> str:
        return run_git(self.remote, "show", f"main:{path}") + "\n"

    def pin(self, path: str) -> object:
        return json.loads(self.on_main(path))
