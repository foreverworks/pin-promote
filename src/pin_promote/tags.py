"""The registry tag follows the pin as the branch's tip holds it, never a pin this run only remembers.

After a promotion (or a run that found the pin unchanged) the branch is fetched again and each
channel's pin is read at its tip. The tag moves to that digest only when it is the digest this run
promoted: if another promotion landed in between, the pin holds a newer build, the tag is left to
that promotion, and a line says so. `pin-promote tag` passes no expected digest and moves the tag
to whatever the tip's pin holds.

The registry has no compare-and-swap, so a newer promotion's tag move can still be overwritten by an
older run's PUT: the pin is read once more just before the PUT, which narrows that window and does not
close it. A later run, or `pin-promote tag`, moves the tag back to the tip's pin (README, "Registry tags").
"""

from collections.abc import Callable
from functools import partial
from pathlib import Path

from .config import Channel, Local
from .promote import fetch, git, pin_at, tip_config
from .registry import RegistryError, SupersededError, move_tag


def _holds(repo: Path, local: Local, channel: Channel, field: str, digest: str) -> bool:
    """Whether the branch's tip, fetched just now, still has this digest in the channel's pin."""
    ref = fetch(repo, local)
    pin = pin_at(repo, ref, channel)
    return pin is not None and pin.get(field) == digest


def follow(
    repo: Path,
    local: Local,
    config_path: str,
    names: list[str],
    expected: dict[str, dict[str, str]] | None,
    *,
    plain_http: bool,
    explicit: bool,
    moved: list[str],
    say: Callable[[str], None],
) -> None:
    """Move the registry tag of each named channel, appending each channel that moved to `moved`.

    `expected` is channel -> the pin values this run promoted or found unchanged (None: any digest).
    A channel without a registry block is skipped, or an error when `explicit` (it was asked for)."""
    ref = fetch(repo, local)
    config = tip_config(repo, ref, config_path, local)
    tip = git(repo, "rev-parse", ref)
    for name in names:
        channel = config["channels"].get(name)
        target = channel.get("registry") if channel else None
        if channel is None or target is None:
            if explicit:
                raise RegistryError(f"{name}: no registry tag is configured at the tip of {config['branch']}")
            continue
        pin = pin_at(repo, ref, channel)
        digest = pin.get(target["field"]) if pin else None
        if not isinstance(digest, str):
            raise RegistryError(f"{name}: {channel['pin']} at {tip} holds no {target['field']}")
        image = f"{target['image']}:{target['tag']}"
        wanted = expected[name].get(target["field"]) if expected is not None else digest
        if digest != wanted:
            say(f"{name}: a newer promotion holds the pin ({digest}), so {image} is left to it")
            continue
        try:
            changed = move_tag(
                target["image"],
                target["tag"],
                digest,
                plain_http=plain_http,
                token_host=target.get("token_host"),
                current=partial(_holds, repo, local, channel, target["field"], digest),
            )
        except SupersededError:
            say(f"{name}: a newer promotion holds the pin, so {image} is left to it")
            continue
        if changed:
            moved.append(name)
            say(f"{name}: moved {image} to {digest}")
        else:
            say(f"{name}: {image} already at {digest}")
