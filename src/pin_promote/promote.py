"""Move pins on a branch, only after their checks, never from a build older than its sources.

One promotion, in order:

1. Fetch the branch, and read the config at its tip (`git show <tip>:<config path>`). The config at
   the tip governs everything after: its checks, fields, sources, journal, subject and registry.
2. Every check the named channels require has passed (`checks.missing`), or nothing happens.
3. Each new pin value fully matches its field's pattern, and the channel's fields are all given.
4. The build's commit must be on the branch: a build of another branch never promotes.
5. A channel whose pin already holds these values is unchanged: a retry commits nothing.
6. For every channel that does change, one `git diff` per class of source, each of which must find
   no file changed between the build's commit and the branch's tip: the channel's own `sources`
   (its `:!` exclusions apply inside this list only), the `shared_sources`, and the channel's pin
   file (a literal path). Otherwise the build is stale and the whole promotion is refused, before
   any commit.
7. Write the pins (and a journal entry) in a temporary worktree of the branch's tip, commit, push.
   Never a force push. A push the remote refuses is tried again, after a pause, only when the branch
   moved meanwhile (from the new tip, steps 1 to 7 again, up to `attempts` times); a push refused
   while the branch stood still (a hook, a permission, a protected branch) fails at once with git's
   own message.

The caller's own checkout is never modified: the commit is made in a worktree that is removed after.
The fetch does update the remote-tracking ref (`refs/remotes/<remote>/<branch>`).
"""

import json
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import NotRequired, TypedDict, cast

from . import checks
from .config import Channel, Config, ConfigError, parse, source_classes

SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
AUTHOR = re.compile(r"(?P<name>[^<>\n]+?) <(?P<email>[^<>\s]+)>")
SHALLOW = "the history may be shallow: fetch the whole history (actions/checkout fetch-depth: 0)"
REGULAR_FILE = ("100644", "100755")
MAX_PAUSE = 8.0


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


class Pushed(TypedDict):
    ok: bool
    commit: str
    error: str  # git push's own stderr when it refused


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


def _entry(repo: Path, ref: str, path: str) -> tuple[str, str] | None:
    """(mode, type) of `path` at `ref`; None only when git lists nothing there. Any git error raises."""
    listed = git(repo, "--literal-pathspecs", "ls-tree", ref, "--", path)
    if not listed:
        return None
    mode, kind = listed.partition("\t")[0].split()[:2]
    return mode, kind


def _show(repo: Path, ref: str, path: str) -> str:
    """The file at `ref`, with a final newline; "" when the path is absent there."""
    entry = _entry(repo, ref, path)
    if entry is None:
        return ""
    if entry[1] != "blob":
        raise GitError(f"{path} at {ref} is not a file")
    return git(repo, "show", f"{ref}:{path}") + "\n"


def tip_config(repo: Path, ref: str, config_path: str, local: Config) -> Config:
    """The config as the branch's tip holds it; PromotionError when it is missing, invalid or moves the branch."""
    where = f"{config_path} at the tip of {local['branch']}"
    entry = _entry(repo, ref, config_path)
    if entry is None:
        raise PromotionError(f"{where}: the config is missing there")
    if entry[0] not in REGULAR_FILE:
        raise PromotionError(f"{where}: not a regular file")
    try:
        config = parse(json.loads(git(repo, "show", f"{ref}:{config_path}")), config_path)
    except json.JSONDecodeError as error:
        raise PromotionError(f"{where}: not JSON: {error}") from error
    except ConfigError as error:
        raise PromotionError(f"{where} is not valid:\n{error}") from error
    for key, theirs, ours in (
        ("branch", config["branch"], local["branch"]),
        ("remote", config["remote"], local["remote"]),
    ):
        if theirs != ours:
            raise PromotionError(f"{where}: its {key} is {theirs!r}, the local config's is {ours!r}")
    return config


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


