"""The command line, end to end in a fixture repository."""

import contextlib
import copy
import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

from pin_promote import cli
from pin_promote import promote as real_promote

from .support import (
    CONFIG,
    CONFIG_PATH,
    FP_B,
    OCI_MANIFEST,
    REPO,
    TAG,
    Repository,
    digest_of,
    manifest,
    new_state,
    run_git,
    serve,
)

SRC = Path(__file__).resolve().parent.parent / "src"


class CliCase(Repository):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["-C", str(self.clone), *args])
            except SystemExit as exit_:
                code = int(exit_.code or 0)
        return code, out.getvalue(), err.getvalue()


class Cli(CliCase):
    def test_promote_with_job_results(self) -> None:
        needs = json.dumps({"boot": {"result": "success"}})
        code, out, err = self.run_cli(
            "promote", "--build", self.build, "--pin", f"vm.fingerprint={FP_B}", "--needs-json", needs, "--json"
        )
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual((result["promoted"], result["commit"], result["tags_moved"]), (["vm"], self.tip(), []))
        code, out, _ = self.run_cli(
            "promote", "--build", self.build, "--pin", f"vm.fingerprint={FP_B}", "--passed", "boot"
        )
        self.assertEqual(code, 0)
        self.assertIn("unchanged: vm", out)

    def test_refused_is_exit_1(self) -> None:
        code, _, err = self.run_cli("promote", "--build", self.build, "--pin", f"vm.fingerprint={FP_B}")
        self.assertEqual(code, 1)
        self.assertIn("refused: checks not passed: boot (not run)", err)
        code, _, err = self.run_cli("promote", "--build", self.build, "--pin", "vm.fingerprint=x", "--passed", "boot")
        self.assertEqual(code, 1)

    def test_usage_is_exit_2(self) -> None:
        code, _, err = self.run_cli("promote", "--build", self.build, "--pin", "vm=x")
        self.assertEqual(code, 2)
        self.assertIn("CHANNEL.FIELD=VALUE", err)

    def test_check_and_show(self) -> None:
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 0)
        self.assertIn("vm: images/vm/pin.json after boot", out)
        code, out, _ = self.run_cli("show", "--ref", "origin/main")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["host"]["os"], "0" * 16)

    def test_a_bad_config_is_exit_1(self) -> None:
        (self.clone / "pin-promote.json").write_text("{}")
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("schema", err)

    def test_a_name_passed_by_hand_cannot_overrule_a_failed_job(self) -> None:
        needs = json.dumps({"boot": {"result": "failure"}})
        code, _, err = self.run_cli(
            "promote",
            "--build",
            self.build,
            "--pin",
            f"vm.fingerprint={FP_B}",
            "--needs-json",
            needs,
            "--passed",
            "boot",
        )
        self.assertEqual(code, 1)
        self.assertIn("refused: boot: the job results say failure; --passed cannot override it", err)

    def test_check_reads_the_local_file_and_promote_the_tip(self) -> None:
        raw = copy.deepcopy(CONFIG)
        raw["channels"]["vm"]["checks"] = ["boot", "lint"]  # type: ignore[index]
        self.set_config(raw)
        (self.clone / CONFIG_PATH).write_text(json.dumps(CONFIG))  # the working copy still has the old config
        code, out, _ = self.run_cli("check")
        self.assertIn("vm: images/vm/pin.json after boot\n", out)
        code, _, err = self.run_cli(
            "promote", "--build", self.build, "--pin", f"vm.fingerprint={FP_B}", "--passed", "boot"
        )
        self.assertEqual(code, 1)
        self.assertIn("checks not passed: lint (not run)", err)

    def test_promote_needs_only_the_remote_branch_and_attempts_from_the_local_file(self) -> None:
        (self.clone / CONFIG_PATH).write_text(json.dumps({"schema": 99, "channels": "from an older version"}))
        code, _, err = self.run_cli("check")
        self.assertEqual(code, 1)  # check and show read the whole local file
        code, out, err = self.run_cli(
            "promote", "--build", self.build, "--pin", f"vm.fingerprint={FP_B}", "--passed", "boot"
        )
        self.assertEqual((code, out), (0, f"promoted vm in {self.tip()}\n"), err)
        (self.clone / CONFIG_PATH).write_text(json.dumps({"branch": "-d"}))
        code, _, err = self.run_cli("promote", "--build", self.build, "--pin", f"vm.fingerprint={FP_B}")
        self.assertEqual(code, 1)
        self.assertIn("branch", err)

    def test_the_config_may_be_in_a_subdirectory_but_not_outside_the_repository(self) -> None:
        self.set_config(copy.deepcopy(CONFIG), "ci/pins.json")
        code, _, err = self.run_cli(
            "promote",
            "--config",
            "ci/pins.json",
            "--build",
            self.build,
            "--pin",
            f"vm.fingerprint={FP_B}",
            "--passed",
            "boot",
        )
        self.assertEqual(code, 0, err)
        for config in ("../pins.json", "/tmp/pins.json"):
            code, _, err = self.run_cli("check", "--config", config)
            self.assertEqual((code, "inside the repository" in err), (1, True))

    def test_show_with_a_bad_ref_or_an_unreadable_pin_fails(self) -> None:
        code, _, err = self.run_cli("show", "--ref", "nonsense")
        self.assertEqual(code, 1)
        self.assertIn("failed: git", err)
        (self.clone / "images/vm/pin.json").write_bytes(b"\xff\xfe\x00 not utf-8")
        run_git(self.clone, "add", "-A")
        run_git(self.clone, "commit", "-q", "-m", "binary pin")
        code, _, err = self.run_cli("show", "--ref", "HEAD")
        self.assertEqual(code, 1)
        self.assertIn("failed:", err)

    def test_sigterm_ends_the_run_through_systemexit(self) -> None:
        previous = signal.getsignal(signal.SIGTERM)
        with cli.exits_on_sigterm(), self.assertRaises(SystemExit) as caught:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1)
        self.assertEqual(caught.exception.code, 128 + signal.SIGTERM)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_sigterm_during_a_push_removes_the_worktree(self) -> None:
        marker = self.root / "hook-started"
        hook = self.remote / "hooks" / "pre-receive"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(f"#!/bin/sh\ntouch {marker}\nsleep 3\n")
        hook.chmod(0o755)
        scratch = self.root / "tmp"
        scratch.mkdir()
        env = {**os.environ, "PYTHONPATH": str(SRC), "TMPDIR": str(scratch)}
        command = [sys.executable, "-m", "pin_promote", "-C", str(self.clone), "promote", "--build", self.build]
        command += ["--pin", f"vm.fingerprint={FP_B}", "--passed", "boot"]
        process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(process.communicate)
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists(), "the push never started")
        self.assertEqual(len(run_git(self.clone, "worktree", "list").splitlines()), 2)
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=20), 128 + signal.SIGTERM)
        self.assertEqual(len(run_git(self.clone, "worktree", "list").splitlines()), 1)
        self.assertEqual(list(scratch.iterdir()), [])


