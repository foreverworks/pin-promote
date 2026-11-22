"""The promotion config: which pin files a repository has, what each holds, what must pass first.

A config is one JSON file in the repository it promotes into (docs/config.md is the reference):

    {
      "schema": 1,
      "branch": "main",
      "shared_sources": ["lock.json"],
      "journal": ["CHANGELOG.md"],
      "channels": {
        "app": {
          "pin": "deploy/app.json",
          "fields": {"digest": "sha256:[0-9a-f]{64}"},
          "sources": ["app/"],
          "checks": ["test", "smoke"]
        }
      }
    }

Parsing is pure: `parse` takes the decoded JSON and returns a checked `Config`, or raises
`ConfigError` naming every problem it found.
"""

import fnmatch
import json
import re
from pathlib import Path, PurePosixPath
from typing import NotRequired, TypedDict, cast

SCHEMA = 1
NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")  # no ".": --pin CHANNEL.FIELD=VALUE splits there
FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# Values near a digest that a registry field's pattern must refuse (it must accept only DIGEST's values).
NEAR_MISSES = (
    "latest",
    "sha256:",
    "sha256:" + "A" * 64,
    "sha256:" + "0" * 63,
    "sha256:" + "0" * 65,
    "sha256:" + "0" * 32,
    "sha256:" + "0" * 63 + "g",
    "sha256:" + "0" * 64 + "0",
    "sha512:" + "0" * 64,
    "SHA256:" + "0" * 64,
    "0" * 64,
    " sha256:" + "0" * 64,
    "sha256:" + "0" * 64 + " ",
    "sha256:" + "0" * 64 + "\n",
)
REGISTRY_IMAGE = re.compile(r"(?:[a-z0-9.-]+|\[::1\])(?::[0-9]+)?(?:/[a-z0-9._-]+)+")
HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]+)?")
REGISTRY_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
TOP_LEVEL = {"schema", "branch", "remote", "attempts", "shared_sources", "journal", "channels"}
CHANNEL_KEYS = {"pin", "fields", "sources", "checks", "subject", "note", "registry"}
REGISTRY_KEYS = {"image", "tag", "field", "token_host"}
GLOB = "*?["
EXCLUDE = (":!", ":^")


class Registry(TypedDict):
    """A registry tag the channel moves to its pinned digest once the pin is on the branch."""

    image: str  # e.g. registry.example.com/team/base, without a tag or digest
    tag: str
    field: str  # the pin field holding the digest (sha256:<64 hex>)
    token_host: NotRequired[str]  # the one host, other than the registry's, a bearer token may be fetched from


class Channel(TypedDict):
    name: str
    pin: str  # repository-relative path of the pin file
    fields: dict[str, str]  # field name -> the pattern its value must fully match; the file's key order
    sources: list[str]  # git pathspecs a build must not be older than; ':!' exclusions apply inside this list only
    checks: list[str]  # every one must have passed for this channel's pin to move
    subject: str  # the commit subject when this channel moves
    note: str  # appended to the channel's journal line; may be empty
    registry: NotRequired[Registry]


class Local(TypedDict):
    """What the local config file supplies: where the branch is and how often a push is tried."""

    branch: str
    remote: str
    attempts: int


class Config(Local):
    shared_sources: list[str]
    journal: list[str]
    channels: dict[str, Channel]


class SourceClasses(TypedDict):
    """What a build of one channel must not be older than, in the three classes git is asked about."""

    own: list[str]  # the channel's sources: pathspecs, with ':!' exclusions that apply to this list only
    shared: list[str]  # the config's shared sources: pathspecs, no exclusions
    pin: str  # the channel's own pin file, always a literal path


class ConfigError(ValueError):
    """The config is not one this version can promote with; the message lists every problem."""


def is_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and str(path) == value.rstrip("/")


def _file_path(value: object) -> bool:
    """A repository-relative path git may track: no '.git' component, which git never stores."""
    return is_relative_path(value) and all(part.casefold() != ".git" for part in PurePosixPath(str(value)).parts)


