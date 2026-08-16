"""Pull chosen fields for active chapter members from ThoughtSpot.

Modes:
  python fetch_members.py --inspect          Pulls a small sample INCLUDING
                                              the Isactive column, unfiltered,
                                              and prints the distinct values
                                              seen. Use this once to confirm
                                              what "active" actually looks
                                              like in the data (1/0? true/
                                              false? Yes/No?) before trusting
                                              the filtered pull below.

  python fetch_members.py                    Pulls all active members' chosen
                                              fields and writes
                                              output/members.csv (or .xlsx /
                                              .json with --format).

  python fetch_members.py --push-salesforce  Same as above, then upserts the
                                              rows into Salesforce Contacts
                                              (see salesforce_client.py for
                                              the field mapping). Test against
                                              a sandbox before running this
                                              against production.

Fields pulled: Personid, Firstname, Lastname, Startdateforterm, Enddateforterm.
Firstname/Lastname are needed because Salesforce requires Contact.LastName on
insert — when a Personid has no existing matching Contact, the upsert creates
one, and a bare record with no name fails REQUIRED_FIELD_MISSING. Add more
names to FIELDS as scope grows — they must match the ThoughtSpot column names
exactly (see the Data Mapping sheet, column B), and salesforce_client.FIELD_MAP
needs a matching entry for anything that should reach Salesforce.

Startdateforterm/Enddateforterm are normalized to plain epoch-seconds ints
in the output (see date_utils.to_epoch_seconds) regardless of whatever raw
shape ThoughtSpot returns, so the export format is stable even if that
changes.

This dataset's grain is (person, membership term) — a long-tenured member
has one row per renewal cycle. dedupe_by_person() collapses that to one row
per person: Startdateforterm = earliest term start across their rows
(original join date), Enddateforterm = latest term end (current term's
expiration).

Isactive does NOT mean "active right now" — confirmed live, it means "was
active during that historical term row." ~5,000 people have some row with
Isactive=True, but only ~2,500 have a term that hasn't actually expired yet
(matching the ThoughtSpot UI's active-member count). filter_currently_active()
is the real "are they active today" check: after dedup, Enddateforterm is
the max across a person's terms, so keeping only rows where that hasn't
passed yet is the correct filter — Isactive alone would badly overcount.
"""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone

import config
from thoughtspot_client import ThoughtSpotClient
from date_utils import to_epoch_seconds, is_plausible_date_epoch

# Plain field names, used as dict/CSV keys and for matching response columns.
FIELDS = ["Personid", "Firstname", "Lastname", "Startdateforterm", "Enddateforterm"]
ACTIVE_FIELD = "Isactive"

# Date columns default to a MONTHLY bucket in ThoughtSpot search unless bound
# explicitly — query tokens (see thoughtspot_client.search_data) request the
# raw day-level value instead.
QUERY_FIELDS = [
    "Personid",
    "Firstname",
    "Lastname",
    "Startdateforterm|daily",
    "Enddateforterm|daily",
    ACTIVE_FIELD,
]


def _column_index(columns: list[str], field: str) -> int:
    """Match a logical field name against the response's actual column
    names, which may come back wrapped (e.g. "Day(Startdateforterm)")
    rather than exactly as requested.

    Tries, in order: exact match; exact match inside a bucket wrapper like
    "Day(field)"/"Month(field)"; and only as a last resort, the single
    column containing the field name anywhere. That loose fallback is
    needed for some real cases but is also a real footgun — e.g.
    "enddateforterm" is a substring of "Day(Membershipenddatefortermforchapter)"
    too ("...enddateforterm" + "forchapter"), so prefer the tighter matches.
    """
    lower = [c.lower() for c in columns]
    target = field.lower()

    if target in lower:
        return lower.index(target)

    wrapped = re.compile(r"^\w+\(" + re.escape(target) + r"\)$")
    wrapped_matches = [i for i, c in enumerate(lower) if wrapped.match(c)]
    if len(wrapped_matches) == 1:
        return wrapped_matches[0]
    if len(wrapped_matches) > 1:
        raise RuntimeError(
            f"Multiple bucket-wrapped matches for {field!r}: "
            f"{[columns[i] for i in wrapped_matches]}"
        )

    matches = [i for i, c in enumerate(lower) if target in c]
    if len(matches) == 1:
        return matches[0]
    raise RuntimeError(
        f"Can't uniquely match {field!r} against response columns {columns}"
    )


def _is_active(value, flag_value: str) -> bool:
    if isinstance(value, bool):
        return value == (str(flag_value).strip().lower() in ("1", "true", "yes", "y"))
    return str(value).strip().lower() == str(flag_value).strip().lower()


def get_dataset_id(client: ThoughtSpotClient) -> str:
    if config.TS_DATASET_ID:
        return config.TS_DATASET_ID
    dataset_id = client.resolve_dataset_id(config.TS_DATASET_NAME)
    print(f"Resolved {config.TS_DATASET_NAME!r} -> {dataset_id}")
    print("Set TS_DATASET_ID in .env to this value to skip this lookup next time.")
    return dataset_id


def inspect(client: ThoughtSpotClient, dataset_id: str):
    columns, rows = client.search_data(dataset_id, QUERY_FIELDS, record_size=20)
    print(f"Columns returned: {columns}")
    idx = _column_index(columns, ACTIVE_FIELD)
    seen = {row[idx] for row in rows}
    print(f"Distinct {ACTIVE_FIELD} values in sample of {len(rows)} rows: {seen}")
    print("Confirm TS_ACTIVE_FLAG_VALUE in .env matches whichever value means 'active'.")
    date_idx = _column_index(columns, "Startdateforterm")
    if rows:
        print(f"Sample Startdateforterm raw value: {rows[0][date_idx]!r}")


