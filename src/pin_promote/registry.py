"""Move a registry tag to a pinned digest: the OCI distribution API, standard library only.

A channel with a `registry` block has a pin field holding a digest (sha256:<64 hex>). Once that pin
is on the branch, the tag is moved to that digest: the manifest is read by digest, its bytes are
checked against the digest, and the same bytes are put under the tag. A tag already on the digest is
left alone, so running it again (a retry after a failed tag move) changes nothing.

Credentials come from the environment, PIN_PROMOTE_REGISTRY_USERNAME and
PIN_PROMOTE_REGISTRY_PASSWORD (for GitHub's registry: the actor and a token with packages: write).
They go only to the registry itself or to the token service its challenge names, over https.
"""

import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from email.message import Message
from http.client import HTTPResponse
from typing import cast

from .config import DIGEST, REGISTRY_IMAGE, REGISTRY_TAG

MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
MAX_MANIFEST = 4 * 1024 * 1024  # the distribution spec's recommended limit
MAX_TOKEN = 64 * 1024
TIMEOUT = 30.0
CHALLENGE = re.compile(r'(\w+)="([^"]*)"')


class RegistryError(RuntimeError):
    """The registry refused or answered something unusable; the tag may not have moved."""


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


def _token(challenge: str, scope: str, scheme: str) -> str | None:
    """The Authorization value a 401's WWW-Authenticate asks for, or None when it asks for none we can give."""
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
    query = {"service": fields.get("service", ""), "scope": scope}
    url = realm + ("&" if parsed.query else "?") + urllib.parse.urlencode({k: v for k, v in query.items() if v})
    headers = {"User-Agent": "pin-promote"}
    if basic:
        headers["Authorization"] = basic
    try:
        with _OPENER.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as response:
            body = json.loads(_read(response, MAX_TOKEN))
    except urllib.error.HTTPError as error:
        raise RegistryError(f"the token service refused: HTTP {error.code}") from None
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
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
            with _OPENER.open(request, timeout=TIMEOUT) as response:
                return response.status, response.headers, _read(response, MAX_MANIFEST)
        except urllib.error.HTTPError as error:
            if error.code == 401 and not auth:
                value = _token(error.headers.get("WWW-Authenticate", ""), scope, scheme)
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


def move_tag(image: str, tag: str, digest: str, *, plain_http: bool = False) -> bool:
    """Point `image:tag` at `digest`; True when it moved, False when it was already there."""
    if not DIGEST.fullmatch(digest):
        raise RegistryError(f"not a sha256 digest: {digest!r}")
    if not REGISTRY_TAG.fullmatch(tag):
        raise RegistryError(f"not a tag: {tag!r}")
    host, repository = split_image(image)
    scheme = "http" if plain_http else "https"
    base = f"{scheme}://{host}/v2/{repository}/manifests/"
    accept = {"Accept": ", ".join(MANIFEST_TYPES)}
    auth: list[str] = []
    pull_push = f"repository:{repository}:pull,push"
    status, headers, body = _request("GET", base + digest, pull_push, scheme, auth, headers=accept)
    if status == 404:
        raise RegistryError(f"{image}@{digest} is not in the registry")
    if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
        raise RegistryError(f"{image}@{digest}: the registry served a manifest with another digest")
    media = headers.get_content_type()
    if media not in MANIFEST_TYPES:
        declared = json.loads(body).get("mediaType") if body.startswith(b"{") else None
        if declared not in MANIFEST_TYPES:
            raise RegistryError(f"{image}@{digest}: not a manifest this tool moves ({media})")
        media = cast(str, declared)
    status, headers, _ = _request("HEAD", base + tag, pull_push, scheme, auth, headers=accept)
    if status == 200 and headers.get("Docker-Content-Digest") == digest:
        return False
    status, headers, _ = _request(
        "PUT", base + tag, pull_push, scheme, auth, data=body, headers={"Content-Type": media}
    )
    if status not in (200, 201):
        raise RegistryError(f"PUT {image}:{tag}: HTTP {status}")
    answered = headers.get("Docker-Content-Digest")
    if answered and answered != digest:
        raise RegistryError(f"{image}:{tag}: the registry stored {answered}, not {digest}")
    return True