def _plain(spec: str) -> str:
    return (spec[2:] if spec.startswith(EXCLUDE) else spec).rstrip("/")


def _pathspec(value: object) -> bool:
    if not isinstance(value, str) or not value or "\0" in value or "\n" in value:
        return False
    plain = value[2:] if value.startswith(EXCLUDE) else value
    # Magic other than exclusion (":(glob)", ":(top)") is left out: it changes what a path means.
    if plain.startswith(":") or not is_relative_path(plain.rstrip("/") or "."):
        return False
    # A pathspec that selects the whole repository (or nothing but wildcards) makes every build stale.
    return plain.rstrip("/") not in ("", ".") and plain.strip("*") != "" and _glob_balanced(plain)


def _glob_balanced(pattern: str) -> bool:
    """Whether every '[' of a glob opens a bracket expression that closes."""
    index, end = 0, len(pattern)
    while index < end:
        if pattern[index] == "[":
            index += 1
            if index < end and pattern[index] in "!^":
                index += 1
            if index < end and pattern[index] == "]":
                index += 1  # a leading ']' is a member of the set
            while index < end and pattern[index] != "]":
                if pattern.startswith("[:", index) and (close := pattern.find(":]", index + 2)) != -1:
                    index = close + 2
                else:
                    index += 1
            if index >= end:
                return False
            index += 1
        else:
            index += 1
    return True


def _matches(path: str, spec: str) -> bool:
    """Whether `path` is inside the file or directory `spec` selects (globs as git pathspec globs)."""
    plain = _plain(spec)
    if any(char in plain for char in GLOB):
        parts = PurePosixPath(path).parts
        return any(fnmatch.fnmatchcase("/".join(parts[:i]), plain) for i in range(1, len(parts) + 1))
    return path == plain or path.startswith(plain + "/")


