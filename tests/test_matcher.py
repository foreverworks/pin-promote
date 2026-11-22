"""The pathspec matcher against git itself: `git diff --name-only -- <specs>` on a real repository."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from pin_promote.config import _pathspec, _selected

PATHS = (
    "images/a/pin.json",
    "images/a/b/pin.json",
    "images/pin.json",
    "images/a/x.txt",
    "images/a/x/y.json",
    "images/b/x",
    "images/ab/x",
    "dir.json/x.txt",
    "dir.json/p.json",
    "a.json",
    "img/a1/x/y.json",
    "app/Pin.json",
    "app/pin.json",
    "app/p1.json",
    "app/p.json",
    "app/-x",
    "a^b/p.json",
    "ab/p.json",
    "a]/p.json",
    "images/a/p.json.txt",
    "x/y/z.json",
    "Dockerfile",
)

SPECS: tuple[tuple[str, ...], ...] = (
    ("images",),
    ("images/",),
    ("images/a",),
    ("images/a/",),
    ("images/a/pin.json",),
    ("images/*",),
    ("images/*/",),
    ("images", ":!images/*/pin.json"),
    ("images", ":^images/*/pin.json"),
    ("images/*/pin.json",),
    ("*.json",),
    ("*.json", ":!dir.json/p.json"),
    ("*.json", ":!*.json"),
    ("images/*/x",),
    ("img/*/x",),
    ("images/a?/pin.json",),
    ("images/?",),
    ("images/[ab]",),
    ("images/[ab]/*",),
    ("images", ":!images/?"),
    ("images", ":!images/[ab]"),
    ("images", ":!images/*/x"),
    ("images", ":!images/a/*"),
    ("images", ":!images/a"),
    ("images", ":!images/a/"),
    ("images", ":!images/a/b"),
    ("images", ":!*pin.json"),
    ("images/[^b]",),
    ("images/[^b]/*",),
    ("images/[!b]/*",),
    ("a[^b]/p.json",),
    ("a[!b]/p.json",),
    ("a[]b]/p.json",),
    ("a[]^]/p.json",),
    ("app/[A-Z]in.json",),
    ("app/[a-c]*.json",),
    ("app/[!A-Z]*.json",),
    ("app/[-x]*",),
    ("app/[x-]*",),
    ("app/p*.json",),
    ("images/**/pin.json",),
    ("**/pin.json",),
    ("images", ":!images/**/pin.json"),
    ("images/**",),
    ("Doc*",),
    ("Doc[a-z]ile",),
    ("images/a?b",),
)


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", *args], text=True, capture_output=True, check=True
    )
    return done.stdout


class AgainstGit(unittest.TestCase):
    root: Path

    @classmethod
    def setUpClass(cls) -> None:
        held = tempfile.TemporaryDirectory()
        cls.addClassCleanup(held.cleanup)
        cls.root = Path(held.name)
        _git(cls.root, "init", "-q", "-b", "main")
        _git(cls.root, "config", "user.name", "Test")
        _git(cls.root, "config", "user.email", "test@example.com")
        _git(cls.root, "config", "commit.gpgsign", "false")
        for body in ("0", "1"):  # committed as 0, then every file differs from HEAD
            for path in PATHS:
                (cls.root / path).parent.mkdir(parents=True, exist_ok=True)
                (cls.root / path).write_text(body)
            if body == "0":
                _git(cls.root, "add", "-A")
                _git(cls.root, "commit", "-q", "-m", "base")

    def selected_by_git(self, *specs: str) -> set[str]:
        return set(_git(self.root, "diff", "--name-only", "--no-renames", "HEAD", "--", *specs).split("\n")) - {""}

    def test_every_spec_selects_what_git_selects(self) -> None:
        for specs in SPECS:
            with self.subTest(specs=specs):
                mine = {path for path in PATHS if _selected(path, list(specs))}
                self.assertEqual(mine, self.selected_by_git(*specs))

    def test_a_wildcard_spec_matches_the_whole_path_only(self) -> None:
        # The cases a directory-prefix match got wrong: git excludes or includes only what the glob matches whole.
        for exclusion in (":!images/?", ":!images/[ab]", ":!images/*/x", ":!images/*"):
            with self.subTest(exclusion=exclusion):
                specs = ["images/", exclusion]
                self.assertEqual({path for path in PATHS if _selected(path, specs)}, self.selected_by_git(*specs))
        self.assertTrue(_selected("images/a/pin.json", ["images/", ":!images/?"]))
        self.assertFalse(_selected("dir.json/x.txt", ["*.json"]))
        self.assertFalse(_selected("images/a/b/pin.json", ["images/", ":!images/*/pin.json"]))
        self.assertTrue(_selected("images/pin.json", ["images/", ":!images/*/pin.json"]))

    def test_every_spec_is_one_the_config_accepts(self) -> None:
        for specs in SPECS:
            for spec in specs:
                with self.subTest(spec=spec):
                    self.assertTrue(_pathspec(spec))
