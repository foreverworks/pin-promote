"""pin-promote's command line.

    pin-promote check   [--config FILE]
    pin-promote show    [--config FILE] [--ref REF]
    pin-promote promote [--config FILE] --build SHA --pin CHANNEL.FIELD=VALUE ...
                        [--passed CHECK ...] [--needs-json JSON | --needs-file FILE]
                        [--run-url URL] [--author "Name <email>"] [--dry-run] [--json]
    pin-promote tag     [--config FILE] --channel NAME [--ref REF] [--plain-http]

Exit status: 0 done (or nothing to do), 1 refused or failed, 2 a usage error.
"""

import argparse
import json
import sys
from pathlib import Path

from . import __version__, checks, registry
from .config import Config, ConfigError, load
from .promote import GitError, PromotionError, Request, current, git, promote

DEFAULT_CONFIG = "pin-promote.json"


def _pins(values: list[str], parser: argparse.ArgumentParser) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for value in values:
        target, equals, given = value.partition("=")
        channel, dot, field = target.partition(".")
        if not equals or not dot or not channel or not field:
            parser.error(f"--pin takes CHANNEL.FIELD=VALUE: {value!r}")
        if field in out.setdefault(channel, {}):
            parser.error(f"--pin {target} given twice")
        out[channel][field] = given
    return out


def _tags(config: Config, repo: Path, names: list[str], ref: str, plain_http: bool, lines: list[str]) -> list[str]:
    """Move each named channel's registry tag to the digest its pin holds at `ref`."""
    moved: list[str] = []
    pins = current(repo, config, ref)
    for name in names:
        channel = config["channels"][name]
        if "registry" not in channel:
            continue
        target = channel["registry"]
        pin = pins.get(name)
        digest = pin.get(target["field"]) if pin else None
        if not isinstance(digest, str):
            raise registry.RegistryError(f"{name}: {channel['pin']} at {ref} holds no {target['field']}")
        image = f"{target['image']}:{target['tag']}"
        if registry.move_tag(target["image"], target["tag"], digest, plain_http=plain_http):
            moved.append(name)
            lines.append(f"{name}: moved {image} to {digest}")
        else:
            lines.append(f"{name}: {image} already at {digest}")
    return moved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pin-promote", description="Move pins only after their checks pass.")
    parser.add_argument("--version", action="version", version=f"pin-promote {__version__}")
    parser.add_argument("-C", dest="repo", default=".", help="the repository (default: the current directory)")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_config(name: str, help_text: str) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text)
        command.add_argument(
            "--config", default=DEFAULT_CONFIG, help=f"relative to the repository (default {DEFAULT_CONFIG})"
        )
        return command

    with_config("check", "check the config and print its channels")
    show = with_config("show", "print every channel's pin at a ref")
    show.add_argument("--ref", default="", help="default: the remote branch as last fetched")
    run = with_config("promote", "move pins after their checks, refusing a stale build")
    run.add_argument("--build", required=True, help="the full commit id the artifacts were built from")
    run.add_argument("--pin", action="append", default=[], metavar="CHANNEL.FIELD=VALUE")
    run.add_argument("--passed", action="append", default=[], metavar="CHECK", help="a check that passed")
    evidence = run.add_mutually_exclusive_group()
    evidence.add_argument("--needs-json", default="", help="job results, GitHub Actions' toJSON(needs)")
    evidence.add_argument("--needs-file", default="", help="the same, from a file")
    run.add_argument("--run-url", default="", help="a link to the build, for the commit and the journal")
    run.add_argument("--author", default="", help="'Name <email>' for the commit (default: git's own)")
    run.add_argument("--dry-run", action="store_true", help="check everything, commit nothing")
    run.add_argument("--plain-http", action="store_true", help="talk to the registry over http (a local test registry)")
    run.add_argument("--json", action="store_true", help="print the result as JSON")
    tag = with_config("tag", "move a channel's registry tag to the digest its pin holds (a retry)")
    tag.add_argument("--channel", required=True)
    tag.add_argument("--ref", default="")
    tag.add_argument("--plain-http", action="store_true")
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    try:
        config = load(repo / args.config)
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 1
    branch_ref = f"refs/remotes/{config['remote']}/{config['branch']}"
    try:
        if args.command == "check":
            for name, channel in config["channels"].items():
                print(f"{name}: {channel['pin']} after {', '.join(channel['checks'])}")
            return 0
        if args.command == "show":
            print(json.dumps(current(repo, config, args.ref or branch_ref), indent=2, sort_keys=True))
            return 0
        if args.command == "tag":
            if args.channel not in config["channels"] or "registry" not in config["channels"][args.channel]:
                parser.error(f"--channel: {args.channel} has no registry tag")
            git(repo, "fetch", "--quiet", config["remote"], f"+refs/heads/{config['branch']}:{branch_ref}")
            lines: list[str] = []
            _tags(config, repo, [args.channel], args.ref or branch_ref, args.plain_http, lines)
            print("\n".join(lines))
            return 0
        needs: dict[str, str] = {}
        if args.needs_json or args.needs_file:
            text = args.needs_json or Path(args.needs_file).read_text(encoding="utf-8")
            needs = checks.from_needs(text)
        request: Request = {
            "build": args.build,
            "pins": _pins(args.pin, parser),
            "results": checks.evidence(args.passed, needs),
            "run_url": args.run_url,
            "dry_run": args.dry_run,
        }
        if args.author:
            request["author"] = args.author
        log: list[str] = []
        result = promote(repo, config, request, log=log)
        for line in log:
            print(line, file=sys.stderr)
        tags: list[str] = []
        if not result["dry_run"]:
            # After the pin is on the branch, and on a retry that changed nothing: the tag follows the pin.
            tags = _tags(config, repo, list(request["pins"]), result["commit"], args.plain_http, log := [])
            for line in log:
                print(line, file=sys.stderr)
        if args.json:
            print(json.dumps({**result, "tags_moved": tags}, sort_keys=True))
        else:
            verb = "would promote" if result["dry_run"] else "promoted"
            if result["promoted"]:
                print(f"{verb} {', '.join(result['promoted'])} in {result['commit']}")
            if result["unchanged"]:
                print(f"unchanged: {', '.join(result['unchanged'])} (at {result['commit']})")
        return 0
    except (PromotionError, checks.EvidenceError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    except (GitError, registry.RegistryError, OSError) as error:
        print(f"failed: {error}", file=sys.stderr)
        return 1
