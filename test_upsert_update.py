"""One-off test of the upsert UPDATE / matching path (not part of the pipeline).

Proves that records keyed by Personid (PMI ID) correctly MATCH and UPDATE an
existing Salesforce Contact — rather than creating a duplicate — while
genuinely-new PMI IDs INSERT, both in a single upsert call.

Data comes from the real ThoughtSpot active pull:
  - UPDATE set = active records whose PMI ID already exists in Salesforce.
    Marked with dummy data so a real update is visible: FirstName gets a
    '_test' suffix, and Chapter_Join_Date__c is set to 2000-01-01.
  - INSERT set = 5 active records whose PMI ID is not yet in Salesforce.

Proof of matching: Salesforce's composite-upsert response returns `created`
per record — False = matched/updated, True = inserted. The update set must all
come back created=False, the insert set all created=True.

Run: python test_upsert_update.py
"""
import requests

import config
from thoughtspot_client import ThoughtSpotClient
from salesforce_client import SalesforceClient
from fetch_members import get_dataset_id, fetch_active

DUMMY_JOIN_EPOCH = 946684800  # 2000-01-01 00:00 UTC — obvious test marker
INSERT_COUNT = 5


def sf_query(sf: SalesforceClient, soql: str) -> list[dict]:
    """Run a SOQL query (mirrors the inline pattern in test_sf_auth.py)."""
    resp = requests.get(
        f"{sf._instance_url}/services/data/{config.SF_API_VERSION}/query",
        headers=sf._headers(),
        params={"q": soql},
    )
    resp.raise_for_status()
    return resp.json()["records"]


def main():
    # 1. Active deduped records from ThoughtSpot.
    ts = ThoughtSpotClient()
    dataset_id = get_dataset_id(ts)
    records = fetch_active(ts, dataset_id)
    by_id = {str(r["Personid"]): r for r in records}

    # 2. Existing Contacts in Salesforce, keyed by Membership_ID__c (a string).
    sf = SalesforceClient()
    sf.authenticate()
    existing_rows = sf_query(
        sf,
        "SELECT Id, Membership_ID__c, FirstName FROM Contact "
        "WHERE Membership_ID__c != null",
    )
    existing_ids = {row["Membership_ID__c"] for row in existing_rows}
    print(f"\n{len(existing_ids)} existing Contacts in Salesforce: {sorted(existing_ids)}")

    missing = sorted(i for i in existing_ids if i not in by_id)
    if missing:
        print(f"Existing SF IDs absent from the active pull (expired / filtered out): {missing}")

    # 3. Split into update (already in SF) and insert (new) sets.
    update_set = []
    for pid in (p for p in by_id if p in existing_ids):
        r = dict(by_id[pid])
        fn = r.get("Firstname") or ""
        r["Firstname"] = fn if fn.endswith("_test") else fn + "_test"
        r["Startdateforterm"] = DUMMY_JOIN_EPOCH
        update_set.append(r)

    insert_set = [dict(by_id[pid]) for pid in by_id if pid not in existing_ids][:INSERT_COUNT]

    batch = update_set + insert_set
    print(f"\nBatch: {len(update_set)} updates + {len(insert_set)} inserts = {len(batch)} records")
    for r in update_set:
        print(f"  UPDATE  PMI {r['Personid']}  FirstName -> {r['Firstname']!r}  join_date -> 2000-01-01")
    for r in insert_set:
        print(f"  INSERT  PMI {r['Personid']}  {r.get('Firstname')} {r.get('Lastname')}")

    if not update_set:
        print("\nNo update-set records found — can't test the update path. "
              "Run a --push-salesforce --limit 5 first so some Contacts exist.")
        return

    # 4. Upsert the combined batch.
    results = sf.upsert_contacts(batch)

    # 5. Verify created flags: expected created == (pid not already in SF).
    print("\nResult verification (created=False means matched+updated, True means inserted):")
    all_ok = True
    for r, res in zip(batch, results):
        pid = str(r["Personid"])
        expected_created = pid not in existing_ids
        created = res.get("created")
        ok = bool(res.get("success")) and created == expected_created
        all_ok = all_ok and ok
        kind = "insert" if created else "update"
        print(f"  PMI {pid}: success={res.get('success')} created={created} ({kind}) "
              f"{'OK' if ok else 'UNEXPECTED'}")
        if not res.get("success"):
            print(f"      errors: {res.get('errors')}")

    print("\nPASS — matching worked as expected." if all_ok
          else "\nFAIL — see UNEXPECTED rows above.")

    # 6. Re-query the batch to confirm final state + no duplicates.
    ids_csv = ",".join(f"'{r['Personid']}'" for r in batch)
    final = sf_query(
        sf,
        "SELECT Membership_ID__c, FirstName, Chapter_Join_Date__c FROM Contact "
        f"WHERE Membership_ID__c IN ({ids_csv})",
    )
    counts = {}
    for row in final:
        counts[row["Membership_ID__c"]] = counts.get(row["Membership_ID__c"], 0) + 1
    dupes = {k: v for k, v in counts.items() if v > 1}

    print(f"\nFinal state in Salesforce ({len(final)} Contacts for {len(batch)} PMI IDs):")
    for row in sorted(final, key=lambda r: r["Membership_ID__c"]):
        print(f"  PMI {row['Membership_ID__c']}: {row['FirstName']!r}  "
              f"join_date={row['Chapter_Join_Date__c']}")
    if dupes:
        print(f"\nWARNING: duplicate Contacts for PMI IDs {dupes} — matching FAILED.")
    else:
        print("\nEach PMI ID resolves to exactly one Contact — no duplicates.")


if __name__ == "__main__":
    main()
