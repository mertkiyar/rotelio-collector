"""Versioned SQLite checkpoints in a private GitHub release."""

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

TAG = "flight-pool"


class VaultError(RuntimeError):
    pass


class PrivateRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urllib.parse.urlparse(newurl)
        if url.scheme != "https" or not (
            url.hostname in ("github.com", "api.github.com")
            or (url.hostname or "").endswith(".githubusercontent.com")
        ):
            raise VaultError("Unexpected download redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected:
            redirected.remove_header("Authorization")
        return redirected


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def inspect_database(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise VaultError("Database integrity check failed")
        for table in ("offers", "queries", "events", "control", "rejected"):
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone():
                raise VaultError("Incomplete observation database")
        return {
            "offer_count": db.execute("SELECT count(*) FROM offers").fetchone()[0],
            "collection_dates": [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT collection_date FROM offers ORDER BY collection_date"
                )
            ],
        }
    finally:
        db.close()


def observation_ids(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return {row[0] for row in db.execute("SELECT observation_id FROM offers")}
    finally:
        db.close()


def snapshot(source, destination):
    source = Path(source)
    if not source.is_file():
        raise VaultError("Observation database does not exist")
    destination = Path(destination)
    if destination.exists():
        raise VaultError("Checkpoint destination already exists")
    original = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    backup = sqlite3.connect(destination)
    try:
        original.backup(backup)
    finally:
        backup.close()
        original.close()
    return inspect_database(destination)


def decode_checkpoint(release):
    try:
        value = json.loads(release["body"])
        if value["format_version"] != 1 or value["status"] != "ready":
            raise ValueError()
        if type(value["asset_id"]) is not int or value["asset_id"] <= 0:
            raise ValueError()
        if not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
            raise ValueError()
        if type(value["offer_count"]) is not int or value["offer_count"] < 1:
            raise ValueError()
        return value
    except (KeyError, TypeError, ValueError):
        raise VaultError(
            "No valid checkpoint; refusing to start an empty pool"
        ) from None


def unpack_checkpoint(archive, destination, metadata):
    if digest(archive) != metadata["sha256"]:
        raise VaultError("Checkpoint checksum mismatch")
    destination = Path(destination)
    if destination.exists():
        raise VaultError("Restore destination already exists")
    try:
        with gzip.open(archive, "rb") as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target)
        actual = inspect_database(destination)
        if actual["offer_count"] != metadata["offer_count"]:
            raise VaultError("Checkpoint observation count mismatch")
        return actual
    except Exception:
        destination.unlink(missing_ok=True)
        raise


class GitHubVault:
    def __init__(self, repository=None, token=None):
        self.repository = repository or os.environ.get("DATA_VAULT_REPOSITORY", "")
        self.token = (token or os.environ.get("DATA_VAULT_TOKEN", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise VaultError("DATA_VAULT_REPOSITORY must be owner/repository")
        if not self.token:
            raise VaultError("DATA_VAULT_TOKEN is not configured")
        if any(ord(character) < 33 or ord(character) > 126 for character in self.token):
            raise VaultError("DATA_VAULT_TOKEN contains invalid characters")
        self.opener = urllib.request.build_opener(PrivateRedirect())
        self.base = "/repos/" + self.repository

    def request(self, method, path, payload=None, binary=None, accept=None):
        url = path if path.startswith("https://") else "https://api.github.com" + path
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in (
            "api.github.com",
            "uploads.github.com",
        ):
            raise VaultError("Unexpected GitHub API endpoint")
        headers = {
            "Authorization": "Bearer " + self.token,
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Rotelio-collector",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        elif binary is not None:
            data = Path(binary).read_bytes()
            headers["Content-Type"] = "application/octet-stream"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=120) as response:
                body = response.read()
                return (
                    body if accept == "application/octet-stream" else json.loads(body)
                )
        except urllib.error.HTTPError as error:
            # Do not include response bodies, authorization headers or signed URLs.
            raise VaultError(
                "GitHub API request failed: HTTP " + str(error.code)
            ) from None
        except (urllib.error.URLError, TimeoutError):
            raise VaultError("GitHub API connection failed") from None

    def verify_private(self):
        info = self.request("GET", self.base)
        if info.get("private") is not True:
            raise VaultError("Data vault is not private; refusing data access")
        return info

    def release(self):
        return self.request("GET", self.base + "/releases/tags/" + TAG)

    def restore(self, destination):
        self.verify_private()
        release = self.release()
        metadata = decode_checkpoint(release)
        with tempfile.TemporaryDirectory(prefix="rotelio-restore-") as directory:
            archive = Path(directory) / "pool.sqlite3.gz"
            archive.write_bytes(
                self.request(
                    "GET",
                    self.base + "/releases/assets/" + str(metadata["asset_id"]),
                    accept="application/octet-stream",
                )
            )
            unpack_checkpoint(archive, destination, metadata)
        return metadata

    def upload(self, release, path, name):
        base = release["upload_url"].split("{", 1)[0]
        url = base + "?" + urllib.parse.urlencode({"name": name})
        return self.request("POST", url, binary=path)

    def publish(
        self,
        database,
        baseline_ids,
        expected_asset_id=None,
        bundle=None,
        initialize=False,
    ):
        self.verify_private()
        release = self.release()
        if initialize:
            try:
                decode_checkpoint(release)
            except VaultError:
                if (
                    json.loads(release.get("body") or "{}").get("status")
                    != "uninitialized"
                ):
                    raise VaultError("Unexpected bootstrap state") from None
            else:
                raise VaultError("A checkpoint already exists; bootstrap is disabled")
        elif decode_checkpoint(release)["asset_id"] != expected_asset_id:
            raise VaultError("Checkpoint changed; refusing a stale writer")

        with tempfile.TemporaryDirectory(prefix="rotelio-checkpoint-") as directory:
            checkpoint = Path(directory) / "pool.sqlite3"
            info = snapshot(database, checkpoint)
            current_ids = observation_ids(checkpoint)
            if not baseline_ids <= current_ids or not current_ids:
                raise VaultError("Previous observations would be lost; upload refused")
            suffix = (
                datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                + "-"
                + uuid.uuid4().hex[:8]
            )
            compressed = Path(directory) / "pool.sqlite3.gz"
            with checkpoint.open("rb") as source, gzip.open(compressed, "wb") as target:
                shutil.copyfileobj(source, target)
            asset = self.upload(release, compressed, "pool-" + suffix + ".sqlite3.gz")
            metadata = {
                "format_version": 1,
                "status": "ready",
                "asset_id": asset["id"],
                "asset_name": asset["name"],
                "sha256": digest(compressed),
                **info,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "run_id": os.environ.get("GITHUB_RUN_ID", "bootstrap"),
            }
            if bundle:
                daily = self.upload(
                    release, bundle, "observations-" + suffix + ".tar.gz"
                )
                metadata["daily_bundle_asset_id"] = daily["id"]
            latest = self.release()
            if (
                not initialize
                and decode_checkpoint(latest)["asset_id"] != expected_asset_id
            ):
                raise VaultError(
                    "Checkpoint changed during upload; assets retained for recovery"
                )
            if (
                initialize
                and json.loads(latest.get("body") or "{}").get("status")
                != "uninitialized"
            ):
                raise VaultError("Bootstrap changed during upload")
            self.request(
                "PATCH",
                self.base + "/releases/" + str(release["id"]),
                {
                    "body": json.dumps(metadata, indent=2),
                },
            )
            # The release update is the commit point. Older assets are never deleted.
            if decode_checkpoint(self.release())["asset_id"] != asset["id"]:
                raise VaultError("Checkpoint publication could not be confirmed")
            return metadata
