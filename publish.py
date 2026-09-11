"""
publish.py -- persistence + status publishing for the paper trader.

Two concerns, both optional (everything degrades to local files when the env vars are absent):

1. Durable journal in Azure Blob Storage, so an ephemeral container keeps its history.
     JOURNAL_BLOB_SAS_URL = https://<acct>.blob.core.windows.net/<container>?<sas with racwl>
   sync_down() pulls journal.csv / paper_state.json at startup; sync_up() pushes after changes.

2. Encrypted status page data. today.json (open position, today's trades, stats, last scan) is
   encrypted with AES-256-GCM under a key derived from JOURNAL_PAGE_PASSPHRASE (PBKDF2-HMAC-SHA256,
   600k iterations) and uploaded as today.json.enc. The static page (site/journal.html) decrypts
   it in the browser with WebCrypto using the same parameters. The blob can be public-read: without
   the passphrase it is noise. This is encryption, not authentication -- anyone can *fetch* it.

Plain urllib against the Blob REST API; no Azure SDK dependency.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

PBKDF2_ITERS = 600_000
_HERE = os.path.dirname(os.path.abspath(__file__))


def _env(name: str) -> Optional[str]:
    v = os.environ.get(name)
    if not v and os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as h:
                v, _ = winreg.QueryValueEx(h, name)
        except OSError:
            v = None
    return v.strip() if v else None


# ---------------------------------------------------------------------------
# encryption
# ---------------------------------------------------------------------------
def encrypt_json(obj: dict, passphrase: str) -> bytes:
    """Returns a JSON envelope {v, kdf, iters, salt, iv, ct} (all base64) the page knows how to open."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, PBKDF2_ITERS, dklen=32)
    ct = AESGCM(key).encrypt(iv, json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8"), None)
    env = {"v": 1, "kdf": "PBKDF2-SHA256", "iters": PBKDF2_ITERS, "cipher": "AES-256-GCM",
           "salt": base64.b64encode(salt).decode(), "iv": base64.b64encode(iv).decode(), "ct": base64.b64encode(ct).decode()}
    return json.dumps(env).encode("utf-8")


def decrypt_json(blob: bytes, passphrase: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    env = json.loads(blob)
    key = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), base64.b64decode(env["salt"]), int(env["iters"]), dklen=32)
    pt = AESGCM(key).decrypt(base64.b64decode(env["iv"]), base64.b64decode(env["ct"]), None)
    return json.loads(pt)


# ---------------------------------------------------------------------------
# blob storage via container SAS URL
# ---------------------------------------------------------------------------
class BlobSync:
    def __init__(self, log=None):
        self.log = log or (lambda *a: None)
        self.sas = _env("JOURNAL_BLOB_SAS_URL")
        cafile = os.environ.get("SSL_CERT_FILE")
        self._ctx = ssl.create_default_context(cafile=cafile if cafile and os.path.exists(cafile) else None)

    @property
    def enabled(self) -> bool:
        return bool(self.sas)

    def _url(self, name: str) -> str:
        base, _, query = self.sas.partition("?")
        return f"{base.rstrip('/')}/{name}?{query}"

    def put(self, name: str, data: bytes, content_type: str = "application/octet-stream") -> bool:
        if not self.enabled:
            return False
        req = urllib.request.Request(self._url(name), data=data, method="PUT", headers={
            "x-ms-blob-type": "BlockBlob", "x-ms-version": "2021-08-06", "Content-Type": content_type,
            "Cache-Control": "no-cache"})
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=30):
                return True
        except urllib.error.HTTPError as e:
            self.log(f"   blob put {name} failed: {e.code} {e.read().decode('utf-8', 'replace')[:120]}")
        except urllib.error.URLError as e:
            self.log(f"   blob put {name} failed: {e}")
        return False

    def get(self, name: str) -> Optional[bytes]:
        if not self.enabled:
            return None
        req = urllib.request.Request(self._url(name), headers={"x-ms-version": "2021-08-06"})
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code != 404:
                self.log(f"   blob get {name} failed: {e.code}")
        except urllib.error.URLError as e:
            self.log(f"   blob get {name} failed: {e}")
        return None

    def sync_down(self, names: list[str]):
        for n in names:
            data = self.get(n)
            if data is not None:
                with open(os.path.join(_HERE, n), "wb") as f:
                    f.write(data)
                self.log(f"   pulled {n} from blob ({len(data)} bytes)")

    def sync_up(self, names: list[str]):
        for n in names:
            p = os.path.join(_HERE, n)
            if os.path.exists(p):
                with open(p, "rb") as f:
                    self.put(n, f.read(), "text/csv" if n.endswith(".csv") else "application/json")


# ---------------------------------------------------------------------------
# status document
# ---------------------------------------------------------------------------
class StatusPublisher:
    """Builds today.json, writes it locally (status/today.json), and -- if a passphrase is set --
    uploads the encrypted form to blob as today.json.enc."""

    def __init__(self, blob: BlobSync, log=None):
        self.blob = blob
        self.log = log or (lambda *a: None)
        self.passphrase = _env("JOURNAL_PAGE_PASSPHRASE")
        os.makedirs(os.path.join(_HERE, "status"), exist_ok=True)

    def publish(self, doc: dict):
        doc = dict(doc)
        doc["published_at"] = datetime.now(timezone.utc).isoformat()
        raw = json.dumps(doc, indent=1, default=str).encode("utf-8")
        with open(os.path.join(_HERE, "status", "today.json"), "wb") as f:
            f.write(raw)
        if self.passphrase:
            enc = encrypt_json(doc, self.passphrase)
            with open(os.path.join(_HERE, "status", "today.json.enc"), "wb") as f:
                f.write(enc)
            if self.blob.enabled:
                self.blob.put("today.json.enc", enc, "application/json")
        elif self.blob.enabled:
            self.log("   JOURNAL_PAGE_PASSPHRASE not set -> status NOT uploaded (would be plaintext)")
