"""Seed an empty private vault from an existing observation database."""

import argparse
import json
import tarfile
import tempfile
from pathlib import Path

from vault import GitHubVault, VaultError, inspect_database, observation_ids


def bootstrap(data_directory, minimum):
    data = Path(data_directory).resolve()
    database = data / "flights.sqlite3"
    original = inspect_database(database)
    if original["offer_count"] < minimum:
        raise VaultError("Source pool has fewer observations than expected")
    baseline = observation_ids(database)
    vault = GitHubVault()
    vault.verify_private()
    with tempfile.TemporaryDirectory(prefix="rotelio-bootstrap-") as directory:
        bundle = Path(directory) / "observations.tar.gz"
        with tarfile.open(bundle, "w:gz") as archive:
            for name in ("daily", "raw", "events.jsonl", "rejected_offers.csv"):
                if (data / name).exists():
                    archive.add(data / name, arcname=name)
        metadata = vault.publish(database, baseline, bundle=bundle, initialize=True)
        restored = Path(directory) / "verified.sqlite3"
        verified = vault.restore(restored)
        if observation_ids(restored) != baseline:
            raise VaultError(
                "Restored checkpoint does not match the original observations"
            )
        print(
            json.dumps(
                {
                    "source_observation_count": original["offer_count"],
                    "restored_observation_count": verified["offer_count"],
                    "observation_identifiers_match": True,
                    "checkpoint_sha256": metadata["sha256"],
                    "vault_private": True,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-directory", type=Path, required=True)
    parser.add_argument("--minimum-observations", type=int, default=1)
    args = parser.parse_args()
    try:
        bootstrap(args.data_directory, args.minimum_observations)
    except VaultError as error:
        print(str(error))
        raise SystemExit(2)