def stale(repo: Path, build: str, ref: str, pathspecs: list[str], *, literal: bool = False) -> list[str]:
    """The files of these pathspecs that changed between the build's commit and `ref`.

    Never called with no pathspec: `git diff` with none selects everything."""
    if not pathspecs:
        return []
    mode = ["--literal-pathspecs"] if literal else []
    changed = git(
        repo, *mode, "diff", "--name-only", "--no-renames", "--ignore-submodules=none", build, ref, "--", *pathspecs
    )
    return changed.splitlines()


def stale_files(repo: Path, config: Config, channel: Channel, build: str, ref: str) -> tuple[str, list[str]]:
    """(what changed, the files), the first class of the three that holds a file; ("", []) when none does."""
    classes = source_classes(config, channel)
    for label, specs, literal in (
        ("its sources", classes["own"], False),
        ("the shared sources", classes["shared"], False),
        ("its pin", [classes["pin"]], True),
    ):
        changed = stale(repo, build, ref, specs, literal=literal)
        if changed:
            return label, changed
    return "", []


def fetch(repo: Path, config: Config) -> str:
    """Fetch the branch into its remote-tracking ref (nothing else changes); that ref's name."""
    remote, branch = config["remote"], config["branch"]
    git(repo, "fetch", "--quiet", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")
    return f"refs/remotes/{remote}/{branch}"


def _shallow_hint(repo: Path) -> str:
    return f"; {SHALLOW}" if git(repo, "rev-parse", "--is-shallow-repository") == "true" else ""


def _on_branch(repo: Path, build: str, ref: str) -> bool:
    done = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", build, ref], capture_output=True, check=False
    )
    if done.returncode not in (0, 1):
        raise PromotionError(f"the build's commit {build} is not in this repository{_shallow_hint(repo)}")
    return done.returncode == 0


def _identity(author: str | None) -> list[str]:
    if not author:
        return []
    match = AUTHOR.fullmatch(author)
    if match is None:
        raise PromotionError(f"the author must be 'Name <email>': {author!r}")
    return ["-c", f"user.name={match['name']}", "-c", f"user.email={match['email']}"]


def _target(tree: Path, relative: str) -> Path:
    """Where to write `relative` in the worktree; PromotionError when a symlink is on the way (or out of it)."""
    walked = tree
    for part in PurePosixPath(relative).parts:
        walked = walked / part
        if walked.is_symlink():
            raise PromotionError(f"{relative}: {part} is a symlink on the branch; nothing is written through one")
        if not walked.exists():
            break
    if not (tree / relative).resolve().is_relative_to(tree.resolve()):
        raise PromotionError(f"{relative}: resolves outside the repository; nothing is written there")
    return tree / relative


def _commit_and_push(
    repo: Path, config: Config, ref: str, changes: list[Edit], journal: str, text: str, author: str | None
) -> Pushed:
    """The push's outcome and the commit made. The worktree is gone when this returns, whatever happened."""
    scratch = Path(tempfile.mkdtemp(prefix="pin-promote-"))
    tree = scratch / "tree"
    try:
        git(repo, "worktree", "add", "--quiet", "--detach", str(tree), ref)
        for change in changes:
            target = _target(tree, change["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change["updated"], encoding="utf-8")
        for name in config["journal"]:
            target = _target(tree, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(journal)
        git(tree, "--literal-pathspecs", "add", "--", *[c["path"] for c in changes], *config["journal"])
        identity = _identity(author)
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
        return {"ok": pushed.returncode == 0, "commit": commit, "error": pushed.stderr.strip()}
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)], capture_output=True, check=False
        )
        shutil.rmtree(scratch, ignore_errors=True)
        subprocess.run(["git", "-C", str(repo), "worktree", "prune"], capture_output=True, check=False)


def _pause(attempt: int) -> float:
    """Seconds to wait before attempt+1: 1, 2, 4, then 8 at most."""
    return min(2.0 ** (attempt - 1), MAX_PAUSE)


