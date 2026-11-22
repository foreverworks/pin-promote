"""The promotion against real git repositories: checks, values, stale builds, retries."""

import json
from pathlib import Path

from pin_promote import PromotionError, Request, promote
from pin_promote.promote import journal_entry, pin_text

from .support import FP_A, FP_B, FP_C, Repository, run_git

PASSED = {"boot": "success", "smoke": "success", "host-test": "success"}


def request(build: str, pins: dict[str, dict[str, str]], **extra: object) -> Request:
    out: Request = {"build": build, "pins": pins, "results": dict(PASSED), "run_url": "https://ci.example.com/run/7"}
    out.update(extra)  # type: ignore[typeddict-item]
    return out


class Promote(Repository):
    def test_moves_the_pin_with_a_journal_entry_and_a_message(self) -> None:
        result = promote(self.clone, self.config, request(self.build, {"vm": {"fingerprint": FP_B}}))
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
        first = promote(self.clone, self.config, request(self.build, {"vm": {"fingerprint": FP_B}}))
        again = promote(self.clone, self.config, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(again["promoted"], [])
        self.assertEqual(again["unchanged"], ["vm"])
        self.assertEqual(again["commit"], first["commit"])
        self.assertEqual(self.tip(), first["commit"])

    def test_the_same_values_written_another_way_are_unchanged(self) -> None:
        self.commit({"images/vm/pin.json": json.dumps({"fingerprint": FP_B}, indent=4) + "\n"}, "pretty")
        tip = self.tip()
        result = promote(self.clone, self.config, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual((result["promoted"], self.tip()), ([], tip))

    def test_two_channels_at_once_join_their_subjects_and_checks(self) -> None:
        pins = {"vm": {"fingerprint": FP_B}, "host": {"os": "1" * 16, "fingerprint": FP_C}}
        result = promote(self.clone, self.config, request(self.build, pins))
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
        result = promote(self.clone, self.config, request(self.build, pins))
        self.assertEqual((result["promoted"], result["unchanged"]), (["vm"], ["host"]))

    def test_a_pin_file_that_does_not_exist_yet_is_created(self) -> None:
        run_git(self.clone, "checkout", "-q", "--detach", "origin/main")
        run_git(self.clone, "rm", "-q", "images/vm/pin.json")
        run_git(self.clone, "commit", "-q", "-m", "no pin yet")
        run_git(self.clone, "push", "-q", "origin", "HEAD:main")
        build = run_git(self.clone, "rev-parse", "HEAD")
        result = promote(self.clone, self.config, request(build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(result["promoted"], ["vm"])
        self.assertEqual(self.pin("images/vm/pin.json"), {"fingerprint": FP_B})

    def test_the_author_is_the_one_given(self) -> None:
        promote(
            self.clone,
            self.config,
            request(self.build, {"vm": {"fingerprint": FP_B}}, author="Promoter <bot@example.com>"),
        )
        self.assertEqual(run_git(self.remote, "log", "-1", "--format=%an <%ae>", "main"), "Promoter <bot@example.com>")
        with self.assertRaisesRegex(PromotionError, "Name <email>"):
            promote(self.clone, self.config, request(self.build, {"vm": {"fingerprint": FP_C}}, author="nobody"))

    def test_the_callers_checkout_is_left_as_it_was(self) -> None:
        run_git(self.clone, "checkout", "-q", "--detach", self.build)
        (self.clone / "docs/readme.md").write_text("an uncommitted edit\n")
        head = run_git(self.clone, "rev-parse", "HEAD")
        promote(self.clone, self.config, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(run_git(self.clone, "rev-parse", "HEAD"), head)
        self.assertEqual((self.clone / "docs/readme.md").read_text(), "an uncommitted edit\n")
        self.assertEqual(len(run_git(self.clone, "worktree", "list").splitlines()), 1)

    def test_a_dry_run_commits_nothing(self) -> None:
        tip = self.tip()
        result = promote(self.clone, self.config, request(self.build, {"vm": {"fingerprint": FP_B}}, dry_run=True))
        self.assertEqual((result["promoted"], result["dry_run"], self.tip()), (["vm"], True, tip))


class Refused(Repository):
    def refused(self, pattern: str, req: Request) -> None:
        tip = self.tip()
        with self.assertRaisesRegex(PromotionError, pattern):
            promote(self.clone, self.config, req)
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
            r"vm: the build is older than its sources or pin on main: images/vm/build\.sh",
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
        result = promote(self.clone, self.config, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(result["promoted"], ["vm"])


class Retry(Repository):
    def refuse_pushes(self, count: int) -> Path:
        """The remote refuses the next `count` pushes, as when main moves between the fetch and the push."""
        counter = self.root / "refusals"
        counter.write_text(str(count))
        hook = self.remote / "hooks" / "pre-receive"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(
            f'#!/bin/sh\nn=$(cat "{counter}")\n[ "$n" -gt 0 ] || exit 0\necho $((n - 1)) > "{counter}"\nexit 1\n'
        )
        hook.chmod(0o755)
        return counter

    def test_a_refused_push_is_tried_again_from_the_new_tip(self) -> None:
        self.refuse_pushes(1)
        log: list[str] = []
        result = promote(self.clone, self.config, request(self.build, {"vm": {"fingerprint": FP_B}}), log=log)
        self.assertEqual((result["promoted"], result["attempts"]), (["vm"], 2))
        self.assertIn("trying again", log[0])
        self.assertEqual(self.pin("images/vm/pin.json"), {"fingerprint": FP_B})

    def test_the_branch_moving_to_a_newer_source_between_attempts_refuses(self) -> None:
        # A concurrent push lands a newer image source while the first push is refused.
        run_git(self.clone, "checkout", "-q", "--detach", "origin/main")
        (self.clone / "images/vm/build.sh").write_text("concurrent\n")
        run_git(self.clone, "commit", "-qam", "concurrent")
        concurrent = run_git(self.clone, "rev-parse", "HEAD")
        run_git(self.clone, "push", "-q", "origin", f"{concurrent}:refs/heads/staged")
        counter = self.refuse_pushes(1)
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text(
            hook.read_text().replace(
                "exit 1\n", f"env -u GIT_QUARANTINE_PATH git update-ref refs/heads/main {concurrent}\nexit 1\n"
            )
        )
        with self.assertRaisesRegex(PromotionError, "older than its sources"):
            promote(self.clone, self.config, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(self.tip(), concurrent)
        self.assertEqual(counter.read_text().strip(), "0")

    def test_refused_every_time(self) -> None:
        self.refuse_pushes(10)
        tip = self.tip()
        with self.assertRaisesRegex(PromotionError, "refused 3 times"):
            promote(self.clone, self.config, request(self.build, {"vm": {"fingerprint": FP_B}}))
        self.assertEqual(self.tip(), tip)
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
