"""pin-promote's command line.

    pin-promote check   [--config FILE]
    pin-promote show    [--config FILE] [--ref REF]
    pin-promote promote [--config FILE] --build SHA --pin CHANNEL.FIELD=VALUE ...
                        [--passed CHECK ...] [--needs-json JSON | --needs-file FILE]
                        [--run-url URL] [--author "Name <email>"] [--dry-run] [--json]
    pin-promote tag     [--config FILE] --channel NAME [--plain-http]

`check` and `show` read the local config file; `promote` and `tag` read only its `remote`, `branch` and
`attempts`, and take everything else from the config at the branch's tip.

Exit status: 0 done (or nothing to do), 1 refused or failed, 2 a usage error.
"""

import argparse
import contextlib
import json
import signal
import sys
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from types import FrameType

from . import __version__, checks, registry, tags
from .config import ConfigError, is_relative_path, load, load_local
from .promote import GitError, PromotionError, Request, current, promote

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


def _terminate(signum: int, frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)  # unwinds the stack, so the temporary worktree is removed


@contextlib.contextmanager
def exits_on_sigterm() -> Iterator[None]:
    """SIGTERM (a CI job cancelled) ends the run through SystemExit instead of killing it mid-push."""
    try:
        previous = signal.signal(signal.SIGTERM, _terminate)
    except ValueError:  # not the main thread: nothing to install
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv: list[str] | None = None) -> int:
    with exits_on_sigterm():
        return _main(argv)


def _main(argv: list[str] | None) -> int:
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
    run.add_argument(
        "--plain-http", action="store_true", help="talk to the registry over http (only for a registry on localhost)"
    )
    run.add_argument("--json", action="store_true", help="print the result as JSON")
    tag = with_config("tag", "move a channel's registry tag to the digest its pin holds at the branch's tip (a retry)")
    tag.add_argument("--channel", required=True)
    tag.add_argument("--plain-http", action="store_true")
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    relative = PurePosixPath(args.config).as_posix()
    if not is_relative_path(relative):
        print(f"failed: --config must be a path inside the repository: {args.config!r}", file=sys.stderr)
        return 1
    try:
        if args.command == "check":
            for name, channel in load(repo / relative, relative)["channels"].items():
                print(f"{name}: {channel['pin']} after {', '.join(channel['checks'])}")
            return 0
        if args.command == "show":
            full = load(repo / relative, relative)
            branch_ref = f"refs/remotes/{full['remote']}/{full['branch']}"
            print(json.dumps(current(repo, full, args.ref or branch_ref), indent=2, sort_keys=True))
            return 0
        config = load_local(repo / relative)  # promote and tag: the tip's config governs the rest
        if args.command == "tag":
            tags.follow(
                repo,
                config,
                relative,
                [args.channel],
                None,
                plain_http=args.plain_http,
                explicit=True,
                moved=[],
                say=print,
            )
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
        result = promote(repo, config, relative, request, log=log)
        for line in log:
            print(line, file=sys.stderr)
        moved: list[str] = []
        tag_error = ""
        if not result["dry_run"]:
            # After the pin is on the branch, and on a retry that changed nothing: the tag follows the pin
            # as the branch's tip holds it now.
            try:
                tags.follow(
                    repo,
                    config,
                    relative,
                    list(request["pins"]),
                    request["pins"],
                    plain_http=args.plain_http,
                    explicit=False,
                    moved=moved,
                    say=lambda line: print(line, file=sys.stderr),
                )
            except (registry.RegistryError, PromotionError, GitError) as error:
                tag_error = str(error)
        if args.json:
            print(
                json.dumps(
                    {**result, "tags_moved": moved, **({"tag_error": tag_error} if tag_error else {})}, sort_keys=True
                )
            )
        else:
            verb = "would promote" if result["dry_run"] else "promoted"
            if result["promoted"]:
                print(f"{verb} {', '.join(result['promoted'])} in {result['commit']}")
            if result["unchanged"]:
                print(f"unchanged: {', '.join(result['unchanged'])} (at {result['commit']})")
        if tag_error:
            print(
                f"failed: the pins are on {config['branch']} in {result['commit']}, but a registry tag did not"
                f" move: {tag_error}. Run this promotion again, or `pin-promote tag --channel <name>`.",
                file=sys.stderr,
            )
            return 1
        return 0
    except ConfigError as error:
        print(error, file=sys.stderr)
        return 1
    except (PromotionError, checks.EvidenceError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    except (GitError, registry.RegistryError, OSError, ValueError) as error:
        print(f"failed: {error}", file=sys.stderr)
        return 1
