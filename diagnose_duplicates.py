"""Triage DUPLICATE_VALUE failures from an upsert run (read-only, no writes).

    python diagnose_duplicates.py [output/upsert_failures_<UTC>.csv]

With no argument, picks the newest output/upsert_failures_*.csv.

Why this exists: an upsert by external ID is supposed to resolve
Membership_ID__c to an existing Contact and UPDATE it. On a 2,463-record run,
209 instead came back

    DUPLICATE_VALUE: duplicate value found: Membership_ID__c duplicates value
    on record with id: 003XXXXXXXXXXXXXXX

which is Salesforce saying it tried to INSERT and the unique index stopped it —
i.e. the lookup found no match for a value that demonstrably exists, since the
error names the record holding it. Re-running with freshly pulled ThoughtSpot
data reproduced it exactly, so it is deterministic, not a race.

Three things hide a record from that lookup while the unique index still sees
it. They need different fixes, so this script tells them apart by asking the
API two questions as the integration user:

  query    — what that user can see
  queryAll — the same plus soft-deleted (Recycle Bin) rows

Only DUPLICATE_VALUE rows are triaged. Any other failure in the CSV is a
different problem and is listed separately rather than given a verdict here —
a REQUIRED_FIELD_MISSING row, for instance, is legitimately absent from both
queries and would otherwise be misreported as a permissions problem.
"""
import csv
import glob
import re
import sys
from collections import Counter

from salesforce_client import (
    SalesforceClient,
    EXTERNAL_ID_FIELD,
    QUERY_CHUNK_SIZE,
    external_id_key,
    _chunks,
    _soql_quote,
)

# "...duplicates value on record with id: 003XXXXXXXXXXXXXXX"
BLOCKING_ID = re.compile(r"record with id:\s*(\w+)")
TRIAGED_STATUS = "DUPLICATE_VALUE"

VERDICTS = {
    "VISIBLE": ("the lookup should have matched -> the two-pass write in "
                "salesforce_client fixes this, ASSUMING the integration user "
                "also has EDIT access (read-only would turn these into "
                "INSUFFICIENT_ACCESS_OR_READONLY instead)"),
    "ID_MISMATCH": ("a different Contact holds this id -> two records are in "
                    "play; re-check the assumptions in this script"),
    "DELETED": ("Recycle Bin shadow: hidden from the lookup, still in the "
                "unique index -> restore or hard-purge those records first; "
                "the two-pass write alone will still collide on insert"),
    "INVISIBLE": ("hidden from the integration user by sharing/FLS -> a "
                  "permissions fix, NOT a code fix: grant View All / Modify "
                  "All on Contact, or correct the sharing rules"),
}
# Only these can be retried as-is; the rest need a fix in Salesforce first.
RETRYABLE = {"VISIBLE"}


def load_failures(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        match = BLOCKING_ID.search(r.get("message") or "")
        r["blocking_id"] = match.group(1) if match else None
    return rows


def classify(row: dict, visible: dict, deleted: dict) -> str:
    key = external_id_key(row["Personid"])
    if key in visible:
        blocking = row["blocking_id"]
        # Salesforce quotes 18-char Ids; a 15-char Id is the same record.
        if blocking and not blocking.startswith(visible[key][:15]):
            return "ID_MISMATCH"
        return "VISIBLE"
    if key in deleted:
        return "DELETED"
    return "INVISIBLE"


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        candidates = sorted(glob.glob("output/upsert_failures_*.csv"))
        if not candidates:
            sys.exit("No output/upsert_failures_*.csv found — pass one as an argument.")
        path = candidates[-1]

    all_rows = load_failures(path)
    print(f"Read {len(all_rows)} failed records from {path}")
    print(f"Status codes present: {dict(Counter(r['statusCode'] for r in all_rows))}")

    failures = [r for r in all_rows if TRIAGED_STATUS in (r["statusCode"] or "")]
    skipped = [r for r in all_rows if r not in failures]
    if skipped:
        print(f"\n{len(skipped)} row(s) are not {TRIAGED_STATUS} and are NOT triaged "
              f"here — they are a separate problem:")
        for r in skipped[:5]:
            print(f"    PMI {r['Personid']}: {r['statusCode']} — {r['message'][:90]}")
        if len(skipped) > 5:
            print(f"    ... and {len(skipped) - 5} more")
    if not failures:
        sys.exit(f"\nNo {TRIAGED_STATUS} rows to triage.")
    print(f"\nTriaging {len(failures)} {TRIAGED_STATUS} row(s).")

    sf = SalesforceClient()
    keys = [r["Personid"] for r in failures]

    # queryAll returns soft-deleted rows too; IsDeleted splits the two sets.
    # Scoped to the failing ids rather than the whole Contact table.
    visible, deleted = {}, {}
    for chunk in _chunks(sorted({external_id_key(k) for k in keys} - {""}),
                         QUERY_CHUNK_SIZE):
        soql = (
            f"SELECT Id, {EXTERNAL_ID_FIELD}, IsDeleted FROM Contact "
            f"WHERE {EXTERNAL_ID_FIELD} IN ({','.join(_soql_quote(k) for k in chunk)})"
        )
        for rec in sf.query(soql, include_deleted=True):
            key = external_id_key(rec.get(EXTERNAL_ID_FIELD))
            if key:
                (deleted if rec.get("IsDeleted") else visible)[key] = rec["Id"]

    print(f"Of those ids: {len(visible)} visible to the integration user, "
          f"{len(deleted)} soft-deleted.\n")

    buckets: dict[str, list[dict]] = {}
    for row in failures:
        buckets.setdefault(classify(row, visible, deleted), []).append(row)

    for verdict, rows in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        print(f"=== {verdict}: {len(rows)} of {len(failures)} ===")
        print(f"    {VERDICTS[verdict]}")
        for r in rows[:5]:
            print(f"      PMI {r['Personid']} ({r['Firstname']} {r['Lastname']})"
                  f"  error named {r['blocking_id']}")
        if len(rows) > 5:
            print(f"      ... and {len(rows) - 5} more")
        print()

    retryable = [r for v in RETRYABLE for r in buckets.get(v, [])]
    if retryable:
        print(f"{len(retryable)} record(s) are safe to retry as-is. Ids for "
              f"fetch_members.py --push-salesforce --personids :\n")
        print(",".join(str(r["Personid"]) for r in retryable))
    blocked = len(failures) - len(retryable)
    if blocked:
        print(f"\n{blocked} record(s) need a Salesforce-side fix first (see the "
              f"verdicts above) and are deliberately left out of that list.")


if __name__ == "__main__":
    main()
