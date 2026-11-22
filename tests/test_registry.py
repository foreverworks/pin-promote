"""The registry tag move, against the fake registry in support.py."""

import os
import time
import unittest
import urllib.parse
from unittest import mock

from pin_promote import registry
from pin_promote.registry import RegistryError, move_tag

from .support import (
    DOCKER_LIST,
    DOCKER_MANIFEST,
    OCI_INDEX,
    OCI_MANIFEST,
    PASSWORD,
    REPO,
    TAG,
    TOKEN,
    USER,
    Seen,
    State,
    basic,
    digest_of,
    manifest,
    new_state,
    serve,
)


class RegistryTestCase(unittest.TestCase):
    def serve(self, state: State) -> str:
        return serve(self, state)

    def setUp(self) -> None:
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PIN_PROMOTE_REGISTRY_USERNAME", None)
        os.environ.pop("PIN_PROMOTE_REGISTRY_PASSWORD", None)
        self.state = new_state()
        self.host = self.serve(self.state)
        self.image = f"{self.host}/{REPO}"

    def token(self, challenge: str, scheme: str = "http", token_host: str | None = None) -> str | None:
        return registry._token(
            challenge, f"repository:{REPO}:pull,push", scheme, self.host, token_host, time.monotonic() + 30
        )

    def set_credentials(self) -> None:
        os.environ["PIN_PROMOTE_REGISTRY_USERNAME"] = USER
        os.environ["PIN_PROMOTE_REGISTRY_PASSWORD"] = PASSWORD

    def add(self, body: bytes, media: str) -> str:
        digest = digest_of(body)
        self.state["manifests"][(REPO, digest)] = (body, media)
        return digest

    def puts(self) -> list[Seen]:
        return [seen for seen in self.state["seen"] if seen["method"] == "PUT"]

    def move(self, digest: str, tag: str = TAG) -> bool:
        return move_tag(self.image, tag, digest, plain_http=True)


class MoveTests(RegistryTestCase):
    def test_moves_a_tag_with_the_exact_bytes_and_media_type(self) -> None:
        body = manifest(OCI_MANIFEST, "exact bytes é  with   odd spacing")
        digest = self.add(body, OCI_MANIFEST)
        self.assertTrue(self.move(digest))
        (put,) = self.puts()
        self.assertEqual(put["path"], f"/v2/{REPO}/manifests/{TAG}")
        self.assertEqual(put["body"], body)
        self.assertEqual(put["content_type"], OCI_MANIFEST)
        self.assertEqual(self.state["manifests"][(REPO, TAG)], (body, OCI_MANIFEST))

    def test_a_tag_already_on_the_digest_is_left_alone(self) -> None:
        digest = self.add(manifest(OCI_MANIFEST), OCI_MANIFEST)
        self.assertTrue(self.move(digest))
        self.assertFalse(self.move(digest))
        self.assertEqual(len(self.puts()), 1)

    def test_a_tag_on_another_digest_is_moved(self) -> None:
        old = self.add(manifest(OCI_MANIFEST, "old"), OCI_MANIFEST)
        new = self.add(manifest(OCI_MANIFEST, "new"), OCI_MANIFEST)
        self.assertTrue(self.move(old))
        self.assertTrue(self.move(new))
        self.assertFalse(self.move(new))
        self.assertEqual(digest_of(self.state["manifests"][(REPO, TAG)][0]), new)
        self.assertEqual(len(self.puts()), 2)

    def test_moves_an_oci_index(self) -> None:
        body = manifest(OCI_INDEX)
        digest = self.add(body, OCI_INDEX)
        self.assertTrue(self.move(digest))
        self.assertEqual(self.puts()[0]["content_type"], OCI_INDEX)
        self.assertEqual(self.state["manifests"][(REPO, TAG)], (body, OCI_INDEX))

    def test_moves_a_docker_manifest_list(self) -> None:
        body = manifest(DOCKER_LIST)
        digest = self.add(body, DOCKER_LIST)
        self.assertTrue(self.move(digest))
        self.assertEqual(self.puts()[0]["content_type"], DOCKER_LIST)
        self.assertEqual(self.state["manifests"][(REPO, TAG)], (body, DOCKER_LIST))

    def test_moves_a_docker_v2_manifest(self) -> None:
        digest = self.add(manifest(DOCKER_MANIFEST), DOCKER_MANIFEST)
        self.assertTrue(self.move(digest))
        self.assertEqual(self.puts()[0]["content_type"], DOCKER_MANIFEST)

    def test_takes_the_media_type_from_the_body_when_the_header_is_generic(self) -> None:
        body = manifest(OCI_INDEX)
        digest = self.add(body, "application/octet-stream")
        self.assertTrue(self.move(digest))
        self.assertEqual(self.puts()[0]["content_type"], OCI_INDEX)

    def test_refuses_what_is_not_a_manifest(self) -> None:
        digest = self.add(b"just some bytes", "application/octet-stream")
        with self.assertRaisesRegex(RegistryError, "not a manifest"):
            self.move(digest)
        self.assertEqual(self.puts(), [])

    def test_the_manifest_is_read_by_digest_with_the_accepted_types(self) -> None:
        digest = self.add(manifest(OCI_MANIFEST), OCI_MANIFEST)
        self.move(digest)
        self.assertEqual(self.state["seen"][0]["path"], f"/v2/{REPO}/manifests/{digest}")
        self.assertEqual([seen["method"] for seen in self.state["seen"]], ["GET", "HEAD", "PUT"])


