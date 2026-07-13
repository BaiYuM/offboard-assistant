from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


BUNDLE_VERSION = 1
SYNC_CONFIG_FILE = "sync-config.json"
SYNC_FILES = {
    "baseline.json",
    "latest-snapshot.json",
    "install-events.jsonl",
    "install-monitor-state.json",
    "offboarding-report.md",
    "handled-items.json",
}


def _url_origin(url: str) -> tuple[str, str, int | None] | None:
    """Return the RFC 6454 origin tuple for a URL, or ``None`` if invalid."""
    try:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        if not scheme or not hostname:
            return None
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme)
    return scheme, hostname.casefold(), port


def _closed_redirect_error(req, fp, code, reason, headers) -> urllib.error.HTTPError:
    if fp is not None:
        try:
            fp.close()
        except Exception:
            pass
    error = urllib.error.HTTPError(req.full_url, code, reason, headers, None)
    # Python 3.14 wraps a ``None`` fp in a temporary file object internally;
    # close it before raising so the rejected redirect cannot trigger a
    # ResourceWarning during exception cleanup.
    error.close()
    error.fp = None
    return error


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow redirects only when the request origin stays unchanged.

    urllib's default redirect handler copies arbitrary request headers,
    including ``Authorization``, to the redirect target.  Refusing a
    cross-origin redirect prevents credentials from leaving the configured
    WebDAV endpoint.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        request_origin = _url_origin(req.full_url)
        redirect_origin = _url_origin(newurl)
        if request_origin is None or redirect_origin is None or request_origin != redirect_origin:
            raise _closed_redirect_error(req, fp, code, "Cross-origin redirect blocked", headers)
        if code not in {301, 302, 303, 307, 308}:
            raise _closed_redirect_error(req, fp, code, msg, headers)
        method = req.get_method()
        if method in {"GET", "HEAD"} or (method == "POST" and code in {301, 302, 303}):
            return super().redirect_request(req, fp, code, msg, headers, newurl)

        redirected_method = "GET" if code == 303 else method
        redirected_data = None if code == 303 else req.data
        excluded_headers = {"content-length", "content-type"} if code == 303 else set()
        redirected_headers = {
            key: value
            for key, value in req.headers.items()
            if key.lower() not in excluded_headers
        }
        return urllib.request.Request(
            newurl,
            data=redirected_data,
            headers=redirected_headers,
            origin_req_host=req.origin_req_host,
            unverifiable=True,
            method=redirected_method,
        )


def _open_url(request: urllib.request.Request, timeout: int):
    opener = urllib.request.build_opener(_SameOriginRedirectHandler())
    return opener.open(request, timeout=timeout)


class CryptoUnavailable(RuntimeError):
    pass


def crypto_available() -> bool:
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return False
    return True


def _derive_fernet_key(passphrase: str, salt: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ImportError as exc:
        raise CryptoUnavailable("Install cryptography to enable encrypted sync bundles.") from exc

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=390000,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise CryptoUnavailable("Install cryptography to enable encrypted sync bundles.") from exc

    salt = os.urandom(16)
    key = _derive_fernet_key(passphrase, salt)
    token = Fernet(key).encrypt(data)
    envelope = {
        "version": BUNDLE_VERSION,
        "kdf": "PBKDF2HMAC-SHA256",
        "iterations": 390000,
        "salt": base64.b64encode(salt).decode("ascii"),
        "token": token.decode("ascii"),
    }
    return json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")


def _decrypt_bytes(data: bytes, passphrase: str) -> bytes:
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise CryptoUnavailable("Install cryptography to enable encrypted sync bundles.") from exc

    envelope = json.loads(data.decode("utf-8"))
    salt = base64.b64decode(envelope["salt"])
    token = envelope["token"].encode("ascii")
    key = _derive_fernet_key(passphrase, salt)
    return Fernet(key).decrypt(token)


def export_encrypted_bundle(state_dir: Path, output_path: Path, passphrase: str) -> None:
    if not passphrase:
        raise ValueError("Passphrase is required.")
    payload: dict[str, Any] = {"version": BUNDLE_VERSION, "files": {}}
    for name in sorted(SYNC_FILES):
        path = state_dir / name
        if not path.exists() or not path.is_file():
            continue
        payload["files"][name] = base64.b64encode(path.read_bytes()).decode("ascii")
    encrypted = _encrypt_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"), passphrase)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(encrypted)


def import_encrypted_bundle(bundle_path: Path, state_dir: Path, passphrase: str) -> list[str]:
    if not passphrase:
        raise ValueError("Passphrase is required.")
    payload = json.loads(_decrypt_bytes(bundle_path.read_bytes(), passphrase).decode("utf-8"))
    state_dir.mkdir(parents=True, exist_ok=True)
    imported: list[str] = []
    for name, encoded in payload.get("files", {}).items():
        if name not in SYNC_FILES:
            continue
        (state_dir / name).write_bytes(base64.b64decode(encoded))
        imported.append(name)
    return imported


def save_sync_config(state_dir: Path, config: dict[str, str]) -> None:
    safe_config = {
        "webdav_url": config.get("webdav_url", ""),
        "username": config.get("username", ""),
        "remote_name": config.get("remote_name", "offboard-assistant.enc"),
    }
    (state_dir / SYNC_CONFIG_FILE).write_text(json.dumps(safe_config, ensure_ascii=False, indent=2), encoding="utf-8")


def load_sync_config(state_dir: Path) -> dict[str, str]:
    path = state_dir / SYNC_CONFIG_FILE
    if not path.exists():
        return {"webdav_url": "", "username": "", "remote_name": "offboard-assistant.enc"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"webdav_url": "", "username": "", "remote_name": "offboard-assistant.enc"}
    return {
        "webdav_url": str(data.get("webdav_url", "")),
        "username": str(data.get("username", "")),
        "remote_name": str(data.get("remote_name", "offboard-assistant.enc")),
    }


def _remote_url(base_url: str, remote_name: str) -> str:
    return base_url.rstrip("/") + "/" + remote_name.lstrip("/")


def webdav_upload(base_url: str, remote_name: str, username: str, password: str, local_file: Path) -> None:
    data = local_file.read_bytes()
    request = urllib.request.Request(_remote_url(base_url, remote_name), data=data, method="PUT")
    request.add_header("Content-Type", "application/octet-stream")
    auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    request.add_header("Authorization", f"Basic {auth}")
    try:
        with _open_url(request, timeout=30) as response:
            if response.status not in {200, 201, 204}:
                raise RuntimeError(f"Unexpected WebDAV status: {response.status}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"WebDAV upload failed: {exc}") from exc


def webdav_download(base_url: str, remote_name: str, username: str, password: str, output_file: Path) -> None:
    request = urllib.request.Request(_remote_url(base_url, remote_name), method="GET")
    auth = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    request.add_header("Authorization", f"Basic {auth}")
    try:
        with _open_url(request, timeout=30) as response:
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_bytes(response.read())
    except urllib.error.URLError as exc:
        raise RuntimeError(f"WebDAV download failed: {exc}") from exc