DATE_FIELDS = {"Startdateforterm", "Enddateforterm"}


def _write_csv(records: list[dict], path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(records)


def _write_json(records: list[dict], path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)


def _write_xlsx(records: list[dict], path: str):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "members"
    ws.append(FIELDS)
    for r in records:
        ws.append([r[field] for field in FIELDS])
    wb.save(path)


WRITERS = {"csv": (_write_csv, "members.csv"),
           "json": (_write_json, "members.json"),
           "xlsx": (_write_xlsx, "members.xlsx")}


def _min_ignore_none(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _max_ignore_none(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _first_non_null(a, b):
    return a if a not in (None, "") else b


def dedupe_by_person(records: list[dict]) -> list[dict]:
    """One row per Personid: Startdateforterm = MIN across their term rows
    (original join date), Enddateforterm = MAX (current term's expiration).
    Some term rows have a null date (e.g. an open-ended current term with
    no Enddateforterm yet) — those are ignored rather than treated as
    "smaller"/"larger" than a real date. Firstname/Lastname should be
    constant per person, but if a row happens to have one missing, prefer
    whichever row actually has a value.
    """
    by_person: dict = {}
    for r in records:
        pid = r["Personid"]
        if pid not in by_person:
            by_person[pid] = dict(r)
        else:
            existing = by_person[pid]
            existing["Startdateforterm"] = _min_ignore_none(
                existing["Startdateforterm"], r["Startdateforterm"]
            )
            existing["Enddateforterm"] = _max_ignore_none(
                existing["Enddateforterm"], r["Enddateforterm"]
            )
            existing["Firstname"] = _first_non_null(existing["Firstname"], r["Firstname"])
            existing["Lastname"] = _first_non_null(existing["Lastname"], r["Lastname"])
    return list(by_person.values())


def filter_currently_active(records: list[dict]) -> list[dict]:
    """Isactive turns out to mean "was active during that historical term
    row," not "is active right now" — confirmed live: ~5,000 people have
    SOME Isactive=True row, but only ~2,500 have a term that hasn't
    actually expired yet (matching what the ThoughtSpot UI shows for
    active members). After dedupe_by_person(), Enddateforterm is already
    the max across a person's terms, so "not yet expired" on that single
    value is the correct currency check — no need to touch the per-row
    filtering or the dedup logic itself.

    Enddateforterm is epoch seconds at 00:00 UTC of the expiry *date*, so we
    compare against the start of today (also 00:00 UTC), not the current
    instant. That keeps the check inclusive of the expiry day (a member whose
    term ends today is still active today) and makes the result independent of
    what time of day a scheduled run happens to fire.
    """
    today_start = int(
        datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
    # is_plausible_date_epoch rejects None and sentinels/out-of-range values, so
    # this treats a sentinel Enddateforterm the same way to_salesforce_date does
    # (no real date -> not confidently active) instead of comparing raw epochs.
    return [
        r for r in records
        if is_plausible_date_epoch(r["Enddateforterm"]) and r["Enddateforterm"] >= today_start
    ]


def fetch_active(client: ThoughtSpotClient, dataset_id: str, out_format: str = "csv") -> list[dict]:
    columns, rows = client.search_data(dataset_id, QUERY_FIELDS)
    idx = _column_index(columns, ACTIVE_FIELD)

    active_rows = [row for row in rows if _is_active(row[idx], config.TS_ACTIVE_FLAG_VALUE)]
    print(f"{len(rows)} total rows fetched, {len(active_rows)} match active flag.")

    raw_records = []
    for row in active_rows:
        record = {}
        for field in FIELDS:
            value = row[_column_index(columns, field)]
            record[field] = to_epoch_seconds(value) if field in DATE_FIELDS else value
        raw_records.append(record)

    distinct_count = len({r["Personid"] for r in raw_records})
    print(f"{len(raw_records)} term rows across {distinct_count} distinct members.")

    deduped = dedupe_by_person(raw_records)
    print(f"Deduped to {len(deduped)} rows (one per person; join=earliest term start, "
          f"expiration=latest term end).")

    records = filter_currently_active(deduped)
    print(f"{len(deduped) - len(records)} of those have an already-expired latest term "
          f"(stale Isactive flag from an old snapshot) — dropped. {len(records)} currently active.")

    write_fn, filename = WRITERS[out_format]
    os.makedirs("output", exist_ok=True)
    out_path = os.path.join("output", filename)
    write_fn(records, out_path)
    print(f"Wrote {len(records)} rows to {out_path}")

    return records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument(
        "--push-salesforce",
        action="store_true",
        help="After fetching, upsert the rows into Salesforce Contacts.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only push the first N fetched records to Salesforce. "
        "For testing --push-salesforce safely before a full run.",
    )
    parser.add_argument(
        "--format",
        choices=["csv", "xlsx", "json"],
        default="csv",
        help="Output file format for output/members.<ext> (default: csv).",
    )
    args = parser.parse_args()

    client = ThoughtSpotClient()
    dataset_id = get_dataset_id(client)

    if args.inspect:
        inspect(client, dataset_id)
        return

    records = fetch_active(client, dataset_id, out_format=args.format)

    if args.push_salesforce:
        from salesforce_client import SalesforceClient
        from reporting import report_upsert

        push_records = records[: args.limit] if args.limit is not None else records
        if args.limit is not None:
            print(f"--limit {args.limit}: pushing {len(push_records)} of {len(records)} records.")
        results = SalesforceClient().upsert_contacts(push_records)
        failure_count = report_upsert(push_records, results)
        # Non-zero exit so a scheduler flags the run as failed (see reporting.py).
        sys.exit(1 if failure_count else 0)


if __name__ == "__main__":
    main()
