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

import json
import re
from pathlib import Path, PurePosixPath
from typing import NotRequired, TypedDict, cast

SCHEMA = 1
NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")  # no ".": --pin CHANNEL.FIELD=VALUE splits there
FIELD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
REGISTRY_IMAGE = re.compile(r"[a-z0-9.-]+(?::[0-9]+)?(?:/[a-z0-9._-]+)+")
REGISTRY_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
TOP_LEVEL = {"schema", "branch", "remote", "attempts", "shared_sources", "journal", "channels"}
CHANNEL_KEYS = {"pin", "fields", "sources", "checks", "subject", "note", "registry"}
REGISTRY_KEYS = {"image", "tag", "field"}


class Registry(TypedDict):
    """A registry tag the channel moves to its pinned digest once the pin is on the branch."""

    image: str  # e.g. registry.example.com/team/base, without a tag or digest
    tag: str
    field: str  # the pin field holding the digest (sha256:<64 hex>)


class Channel(TypedDict):
    name: str
    pin: str  # repository-relative path of the pin file
    fields: dict[str, str]  # field name -> the pattern its value must fully match; the file's key order
    sources: list[str]  # git pathspecs a build must not be older than; the pin and shared sources join them
    checks: list[str]  # every one must have passed for this channel's pin to move
    subject: str  # the commit subject when this channel moves
    note: str  # appended to the channel's journal line; may be empty
    registry: NotRequired[Registry]


class Config(TypedDict):
    branch: str
    remote: str
    attempts: int
    shared_sources: list[str]
    journal: list[str]
    channels: dict[str, Channel]


class ConfigError(ValueError):
    """The config is not one this version can promote with; the message lists every problem."""


def _relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and str(path) == value.rstrip("/")


def _pathspec(value: object) -> bool:
    if not isinstance(value, str) or not value or "\0" in value or "\n" in value:
        return False
    plain = value[2:] if value.startswith((":!", ":^")) else value
    # Magic other than exclusion (":(glob)", ":(top)") is left out: it changes what a path means.
    return not plain.startswith(":") and _relative_path(plain.rstrip("/") or ".")


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _channel(name: str, raw: object, problems: list[str]) -> Channel | None:
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
    if not _relative_path(pin) or not str(pin).endswith(".json"):
        problems.append(f"{where}.pin: a repository-relative path to a .json file, without '..'")
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
        problems.append(f"{where}.sources: a list of repository-relative git pathspecs (':!' excludes one)")
        sources = []
    sources = cast(list[str], sources)
    if sources and all(item.startswith((":!", ":^")) for item in sources):
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
            if not isinstance(image, str) or not REGISTRY_IMAGE.fullmatch(image):
                problems.append(
                    f"{where}.registry.image: a registry host and repository, e.g. registry.example.com/team/app"
                )
            if not isinstance(tag, str) or not REGISTRY_TAG.fullmatch(tag):
                problems.append(f"{where}.registry.tag: a tag name")
            if not isinstance(target, str) or target not in patterns:
                problems.append(f"{where}.registry.field: one of the channel's fields, the one holding the digest")
            elif any(re.fullmatch(patterns[target], bad) for bad in ("latest", "sha256:", "sha256:" + "A" * 64)):
                problems.append(f"{where}.registry.field: the field's pattern must accept only sha256:<64 hex> digests")
            if isinstance(image, str) and isinstance(tag, str) and isinstance(target, str):
                channel["registry"] = {"image": image, "tag": tag, "field": target}
    return channel


def parse(raw: object) -> Config:
    """A checked config from decoded JSON; ConfigError lists every problem at once."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        raise ConfigError("the config must be a JSON object")
    data = cast(dict[str, object], raw)
    if data.get("schema") != SCHEMA:
        problems.append(f"schema: this version reads schema {SCHEMA}, the config says {data.get('schema')!r}")
    for key in sorted(set(data) - TOP_LEVEL):
        problems.append(f"{key}: unknown key")
    branch = data.get("branch", "main")
    if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9._/-]+", branch) or ".." in branch:
        problems.append("branch: a branch name")
        branch = "main"
    remote = data.get("remote", "origin")
    if not isinstance(remote, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", remote):
        problems.append("remote: a remote name")
        remote = "origin"
    attempts = data.get("attempts", 3)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 20:
        problems.append("attempts: a whole number from 1 to 20")
        attempts = 3
    shared = data.get("shared_sources", [])
    if not _strings(shared) or not all(_pathspec(item) for item in cast(list[str], shared)):
        problems.append("shared_sources: a list of repository-relative git pathspecs")
        shared = []
    journal = data.get("journal", [])
    if not _strings(journal) or not all(_relative_path(item) for item in cast(list[str], journal)):
        problems.append("journal: a list of repository-relative file paths")
        journal = []
    raw_channels = data.get("channels")
    channels: dict[str, Channel] = {}
    if not isinstance(raw_channels, dict) or not raw_channels:
        problems.append("channels: an object with at least one channel")
    else:
        for name, body in cast(dict[str, object], raw_channels).items():
            channel = _channel(name, body, problems)
            if channel is not None:
                channels[name] = channel
        pins = [c["pin"] for c in channels.values()]
        for pin in sorted({p for p in pins if pins.count(p) > 1}):
            problems.append(f"channels: two channels write {pin}")
        for pin in sorted(set(pins) & set(cast(list[str], journal))):
            problems.append(f"journal: {pin} is a channel's pin file")
    if problems:
        raise ConfigError("\n".join(problems))
    return {
        "branch": branch,
        "remote": remote,
        "attempts": attempts,
        "shared_sources": cast(list[str], shared),
        "journal": cast(list[str], journal),
        "channels": channels,
    }


def load(path: Path) -> Config:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from error
    try:
        return parse(raw)
    except ConfigError as error:
        raise ConfigError("\n".join(f"{path}: {line}" for line in str(error).splitlines())) from error


def sources(config: Config, channel: Channel) -> list[str]:
    """What a build of this channel must not be older than: its sources, the shared ones, its pin."""
    out = [*channel["sources"], *config["shared_sources"], channel["pin"]]
    return list(dict.fromkeys(out))
