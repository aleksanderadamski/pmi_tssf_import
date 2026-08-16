"""One-off investigation, not part of the pipeline:

1. Does Isactive=true include people with no chapter membership at all
   (active only via certification)? Tests that against Ishaschapter.
2. Are Membershiporiginaljoindateforchapter / Expirationdateforchapter /
   Membershipenddatefortermforchapter already a single stable value per
   person, unlike Startdateforterm/Enddateforterm (which vary per term
   row)? If so, they may be a cleaner source than our manual dedup.

    python investigate_fields.py
"""
from collections import defaultdict

from thoughtspot_client import ThoughtSpotClient
from fetch_members import get_dataset_id, _column_index, _is_active
import config

FIELDS = [
    "Personid",
    "Isactive",
    "Ishaschapter",
    "Startdateforterm|daily",
    "Enddateforterm|daily",
    "Membershiporiginaljoindateforchapter|daily",
    "Expirationdateforchapter|daily",
    "Membershipenddatefortermforchapter|daily",
]


def main():
    client = ThoughtSpotClient()
    dataset_id = get_dataset_id(client)
    columns, rows = client.search_data(dataset_id, FIELDS)
    print(f"{len(rows)} total rows fetched.\n")

    idx = {f.split("|")[0]: _column_index(columns, f.split("|")[0]) for f in FIELDS}

    active_idx = idx["Isactive"]
    active_rows = [r for r in rows if _is_active(r[active_idx], config.TS_ACTIVE_FLAG_VALUE)]
    print(f"{len(active_rows)} active rows.")

    # 1. Ishaschapter breakdown among active rows / active distinct persons
    haschapter_idx = idx["Ishaschapter"]
    person_idx = idx["Personid"]

    by_person_haschapter = defaultdict(set)
    for r in active_rows:
        by_person_haschapter[r[person_idx]].add(r[haschapter_idx])

    true_only = sum(1 for v in by_person_haschapter.values() if v == {True})
    false_only = sum(1 for v in by_person_haschapter.values() if v == {False})
    mixed = sum(1 for v in by_person_haschapter.values() if len(v) > 1)
    print(f"\nDistinct active persons: {len(by_person_haschapter)}")
    print(f"  Ishaschapter always True:  {true_only}")
    print(f"  Ishaschapter always False: {false_only}  <- active but no chapter membership?")
    print(f"  Mixed True/False across their rows: {mixed}")

    # 2. Stability of the "forchapter" date fields per person, vs Startdateforterm/Enddateforterm
    for field in ("Startdateforterm", "Enddateforterm", "Membershiporiginaljoindateforchapter",
                  "Expirationdateforchapter", "Membershipenddatefortermforchapter"):
        f_idx = idx[field]
        by_person_vals = defaultdict(set)
        for r in active_rows:
            by_person_vals[r[person_idx]].add(r[f_idx])
        multi = sum(1 for v in by_person_vals.values() if len(v) > 1)
        print(f"\n{field}: {multi} of {len(by_person_vals)} persons have >1 distinct value "
              f"across their active rows (0 = stable per person).")

    # Sample rows for a person with multiple active rows, to eyeball directly
    multi_person = next((pid for pid, vals in by_person_haschapter.items()), None)
    sample_rows = [r for r in active_rows if r[person_idx] == multi_person]
    if len(sample_rows) > 1:
        print(f"\nSample: all active rows for Personid {multi_person}:")
        for r in sample_rows:
            print("  " + ", ".join(f"{f}={r[idx[f]]!r}" for f in idx))


if __name__ == "__main__":
    main()
