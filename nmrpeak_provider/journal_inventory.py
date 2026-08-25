"""Report frozen generations referenced by the admitted provider journal."""

from __future__ import annotations

import argparse
from pathlib import Path

from .attempt_journal_store import (
    AttemptJournalStateRejected,
    AttemptJournalStore,
)
from .canonical_json import canonical_json_bytes
from .provider_config import JOURNAL_MAXIMUM_RECORDS, JOURNAL_PATH


def journal_generation_inventory(root: Path) -> bytes:
    """Return the complete stable set of referenced frozen generation IDs."""

    with AttemptJournalStore(
        root,
        maximum_records=JOURNAL_MAXIMUM_RECORDS,
        read_only=True,
    ) as journal:
        generation_ids = sorted(
            {record.frozen_generation_id for record in journal.records()}
        )
    return canonical_json_bytes(
        {
            "schema_id": "nmrpeak.journal_generation_inventory.v1",
            "frozen_generation_ids": generation_ids,
        }
    )


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args(arguments)
    try:
        print(journal_generation_inventory(JOURNAL_PATH).decode("ascii"))
    except (AttemptJournalStateRejected, OSError, UnicodeError) as error:
        parser.exit(2, f"Cannot inspect provider journal: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