class AuthTests(RegistryTestCase):
    def test_bearer_token_from_the_challenge(self) -> None:
        self.state["auth"] = "bearer"
        self.set_credentials()
        digest = self.add(manifest(OCI_MANIFEST), OCI_MANIFEST)
        self.assertTrue(self.move(digest))
        tokens = [seen for seen in self.state["seen"] if seen["path"].startswith("/token")]
        (call,) = tokens
        self.assertEqual(call["authorization"], basic(USER, PASSWORD))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(call["path"]).query)
        self.assertEqual(query, {"service": ["x"], "scope": [f"repository:{REPO}:pull,push"]})
        calls = [seen for seen in self.state["seen"] if not seen["path"].startswith("/token")]
        self.assertIsNone(calls[0]["authorization"])  # the unauthenticated first try that drew the 401
        self.assertEqual([call["method"] for call in calls], ["GET", "GET", "HEAD", "PUT"])
        for later in calls[1:]:
            self.assertEqual(later["authorization"], f"Bearer {TOKEN}")

    def test_bearer_token_without_credentials_is_asked_anonymously(self) -> None:
        self.state["auth"] = "bearer"
        digest = self.add(manifest(OCI_MANIFEST), OCI_MANIFEST)
        self.assertTrue(self.move(digest))
        (call,) = [seen for seen in self.state["seen"] if seen["path"].startswith("/token")]
        self.assertIsNone(call["authorization"])

    def test_basic_challenge_uses_the_credentials_directly(self) -> None:
        self.state["auth"] = "basic"
        self.set_credentials()
        digest = self.add(manifest(OCI_MANIFEST), OCI_MANIFEST)
        self.assertTrue(self.move(digest))
        self.assertEqual([seen for seen in self.state["seen"] if seen["path"].startswith("/token")], [])
        calls = self.state["seen"]
        self.assertIsNone(calls[0]["authorization"])
        for later in calls[1:]:
            self.assertEqual(later["authorization"], basic(USER, PASSWORD))

    def test_basic_challenge_without_credentials_fails(self) -> None:
        self.state["auth"] = "basic"
        digest = self.add(manifest(OCI_MANIFEST), OCI_MANIFEST)
        with self.assertRaisesRegex(RegistryError, "wants credentials"):
            self.move(digest)
        self.assertEqual(self.puts(), [])

    def test_bearer_without_credentials_fails_when_the_token_service_wants_them(self) -> None:
        self.state["auth"] = "bearer"
        self.state["token_needs_credentials"] = True
        digest = self.add(manifest(OCI_MANIFEST), OCI_MANIFEST)
        with self.assertRaisesRegex(RegistryError, "token service refused: HTTP 401"):
            self.move(digest)
        self.assertEqual(self.puts(), [])

    def test_wrong_credentials_are_refused_after_one_attempt(self) -> None:
        self.state["auth"] = "basic"
        os.environ["PIN_PROMOTE_REGISTRY_USERNAME"] = USER
        os.environ["PIN_PROMOTE_REGISTRY_PASSWORD"] = "wrong"
        digest = self.add(manifest(OCI_MANIFEST), OCI_MANIFEST)
        with self.assertRaises(RegistryError):
            self.move(digest)
        self.assertEqual(len(self.state["seen"]), 2)  # the first try and the one retry, no loop
        self.assertEqual(self.puts(), [])

    def test_an_unknown_challenge_scheme_fails(self) -> None:
        self.assertIsNone(self.token('Negotiate realm="x"'))


