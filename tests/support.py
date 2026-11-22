"""Offline fixtures: a bare remote and a clone of it with a config and pin files on main, and a fake registry."""

import base64
import hashlib
import json
import re
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TypedDict

from pin_promote import Config, parse

CONFIG_PATH = "pin-promote.json"
FP_A, FP_B, FP_C = "a" * 64, "b" * 64, "c" * 64
DIGEST_A = "sha256:" + "1" * 64

CONFIG: dict[str, object] = {
    "schema": 1,
    "shared_sources": ["lock.json"],
    "journal": ["CHANGELOG.md"],
    "channels": {
        "vm": {
            "pin": "images/vm/pin.json",
            "fields": {"fingerprint": "[0-9a-f]{64}"},
            "sources": ["images/", ":!images/host/"],
            "checks": ["boot"],
            "subject": "vm: pin the verified image",
        },
        "host": {
            "pin": "images/host/pin.json",
            "fields": {"os": "[0-9a-f]{16}", "fingerprint": "[0-9a-f]{64}"},
            "sources": ["images/host/", "install/"],
            "checks": ["smoke", "host-test"],
            "note": "what the next install installs",
        },
    },
}


def run_git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, check=True)
    return done.stdout.strip()


class Repository(unittest.TestCase):
    """`self.remote` (bare) and `self.clone`, main holding the config, two pins and their sources."""

    def setUp(self) -> None:
        held = tempfile.TemporaryDirectory()
        self.addCleanup(held.cleanup)
        self.root = Path(held.name)
        self.remote = self.root / "remote.git"
        self.clone = self.root / "clone"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.remote)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.clone)], check=True)
        # A machine-wide hooks path (a pre-push guard, say) is not this fixture's: hooks live in the repos.
        run_git(self.remote, "config", "core.hooksPath", str(self.remote / "hooks"))
        settings = (
            ("user.name", "Test"),
            ("user.email", "test@example.com"),
            ("commit.gpgsign", "false"),
            ("core.hooksPath", str(self.root / "no-hooks")),
        )
        for key, value in settings:
            run_git(self.clone, "config", key, value)
        run_git(self.clone, "remote", "add", "origin", str(self.remote))
        files = {
            CONFIG_PATH: json.dumps(CONFIG, indent=2) + "\n",
            "lock.json": '{"tool": "1.0"}\n',
            "CHANGELOG.md": "# Changes\n",
            "images/vm/build.sh": "build\n",
            "images/vm/pin.json": json.dumps({"fingerprint": FP_A}) + "\n",
            "images/host/spec.yml": "spec\n",
            "images/host/pin.json": json.dumps({"os": "0" * 16, "fingerprint": FP_A}) + "\n",
            "install/driver.py": "driver\n",
            "docs/readme.md": "docs\n",
        }
        self.build = self.commit(files, "fixture")
        self.config: Config = parse(CONFIG)

    def commit(self, files: dict[str, str], message: str) -> str:
        """Commit these files on top of origin/main (or as the first commit) and push; the new tip."""
        if run_git(self.clone, "ls-remote", "origin", "main"):
            run_git(self.clone, "fetch", "-q", "origin", "main")
            run_git(self.clone, "checkout", "-q", "--detach", "origin/main")
        for name, text in files.items():
            path = self.clone / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        run_git(self.clone, "add", "-A")
        run_git(self.clone, "commit", "-q", "-m", message)
        run_git(self.clone, "push", "-q", "origin", "HEAD:refs/heads/main")
        run_git(self.clone, "fetch", "-q", "origin", "main")
        return run_git(self.clone, "rev-parse", "HEAD")

    def set_config(self, raw: dict[str, object], path: str = CONFIG_PATH) -> str:
        """Commit this config on main, and make it the local one too; the new tip."""
        tip = self.commit({path: json.dumps(raw, indent=2) + "\n"}, "the config changes")
        self.config = parse(raw, path)
        return tip

    def tip(self) -> str:
        return run_git(self.remote, "rev-parse", "main")

    def on_main(self, path: str) -> str:
        return run_git(self.remote, "show", f"main:{path}") + "\n"

    def pin(self, path: str) -> object:
        return json.loads(self.on_main(path))


# ---- a fake registry on 127.0.0.1 ------------------------------------------------------------------
# It speaks just enough of the distribution API (GET, HEAD and PUT of manifests, a challenge on 401 and
# a token endpoint). Its whole state is one TypedDict, so a test reads what the registry holds and what
# it saw without reaching into a server object.

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


def serve(case: unittest.TestCase, state: State) -> str:
    """Start a fake registry for `state`, stopped when the test ends; its host:port."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    case.addCleanup(thread.join)
    case.addCleanup(server.server_close)
    case.addCleanup(server.shutdown)
    return f"127.0.0.1:{server.server_address[1]}"
