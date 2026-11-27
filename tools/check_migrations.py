#!/usr/bin/env python3
"""Static checks for the forward-only migration layout.

This check deliberately does not parse SQL. PostgreSQL remains the authority
for SQL syntax and constraint validation; this script catches migration files
that would make the documented application order ambiguous.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
NUMBERED = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
SITE = re.compile(r"^(\d{2})_[a-z0-9_]+\.sql$")


def fail(message: str) -> None:
    print(f"migration check: {message}", file=sys.stderr)
    raise SystemExit(1)


def check_numbered_migrations() -> None:
    files = sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    if not files:
        fail("no numbered root migrations found")

    numbers: list[int] = []
    for path in files:
        match = NUMBERED.fullmatch(path.name)
        if match is None:
            fail(f"invalid migration name: {path.relative_to(ROOT)}")
        numbers.append(int(match.group(1)))
        sql = path.read_text(encoding="utf-8").upper()
        if "BEGIN;" not in sql or "COMMIT;" not in sql:
            fail(f"root migration must be transactional: {path.relative_to(ROOT)}")

    if numbers != list(range(numbers[0], numbers[-1] + 1)):
        fail(f"root migration numbers are not contiguous: {numbers}")
    if numbers[0] != 1:
        fail("the initial root migration must be numbered 0001")


def check_site_migrations() -> None:
    required = {"10_id_sequences.sql", "20_publication.sql", "30_subscription.sql"}
    for site in ("a", "cloud"):
        directory = MIGRATIONS / "site" / site
        files = sorted(directory.glob("*.sql"))
        names = [path.name for path in files]
        if not required.issubset(names):
            fail(f"{site} is missing required site migrations: {sorted(required - set(names))}")

        numbers: list[int] = []
        for path in files:
            match = SITE.fullmatch(path.name)
            if match is None:
                fail(f"invalid site migration name: {path.relative_to(ROOT)}")
            numbers.append(int(match.group(1)))
        if numbers != sorted(set(numbers)):
            fail(f"{site} site migrations must have unique ascending numbers: {numbers}")

        for path in files:
            sql = path.read_text(encoding="utf-8").upper()
            if "CREATE SUBSCRIPTION" in sql:
                if path != files[-1]:
                    fail(f"subscription migration must be last: {path.relative_to(ROOT)}")
                if "BEGIN;" in sql or "COMMIT;" in sql:
                    fail(f"subscription migration must run outside a transaction: {path.relative_to(ROOT)}")
            elif "BEGIN;" not in sql or "COMMIT;" not in sql:
                fail(f"site migration must be transactional: {path.relative_to(ROOT)}")

        subscription_files = [path for path in files if "CREATE SUBSCRIPTION" in path.read_text(encoding="utf-8").upper()]
        if len(subscription_files) != 1:
            fail(f"{site} must have exactly one subscription migration")


def main() -> int:
    check_numbered_migrations()
    check_site_migrations()
    print("migration check: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