def _selected(path: str, specs: list[str]) -> bool:
    included = [spec for spec in specs if not spec.startswith(EXCLUDE)]
    excluded = [spec for spec in specs if spec.startswith(EXCLUDE)]
    return any(_matches(path, spec) for spec in included) and not any(_matches(path, spec) for spec in excluded)


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _channel(name: str, raw: object, problems: list[str], config_path: str | None) -> Channel | None:
    where = f"channels.{name}"
    if not NAME.fullmatch(name):
        problems.append(f"{where}: a channel name is lower case letters, digits, '_' and '-'")
    if not isinstance(raw, dict):
        problems.append(f"{where}: must be an object")
        return None
    data = cast(dict[str, object], raw)
    for key in sorted(set(data) - CHANNEL_KEYS):
        problems.append(f"{where}.{key}: unknown key")
    pin = data.get("pin")
    if not _file_path(pin) or not str(pin).endswith(".json"):
        problems.append(f"{where}.pin: a repository-relative path to a .json file, without '..' or a '.git' part")
    elif pin == config_path:
        problems.append(f"{where}.pin: this is the config file itself")
    fields = data.get("fields")
    patterns: dict[str, str] = {}
    if not isinstance(fields, dict) or not fields:
        problems.append(f"{where}.fields: an object of field name -> pattern, at least one")
    else:
        for field, pattern in cast(dict[str, object], fields).items():
            if not FIELD.fullmatch(field):
                problems.append(
                    f"{where}.fields.{field}: a field name is ASCII letters, digits and '_', not first a digit"
                )
            if not isinstance(pattern, str) or not pattern:
                problems.append(f"{where}.fields.{field}: the pattern must be a non-empty string")
                continue
            try:
                re.compile(pattern)
            except re.error as error:
                problems.append(f"{where}.fields.{field}: not a regular expression: {error}")
                continue
            if re.fullmatch(pattern, ""):
                problems.append(f"{where}.fields.{field}: the pattern accepts an empty value")
            patterns[field] = pattern
    sources = data.get("sources", [])
    if not _strings(sources) or not all(_pathspec(item) for item in cast(list[str], sources)):
        problems.append(
            f"{where}.sources: a list of repository-relative git pathspecs (':!' excludes one);"
            " not '.' or anything else that selects the whole repository"
        )
        sources = []
    sources = cast(list[str], sources)
    if sources and all(item.startswith(EXCLUDE) for item in sources):
        problems.append(f"{where}.sources: exclusions alone select nothing")
    checks = data.get("checks")
    if not _strings(checks) or not checks or not all(cast(list[str], checks)):
        problems.append(f"{where}.checks: the checks that must pass first, at least one (a pin never moves unchecked)")
        checks = []
    checks = cast(list[str], checks)
    if len(set(checks)) != len(checks):
        problems.append(f"{where}.checks: listed twice: {sorted({c for c in checks if checks.count(c) > 1})}")
    subject = data.get("subject", f"{name}: pin the verified build")
    if not isinstance(subject, str) or not subject.strip() or "\n" in subject:
        problems.append(f"{where}.subject: one non-empty line")
        subject = ""
    note = data.get("note", "")
    if not isinstance(note, str) or "\n" in note:
        problems.append(f"{where}.note: one line of text")
        note = ""
    channel: Channel = {
        "name": name,
        "pin": str(pin),
        "fields": patterns,
        "sources": sources,
        "checks": checks,
        "subject": subject,
        "note": note,
    }
    if "registry" in data:
        registry = data["registry"]
        if not isinstance(registry, dict):
            problems.append(f"{where}.registry: must be an object")
        else:
            reg = cast(dict[str, object], registry)
            for key in sorted(set(reg) - REGISTRY_KEYS):
                problems.append(f"{where}.registry.{key}: unknown key")
            image, tag, target = reg.get("image"), reg.get("tag"), reg.get("field")
            if (
                not isinstance(image, str)
                or not REGISTRY_IMAGE.fullmatch(image)
                or any(part in (".", "..") for part in image.split("/"))
            ):
                problems.append(
                    f"{where}.registry.image: a registry host and repository, e.g. registry.example.com/team/app,"
                    " without '.' or '..' as a path part"
                )
            token_host = reg.get("token_host")
            if "token_host" in reg and (not isinstance(token_host, str) or not HOSTNAME.fullmatch(token_host)):
                problems.append(f"{where}.registry.token_host: a host name (lower case, optional :port)")
                token_host = None
            if not isinstance(tag, str) or not REGISTRY_TAG.fullmatch(tag):
                problems.append(f"{where}.registry.tag: a tag name")
            if not isinstance(target, str) or target not in patterns:
                problems.append(f"{where}.registry.field: one of the channel's fields, the one holding the digest")
            elif any(re.fullmatch(patterns[target], bad) for bad in NEAR_MISSES):
                problems.append(f"{where}.registry.field: the field's pattern must accept only sha256:<64 hex> digests")
            if isinstance(image, str) and isinstance(tag, str) and isinstance(target, str):
                channel["registry"] = {"image": image, "tag": tag, "field": target}
                if isinstance(token_host, str):
                    channel["registry"]["token_host"] = token_host
    return channel


def _local(data: dict[str, object], problems: list[str]) -> Local:
    """The branch, the remote and the attempts of a config object; one source for both readers."""
    branch = data.get("branch", "main")
    if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or ".." in branch:
        problems.append("branch: a branch name, starting with a letter or a digit")
        branch = "main"
    remote = data.get("remote", "origin")
    if not isinstance(remote, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote):
        problems.append("remote: a remote name, starting with a letter or a digit")
        remote = "origin"
    attempts = data.get("attempts", 3)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 20:
        problems.append("attempts: a whole number from 1 to 20")
        attempts = 3
    return {"branch": branch, "remote": remote, "attempts": attempts}


def parse_local(raw: object) -> Local:
    """The branch, remote and attempts of the local config file; every other key is the tip's business.

    The local file may be older than the branch's tip config, or invalid in what it only describes:
    a promotion reads the rest from the tip, so only what it needs to reach the tip is checked."""
    if not isinstance(raw, dict):
        raise ConfigError("the config must be a JSON object")
    problems: list[str] = []
    local = _local(cast(dict[str, object], raw), problems)
    if problems:
        raise ConfigError("\n".join(problems))
    return local


