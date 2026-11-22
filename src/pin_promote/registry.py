"""Move a registry tag to a pinned digest: the OCI distribution API, standard library only.

A channel with a `registry` block has a pin field holding a digest (sha256:<64 hex>). Once that pin
is on the branch, the tag is moved to that digest: the manifest is read by digest, its bytes are
checked against the digest, and the same bytes are put under the tag. A tag already on the digest is
left alone, so running it again (a retry after a failed tag move) changes nothing.

Credentials come from the environment, PIN_PROMOTE_REGISTRY_USERNAME and
PIN_PROMOTE_REGISTRY_PASSWORD (for GitHub's registry: the actor and a token with packages: write).
They go only to the registry itself or to a token service on the registry's own host (or on the one
host the channel's `token_host` names), over https. Plain http is for a registry on this machine only.
Every move has one overall deadline.
"""

import base64
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from email.message import Message
from http.client import HTTPResponse

from .config import DIGEST, REGISTRY_IMAGE, REGISTRY_TAG

MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
MAX_MANIFEST = 4 * 1024 * 1024  # the distribution spec's recommended limit
MAX_TOKEN = 64 * 1024
TIMEOUT = 30.0  # one request
DEADLINE = 120.0  # all the requests of one move
LOOPBACK = ("127.0.0.1", "::1", "localhost")
CHALLENGE = re.compile(r'(\w+)="([^"]*)"')


class RegistryError(RuntimeError):
    """The registry refused or answered something unusable; the tag may not have moved."""


class SupersededError(RegistryError):
    """The pin no longer holds this digest, so the tag was left alone (see `move_tag`'s `current`)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None  # a redirect would carry the Authorization header somewhere else


_OPENER = urllib.request.build_opener(_NoRedirect)


def split_image(image: str) -> tuple[str, str]:
    """(host, repository) of `registry.example.com/team/app`."""
    if not REGISTRY_IMAGE.fullmatch(image):
        raise RegistryError(f"not a registry image name: {image!r}")
    host, _, repository = image.partition("/")
    return host, repository


def _host_name(host: str) -> str:
    """The name of a `host[:port]` (a bracketed IPv6 address loses its brackets)."""
    return urllib.parse.urlsplit("//" + host).hostname or ""


def _timeout(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise RegistryError(f"the registry move took longer than {DEADLINE:g} seconds")
    return min(TIMEOUT, left)


def _read(response: HTTPResponse, limit: int) -> bytes:
    body = response.read(limit + 1)
    if len(body) > limit:
        raise RegistryError(f"the registry's answer is larger than {limit} bytes")
    return body


def _credentials() -> str | None:
    user = os.environ.get("PIN_PROMOTE_REGISTRY_USERNAME", "")
    password = os.environ.get("PIN_PROMOTE_REGISTRY_PASSWORD", "")
    if not user and not password:
        return None
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _token(challenge: str, scope: str, scheme: str, host: str, token_host: str | None, deadline: float) -> str | None:
    """The Authorization value a 401's WWW-Authenticate asks for, or None when it asks for none we can give.

    A bearer realm must use the registry's scheme and be on the registry's host (or on `token_host`):
    anything else is refused before a credential leaves."""
    kind, _, params = challenge.partition(" ")
    basic = _credentials()
    if kind.lower() == "basic":
        return basic
    if kind.lower() != "bearer":
        return None
    fields = dict(CHALLENGE.findall(params))
    realm = fields.get("realm", "")
    parsed = urllib.parse.urlsplit(realm)
    if parsed.scheme != scheme or not parsed.netloc:
        raise RegistryError(f"the registry's token service is not {scheme}: {realm!r}")
    if parsed.netloc.lower() not in (host, token_host):
        raise RegistryError(
            f"the registry's token service is on {parsed.netloc!r}, not on {host!r}; a channel names another host"
            f" it trusts with registry.token_host: {realm!r}"
        )
    query = {"service": fields.get("service", ""), "scope": scope}
    url = realm + ("&" if parsed.query else "?") + urllib.parse.urlencode({k: v for k, v in query.items() if v})
    headers = {"User-Agent": "pin-promote"}
    if basic:
        headers["Authorization"] = basic
    try:
        with _OPENER.open(urllib.request.Request(url, headers=headers), timeout=_timeout(deadline)) as response:
            body = json.loads(_read(response, MAX_TOKEN))
    except urllib.error.HTTPError as error:
        raise RegistryError(f"the token service refused: HTTP {error.code}") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise RegistryError(f"the token service: {error}") from None
    token = body.get("token") or body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise RegistryError("the token service answered no token")
    return f"Bearer {token}"


def _request(
    method: str,
    url: str,
    scope: str,
    scheme: str,
    host: str,
    token_host: str | None,
    deadline: float,
    auth: list[str],
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Message, bytes]:
    """One call, authenticating once on a 401. `auth` holds the Authorization value across calls."""
    for _ in range(2):
        sent = {"User-Agent": "pin-promote", **(headers or {})}
        if auth:
            sent["Authorization"] = auth[0]
        request = urllib.request.Request(url, data=data, method=method, headers=sent)
        try:
            with _OPENER.open(request, timeout=_timeout(deadline)) as response:
                return response.status, response.headers, _read(response, MAX_MANIFEST)
        except urllib.error.HTTPError as error:
            if error.code == 401 and not auth:
                value = _token(error.headers.get("WWW-Authenticate", ""), scope, scheme, host, token_host, deadline)
                if value is None:
                    raise RegistryError(f"{method} {url}: the registry wants credentials") from None
                auth.append(value)
                continue
            if error.code == 404:
                return 404, error.headers, b""
            raise RegistryError(f"{method} {url}: HTTP {error.code}") from None
        except (urllib.error.URLError, OSError) as error:
            raise RegistryError(f"{method} {url}: {error}") from None
    raise RegistryError(f"{method} {url}: refused after authenticating")


def _declared_media(body: bytes) -> str | None:
    """The mediaType a manifest's JSON declares; RegistryError when the body is not JSON at all."""
    if not body.startswith(b"{"):
        return None
    try:
        parsed = json.loads(body)
    except ValueError as error:
        raise RegistryError(f"the manifest is not JSON: {error}") from None
    declared = parsed.get("mediaType") if isinstance(parsed, dict) else None
    return declared if isinstance(declared, str) else None