class Tags(CliCase):
    """The registry tag follows the pin at the branch's tip, against the fake registry."""

    def setUp(self) -> None:
        super().setUp()
        self.state = new_state()
        self.host = serve(self, self.state)
        self.first = self.add("one")
        self.second = self.add("two")
        self.build = self.configure()

    def add(self, note: str) -> str:
        body = manifest(OCI_MANIFEST, note)
        digest = digest_of(body)
        self.state["manifests"][(REPO, digest)] = (body, OCI_MANIFEST)
        return digest

    def configure(self, **registry: str) -> str:
        raw = copy.deepcopy(CONFIG)
        raw["channels"]["app"] = {  # type: ignore[index]
            "pin": "deploy/app.json",
            "fields": {"digest": "sha256:[0-9a-f]{64}"},
            "sources": ["app/"],
            "checks": ["test"],
            "registry": {"image": f"{self.host}/{REPO}", "tag": TAG, "field": "digest", **registry},
        }
        return self.set_config(raw)

    def promote(self, digest: str, *extra: str) -> tuple[int, str, str]:
        return self.run_cli(
            "promote",
            "--build",
            self.build,
            "--pin",
            f"app.digest={digest}",
            "--passed",
            "test",
            "--plain-http",
            *extra,
        )

    def tagged(self) -> str | None:
        found = self.state["manifests"].get((REPO, TAG))
        return digest_of(found[0]) if found else None

    def test_promote_moves_the_pin_and_then_the_tag(self) -> None:
        code, out, err = self.promote(self.first, "--json")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual((result["promoted"], result["tags_moved"], "tag_error" in result), (["app"], ["app"], False))
        self.assertEqual(self.pin("deploy/app.json"), {"digest": self.first})
        self.assertEqual(self.tagged(), self.first)
        self.assertIn(f"app: moved {self.host}/{REPO}:{TAG} to {self.first}", err)

    def test_an_unchanged_run_still_moves_the_tag(self) -> None:
        self.commit({"deploy/app.json": json.dumps({"digest": self.first}) + "\n"}, "pinned by an earlier run")
        code, out, _ = self.promote(self.first)
        self.assertEqual(code, 0)
        self.assertIn("unchanged: app", out)
        self.assertEqual(self.tagged(), self.first)

    def test_the_tag_command_follows_the_fetched_tip(self) -> None:
        self.commit({"deploy/app.json": json.dumps({"digest": self.second}) + "\n"}, "pinned")
        code, out, err = self.run_cli("tag", "--channel", "app", "--plain-http")
        self.assertEqual((code, self.tagged()), (0, self.second), err)
        self.assertIn(f"app: moved {self.host}/{REPO}:{TAG} to {self.second}", out)
        code, out, _ = self.run_cli("tag", "--channel", "app", "--plain-http")
        self.assertIn("already at", out)

    def test_the_tag_command_reads_the_tip_not_a_stale_remote_tracking_ref(self) -> None:
        run_git(self.clone, "fetch", "-q", "origin", "main")
        old = run_git(self.clone, "rev-parse", "origin/main")
        self.commit({"deploy/app.json": json.dumps({"digest": self.second}) + "\n"}, "pinned")
        run_git(self.clone, "update-ref", "refs/remotes/origin/main", old)  # the local ref is behind the remote
        code, _, err = self.run_cli("tag", "--channel", "app", "--plain-http")
        self.assertEqual((code, self.tagged()), (0, self.second), err)

    def test_the_tag_command_checks_the_channel_against_the_tip_not_the_local_file(self) -> None:
        self.commit({"deploy/app.json": json.dumps({"digest": self.second}) + "\n"}, "pinned")
        (self.clone / CONFIG_PATH).write_text(json.dumps(CONFIG))  # an older local file: no "app" channel at all
        code, _, err = self.run_cli("tag", "--channel", "app", "--plain-http")
        self.assertEqual((code, self.tagged()), (0, self.second), err)

    def test_a_newer_promotion_between_the_read_and_the_put_keeps_the_tag_where_it_is(self) -> None:
        self.commit({"deploy/app.json": json.dumps({"digest": self.first}) + "\n"}, "pinned")
        real = sys.modules["pin_promote.tags"].move_tag

        def then_the_pin_moves(*args: object, **kwargs: object) -> bool:
            self.commit({"deploy/app.json": json.dumps({"digest": self.second}) + "\n"}, "a newer promotion")
            return real(*args, **kwargs)  # type: ignore[no-any-return]

        with mock.patch.object(sys.modules["pin_promote.tags"], "move_tag", then_the_pin_moves):
            code, _, err = self.run_cli("tag", "--channel", "app", "--plain-http")
        self.assertEqual(code, 0, err)
        self.assertIn("a newer promotion holds the pin", err + _)
        self.assertEqual([seen for seen in self.state["seen"] if seen["method"] == "PUT"], [])

    def test_the_tag_command_has_no_ref_option(self) -> None:
        code, _, err = self.run_cli("tag", "--channel", "app", "--ref", "HEAD")
        self.assertEqual(code, 2)
        self.assertIn("--ref", err)
        code, _, err = self.run_cli("tag", "--channel", "vm")
        self.assertEqual((code, "no registry tag is configured at the tip of main" in err), (1, True))

    def test_a_channel_whose_pin_does_not_exist_yet_fails_the_tag_command(self) -> None:
        code, _, err = self.run_cli("tag", "--channel", "app", "--plain-http")
        self.assertEqual(code, 1)
        self.assertIn("holds no digest", err)

    def test_a_newer_promotion_keeps_the_tag_where_it_is(self) -> None:

        def then_another_promotion_lands(*args: object, **kwargs: object) -> object:
            result = real_promote(*args, **kwargs)  # type: ignore[arg-type]
            self.commit({"deploy/app.json": json.dumps({"digest": self.second}) + "\n"}, "a newer promotion")
            return result

        with mock.patch("pin_promote.cli.promote", then_another_promotion_lands):
            code, out, err = self.promote(self.first, "--json")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual((result["promoted"], result["tags_moved"]), (["app"], []))
        self.assertIn(f"app: a newer promotion holds the pin ({self.second})", err)
        self.assertIsNone(self.tagged())
        self.assertEqual([seen for seen in self.state["seen"] if seen["method"] == "PUT"], [])

    def test_a_failing_tag_after_a_pushed_pin(self) -> None:
        missing = "sha256:" + "9" * 64  # the registry does not hold this digest
        code, out, err = self.promote(missing, "--json")
        self.assertEqual(code, 1)
        result = json.loads(out)  # the result is still printed
        self.assertEqual((result["promoted"], result["tags_moved"]), (["app"], []))
        self.assertIn("is not in the registry", result["tag_error"])
        self.assertIn("failed: the pins are on main in " + result["commit"], err)
        self.assertEqual(self.pin("deploy/app.json"), {"digest": missing})
        code, out, err = self.promote(missing)  # without --json: the lines, and the same exit
        self.assertEqual(code, 1)
        self.assertIn("unchanged: app", out)
        self.assertIn("the pins are on main", err)

    def test_running_it_again_after_a_failed_tag_moves_the_tag(self) -> None:
        self.state["auth"] = "basic"  # the registry wants credentials this run has not got
        self.assertEqual(self.promote(self.first)[0], 1)
        self.assertEqual(self.pin("deploy/app.json"), {"digest": self.first})
        self.state["auth"] = "none"
        code, out, _ = self.promote(self.first, "--json")
        self.assertEqual((code, json.loads(out)["promoted"], self.tagged()), (0, [], self.first))

    def test_the_token_host_of_the_channel_is_honoured(self) -> None:
        other = new_state()
        other_host = serve(self, other)
        self.state["auth"] = "bearer"
        self.state["realm"] = f"http://{other_host}/token"
        code, out, err = self.promote(self.first, "--json")  # the config names no token_host
        self.assertEqual(code, 1)
        self.assertIn("token service is on", json.loads(out)["tag_error"])
        self.assertEqual(other["seen"], [])
        self.build = self.configure(token_host=other_host)
        code, out, err = self.promote(self.first, "--json")
        self.assertEqual((code, json.loads(out)["tags_moved"], self.tagged()), (0, ["app"], self.first), err)