class TokenRealmTests(RegistryTestCase):
    def test_an_https_realm_in_plain_http_mode_is_refused_before_credentials_are_sent(self) -> None:
        self.state["auth"] = "bearer"
        self.state["realm"] = f"https://{self.host}/token"
        self.set_credentials()
        digest = self.add(manifest(OCI_MANIFEST), OCI_MANIFEST)
        with self.assertRaisesRegex(RegistryError, "token service is not http"):
            self.move(digest)
        self.assertEqual([seen for seen in self.state["seen"] if seen["authorization"]], [])
        self.assertEqual(self.puts(), [])

    def test_an_http_realm_in_https_mode_is_refused_before_credentials_are_sent(self) -> None:
        # The fake cannot speak https, so the challenge is handed to the token step as an https registry's would be.
        token_state = new_state()
        token_host = self.serve(token_state)
        self.set_credentials()
        with self.assertRaisesRegex(RegistryError, "token service is not https"):
            self.token(f'Bearer realm="http://{token_host}/token",service="x"', "https")
        self.assertEqual(token_state["seen"], [])

    def test_a_realm_without_a_host_is_refused(self) -> None:
        with self.assertRaisesRegex(RegistryError, "token service is not http"):
            self.token('Bearer realm="http:///token"')
        with self.assertRaisesRegex(RegistryError, "token service is not http"):
            self.token("Bearer service=x")

    def test_the_same_scheme_on_another_host_is_refused_before_credentials_are_sent(self) -> None:
        other = new_state()
        other_host = self.serve(other)
        self.state["auth"] = "bearer"
        self.state["realm"] = f"http://{other_host}/token"
        self.set_credentials()
        digest = self.add(manifest(OCI_MANIFEST), OCI_MANIFEST)
        with self.assertRaisesRegex(RegistryError, f"token service is on '{other_host}', not on '{self.host}'"):
            self.move(digest)
        self.assertEqual(other["seen"], [])  # not even a request, so no credential went there
        self.assertEqual([seen for seen in self.state["seen"] if seen["authorization"]], [])
        self.assertEqual(self.puts(), [])

    def test_a_userinfo_trick_does_not_pass_for_the_registry_host(self) -> None:
        with self.assertRaisesRegex(RegistryError, "token service is on"):
            self.token(f'Bearer realm="http://{self.host}@evil.example.com/token"')

    def test_the_channels_token_host_allows_another_host(self) -> None:
        other = new_state()
        other_host = self.serve(other)
        self.state["auth"] = "bearer"
        self.state["realm"] = f"http://{other_host}/token"
        self.set_credentials()
        digest = self.add(manifest(OCI_MANIFEST), OCI_MANIFEST)
        self.assertTrue(move_tag(self.image, TAG, digest, plain_http=True, token_host=other_host))
        (call,) = [seen for seen in other["seen"] if seen["path"].startswith("/token")]
        self.assertEqual(call["authorization"], basic(USER, PASSWORD))
        self.assertEqual(len(self.puts()), 1)

    def test_token_host_names_one_host_only(self) -> None:
        other = new_state()
        other_host = self.serve(other)
        self.state["auth"] = "bearer"
        self.state["realm"] = f"http://{other_host}/token"
        digest = self.add(manifest(OCI_MANIFEST), OCI_MANIFEST)
        with self.assertRaisesRegex(RegistryError, "token service is on"):
            move_tag(self.image, TAG, digest, plain_http=True, token_host="registry.example.com")
        self.assertEqual(other["seen"], [])


class SchemeAndDeadlineTests(RegistryTestCase):
    def test_plain_http_is_for_this_machine_only(self) -> None:
        digest = digest_of(b"x")
        for image in ("registry.example.com/team/app", "192.0.2.5:5000/app", "localhost.example.com/app"):
            with self.subTest(image=image), self.assertRaisesRegex(RegistryError, "plain http is only for"):
                move_tag(image, TAG, digest, plain_http=True)
        self.assertEqual(self.state["seen"], [])

    def test_the_loopback_names(self) -> None:
        self.assertEqual(
            [registry._host_name(host) for host in ("127.0.0.1:5000", "localhost", "[::1]:5000", "[::1]")],
            ["127.0.0.1", "localhost", "::1", "::1"],
        )
        self.assertTrue(all(name in registry.LOOPBACK for name in ("127.0.0.1", "localhost", "::1")))

    def test_an_expired_deadline_stops_before_any_request(self) -> None:
        digest = self.add(manifest(OCI_MANIFEST), OCI_MANIFEST)
        with mock.patch.object(registry, "DEADLINE", 0.0), self.assertRaisesRegex(RegistryError, "took longer"):
            self.move(digest)
        self.assertEqual(self.state["seen"], [])

    def test_a_deadline_bounds_each_requests_timeout(self) -> None:
        self.assertLessEqual(registry._timeout(time.monotonic() + 2), 2)
        self.assertEqual(registry._timeout(time.monotonic() + 1000), registry.TIMEOUT)
        with self.assertRaisesRegex(RegistryError, "took longer"):
            registry._timeout(time.monotonic() - 1)

    def test_a_manifest_that_is_not_json_is_a_registry_error(self) -> None:
        for body in (b"{not json", b'{"mediaType": 5}', b"{}", b'{"a": '):
            digest = self.add(body, "application/octet-stream")
            with self.subTest(body=body), self.assertRaises(RegistryError):
                self.move(digest)
        self.assertEqual(self.puts(), [])
        digest = self.add(b"{not json", "application/octet-stream")
        with self.assertRaisesRegex(RegistryError, "not JSON"):
            self.move(digest)


