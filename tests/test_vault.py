import gzip
import json
import sqlite3
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from vault import (
    GitHubVault,
    PrivateRedirect,
    VaultError,
    decode_checkpoint,
    digest,
    inspect_database,
    observation_ids,
    snapshot,
    unpack_checkpoint,
)


def database(path, identifiers=("first", "second")):
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE offers(observation_id TEXT PRIMARY KEY, collection_date TEXT)"
    )
    for table in ("queries", "events", "control", "rejected"):
        db.execute("CREATE TABLE " + table + "(value TEXT)")
    db.executemany(
        "INSERT INTO offers VALUES(?, '2026-10-15')", [(i,) for i in identifiers]
    )
    db.commit()
    db.close()


class VaultTests(unittest.TestCase):
    def test_invalid_token_is_rejected_without_echoing_the_value(self):
        token = "unit-test\nplaceholder"
        with self.assertRaises(VaultError) as result:
            GitHubVault("example/private-data", token)
        self.assertNotIn("placeholder", str(result.exception))
        vault = GitHubVault("example/private-data", " unit-test-placeholder\n")
        self.assertEqual(vault.token, "unit-test-placeholder")

    def test_consistent_backup_and_checksum_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original.sqlite3"
            copied = root / "copied.sqlite3"
            restored = root / "restored.sqlite3"
            database(original)
            result = snapshot(original, copied)
            compressed = root / "pool.gz"
            with copied.open("rb") as source, gzip.open(compressed, "wb") as target:
                target.write(source.read())
            unpack_checkpoint(
                compressed, restored, {"sha256": digest(compressed), "offer_count": 2}
            )
            self.assertEqual(result["offer_count"], 2)
            self.assertEqual(observation_ids(restored), observation_ids(original))

    def test_corrupt_checkpoint_does_not_create_a_pool(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.gz"
            archive.write_bytes(b"not a checkpoint")
            destination = root / "pool.sqlite3"
            with self.assertRaisesRegex(VaultError, "checksum"):
                unpack_checkpoint(
                    archive, destination, {"sha256": "0" * 64, "offer_count": 2}
                )
            self.assertFalse(destination.exists())

    def test_count_mismatch_removes_incomplete_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            archive = root / "pool.gz"
            database(source)
            with source.open("rb") as reader, gzip.open(archive, "wb") as writer:
                writer.write(reader.read())
            destination = root / "pool.sqlite3"
            with self.assertRaisesRegex(VaultError, "count mismatch"):
                unpack_checkpoint(
                    archive, destination, {"sha256": digest(archive), "offer_count": 3}
                )
            self.assertFalse(destination.exists())

    def test_missing_checkpoint_never_starts_empty(self):
        for body in ("", "{}", '{"status":"uninitialized"}'):
            with self.subTest(body=body), self.assertRaises(VaultError):
                decode_checkpoint({"body": body})

    def test_public_vault_is_rejected_before_download(self):
        vault = GitHubVault("example/private-data", "unit-test-placeholder")
        vault.request = Mock(return_value={"private": False})
        with self.assertRaisesRegex(VaultError, "not private"):
            vault.restore(Path("unused.sqlite3"))
        self.assertEqual(vault.request.call_count, 1)

    def test_stale_writer_cannot_publish(self):
        vault = GitHubVault("example/private-data", "unit-test-placeholder")
        vault.verify_private = Mock()
        vault.release = Mock(
            return_value={
                "body": json.dumps(
                    {
                        "format_version": 1,
                        "status": "ready",
                        "asset_id": 7,
                        "sha256": "0" * 64,
                        "offer_count": 2,
                    }
                )
            }
        )
        vault.upload = Mock()
        with self.assertRaisesRegex(VaultError, "stale writer"):
            vault.publish(Path("unused.sqlite3"), {"first"}, expected_asset_id=6)
        vault.upload.assert_not_called()

    def test_old_observations_cannot_be_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "pool.sqlite3"
            database(source, ("second",))
            vault = GitHubVault("example/private-data", "unit-test-placeholder")
            vault.verify_private = Mock()
            vault.release = Mock(
                return_value={
                    "body": json.dumps(
                        {
                            "format_version": 1,
                            "status": "ready",
                            "asset_id": 6,
                            "sha256": "0" * 64,
                            "offer_count": 2,
                        }
                    )
                }
            )
            vault.upload = Mock()
            with self.assertRaisesRegex(VaultError, "would be lost"):
                vault.publish(source, {"first", "second"}, expected_asset_id=6)
            vault.upload.assert_not_called()

    def test_redirect_does_not_forward_token(self):
        request = urllib.request.Request(
            "https://api.github.com/test",
            headers={"Authorization": "Bearer unit-test-placeholder"},
        )
        redirected = PrivateRedirect().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://release-assets.githubusercontent.com/asset",
        )
        self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaisesRegex(VaultError, "Unexpected"):
            PrivateRedirect().redirect_request(
                request, None, 302, "Found", {}, "https://example.com/asset"
            )


if __name__ == "__main__":
    unittest.main()
