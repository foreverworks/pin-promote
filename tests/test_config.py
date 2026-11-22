"""The config format: what parses, and every problem named at once."""

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pin_promote import ConfigError, load, parse, sources

from .support import CONFIG


def changed(**channel: object) -> dict[str, object]:
    raw = copy.deepcopy(CONFIG)
    vm = raw["channels"]["vm"]  # type: ignore[index]
    vm.update(channel)
    return raw


class Parse(unittest.TestCase):
    def test_defaults(self) -> None:
        config = parse(CONFIG)
        self.assertEqual((config["branch"], config["remote"], config["attempts"]), ("main", "origin", 3))
        self.assertEqual(config["channels"]["host"]["subject"], "host: pin the verified build")
        self.assertEqual(
            sources(config, config["channels"]["vm"]), ["images/", ":!images/host/", "lock.json", "images/vm/pin.json"]
        )

    def problems(self, raw: object) -> str:
        with self.assertRaises(ConfigError) as caught:
            parse(raw)
        return str(caught.exception)

    def test_every_problem_at_once(self) -> None:
        text = self.problems(changed(pin="/abs.json", checks=[], fields={}, surprise=1))
        for expected in ("channels.vm.pin", "channels.vm.checks", "channels.vm.fields", "channels.vm.surprise"):
            self.assertIn(expected, text)

    def test_a_pin_never_moves_unchecked(self) -> None:
        self.assertIn("never moves unchecked", self.problems(changed(checks=[])))
        self.assertIn("listed twice", self.problems(changed(checks=["boot", "boot"])))

    def test_paths(self) -> None:
        self.assertIn(".pin", self.problems(changed(pin="../out.json")))
        self.assertIn(".pin", self.problems(changed(pin="images/vm/pin.txt")))
        self.assertIn(".sources", self.problems(changed(sources=[":(glob)**"])))
        self.assertIn(".sources", self.problems(changed(sources=["../x"])))
        self.assertIn("exclusions alone", self.problems(changed(sources=[":!docs/"])))
        self.assertIn("two channels write", self.problems(changed(pin="images/host/pin.json")))
        raw = copy.deepcopy(CONFIG)
        raw["journal"] = ["images/vm/pin.json"]
        self.assertIn("is a channel's pin file", self.problems(raw))

    def test_fields(self) -> None:
        self.assertIn("not a regular expression", self.problems(changed(fields={"fingerprint": "["})))
        self.assertIn("accepts an empty value", self.problems(changed(fields={"fingerprint": "[0-9a-f]*"})))
        self.assertIn("field name", self.problems(changed(fields={"finger print": "x+"})))

    def test_registry(self) -> None:
        good = {"image": "registry.example.com/team/app", "tag": "stable", "field": "digest"}
        config = parse(changed(fields={"digest": "sha256:[0-9a-f]{64}"}, registry=good))
        self.assertEqual(config["channels"]["vm"]["registry"], good)
        loose = changed(fields={"digest": "sha256:.+"}, registry=good)
        self.assertIn("only sha256:<64 hex>", self.problems(loose))
        self.assertIn("registry.field", self.problems(changed(registry={**good, "field": "other"})))
        self.assertIn(
            "registry.image",
            self.problems(changed(fields={"digest": "sha256:[0-9a-f]{64}"}, registry={**good, "image": "app:latest"})),
        )

    def test_top_level(self) -> None:
        self.assertIn("schema", self.problems({**CONFIG, "schema": 2}))
        self.assertIn("attempts", self.problems({**CONFIG, "attempts": 0}))
        self.assertIn("attempts", self.problems({**CONFIG, "attempts": True}))
        self.assertIn("branch", self.problems({**CONFIG, "branch": "a..b"}))
        self.assertIn("channels", self.problems({**CONFIG, "channels": {}}))
        self.assertIn("JSON object", self.problems([]))

    def test_load_names_the_file(self) -> None:
        with TemporaryDirectory() as held:
            path = Path(held) / "pin-promote.json"
            path.write_text(json.dumps({**CONFIG, "schema": 9}))
            with self.assertRaisesRegex(ConfigError, f"{path}: schema"):
                load(path)
            path.write_text("{")
            with self.assertRaisesRegex(ConfigError, str(path)):
                load(path)