class RefusalTests(RegistryTestCase):
    def test_bytes_that_do_not_match_the_digest(self) -> None:
        claimed = digest_of(manifest(OCI_MANIFEST, "what was pinned"))
        self.state["manifests"][(REPO, claimed)] = (manifest(OCI_MANIFEST, "something else"), OCI_MANIFEST)
        with self.assertRaisesRegex(RegistryError, "another digest"):
            self.move(claimed)
        self.assertEqual(self.puts(), [])
        self.assertNotIn((REPO, TAG), self.state["manifests"])

    def test_digest_not_found(self) -> None:
        with self.assertRaisesRegex(RegistryError, "is not in the registry"):
            self.move(digest_of(b"absent"))
        self.assertEqual(self.puts(), [])

    def test_put_answering_another_digest(self) -> None:
        self.state["put_digest"] = "sha256:" + "0" * 64
        digest = self.add(manifest(OCI_MANIFEST), OCI_MANIFEST)
        with self.assertRaisesRegex(RegistryError, "the registry stored"):
            self.move(digest)

    def test_redirect_is_not_followed_and_the_target_never_sees_credentials(self) -> None:
        target = new_state()
        target_host = self.serve(target)
        digest = self.add(manifest(OCI_MANIFEST), OCI_MANIFEST)
        self.state["auth"] = "basic"
        self.state["redirect"] = f"http://{target_host}/v2/{REPO}/manifests/{digest}"
        self.set_credentials()
        with self.assertRaisesRegex(RegistryError, "HTTP 307"):
            self.move(digest)
        self.assertEqual(target["seen"], [])
        authenticated = [seen for seen in self.state["seen"] if seen["authorization"]]
        self.assertEqual(len(authenticated), 1)  # the credentials did go to the registry itself
        self.assertEqual(self.puts(), [])

    def test_an_answer_larger_than_the_limit(self) -> None:
        digest = self.add(manifest(OCI_MANIFEST, "x" * 300), OCI_MANIFEST)
        with (
            mock.patch.object(registry, "MAX_MANIFEST", 100),
            self.assertRaisesRegex(RegistryError, "larger than 100 bytes"),
        ):
            self.move(digest)
        self.assertEqual(self.puts(), [])

    def test_an_answer_at_the_limit_is_accepted(self) -> None:
        body = manifest(OCI_MANIFEST, "y" * 50)
        digest = self.add(body, OCI_MANIFEST)
        with mock.patch.object(registry, "MAX_MANIFEST", len(body)):
            self.assertTrue(self.move(digest))

    def test_an_unreachable_registry(self) -> None:
        digest = digest_of(b"x")
        with self.assertRaises(RegistryError):
            move_tag("127.0.0.1:1/team/app", TAG, digest, plain_http=True)


class InputTests(RegistryTestCase):
    def assert_no_request(self) -> None:
        self.assertEqual(self.state["seen"], [])

    def test_bad_digests(self) -> None:
        good = "a" * 64
        for digest in (
            "",
            "sha256:",
            "sha256:abc",
            "sha256:" + "A" * 64,
            "sha256:" + good + "0",
            "sha512:" + good,
            good,
            f"sha256:{good}\n",
            "sha256:" + "g" * 64,
        ):
            with self.subTest(digest=digest), self.assertRaisesRegex(RegistryError, "not a sha256 digest"):
                self.move(digest)
        self.assert_no_request()

    def test_bad_tags(self) -> None:
        digest = "sha256:" + "a" * 64
        for tag in ("", "-lead", ".lead", "has space", "with/slash", "a" * 129, "colon:tag", "tag\n"):
            with self.subTest(tag=tag), self.assertRaisesRegex(RegistryError, "not a tag"):
                self.move(digest, tag)
        self.assert_no_request()

    def test_bad_image_names(self) -> None:
        digest = "sha256:" + "a" * 64
        for image in (
            "",
            "noslash",
            "host/",
            "UPPER.example.com/app",
            "https://host/app",
            "host/a b",
            f"{self.host}/{REPO}:tag",
        ):
            with self.subTest(image=image), self.assertRaisesRegex(RegistryError, "not a registry image name"):
                move_tag(image, TAG, digest, plain_http=True)
        self.assert_no_request()

    def test_split_image(self) -> None:
        self.assertEqual(registry.split_image("registry.example.com/team/app"), ("registry.example.com", "team/app"))
        self.assertEqual(registry.split_image("127.0.0.1:5000/app"), ("127.0.0.1:5000", "app"))


if __name__ == "__main__":
    unittest.main()
