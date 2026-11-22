"""The command line, end to end in a fixture repository."""

import contextlib
import io
import json

from pin_promote import cli

from .support import FP_B, Repository


class Cli(Repository):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["-C", str(self.clone), *args])
            except SystemExit as exit_:
                code = int(exit_.code or 0)
        return code, out.getvalue(), err.getvalue()

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