def parse(raw: object, config_path: str | None = None) -> Config:
    """A checked config from decoded JSON; ConfigError lists every problem at once.

    `config_path` is the config file's repository-relative path: no pin or journal may be that file."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        raise ConfigError("the config must be a JSON object")
    data = cast(dict[str, object], raw)
    if data.get("schema") != SCHEMA:
        problems.append(f"schema: this version reads schema {SCHEMA}, the config says {data.get('schema')!r}")
    for key in sorted(set(data) - TOP_LEVEL):
        problems.append(f"{key}: unknown key")
    local = _local(data, problems)
    shared = data.get("shared_sources", [])
    if (
        not _strings(shared)
        or not all(_pathspec(item) for item in cast(list[str], shared))
        or any(item.startswith(EXCLUDE) for item in cast(list[str], shared))
    ):
        problems.append(
            "shared_sources: a list of repository-relative git pathspecs, without ':!' exclusions"
            " and without one that selects the whole repository"
        )
        shared = []
    journal = data.get("journal", [])
    if not _strings(journal) or not all(_file_path(item) for item in cast(list[str], journal)):
        problems.append("journal: a list of repository-relative file paths, without a '.git' part")
        journal = []
    raw_channels = data.get("channels")
    channels: dict[str, Channel] = {}
    if not isinstance(raw_channels, dict) or not raw_channels:
        problems.append("channels: an object with at least one channel")
    else:
        for name, body in cast(dict[str, object], raw_channels).items():
            channel = _channel(name, body, problems, config_path)
            if channel is not None:
                channels[name] = channel
        pins = [c["pin"] for c in channels.values()]
        for pin in sorted({p for p in pins if pins.count(p) > 1}):
            problems.append(f"channels: two channels write {pin}")
        for owner in channels.values():
            if _selected(owner["pin"], cast(list[str], shared)):
                problems.append(
                    f"channels.{owner['name']}.pin: {owner['pin']} is inside shared_sources, so every promotion"
                    " would make builds stale"
                )
            for other in channels.values():
                if other is not owner and _selected(owner["pin"], other["sources"]):
                    problems.append(
                        f"channels.{owner['name']}.pin: {owner['pin']} is inside the sources of channel"
                        f" {other['name']}, so a promotion of {owner['name']} would make its builds stale"
                        f" (exclude it there, e.g. ':!{owner['pin']}')"
                    )
        for pin in sorted(set(pins) & set(cast(list[str], journal))):
            problems.append(f"journal: {pin} is a channel's pin file")
        for path in cast(list[str], journal):
            if path == config_path:
                problems.append(f"journal: {path} is the config file itself")
            if _selected(path, cast(list[str], shared)):
                problems.append(f"journal: {path} is inside shared_sources, so every promotion would make builds stale")
            for channel in channels.values():
                if _selected(path, channel["sources"]):
                    problems.append(
                        f"journal: {path} is inside the sources of channel {channel['name']}, so a promotion would"
                        " make its builds stale"
                    )
    if problems:
        raise ConfigError("\n".join(problems))
    return {
        **local,
        "shared_sources": cast(list[str], shared),
        "journal": cast(list[str], journal),
        "channels": channels,
    }


def load_local(path: Path) -> Local:
    """The local config file's branch, remote and attempts (see `parse_local`)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from error
    try:
        return parse_local(raw)
    except ConfigError as error:
        raise ConfigError("\n".join(f"{path}: {line}" for line in str(error).splitlines())) from error


def load(path: Path, relative: str | None = None) -> Config:
    """The config file at `path`; `relative` is its path inside the repository (default: the file's name)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from error
    try:
        return parse(raw, path.name if relative is None else relative)
    except ConfigError as error:
        raise ConfigError("\n".join(f"{path}: {line}" for line in str(error).splitlines())) from error


def source_classes(config: Config, channel: Channel) -> SourceClasses:
    """What a build of this channel must not be older than: its sources, the shared ones, its pin."""
    return {"own": channel["sources"], "shared": config["shared_sources"], "pin": channel["pin"]}
