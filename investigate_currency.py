"""Tests whether Isactive means "active as of today" or "was active at some
point in this row's historical snapshot" — the latter would explain why our
active-member count (~5,000) is roughly double what the ThoughtSpot UI shows
(~2,500): a person who lapsed years ago but was active in an older row would
still be counted.

For each of our currently-flagged-active persons, checks whether their
LATEST term (by Enddateforterm) has actually not yet expired as of today.

    python investigate_currency.py
"""
from datetime import datetime, timezone
from collections import defaultdict

from thoughtspot_client import ThoughtSpotClient
from fetch_members import get_dataset_id, _column_index, _is_active
import config

FIELDS = ["Personid", "Isactive", "Startdateforterm|daily", "Enddateforterm|daily"]


def main():
    client = ThoughtSpotClient()
    dataset_id = get_dataset_id(client)
    columns, rows = client.search_data(dataset_id, FIELDS)

    idx = {f.split("|")[0]: _column_index(columns, f.split("|")[0]) for f in FIELDS}
    active_idx = idx["Isactive"]
    person_idx = idx["Personid"]
    end_idx = idx["Enddateforterm"]

    active_rows = [r for r in rows if _is_active(r[active_idx], config.TS_ACTIVE_FLAG_VALUE)]

    latest_end_by_person = defaultdict(lambda: None)
    for r in active_rows:
        pid = r[person_idx]
        end = r[end_idx]
        if end is not None and (latest_end_by_person[pid] is None or end > latest_end_by_person[pid]):
            latest_end_by_person[pid] = end

    today_epoch = int(datetime.now(timezone.utc).timestamp())
    print(f"Reference 'today' (epoch seconds): {today_epoch} "
          f"({datetime.now(timezone.utc).date().isoformat()})")

    not_expired = sum(1 for end in latest_end_by_person.values() if end is not None and end >= today_epoch)
    expired = sum(1 for end in latest_end_by_person.values() if end is not None and end < today_epoch)
    no_end_date = sum(1 for end in latest_end_by_person.values() if end is None)

    print(f"\n{len(latest_end_by_person)} distinct 'active' persons (our current logic).")
    print(f"  Latest term NOT yet expired (genuinely current): {not_expired}")
    print(f"  Latest term already expired (stale Isactive flag?): {expired}")
    print(f"  No end date at all: {no_end_date}")


if __name__ == "__main__":
    main()
