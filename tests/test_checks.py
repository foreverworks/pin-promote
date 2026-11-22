"""The evidence a promotion reads."""

import json
import unittest

from pin_promote import EvidenceError, evidence, from_needs, missing


class Checks(unittest.TestCase):
    def test_github_needs(self) -> None:
        needs = json.dumps({"boot": {"result": "success", "outputs": {}}, "smoke": {"result": "failure"}})
        results = from_needs(needs)
        self.assertEqual(results, {"boot": "success", "smoke": "failure"})
        self.assertEqual(missing(["boot", "smoke", "lint"], results), ["smoke (failure)", "lint (not run)"])

    def test_by_hand_overrides_nothing_but_adds(self) -> None:
        self.assertEqual(evidence(["lint"], {"boot": "success"}), {"boot": "success", "lint": "success"})

    def test_a_name_passed_by_hand_cannot_overrule_a_job_result(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "smoke: the job results say failure; --passed cannot override it"):
            evidence(["smoke"], {"smoke": "failure"})
        for result in ("skipped", "cancelled"):
            with self.assertRaisesRegex(EvidenceError, f"the job results say {result}"):
                evidence(["boot", "smoke"], {"smoke": result})
        self.assertEqual(evidence(["smoke"], {"smoke": "success"}), {"smoke": "success"})  # agreeing is fine
        self.assertEqual(evidence(["smoke"], {"boot": "failure"}), {"boot": "failure", "smoke": "success"})

    def test_unreadable(self) -> None:
        for text in ("[", "[]", '{"boot": {}}', '{"boot": "success"}'):
            with self.assertRaises(EvidenceError):
                from_needs(text)