def promote(
    repo: Path,
    config: Config,
    config_path: str,
    request: Request,
    *,
    log: list[str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Result:
    """Promote the requested pins; PromotionError when refused (nothing pushed).

    `config` is the local config, used only for the remote, the branch and the attempts: on every
    attempt the config at the branch's tip, read from `config_path` (repository-relative), governs."""
    notes = log if log is not None else []
    names = list(request["pins"])
    if not names:
        raise PromotionError("no pin to promote")
    build = request["build"]
    if not SHA.fullmatch(build):
        raise PromotionError(f"the build's commit must be a full hex object name: {build!r}")
    _identity(request.get("author"))
    day = datetime.now(UTC).date().isoformat()
    attempts = config["attempts"]
    for attempt in range(1, attempts + 1):
        ref = fetch(repo, config)
        tip = git(repo, "rev-parse", ref)
        governing = tip_config(repo, ref, config_path, config)
        unknown = sorted(set(names) - set(governing["channels"]))
        if unknown:
            raise PromotionError(f"no such channel: {', '.join(unknown)}")
        required = required_checks(governing, names)
        failed = checks.missing(required, request["results"])
        if failed:
            raise PromotionError(f"checks not passed: {', '.join(failed)}")
        for name in names:
            pin_text(governing["channels"][name], request["pins"][name])
        if not _on_branch(repo, build, ref):
            raise PromotionError(f"the build's commit {build} is not on {config['branch']}{_shallow_hint(repo)}")
        planned = edits(repo, governing, request, ref)
        changes = [edit for edit in planned if edit["current"] != edit["updated"]]
        moved = [edit["channel"] for edit in changes]
        kept = [edit["channel"] for edit in planned if edit["current"] == edit["updated"]]
        dry = bool(request.get("dry_run"))
        if not changes:
            return {"commit": tip, "promoted": [], "unchanged": kept, "attempts": attempt, "dry_run": dry}
        for name in moved:
            what, changed = stale_files(repo, governing, governing["channels"][name], build, ref)
            if changed:
                raise PromotionError(
                    f"{name}: the build is older than {what} on {config['branch']}: {', '.join(changed)}"
                )
        if dry:
            return {"commit": tip, "promoted": moved, "unchanged": kept, "attempts": attempt, "dry_run": True}
        journal = journal_entry(governing, request, moved, required, day) if governing["journal"] else ""
        moved_request = cast(Request, {**request, "pins": {name: request["pins"][name] for name in moved}})
        text = message(governing, moved_request, moved, required)
        pushed = _commit_and_push(repo, governing, ref, changes, journal, text, request.get("author"))
        if pushed["ok"]:
            return {
                "commit": pushed["commit"],
                "promoted": moved,
                "unchanged": kept,
                "attempts": attempt,
                "dry_run": False,
            }
        fetch(repo, config)
        if git(repo, "rev-parse", ref) == tip:
            raise PromotionError(
                f"the push to {config['branch']} was refused and the branch did not move,"
                f" so trying again would change nothing: {pushed['error'] or 'git gave no reason'}"
            )
        notes.append(f"the push was refused (attempt {attempt} of {attempts}); {config['branch']} moved, trying again")
        if attempt < attempts:
            sleep(_pause(attempt))
    raise PromotionError(f"the push was refused {attempts} times; nothing was promoted")


def pin_at(repo: Path, ref: str, channel: Channel) -> dict[str, object] | None:
    """One channel's pin as `ref` holds it (None when the file does not exist there)."""
    text = _show(repo, ref, channel["pin"])
    if not text:
        return None
    parsed: object = json.loads(text)
    if not isinstance(parsed, dict):
        raise PromotionError(f"{channel['pin']} at {ref} is not a JSON object")
    return cast(dict[str, object], parsed)


def current(repo: Path, config: Config, ref: str) -> dict[str, dict[str, object] | None]:
    """Every channel's pin as `ref` holds it (None when the file does not exist there)."""
    return {name: pin_at(repo, ref, channel) for name, channel in config["channels"].items()}
