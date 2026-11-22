"""The config format: what parses, and every problem named at once."""

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pin_promote import ConfigError, load, load_local, parse, parse_local, source_classes

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
            source_classes(config, config["channels"]["vm"]),
            {"own": ["images/", ":!images/host/"], "shared": ["lock.json"], "pin": "images/vm/pin.json"},
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

    def test_a_pathspec_that_selects_everything_is_refused(self) -> None:
        for spec in (".", "./", ":!.", ":^.", ":!", ":^/", "*", "**"):
            with self.subTest(spec=spec):
                self.assertIn(".sources", self.problems(changed(sources=["images/", spec])))
                self.assertIn("shared_sources", self.problems({**CONFIG, "shared_sources": [spec]}))
        self.assertIn(".sources", self.problems(changed(sources=[""])))

    def test_globs_are_allowed_in_sources_and_shared_sources(self) -> None:
        config = parse({**changed(sources=["images/*.sh", "img[12]/", "a?c"]), "shared_sources": ["*.lock", "x/**"]})
        self.assertEqual(config["channels"]["vm"]["sources"], ["images/*.sh", "img[12]/", "a?c"])
        self.assertEqual(config["shared_sources"], ["*.lock", "x/**"])

    def test_shared_sources_have_no_exclusions(self) -> None:
        text = self.problems({**CONFIG, "shared_sources": ["lock.json", ":!lock.json"]})
        self.assertIn("shared_sources", text)
        self.assertIn("without ':!' exclusions", text)
        self.assertIn("shared_sources", self.problems({**CONFIG, "shared_sources": [":^docs/"]}))

    def test_remote_and_branch_start_with_a_letter_or_a_digit(self) -> None:
        for key, bad in (("remote", "-upload-pack=x"), ("remote", ".hidden"), ("branch", "-d"), ("branch", "/main")):
            with self.subTest(key=key, bad=bad):
                self.assertIn(f"{key}:", self.problems({**CONFIG, key: bad}))
        self.assertEqual(parse({**CONFIG, "branch": "release/1.0", "remote": "up_stream.2"})["branch"], "release/1.0")

    def test_no_git_directory_in_a_pin_or_a_journal(self) -> None:
        for pin in (".git/pin.json", "a/.git/pin.json", "a/.GIT/pin.json"):
            self.assertIn(".pin", self.problems(changed(pin=pin)))
        for journal in (".git/hooks/log", "a/.git"):
            self.assertIn("journal", self.problems({**CONFIG, "journal": [journal]}))
        parse({**CONFIG, "journal": [".github/CHANGELOG.md", "a/.gitkeep"]})

    def test_a_pin_or_a_journal_may_not_be_the_config_file(self) -> None:
        self.assertIn("config file itself", self.problems_at("pin-promote.json", changed(pin="pin-promote.json")))
        self.assertIn("config file itself", self.problems_at("ci/pins.json", {**CONFIG, "journal": ["ci/pins.json"]}))
        parse(changed(pin="pin-promote.json"))  # no config path given: nothing to compare with
        parse(changed(pin="pin-promote.json"), "other.json")

    def problems_at(self, path: str, raw: object) -> str:
        with self.assertRaises(ConfigError) as caught:
            parse(raw, path)
        return str(caught.exception)

    def test_a_journal_inside_the_sources_is_refused(self) -> None:
        def journal(*paths: str, **channel: object) -> dict[str, object]:
            return {**changed(**channel), "journal": list(paths)}

        self.assertIn("sources of channel vm", self.problems(journal("images/vm/NOTES.md")))
        self.assertIn("sources of channel host", self.problems(journal("images/host/NOTES.md")))
        self.assertIn("inside shared_sources", self.problems({**CONFIG, "journal": ["lock.json"]}))
        self.assertIn(
            "inside shared_sources", self.problems({**CONFIG, "shared_sources": ["*.lock"], "journal": ["a/b.lock"]})
        )
        self.assertIn("sources of channel vm", self.problems(journal("images/log.md", sources=["im*/"])))
        self.assertIn("sources of channel vm", self.problems(journal("images/vm", sources=["images/vm/"])))
        # Outside, or excluded from the channel's own sources: fine.
        parse(journal("CHANGELOG.md", "docs/log.md"))
        only_vm = {**CONFIG, "journal": ["images/notes/LOG.md"], "channels": {"vm": CONFIG["channels"]["vm"]}}  # type: ignore[index]
        parse({**only_vm, "channels": {"vm": {**only_vm["channels"]["vm"], "sources": ["images/", ":!images/notes/"]}}})  # type: ignore[index]

    def test_registry_image_parts_are_not_dots(self) -> None:
        fields = {"digest": "sha256:[0-9a-f]{64}"}
        for image in ("registry.example.com/../app", "registry.example.com/./app", "../team/app", "./team/app"):
            registry = {"image": image, "tag": "stable", "field": "digest"}
            with self.subTest(image=image):
                self.assertIn("registry.image", self.problems(changed(fields=fields, registry=registry)))
        ok = {"image": "[::1]:5000/team/app", "tag": "stable", "field": "digest"}
        parse(changed(fields=fields, registry=ok))

    def test_registry_token_host(self) -> None:
        fields = {"digest": "sha256:[0-9a-f]{64}"}
        good = {"image": "registry.example.com/team/app", "tag": "stable", "field": "digest"}
        for host in ("auth.example.com", "token.example.com:8443", "localhost"):
            config = parse(changed(fields=fields, registry={**good, "token_host": host}))
            self.assertEqual(config["channels"]["vm"]["registry"]["token_host"], host)
        for bad in ("Auth.Example.com", "a b", "", "x/y", "https://auth.example.com", "u@auth.example.com", 5, "a:b"):
            with self.subTest(host=bad):
                text = self.problems(changed(fields=fields, registry={**good, "token_host": bad}))
                self.assertIn("registry.token_host", text)
        self.assertNotIn("token_host", parse(changed(fields=fields, registry=good))["channels"]["vm"]["registry"])

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

    def test_a_registry_field_must_accept_only_full_digests(self) -> None:
        good = {"image": "registry.example.com/team/app", "tag": "stable", "field": "digest"}
        for pattern in (
            "sha256:[0-9a-f]+",
            "sha256:[0-9a-f]{64,}",
            "sha256:[0-9a-f]{32,64}",
            "sha256:[0-9a-f]{63,64}",
            "sha256:[0-9a-f]{64}.*",
            "[0-9a-f]{64}",
            "(sha256|sha512):[0-9a-f]{64}",
            "sha256:[0-9a-fA-F]{64}",
            "sha256:[0-9a-f]{64} ?",
        ):
            with self.subTest(pattern=pattern):
                text = self.problems(changed(fields={"digest": pattern}, registry=good))
                self.assertIn("only sha256:<64 hex>", text)
        parse(changed(fields={"digest": "sha256:[0-9a-f]{64}"}, registry=good))

    def test_a_pin_inside_another_channels_sources_or_the_shared_sources_is_refused(self) -> None:
        def host(*sources: str, **top: object) -> dict[str, object]:
            raw = {**CONFIG, **top}
            raw["channels"] = {
                "vm": CONFIG["channels"]["vm"],  # type: ignore[index]
                "host": {**CONFIG["channels"]["host"], "sources": list(sources)},  # type: ignore[index]
            }
            return raw

        text = self.problems(host("images/", "install/"))
        self.assertIn("channels.vm.pin: images/vm/pin.json is inside the sources of channel host", text)
        self.assertNotIn("channels.host.pin", text)  # its own pin is the pin class's business, not this check's
        self.assertIn("channels.vm.pin", self.problems(host("images/v*/", "install/")))
        self.assertIn(
            "channels.vm.pin: images/vm/pin.json is inside shared_sources",
            self.problems(host("install/", shared_sources=["images/vm/"])),
        )
        self.assertIn(
            "channels.host.pin: images/host/pin.json is inside shared_sources",
            self.problems(host("install/", shared_sources=["images/"])),
        )
        # An exclusion in the other channel's sources hides the pin: fine.
        parse(host("images/", ":!images/*/pin.json", "install/"))
        parse(host("images/", ":!images/vm/pin.json", "install/"))
        parse(host("install/"))

    def test_a_malformed_glob_is_refused(self) -> None:
        for bad in ("app/[", "app/[a", "app/[]", "app/[!", "app/[[:alpha:]", "im[a-"):
            with self.subTest(glob=bad):
                self.assertIn("sources", self.problems(changed(sources=[bad])))
                self.assertIn("sources", self.problems(changed(sources=["images/", ":!" + bad])))
                self.assertIn("shared_sources", self.problems({**CONFIG, "shared_sources": [bad]}))
        for good in ("app/[ab]*", "app/[]x]", "app/[!a]", "app/[[:alpha:]]x", "app/*.json", "a?/b"):
            with self.subTest(glob=good):
                parse(changed(sources=[good]))

    def test_the_local_file_is_read_leniently(self) -> None:
        self.assertEqual(
            parse_local({"schema": 99, "channels": "nonsense", "branch": "release/1", "attempts": 5}),
            {"branch": "release/1", "remote": "origin", "attempts": 5},
        )
        self.assertEqual(parse_local({}), {"branch": "main", "remote": "origin", "attempts": 3})
        with self.assertRaisesRegex(ConfigError, "branch"):
            parse_local({"branch": "-d"})
        with self.assertRaisesRegex(ConfigError, "attempts"):
            parse_local({"attempts": 0})
        with self.assertRaisesRegex(ConfigError, "JSON object"):
            parse_local([])
        with TemporaryDirectory() as held:
            path = Path(held) / "pin-promote.json"
            path.write_text(json.dumps({"schema": 1, "channels": {}, "remote": "up"}))
            self.assertEqual(load_local(path)["remote"], "up")
            path.write_text("{")
            with self.assertRaisesRegex(ConfigError, str(path)):
                load_local(path)

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
            path.write_text(json.dumps({**CONFIG, "journal": ["ci/pins.json"]}))
            with self.assertRaisesRegex(ConfigError, "config file itself"):
                load(path, "ci/pins.json")
            self.assertEqual(load(path, "elsewhere.json")["journal"], ["ci/pins.json"])
            path.write_text("{")
            with self.assertRaisesRegex(ConfigError, str(path)):
                load(path)
