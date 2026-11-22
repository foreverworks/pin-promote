"""Move pins on a branch, only after their checks, never from a build older than its sources.

One promotion, in order:

1. Every check the named channels require has passed (`checks.missing`), or nothing happens.
2. Each new pin value fully matches its field's pattern, and the channel's fields are all given.
3. Fetch the branch. The build's commit must be on it: a build of another branch never promotes.
4. A channel whose pin already holds these values is unchanged: a retry commits nothing.
5. For every channel that does change: no file of its sources, the shared sources or its own pin
   changed between the build's commit and the branch's tip. Otherwise the build is stale and the
   whole promotion is refused, before any commit.
6. Write the pins (and a journal entry) in a temporary worktree of the branch's tip, commit, push.
   Never a force push: a push the remote refuses, because the branch moved meanwhile, is tried again
   from the new tip, steps 3 to 6 included, up to `attempts` times.

The caller's own checkout is never modified: the commit is made in a worktree that is removed after.
"""

import json
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from . import checks
from .config import Channel, Config, sources

SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
AUTHOR = re.compile(r"(?P<name>[^<>\n]+?) <(?P<email>[^<>\s]+)>")


class PromotionError(ValueError):
    """The promotion was refused; nothing was committed or pushed."""


class GitError(RuntimeError):
    """A git command failed for a reason other than a refused push."""


class Request(TypedDict):
    build: str  # the commit the artifacts were built from
    pins: dict[str, dict[str, str]]  # channel -> field -> new value
    results: dict[str, str]  # check -> result ("success" passes)
    run_url: str  # a link to the build, for the commit and the journal; may be empty
    author: NotRequired[str]  # "Name <email>"; git's own identity when absent
    dry_run: NotRequired[bool]


class Edit(TypedDict):
    channel: str
    path: str
    current: str  # the file on the branch, "" when it does not exist yet
    updated: str


class Result(TypedDict):
    commit: str  # the branch tip that holds the pins
    promoted: list[str]  # channels whose pin moved in this run
    unchanged: list[str]  # channels whose pin already held these values
    attempts: int
    dry_run: bool


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=False)
    if done.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout.strip()


# ---- pure: values, files, messages ---------------------------------------------------------------


def pin_text(channel: Channel, values: dict[str, str]) -> str:
    """The pin file for these values, in the order the config lists the fields."""
    fields = channel["fields"]
    name = channel["name"]
    unknown = sorted(set(values) - set(fields))
    absent = [field for field in fields if field not in values]
    if unknown:
        raise PromotionError(f"{name}: no such field: {', '.join(unknown)}")
    if absent:
        raise PromotionError(f"{name}: every field is given at once; missing: {', '.join(absent)}")
    for field, pattern in fields.items():
        if not re.fullmatch(pattern, values[field]):
            raise PromotionError(f"{name}.{field}: {values[field]!r} does not match {pattern}")
    return json.dumps({field: values[field] for field in fields}) + "\n"


def same_pin(current: str, updated: str) -> bool:
    """The same values written another way are the same pin: a retry changes nothing."""
    if not current:
        return False
    try:
        return bool(json.loads(current) == json.loads(updated))
    except json.JSONDecodeError:
        return False


def required_checks(config: Config, names: list[str]) -> list[str]:
    return list(dict.fromkeys(check for name in names for check in config["channels"][name]["checks"]))


def subject(config: Config, names: list[str]) -> str:
    return "; ".join(dict.fromkeys(config["channels"][name]["subject"] for name in names))


def _values(edit_values: dict[str, str]) -> str:
    return ", ".join(f"{field} `{value}`" for field, value in edit_values.items())


def message(config: Config, request: Request, names: list[str], passed: list[str]) -> str:
    lines = [subject(config, names), ""]
    lines += [f"{name}: {_values(request['pins'][name])}" for name in names]
    lines += ["", f"Build: {request['build']}"]
    if request["run_url"]:
        lines.append(f"Run: {request['run_url']}")
    lines.append(f"Checks passed: {', '.join(passed)}")
    return "\n".join(lines) + "\n"


def journal_entry(config: Config, request: Request, names: list[str], passed: list[str], day: str) -> str:
    source = f"[build]({request['run_url']})" if request["run_url"] else "build"
    lines = [f"\n## {day}: promoted {', '.join(names)}\n"]
    for name in names:
        note = config["channels"][name]["note"]
        lines.append(f"- {name}: {_values(request['pins'][name])}" + (f". {note}" if note else ""))
    lines.append(f"\nFrom {source} of `{request['build']}`, after {', '.join(passed)} passed.\n")
    return "\n".join(lines)


# ---- git ------------------------------------------------------------------------------------------


def _show(repo: Path, ref: str, path: str) -> str:
    try:
        git(repo, "cat-file", "-e", f"{ref}:{path}")
    except GitError:
        return ""
    return git(repo, "show", f"{ref}:{path}") + "\n"


def edits(repo: Path, config: Config, request: Request, ref: str) -> list[Edit]:
    """Every requested channel's pin as `ref` holds it and as it would be; unchanged ones included."""
    out: list[Edit] = []
    for name, values in request["pins"].items():
        channel = config["channels"][name]
        current = _show(repo, ref, channel["pin"])
        updated = pin_text(channel, values)
        out.append(
            {
                "channel": name,
                "path": channel["pin"],
                "current": current,
                "updated": current if same_pin(current, updated) else updated,
            }
        )
    return out


