"""Operator-only explicit service dataset activation; does not attest its source."""

import argparse
import asyncio
import sys

from app.config import Settings
from app.db import Database
from app.planning.datasets import DatasetProvider
from app.planning.registry import activate_dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True)
    parser.add_argument("--sha256", required=True, help="Independently supplied SHA-256 pin")
    parser.add_argument("--expected-revision", type=int, required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args()

    async def run():
        settings = Settings()
        provider = DatasetProvider.from_startup_file(args.path, args.sha256)
        database = Database(settings)
        try:
            return await activate_dataset(
                database,
                settings,
                provider,
                expected_revision=args.expected_revision,
                reason=args.reason,
            )
        finally:
            await database.close()

    try:
        revision = asyncio.run(run())
    except Exception:  # noqa: BLE001 - file paths and database details are private
        print("Dataset activation refused; details redacted.", file=sys.stderr)
        return 1
    print(f"Dataset generation {revision} activated; source verification is not implied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
