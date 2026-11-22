"""The promotion against real git repositories: checks, values, stale builds, retries."""

import copy
import json
import subprocess
from pathlib import Path

from pin_promote import GitError, PromotionError, Request, current, parse, promote
from pin_promote.promote import journal_entry, pin_text

from .support import CONFIG, CONFIG_PATH, FP_A, FP_B, FP_C, Repository, run_git

PASSED = {"boot": "success", "smoke": "success", "host-test": "success"}


def request(build: str, pins: dict[str, dict[str, str]], **extra: object) -> Request:
    out: Request = {"build": build, "pins": pins, "results": dict(PASSED), "run_url": "https://ci.example.com/run/7"}
    out.update(extra)  # type: ignore[typeddict-item]
    return out


class Promote(Repository):
    def test_moves_the_pin_with_a_journal_entry_and_a_message(self) -> None:
        result = promote(self.clone, self.config, CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(result["promoted"], ["vm"])
        self.assertEqual(result["commit"], self.tip())
        self.assertEqual(self.on_main("images/vm/pin.json"), json.dumps({"fingerprint": FP_B}) + "\n")
        self.assertEqual(
            run_git(self.remote, "diff", "--name-only", "main~1", "main").split(),
            ["CHANGELOG.md", "images/vm/pin.json"],
        )
        changelog = self.on_main("CHANGELOG.md")
        self.assertIn(f"- vm: fingerprint `{FP_B}`", changelog)
        self.assertIn(f"[build](https://ci.example.com/run/7) of `{self.build}`, after boot passed", changelog)
        body = run_git(self.remote, "log", "-1", "--format=%B", "main")
        self.assertTrue(body.startswith("vm: pin the verified image\n"))
        self.assertIn(f"Build: {self.build}", body)
        self.assertIn("Checks passed: boot", body)

    def test_a_retry_with_the_same_values_commits_nothing(self) -> None:
        first = promote(self.clone, self.config, CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_B}}))
        again = promote(self.clone, self.config, CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(again["promoted"], [])
        self.assertEqual(again["unchanged"], ["vm"])
        self.assertEqual(again["commit"], first["commit"])
        self.assertEqual(self.tip(), first["commit"])

    def test_the_same_values_written_another_way_are_unchanged(self) -> None:
        self.commit({"images/vm/pin.json": json.dumps({"fingerprint": FP_B}, indent=4) + "\n"}, "pretty")
        tip = self.tip()
        result = promote(self.clone, self.config, CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual((result["promoted"], self.tip()), ([], tip))

    def test_two_channels_at_once_join_their_subjects_and_checks(self) -> None:
        pins = {"vm": {"fingerprint": FP_B}, "host": {"os": "1" * 16, "fingerprint": FP_C}}
        result = promote(self.clone, self.config, CONFIG_PATH, request(self.build, pins))
        self.assertEqual(result["promoted"], ["vm", "host"])
        self.assertEqual(self.pin("images/host/pin.json"), {"os": "1" * 16, "fingerprint": FP_C})
        self.assertEqual(list(json.loads(self.on_main("images/host/pin.json"))), ["os", "fingerprint"])
        subject = run_git(self.remote, "log", "-1", "--format=%s", "main")
        self.assertEqual(subject, "vm: pin the verified image; host: pin the verified build")
        self.assertIn("after boot, smoke, host-test passed", self.on_main("CHANGELOG.md"))
        self.assertIn("what the next install installs", self.on_main("CHANGELOG.md"))

    def test_an_unchanged_channel_does_not_block_a_changed_one(self) -> None:
        self.commit({"install/driver.py": "newer\n"}, "a host source moves")  # host is stale, vm is not
        pins = {"vm": {"fingerprint": FP_B}, "host": {"os": "0" * 16, "fingerprint": FP_A}}
        result = promote(self.clone, self.config, CONFIG_PATH, request(self.build, pins))
        self.assertEqual((result["promoted"], result["unchanged"]), (["vm"], ["host"]))

    def test_a_pin_file_that_does_not_exist_yet_is_created(self) -> None:
        run_git(self.clone, "checkout", "-q", "--detach", "origin/main")
        run_git(self.clone, "rm", "-q", "images/vm/pin.json")
        run_git(self.clone, "commit", "-q", "-m", "no pin yet")
        run_git(self.clone, "push", "-q", "origin", "HEAD:main")
        build = run_git(self.clone, "rev-parse", "HEAD")
        result = promote(self.clone, self.config, CONFIG_PATH, request(build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(result["promoted"], ["vm"])
        self.assertEqual(self.pin("images/vm/pin.json"), {"fingerprint": FP_B})

    def test_the_author_is_the_one_given(self) -> None:
        promote(
            self.clone,
            self.config,
            CONFIG_PATH,
            request(self.build, {"vm": {"fingerprint": FP_B}}, author="Promoter <bot@example.com>"),
        )
        self.assertEqual(run_git(self.remote, "log", "-1", "--format=%an <%ae>", "main"), "Promoter <bot@example.com>")
        with self.assertRaisesRegex(PromotionError, "Name <email>"):
            promote(
                self.clone,
                self.config,
                CONFIG_PATH,
                request(self.build, {"vm": {"fingerprint": FP_C}}, author="nobody"),
            )

    def test_the_callers_checkout_is_left_as_it_was(self) -> None:
        run_git(self.clone, "checkout", "-q", "--detach", self.build)
        (self.clone / "docs/readme.md").write_text("an uncommitted edit\n")
        head = run_git(self.clone, "rev-parse", "HEAD")
        promote(self.clone, self.config, CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(run_git(self.clone, "rev-parse", "HEAD"), head)
        self.assertEqual((self.clone / "docs/readme.md").read_text(), "an uncommitted edit\n")
        self.assertEqual(len(run_git(self.clone, "worktree", "list").splitlines()), 1)

    def test_a_dry_run_commits_nothing(self) -> None:
        tip = self.tip()
        result = promote(
            self.clone, self.config, CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_B}}, dry_run=True)
        )
        self.assertEqual((result["promoted"], result["dry_run"], self.tip()), (["vm"], True, tip))


class Refused(Repository):
    def refused(self, pattern: str, req: Request) -> None:
        tip = self.tip()
        with self.assertRaisesRegex(PromotionError, pattern):
            promote(self.clone, self.config, CONFIG_PATH, req)
        self.assertEqual(self.tip(), tip, "a refused promotion pushed")

    def test_a_check_that_did_not_pass(self) -> None:
        self.refused(
            r"checks not passed: boot \(failure\)",
            request(self.build, {"vm": {"fingerprint": FP_B}}, results={"boot": "failure"}),
        )
        self.refused(
            r"checks not passed: boot \(not run\)", request(self.build, {"vm": {"fingerprint": FP_B}}, results={})
        )

    def test_every_check_of_every_channel_named(self) -> None:
        pins = {"vm": {"fingerprint": FP_B}, "host": {"os": "1" * 16, "fingerprint": FP_C}}
        self.refused(r"host-test \(skipped\)", request(self.build, pins, results={**PASSED, "host-test": "skipped"}))

    def test_a_value_that_does_not_match_its_field(self) -> None:
        self.refused(r"vm\.fingerprint: 'B+' does not match", request(self.build, {"vm": {"fingerprint": "B" * 64}}))
        self.refused("missing: fingerprint", request(self.build, {"host": {"os": "1" * 16}}))
        self.refused("no such field: digest", request(self.build, {"vm": {"fingerprint": FP_B, "digest": "x"}}))
        self.refused("no such channel: other", request(self.build, {"other": {"fingerprint": FP_B}}))
        self.refused("full hex object name", request("HEAD", {"vm": {"fingerprint": FP_B}}))

    def test_a_build_of_another_branch(self) -> None:
        run_git(self.clone, "checkout", "-q", "--detach", self.build)
        (self.clone / "images/vm/build.sh").write_text("a branch\n")
        run_git(self.clone, "commit", "-qam", "a branch")
        branch = run_git(self.clone, "rev-parse", "HEAD")
        self.refused("is not on main", request(branch, {"vm": {"fingerprint": FP_B}}))
        self.refused("is not in this repository", request("f" * 40, {"vm": {"fingerprint": FP_B}}))

    def test_a_build_older_than_its_sources(self) -> None:
        self.commit({"images/vm/build.sh": "newer\n"}, "the image's source moves")
        self.refused(
            r"vm: the build is older than its sources on main: images/vm/build\.sh",
            request(self.build, {"vm": {"fingerprint": FP_B}}),
        )

    def test_a_build_older_than_the_lock(self) -> None:
        """A shared source, such as the input lock, refuses an older build of every channel."""
        self.commit({"lock.json": '{"tool": "1.1"}\n'}, "the lock moves")
        self.refused("vm: the build is older .*: lock.json", request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.refused(
            "host: the build is older .*: lock.json",
            request(self.build, {"host": {"os": "1" * 16, "fingerprint": FP_B}}),
        )

    def test_a_pin_that_moved_meanwhile(self) -> None:
        self.commit({"images/host/pin.json": json.dumps({"os": "2" * 16, "fingerprint": FP_C}) + "\n"}, "by hand")
        self.refused(
            "host: .*images/host/pin.json", request(self.build, {"host": {"os": "1" * 16, "fingerprint": FP_B}})
        )

    def test_an_excluded_path_or_unrelated_file_does_not_block(self) -> None:
        self.commit({"images/host/spec.yml": "another host\n", "docs/readme.md": "more docs\n"}, "elsewhere")
        result = promote(self.clone, self.config, CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(result["promoted"], ["vm"])


class Retry(Repository):
    def hook(self, refuse: int, *, move: str = "", empty: bool = False) -> Path:
        """A pre-receive hook refusing the next `refuse` pushes; the count of pushes it saw is in the file.

        With `move` (a commit already on the remote) or `empty`, each refusal first moves main, outside the
        quarantine, as a concurrent push would: to that commit, or by one more empty commit."""
        seen = self.root / "pushes"
        seen.write_text("0")
        # A hook runs in a quarantine: objects it writes would vanish with the refused push.
        outside = "env -u GIT_QUARANTINE_PATH -u GIT_OBJECT_DIRECTORY -u GIT_ALTERNATE_OBJECT_DIRECTORIES"
        lines = [
            "#!/bin/sh",
            f'n=$(cat "{seen}")',
            f'echo $((n + 1)) > "{seen}"',
            f'[ "$n" -lt {refuse} ] || exit 0',
        ]
        if move:
            lines.append(f"{outside} git update-ref refs/heads/main {move}")
        if empty:
            lines += [
                "export GIT_AUTHOR_NAME=x GIT_AUTHOR_EMAIL=x@example.com",
                "export GIT_COMMITTER_NAME=x GIT_COMMITTER_EMAIL=x@example.com",
                f"new=$({outside} git commit-tree main^{{tree}} -p main -m concurrent)",
                f'{outside} git update-ref refs/heads/main "$new"',
            ]
        lines += ['echo "the remote says no" >&2', "exit 1"]
        hook = self.remote / "hooks" / "pre-receive"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text("\n".join(lines) + "\n")
        hook.chmod(0o755)
        return seen

    def pushes(self, seen: Path) -> int:
        return int(seen.read_text())

    def test_a_refused_push_after_the_branch_moved_is_tried_again_from_the_new_tip(self) -> None:
        seen = self.hook(1, empty=True)
        log: list[str] = []
        pauses: list[float] = []
        result = promote(
            self.clone,
            self.config,
            CONFIG_PATH,
            request(self.build, {"vm": {"fingerprint": FP_B}}),
            log=log,
            sleep=pauses.append,
        )
        self.assertEqual((result["promoted"], result["attempts"], self.pushes(seen)), (["vm"], 2, 2))
        self.assertIn("main moved, trying again", log[0])
        self.assertEqual(pauses, [1.0])
        self.assertEqual(self.pin("images/vm/pin.json"), {"fingerprint": FP_B})
        self.assertEqual(run_git(self.remote, "log", "--format=%s", "-3", "main").splitlines()[1], "concurrent")

    def test_the_pause_grows_between_attempts(self) -> None:
        self.hook(10, empty=True)
        pauses: list[float] = []
        config = parse({**CONFIG, "attempts": 4})
        with self.assertRaisesRegex(PromotionError, "refused 4 times"):
            promote(
                self.clone,
                config,
                CONFIG_PATH,
                request(self.build, {"vm": {"fingerprint": FP_B}}),
                sleep=pauses.append,
            )
        self.assertEqual(pauses, [1.0, 2.0, 4.0])  # none after the last attempt

    def test_the_branch_moving_to_a_newer_source_between_attempts_refuses(self) -> None:
        # A concurrent push lands a newer image source while the first push is refused.
        run_git(self.clone, "checkout", "-q", "--detach", "origin/main")
        (self.clone / "images/vm/build.sh").write_text("concurrent\n")
        run_git(self.clone, "commit", "-qam", "concurrent")
        concurrent = run_git(self.clone, "rev-parse", "HEAD")
        run_git(self.clone, "push", "-q", "origin", f"{concurrent}:refs/heads/staged")
        seen = self.hook(1, move=concurrent)
        with self.assertRaisesRegex(PromotionError, "older than its sources"):
            promote(
                self.clone,
                self.config,
                CONFIG_PATH,
                request(self.build, {"vm": {"fingerprint": FP_B}}),
                sleep=lambda _: None,
            )
        self.assertEqual(self.tip(), concurrent)
        self.assertEqual(self.pushes(seen), 1)

    def test_a_refusal_that_did_not_move_the_branch_fails_at_once_with_gits_message(self) -> None:
        seen = self.hook(10)  # refuses, and leaves main where it was: a hook, a permission, a protected branch
        tip = self.tip()
        pauses: list[float] = []
        with self.assertRaisesRegex(PromotionError, "did not move.*the remote says no") as caught:
            promote(
                self.clone,
                self.config,
                CONFIG_PATH,
                request(self.build, {"vm": {"fingerprint": FP_B}}),
                sleep=pauses.append,
            )
        self.assertIn("pre-receive hook declined", str(caught.exception))
        self.assertEqual((self.pushes(seen), pauses, self.tip()), (1, [], tip))
        self.assertEqual(len(run_git(self.clone, "worktree", "list").splitlines()), 1)

    def test_refused_every_time_while_the_branch_keeps_moving(self) -> None:
        self.hook(10, empty=True)
        with self.assertRaisesRegex(PromotionError, "refused 3 times"):
            promote(
                self.clone,
                self.config,
                CONFIG_PATH,
                request(self.build, {"vm": {"fingerprint": FP_B}}),
                sleep=lambda _: None,
            )
        self.assertEqual(self.pin("images/vm/pin.json"), {"fingerprint": FP_A})
        self.assertEqual(len(run_git(self.clone, "worktree", "list").splitlines()), 1)


class Pure(Repository):
    def test_pin_text_keeps_the_config_order(self) -> None:
        host = self.config["channels"]["host"]
        self.assertEqual(
            pin_text(host, {"fingerprint": FP_A, "os": "1" * 16}),
            json.dumps({"os": "1" * 16, "fingerprint": FP_A}) + "\n",
        )

    def test_journal_without_a_run_url(self) -> None:
        req = request(self.build, {"vm": {"fingerprint": FP_B}}, run_url="")
        entry = journal_entry(self.config, req, ["vm"], ["boot"], "2026-01-02")
        self.assertTrue(entry.startswith("\n## 2026-01-02: promoted vm\n"))
        self.assertIn(f"From build of `{self.build}`, after boot passed.", entry)


def with_vm(raw: dict[str, object] | None = None, **vm: object) -> dict[str, object]:
    """The fixture config with these keys of the `vm` channel replaced."""
    out = copy.deepcopy(raw or CONFIG)
    out["channels"]["vm"].update(vm)  # type: ignore[index]
    return out


class StaleByClass(Repository):
    """One `git diff` per class: the channel's sources, the shared sources, its pin."""

    def refused(self, pattern: str) -> None:
        tip = self.tip()
        with self.assertRaisesRegex(PromotionError, pattern):
            promote(self.clone, self.config, CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(self.tip(), tip, "a refused promotion pushed")

    def test_a_channel_exclusion_of_the_shared_lock_no_longer_lets_a_stale_build_through(self) -> None:
        self.set_config(with_vm(sources=["images/", ":!images/host/", ":!lock.json"]))
        self.commit({"lock.json": '{"tool": "2.0"}\n'}, "the lock moves")
        self.refused(r"vm: the build is older than the shared sources on main: lock\.json")

    def test_a_channel_exclusion_of_its_own_pin_no_longer_lets_a_stale_build_through(self) -> None:
        self.set_config(with_vm(sources=["images/", ":!images/host/", ":!images/vm/pin.json"]))
        self.commit({"images/vm/pin.json": json.dumps({"fingerprint": FP_C}) + "\n"}, "another promotion")
        self.refused(r"vm: the build is older than its pin on main: images/vm/pin\.json")

    def test_the_channels_exclusions_still_apply_inside_its_own_sources(self) -> None:
        self.commit({"images/host/spec.yml": "changed\n"}, "an excluded path")
        result = promote(self.clone, self.config, CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(result["promoted"], ["vm"])

    def test_the_pin_is_a_literal_path(self) -> None:
        """A pin named like a glob is that one file; a neighbour the glob would match does not refuse."""
        raw = with_vm(pin="deploy/a[1].json")
        self.set_config(raw)
        self.commit({"deploy/a1.json": "{}\n", "deploy/a[1].json": json.dumps({"fingerprint": FP_A}) + "\n"}, "pins")
        build = self.tip()
        self.commit({"deploy/a1.json": '{"neighbour": true}\n'}, "the neighbour changes")
        result = promote(self.clone, self.config, CONFIG_PATH, request(build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(result["promoted"], ["vm"])
        self.assertEqual(self.pin("deploy/a[1].json"), {"fingerprint": FP_B})
        self.commit({"deploy/a[1].json": json.dumps({"fingerprint": FP_C}) + "\n"}, "the pin moves")
        with self.assertRaisesRegex(PromotionError, r"its pin on main: deploy/a\[1\]\.json"):
            promote(self.clone, self.config, CONFIG_PATH, request(build, {"vm": {"fingerprint": FP_A}}))

    def test_globs_select_files_in_sources_and_shared_sources(self) -> None:
        self.set_config({**with_vm(sources=["images/vm/*.sh"]), "shared_sources": ["*.lock"]})
        self.commit({"images/vm/other.txt": "x\n", "docs/readme.md": "y\n"}, "unrelated")
        dry = request(self.build, {"vm": {"fingerprint": FP_B}}, dry_run=True)
        self.assertEqual(promote(self.clone, self.config, CONFIG_PATH, dry)["promoted"], ["vm"])
        self.commit({"images/vm/build.sh": "newer\n"}, "a source")
        self.refused(r"its sources on main: images/vm/build\.sh")

    def test_a_glob_in_the_shared_sources_refuses(self) -> None:
        self.set_config({**CONFIG, "shared_sources": ["*.lock"]})
        self.commit({"deps/yarn.lock": "x\n"}, "a lock")
        self.refused(r"the shared sources on main: deps/yarn\.lock")

    def test_a_changed_submodule_counts(self) -> None:
        self.set_config({**CONFIG, "shared_sources": ["lock.json", "vendor/"]})
        other = self.root / "other"
        subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
        run_git(
            other, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "a"
        )
        first = run_git(other, "rev-parse", "HEAD")
        run_git(
            other, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "b"
        )
        second = run_git(other, "rev-parse", "HEAD")
        run_git(self.clone, "checkout", "-q", "--detach", "origin/main")
        for commit in (first, second):
            run_git(self.clone, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/lib")
            run_git(self.clone, "commit", "-q", "-m", f"vendor at {commit[:7]}")
            if commit == first:
                run_git(self.clone, "push", "-q", "origin", "HEAD:main")
                build = run_git(self.clone, "rev-parse", "HEAD")
        run_git(self.clone, "push", "-q", "origin", "HEAD:main")
        with self.assertRaisesRegex(PromotionError, r"the shared sources on main: vendor/lib"):
            promote(self.clone, self.config, CONFIG_PATH, request(build, {"vm": {"fingerprint": FP_B}}))


class ConfigAtTheTip(Repository):
    """The config on the branch's tip governs, whatever the caller's local copy says."""

    def test_a_required_check_added_after_the_build_is_enforced_for_that_build(self) -> None:
        self.set_config(with_vm(checks=["boot", "lint"]))
        local = parse(CONFIG)  # the caller still holds the old config
        with self.assertRaisesRegex(PromotionError, r"checks not passed: lint \(not run\)"):
            promote(self.clone, local, CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(self.pin("images/vm/pin.json"), {"fingerprint": FP_A})
        done = request(self.build, {"vm": {"fingerprint": FP_B}}, results={**PASSED, "lint": "success"})
        self.assertEqual(promote(self.clone, local, CONFIG_PATH, done)["promoted"], ["vm"])
        self.assertIn("after boot, lint passed", self.on_main("CHANGELOG.md"))

    def test_a_shared_source_added_after_the_build_is_enforced_for_that_build(self) -> None:
        self.set_config({**CONFIG, "shared_sources": ["lock.json", "docs/"]})
        self.commit({"docs/readme.md": "newer docs\n"}, "docs move")
        local = parse(CONFIG)
        with self.assertRaisesRegex(PromotionError, r"the shared sources on main: docs/readme\.md"):
            promote(self.clone, local, CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_B}}))

    def test_the_tip_decides_the_fields_the_journal_the_subject_and_the_note(self) -> None:
        raw = {**with_vm(subject="vm: the tip's subject"), "journal": ["NEWS.md"]}
        self.set_config(raw)
        promote(self.clone, parse(CONFIG), CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(run_git(self.remote, "log", "-1", "--format=%s", "main"), "vm: the tip's subject")
        self.assertIn("promoted vm", self.on_main("NEWS.md"))
        self.assertNotIn("promoted vm", self.on_main("CHANGELOG.md"))
        self.set_config(with_vm(fields={"fingerprint": "[0-9a-f]{64}", "extra": "x+"}))
        with self.assertRaisesRegex(PromotionError, "missing: extra"):
            promote(self.clone, parse(CONFIG), CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_C}}))

    def test_a_channel_that_is_not_in_the_tips_config(self) -> None:
        raw = copy.deepcopy(CONFIG)
        del raw["channels"]["host"]  # type: ignore[attr-defined]
        self.set_config(raw)
        pins = {"host": {"os": "1" * 16, "fingerprint": FP_B}}
        with self.assertRaisesRegex(PromotionError, "no such channel: host"):
            promote(self.clone, parse(CONFIG), CONFIG_PATH, request(self.build, pins))

    def test_a_missing_config_is_refused(self) -> None:
        with self.assertRaisesRegex(PromotionError, r"nope\.json at the tip of main: the config is missing"):
            promote(self.clone, self.config, "nope.json", request(self.build, {"vm": {"fingerprint": FP_B}}))

    def test_an_invalid_config_is_refused(self) -> None:
        self.commit({CONFIG_PATH: json.dumps({**CONFIG, "surprise": 1})}, "broken")
        with self.assertRaisesRegex(PromotionError, "is not valid:\nsurprise: unknown key"):
            promote(self.clone, self.config, CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.commit({CONFIG_PATH: "{"}, "not json")
        with self.assertRaisesRegex(PromotionError, "not JSON"):
            promote(self.clone, self.config, CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_B}}))

    def test_a_config_that_moves_the_branch_or_the_remote_is_refused(self) -> None:
        for key, value in (("branch", "pins"), ("remote", "upstream")):
            self.commit({CONFIG_PATH: json.dumps({**CONFIG, key: value})}, f"another {key}")
            with self.subTest(key=key), self.assertRaisesRegex(PromotionError, f"its {key} is '{value}'"):
                promote(self.clone, self.config, CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(self.pin("images/vm/pin.json"), {"fingerprint": FP_A})

    def test_the_config_may_live_in_a_subdirectory(self) -> None:
        self.set_config(with_vm(checks=["boot", "lint"]), "ci/pins.json")
        # The root config still says one check; only the file named is read.
        with self.assertRaisesRegex(PromotionError, "lint"):
            promote(self.clone, self.config, "ci/pins.json", request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(
            promote(self.clone, self.config, CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_B}}))[
                "promoted"
            ],
            ["vm"],
        )

    def test_a_config_that_is_not_a_regular_file_is_refused(self) -> None:
        run_git(self.clone, "checkout", "-q", "--detach", "origin/main")
        (self.clone / "linked.json").symlink_to("pin-promote.json")
        run_git(self.clone, "add", "linked.json")
        run_git(self.clone, "commit", "-q", "-m", "a link")
        run_git(self.clone, "push", "-q", "origin", "HEAD:main")
        with self.assertRaisesRegex(PromotionError, "not a regular file"):
            promote(self.clone, self.config, "linked.json", request(self.build, {"vm": {"fingerprint": FP_B}}))


class NoWritingThroughLinks(Repository):
    def link(self, name: str, target: str) -> None:
        run_git(self.clone, "checkout", "-q", "--detach", "origin/main")
        path = self.clone / name
        if path.is_dir() and not path.is_symlink():
            run_git(self.clone, "rm", "-q", "-r", "-f", name)
        elif path.exists() or path.is_symlink():
            run_git(self.clone, "rm", "-q", "-f", name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
        run_git(self.clone, "add", name)
        run_git(self.clone, "commit", "-q", "-m", f"{name} links to {target}")
        run_git(self.clone, "push", "-q", "origin", "HEAD:main")
        run_git(self.clone, "fetch", "-q", "origin", "main")

    def refused(self, pattern: str) -> None:
        tip = self.tip()
        build = run_git(self.clone, "rev-parse", "origin/main")
        with self.assertRaisesRegex(PromotionError, pattern):
            promote(self.clone, self.config, CONFIG_PATH, request(build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual((self.tip(), self.on_main("docs/readme.md")), (tip, "docs\n"))
        self.assertEqual(len(run_git(self.clone, "worktree", "list").splitlines()), 1)

    def test_a_pin_inside_a_linked_directory(self) -> None:
        self.link("images/vm", "../docs")
        self.refused(r"images/vm/pin\.json: vm is a symlink on the branch")

    def test_a_pin_that_is_a_link(self) -> None:
        self.link("images/vm/pin.json", "../../docs/readme.md")
        self.refused(r"images/vm/pin\.json: pin\.json is a symlink on the branch")

    def test_a_journal_that_is_a_link(self) -> None:
        self.link("CHANGELOG.md", "docs/readme.md")
        self.refused(r"CHANGELOG\.md: CHANGELOG\.md is a symlink on the branch")

    def test_a_link_out_of_the_repository(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        self.link("images/vm", str(outside))
        self.refused(r"is a symlink")
        self.assertEqual(list(outside.iterdir()), [])


class Shallow(Repository):
    def shallow_clone(self) -> Path:
        self.commit({"docs/readme.md": "newer\n"}, "a later commit")
        clone = self.root / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{self.remote}", str(clone)], check=True)
        return clone

    def test_a_build_missing_from_a_shallow_clone_says_so(self) -> None:
        clone = self.shallow_clone()
        with self.assertRaisesRegex(
            PromotionError, r"not in this repository; the history may be shallow: fetch the whole history"
        ) as caught:
            promote(clone, self.config, CONFIG_PATH, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertIn("actions/checkout fetch-depth: 0", str(caught.exception))

    def test_a_clone_with_the_whole_history_does_not_say_it(self) -> None:
        with self.assertRaises(PromotionError) as caught:
            promote(self.clone, self.config, CONFIG_PATH, request("f" * 40, {"vm": {"fingerprint": FP_B}}))
        self.assertNotIn("shallow", str(caught.exception))


class Reading(Repository):
    def test_an_absent_path_is_none_and_a_bad_ref_is_an_error(self) -> None:
        run_git(self.clone, "checkout", "-q", "--detach", "origin/main")
        run_git(self.clone, "rm", "-q", "images/vm/pin.json")
        run_git(self.clone, "commit", "-q", "-m", "no pin")
        run_git(self.clone, "push", "-q", "origin", "HEAD:main")
        run_git(self.clone, "fetch", "-q", "origin", "main")
        pins = current(self.clone, self.config, "origin/main")
        self.assertEqual((pins["vm"], (pins["host"] or {})["os"]), (None, "0" * 16))
        with self.assertRaisesRegex(GitError, "nonsense"):
            current(self.clone, self.config, "nonsense")

    def test_a_pin_that_is_a_directory_is_an_error_not_an_absent_file(self) -> None:
        raw = with_vm(pin="deploy/a.json")
        self.set_config(raw)
        self.commit({"deploy/a.json/inner": "x\n"}, "a directory")
        with self.assertRaisesRegex(GitError, "deploy/a.json at .* is not a file"):
            current(self.clone, self.config, "origin/main")

    def test_a_pin_that_is_not_an_object_is_refused(self) -> None:
        self.commit({"images/vm/pin.json": "[1]\n"}, "an array")
        with self.assertRaisesRegex(PromotionError, "not a JSON object"):
            current(self.clone, self.config, "origin/main")