def stale(repo: Path, build: str, ref: str, pathspecs: list[str]) -> list[str]:
    """The files of these pathspecs that changed between the build's commit and `ref`."""
    changed = git(repo, "diff", "--name-only", "--no-renames", build, ref, "--", *pathspecs)
    return changed.splitlines()


def _fetch(repo: Path, config: Config) -> str:
    remote, branch = config["remote"], config["branch"]
    git(repo, "fetch", "--quiet", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")
    return f"refs/remotes/{remote}/{branch}"


def _on_branch(repo: Path, build: str, ref: str) -> bool:
    done = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", build, ref], capture_output=True, check=False
    )
    if done.returncode not in (0, 1):
        raise PromotionError(f"the build's commit {build} is not in this repository")
    return done.returncode == 0


def _commit_and_push(
    repo: Path, config: Config, ref: str, changes: list[Edit], journal: str, text: str, author: str | None
) -> tuple[bool, str]:
    """(pushed, commit). The worktree is gone when this returns, whatever happened."""
    scratch = Path(tempfile.mkdtemp(prefix="pin-promote-"))
    tree = scratch / "tree"
    try:
        git(repo, "worktree", "add", "--quiet", "--detach", str(tree), ref)
        for change in changes:
            target = tree / change["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change["updated"], encoding="utf-8")
        for name in config["journal"]:
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(journal)
        git(tree, "add", "--", *[c["path"] for c in changes], *config["journal"])
        identity: list[str] = []
        if author:
            match = AUTHOR.fullmatch(author)
            if match is None:
                raise PromotionError(f"the author must be 'Name <email>': {author!r}")
            identity = ["-c", f"user.name={match['name']}", "-c", f"user.email={match['email']}"]
        done = subprocess.run(
            ["git", "-C", str(tree), *identity, "commit", "--quiet", "-F", "-"],
            input=text,
            text=True,
            capture_output=True,
            check=False,
        )
        if done.returncode != 0:
            raise GitError(f"git commit: {done.stderr.strip() or done.stdout.strip()}")
        commit = git(tree, "rev-parse", "HEAD")
        pushed = subprocess.run(
            ["git", "-C", str(tree), "push", "--quiet", config["remote"], f"HEAD:refs/heads/{config['branch']}"],
            text=True,
            capture_output=True,
            check=False,
        )
        return pushed.returncode == 0, commit
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)], capture_output=True, check=False
        )
        shutil.rmtree(scratch, ignore_errors=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True, check=False)


def promote(repo: Path, config: Config, request: Request, *, log: list[str] | None = None) -> Result:
    """Promote the requested pins; PromotionError when refused (nothing pushed)."""
    notes = log if log is not None else []
    names = list(request["pins"])
    if not names:
        raise PromotionError("no pin to promote")
    unknown = sorted(set(names) - set(config["channels"]))
    if unknown:
        raise PromotionError(f"no such channel: {', '.join(unknown)}")
    build = request["build"]
    if not SHA.fullmatch(build):
        raise PromotionError(f"the build's commit must be a full hex object name: {build!r}")
    required = required_checks(config, names)
    failed = checks.missing(required, request["results"])
    if failed:
        raise PromotionError(f"checks not passed: {', '.join(failed)}")
    for name in names:  # values first: a bad value is refused before any network
        pin_text(config["channels"][name], request["pins"][name])
    day = datetime.now(UTC).date().isoformat()
    for attempt in range(1, config["attempts"] + 1):
        ref = _fetch(repo, config)
        if not _on_branch(repo, build, ref):
            raise PromotionError(f"the build's commit {build} is not on {config['branch']}")
        planned = edits(repo, config, request, ref)
        changes = [edit for edit in planned if edit["current"] != edit["updated"]]
        moved = [edit["channel"] for edit in changes]
        kept = [edit["channel"] for edit in planned if edit["current"] == edit["updated"]]
        tip = git(repo, "rev-parse", ref)
        if not changes:
            return {
                "commit": tip,
                "promoted": [],
                "unchanged": kept,
                "attempts": attempt,
                "dry_run": bool(request.get("dry_run")),
            }
        for name in moved:
            changed = stale(repo, build, ref, sources(config, config["channels"][name]))
            if changed:
                raise PromotionError(
                    f"{name}: the build is older than its sources or pin on {config['branch']}: {', '.join(changed)}"
                )
        if request.get("dry_run"):
            return {"commit": tip, "promoted": moved, "unchanged": kept, "attempts": attempt, "dry_run": True}
        journal = journal_entry(config, request, moved, required, day) if config["journal"] else ""
        moved_request = cast(Request, {**request, "pins": {name: request["pins"][name] for name in moved}})
        text = message(config, moved_request, moved, required)
        pushed, commit = _commit_and_push(repo, config, ref, changes, journal, text, request.get("author"))
        if pushed:
            return {"commit": commit, "promoted": moved, "unchanged": kept, "attempts": attempt, "dry_run": False}
        notes.append(f"the push was refused (attempt {attempt} of {config['attempts']}); trying again from the new tip")
    raise PromotionError(f"the push was refused {config['attempts']} times; nothing was promoted")


def current(repo: Path, config: Config, ref: str) -> dict[str, dict[str, object] | None]:
    """Every channel's pin as `ref` holds it (None when the file does not exist there)."""
    out: dict[str, dict[str, object] | None] = {}
    for name, channel in config["channels"].items():
        text = _show(repo, ref, channel["pin"])
        out[name] = cast(dict[str, object], json.loads(text)) if text else None
    return out