def move_tag(
    image: str,
    tag: str,
    digest: str,
    *,
    plain_http: bool = False,
    token_host: str | None = None,
    current: Callable[[], bool] | None = None,
) -> bool:
    """Point `image:tag` at `digest`; True when it moved, False when it was already there.

    `plain_http` is for a registry on this machine; `token_host` names the one other host a bearer
    token may be fetched from (default: the registry's own). `current`, when given, is asked just
    before the PUT whether the pin still holds `digest`; if not, SupersededError is raised and the tag is
    untouched. The registry has no compare-and-swap, so this only narrows the window in which a newer
    promotion's tag move can be overwritten (README, "Registry tags")."""
    deadline = time.monotonic() + DEADLINE
    if not DIGEST.fullmatch(digest):
        raise RegistryError(f"not a sha256 digest: {digest!r}")
    if not REGISTRY_TAG.fullmatch(tag):
        raise RegistryError(f"not a tag: {tag!r}")
    host, repository = split_image(image)
    if plain_http and _host_name(host) not in LOOPBACK:
        raise RegistryError(f"plain http is only for a registry on this machine (localhost), not {host}")
    scheme = "http" if plain_http else "https"
    base = f"{scheme}://{host}/v2/{repository}/manifests/"
    accept = {"Accept": ", ".join(MANIFEST_TYPES)}
    auth: list[str] = []
    pull_push = f"repository:{repository}:pull,push"
    call = (pull_push, scheme, host, token_host, deadline, auth)
    status, headers, body = _request("GET", base + digest, *call, headers=accept)
    if status == 404:
        raise RegistryError(f"{image}@{digest} is not in the registry")
    if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
        raise RegistryError(f"{image}@{digest}: the registry served a manifest with another digest")
    media = headers.get_content_type()
    if media not in MANIFEST_TYPES:
        declared = _declared_media(body)
        if declared not in MANIFEST_TYPES:
            raise RegistryError(f"{image}@{digest}: not a manifest this tool moves ({media})")
        media = declared
    status, headers, _ = _request("HEAD", base + tag, *call, headers=accept)
    if status == 200 and headers.get("Docker-Content-Digest") == digest:
        return False
    if current is not None and not current():
        raise SupersededError(f"{image}:{tag}: the pin no longer holds {digest}")
    status, headers, _ = _request("PUT", base + tag, *call, data=body, headers={"Content-Type": media})
    if status not in (200, 201):
        raise RegistryError(f"PUT {image}:{tag}: HTTP {status}")
    answered = headers.get("Docker-Content-Digest")
    if answered and answered != digest:
        raise RegistryError(f"{image}:{tag}: the registry stored {answered}, not {digest}")
    return True
