"""The registry tag move, against a fake registry on 127.0.0.1.

The fake speaks just enough of the distribution API (GET, HEAD and PUT of manifests, a challenge on
401 and a token endpoint). Its whole state is one TypedDict, so a test reads what the registry holds
and what it saw without reaching into a server object.
"""

import base64
import hashlib
import json
import os
import re
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TypedDict
from unittest import mock

from pin_promote import registry
from pin_promote.registry import RegistryError, move_tag

REPO = "team/app"
TAG = "stable"
USER = "someone"
PASSWORD = "s3cret"
TOKEN = "tok-1"
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
MANIFEST_PATH = re.compile(r"/v2/(.+)/manifests/([^/]+)")


class Seen(TypedDict):
    method: str
    path: str
    authorization: str | None
    content_type: str | None
    body: bytes


class State(TypedDict):
    manifests: dict[tuple[str, str], tuple[bytes, str]]  # (repository, tag or digest) -> (bytes, media type)
    seen: list[Seen]
    auth: str  # "none", "basic" or "bearer": what the manifest endpoints demand
    realm: str  # the token realm to challenge with; "" means this server's own /token
    token_needs_credentials: bool
    put_digest: str | None  # what a PUT answers as Docker-Content-Digest, instead of the true one
    redirect: str | None  # answer a manifest GET with a 307 to this URL


def digest_of(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def manifest(media: str, extra: str = "") -> bytes:
    return json.dumps({"schemaVersion": 2, "mediaType": media, "note": extra}).encode()


def new_state(auth: str = "none") -> State:
    return State(
        manifests={},
        seen=[],
        auth=auth,
        realm="",
        token_needs_credentials=False,
        put_digest=None,
        redirect=None,
    )


def make_handler(state: State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):  # the one class http.server cannot do without
        def log_message(self, format: str, *args: object) -> None:
            return None

        def _send(self, status: int, headers: dict[str, str], body: bytes = b"") -> None:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _challenge(self) -> None:
            if state["auth"] == "basic":
                challenge = 'Basic realm="fake"'
            else:
                realm = state["realm"] or f"http://{self.headers.get('Host')}/token"
                challenge = f'Bearer realm="{realm}",service="x",scope="repository:{REPO}:pull"'
            self._send(401, {"WWW-Authenticate": challenge})

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            authorization = self.headers.get("Authorization")
            state["seen"].append(
                Seen(
                    method=self.command,
                    path=self.path,
                    authorization=authorization,
                    content_type=self.headers.get("Content-Type"),
                    body=body,
                )
            )
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/token":
                if state["token_needs_credentials"] and authorization != basic(USER, PASSWORD):
                    return self._send(401, {})
                return self._send(200, {"Content-Type": "application/json"}, json.dumps({"token": TOKEN}).encode())
            found = MANIFEST_PATH.fullmatch(parsed.path)
            if not found:
                return self._send(404, {})
            wanted = {"none": None, "basic": basic(USER, PASSWORD), "bearer": f"Bearer {TOKEN}"}[state["auth"]]
            if wanted is not None and authorization != wanted:
                return self._challenge()
            key = (found.group(1), found.group(2))
            if self.command == "PUT":
                media = self.headers.get("Content-Type", "")
                state["manifests"][key] = (body, media)
                answered = state["put_digest"] or digest_of(body)
                return self._send(201, {"Docker-Content-Digest": answered})
            if self.command == "GET" and state["redirect"]:
                return self._send(307, {"Location": state["redirect"]})
            if key not in state["manifests"]:
                return self._send(404, {})
            stored, media = state["manifests"][key]
            headers = {"Content-Type": media, "Docker-Content-Digest": digest_of(stored)}
            self._send(200, headers, stored)

        def do_GET(self) -> None:
            self._handle()

        def do_HEAD(self) -> None:
            self._handle()

        def do_PUT(self) -> None:
            self._handle()

    return Handler


class RegistryTestCase(unittest.TestCase):
    def serve(self, state: State) -> str:
        """Start a fake registry for `state`; its host:port."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"127.0.0.1:{server.server_address[1]}"

    def setUp(self) -> None:
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PIN_PROMOTE_REGISTRY_USERNAME", None)
        os.environ.pop("PIN_PROMOTE_REGISTRY_PASSWORD", None)
        self.state = new_state()
        self.host = self.serve(self.state)
        self.image = f"{self.host}/{REPO}"

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
        self.assertIsNone(registry._token('Negotiate realm="x"', "scope", "http"))


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
        challenge = f'Bearer realm="http://{token_host}/token",service="x"'
        with self.assertRaisesRegex(RegistryError, "token service is not https"):
            registry._token(challenge, f"repository:{REPO}:pull,push", "https")
        self.assertEqual(token_state["seen"], [])

    def test_a_realm_without_a_host_is_refused(self) -> None:
        with self.assertRaisesRegex(RegistryError, "token service is not http"):
            registry._token('Bearer realm="http:///token"', "scope", "http")
        with self.assertRaisesRegex(RegistryError, "token service is not http"):
            registry._token("Bearer service=x", "scope", "http")


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
