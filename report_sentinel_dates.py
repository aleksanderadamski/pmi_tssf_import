"""Measure whether omitting empty values leaves stale data in Salesforce.

    python report_sentinel_dates.py

STRICTLY READ-ONLY. Nothing is written to Salesforce.

Background: the sync omits an empty value from the write payload instead of
sending null, so it can never blank a field someone filled in by hand. The open
question is whether that ever leaves Salesforce asserting something the source
contradicts. ThoughtSpot encodes "no date" as the sentinel epoch -2208988800
(1900-01-01), which reaches the payload as "omit this field".

The domain argument says this is fine for expiry dates: a PMP that expired
STAYS expired until the member renews, at which point the source supplies a new
real date and the sync writes it. An old date in Salesforce is therefore usually
correct. This script tests that rather than assuming it.

It reports, in order:

  1. What the source actually sends, at BOTH grains. Post-dedupe is what the
     sync writes; raw per-term rows matter because dedupe_by_person collapses
     Pmpexpiredate with MAX, and the sentinel is numerically smaller than any
     real epoch — so a member with one sentinel row and one real row is deduped
     to the real date and would vanish from a post-dedupe-only count. Note the
     MIN merges (Startdateforterm, Pmporiginalgrantdate) do the OPPOSITE: the
     sentinel wins and discards the real date. That is a live bug — see
     NEXT_STEPS §13.
  2. The distinct raw values behind every "would be omitted" bucket, so
     "is the sentinel really -2208988800?" is answered from data, not memory.
  3. What a sentinel expiry co-occurs with, and what the co-occurring values
     actually ARE. Measured 2026-09-18: the sentinel lands on all three PMP
     date fields at once, which rules out "this credential never expires" (a
     non-expiring cert would still have a grant date). The remaining reading is
     "no dates recorded", but confirming it needs more than a non-empty check —
     a *pipeline* status may mean "In Progress", and a certification list may
     hold only CAPM/ACP. So this section also prints the distinct status
     values, whether PMP is actually in the list, and how many distinct PEOPLE
     are behind the row count.
  4. The number that decides the question: Contacts where the source would omit
     a field but Salesforce currently holds a value — checked for EVERY mapped
     field, not just the expiry, because omit-empties applies to all of them.
     A stale text field is the sharper case: "expired 2019" is old but still
     self-dating and true, whereas a Certifications__c still reading "PMP"
     after the credential was removed upstream is an undated false claim that
     no future sync will correct.

Scope limits, stated because they bound the conclusion:
  - Only CURRENTLY-ACTIVE members are examined. A lapsed member's Contact still
    holds whatever the last sync wrote and is not counted here.
  - Contacts the integration user cannot see are reported as UNKNOWN, never
    folded into a zero. See the visibility gotcha in CLAUDE.md.

Side effects (both gitignored): refreshes output/members.csv — which overwrites
the artifact of whatever the last push sent — and writes
output/sentinel_conflicts_<UTC>.csv when conflicts exist.
"""
import csv
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone

import config
from thoughtspot_client import ThoughtSpotClient
from fetch_members import (
    get_dataset_id, fetch_active, dedupe_by_person,
    QUERY_FIELDS, FIELDS, ACTIVE_FIELD, DATE_FIELDS,
    _column_index, _is_active,
)
from date_utils import to_epoch_seconds, to_salesforce_date, is_plausible_date_epoch
from salesforce_client import (
    SalesforceClient, EXTERNAL_ID_FIELD, FIELD_MAP, QUERY_CHUNK_SIZE,
    external_id_key, _chunks, _soql_quote,
)

PMP_DATE_FIELDS = ["Pmpstartdate", "Pmpexpiredate", "Pmporiginalgrantdate"]
# In-window but implausibly early: a second "no date" encoding (e.g. epoch 0 =
# 1970-01-01) would pass is_plausible_date_epoch and be WRITTEN, so surface it.
SUSPICIOUS_BEFORE = 631152000  # 1990-01-01


def would_omit(ts_field: str, raw):
    """Exactly the production rule from salesforce_client.upsert_contacts:
    apply the FIELD_MAP transform, then omit None / blank strings.
    """
    transform = FIELD_MAP[ts_field][1]
    value = transform(raw) if transform else raw
    return value is None or (isinstance(value, str) and not value.strip())


def fetch_raw_rows(client, dataset_id):
    """Per-term rows before dedupe_by_person collapses them."""
    columns, rows = client.search_data(dataset_id, QUERY_FIELDS)
    idx = _column_index(columns, ACTIVE_FIELD)
    active = [r for r in rows if _is_active(r[idx], config.TS_ACTIVE_FLAG_VALUE)]
    out = []
    for row in active:
        rec = {}
        for field in FIELDS:
            v = row[_column_index(columns, field)]
            rec[field] = to_epoch_seconds(v) if field in DATE_FIELDS else v
        out.append(rec)
    return out


def bucket(ts_field, raw):
    if would_omit(ts_field, raw):
        return "omitted" if raw not in (None, "") else "empty"
    return "written"


def main():
    ts = ThoughtSpotClient()
    dataset_id = get_dataset_id(ts)
    raw_rows = fetch_raw_rows(ts, dataset_id)
    records = fetch_active(ts, dataset_id)

    print(f"\n{'=' * 72}")
    print(f"{len(raw_rows)} raw term rows -> {len(records)} currently-active members")
    print(f"{'=' * 72}")

    # --- 1. What the source sends, at both grains ---------------------------
    print("\n### 1. What ThoughtSpot sends for the PMP date fields\n")
    print(f"  {'field':<24} {'grain':<11} {'written':>8} {'omitted':>8} {'empty':>8}")
    for field in PMP_DATE_FIELDS:
        for label, data in (("raw rows", raw_rows), ("per-member", records)):
            c = Counter(bucket(field, r.get(field)) for r in data)
            print(f"  {field:<24} {label:<11} {c['written']:>8} "
                  f"{c['omitted']:>8} {c['empty']:>8}")
    print("\n  'omitted' = a non-empty source value that still reaches Salesforce as")
    print("  'skip this field' (the sentinel, or anything unparseable).")
    print("  Raw-row 'omitted' high but per-member 0 has TWO explanations: dedupe's")
    print("  MAX masked the sentinel behind a real date, OR those members were")
    print("  dropped by the currency filter. This report cannot tell them apart.")
    print("  And for Pmporiginalgrantdate dedupe uses MIN, where the sentinel WINS")
    print("  and discards the real date — a per-member non-zero there is the bug in")
    print("  NEXT_STEPS §13, not an anomaly in the source.")

    # --- 2. The actual raw values behind 'omitted' ---------------------------
    print("\n### 2. Distinct raw values that get omitted (is it really the sentinel?)\n")
    found_any = False
    for field in PMP_DATE_FIELDS + [f for f in FIELD_MAP if f not in PMP_DATE_FIELDS]:
        vals = Counter(
            repr(r.get(field)) for r in raw_rows
            if field in r and would_omit(field, r.get(field)) and r.get(field) not in (None, "")
        )
        if vals:
            found_any = True
            print(f"  {field}:")
            for v, n in vals.most_common(5):
                note = "  <- the documented 1900-01-01 sentinel" if v == "-2208988800" else ""
                print(f"      {v:>16} x{n}{note}")
    if not found_any:
        print("  None. Every omitted value is genuinely empty/null — the sentinel\n"
              "  does not appear in this data at all.")

    # Second-encoding check (finding: epoch 0 would be WRITTEN, not omitted)
    print("\n  In-window but suspiciously early dates (would be WRITTEN as-is):")
    odd = Counter()
    for field in PMP_DATE_FIELDS:
        for r in raw_rows:
            v = r.get(field)
            if is_plausible_date_epoch(v) and v < SUSPICIOUS_BEFORE:
                odd[(field, to_salesforce_date(v))] += 1
    if odd:
        for (field, d), n in odd.most_common(10):
            print(f"      {field} = {d} x{n}   <- possible second 'no date' encoding")
    else:
        print("      none")

    # --- 3. What a sentinel expiry co-occurs with ----------------------------
    sentinel_rows = [r for r in raw_rows
                     if would_omit("Pmpexpiredate", r.get("Pmpexpiredate"))
                     and r.get("Pmpexpiredate") not in (None, "")]
    print(f"\n### 3. What a sentinel PMP expiry co-occurs with "
          f"({len(sentinel_rows)} raw rows)\n")
    if not sentinel_rows:
        print("  No row carries a sentinel PMP expiry in this pull — so nothing here\n"
              "  constrains what the sentinel means. (It was present on 9 rows as of\n"
              "  2026-09-18; absence now means the source changed, not that the\n"
              "  question is closed.)")
    else:
        combos = Counter()
        for r in sentinel_rows:
            combos[(
                "status" if str(r.get("Pmppipelinestatus") or "").strip() else "no status",
                "start" if is_plausible_date_epoch(r.get("Pmpstartdate")) else "no start",
                "certs" if str(r.get("Certificationlist") or "").strip() else "no certs",
            )] += 1
        for (a, b, c), n in combos.most_common():
            print(f"  {n:>6}  {a:<10} {b:<9} {c}")
        people = {r["Personid"] for r in sentinel_rows}
        print(f"\n  Those {len(sentinel_rows)} rows are {len(people)} distinct person(s) "
              f"— the dataset is (person, term) grain, so rows overstate people.")

        print("\n  Distinct Pmppipelinestatus values on those rows:")
        for v, n in Counter(r.get("Pmppipelinestatus") for r in sentinel_rows).most_common():
            print(f"      {v!r} x{n}")
        print("  (a *pipeline* status like 'In Progress' would mean the member does NOT")
        print("   hold the credential — which changes what the sentinel means.)")

        print("\n  Does Certificationlist actually contain PMP?")
        pmp = Counter(
            "PMP present" if "PMP" in str(r.get("Certificationlist") or "").upper()
            else "no PMP in list"
            for r in sentinel_rows
        )
        for v, n in pmp.most_common():
            print(f"      {v}: {n}")
        print("  (a list holding only CAPM/PMI-ACP scores as 'certs' above but does not")
        print("   evidence a PMP.)")

        print("\n  Reading it: all three PMP dates carrying the sentinel together rules")
        print("  out 'never expires' — a non-expiring cert would still have a grant")
        print("  date. Whether the rest reads as 'no dates recorded' depends on the two")
        print("  checks above. Omitting is correct either way: never write a placeholder.")

    # --- 4. The number that decides it ---------------------------------------
    print("\n### 4. Contacts where Salesforce holds a value the source would omit\n")
    omit_by_member = defaultdict(list)
    for r in records:
        for ts_field in FIELD_MAP:
            if ts_field in r and would_omit(ts_field, r.get(ts_field)):
                omit_by_member[external_id_key(r["Personid"])].append(ts_field)
    omit_by_member.pop("", None)

    if not omit_by_member:
        print("  No active member has ANY omitted field. Nothing can be stale.")
        return

    sf = SalesforceClient()
    sf_fields = [FIELD_MAP[f][0] for f in FIELD_MAP]
    wanted = sorted(omit_by_member)
    current, dup_keys = {}, set()
    for chunk in _chunks(wanted, QUERY_CHUNK_SIZE):
        soql = (
            f"SELECT Id, {EXTERNAL_ID_FIELD}, {', '.join(sf_fields)} FROM Contact "
            f"WHERE {EXTERNAL_ID_FIELD} IN ({','.join(_soql_quote(k) for k in chunk)})"
        )
        for rec in sf.query(soql):
            key = external_id_key(rec.get(EXTERNAL_ID_FIELD))
            if not key:
                continue
            if key in current and current[key]["Id"] != rec["Id"]:
                dup_keys.add(key)  # same ambiguity fetch_existing_contact_ids refuses
                continue
            current[key] = rec

    unresolved = [k for k in wanted if k not in current and k not in dup_keys]
    print(f"  {len(wanted)} member(s) have at least one omitted field.")
    print(f"  {len(current)} resolved in Salesforce, {len(unresolved)} NOT VISIBLE, "
          f"{len(dup_keys)} ambiguous.")

    by_member = {r["Personid"]: r for r in records}
    conflicts = []
    for key, ts_fields in omit_by_member.items():
        contact = current.get(key)
        if not contact:
            continue
        for ts_field in ts_fields:
            sf_field = FIELD_MAP[ts_field][0]
            sf_value = contact.get(sf_field)
            if sf_value not in (None, ""):
                conflicts.append((key, contact["Id"], ts_field, sf_field, sf_value))

    # Never report a clean zero on incomplete coverage.
    if unresolved or dup_keys:
        print(f"\n  *** RESULT IS INCOMPLETE ***")
        print(f"  {len(unresolved) + len(dup_keys)} member(s) could not be checked: "
              f"{len(unresolved)} invisible to the integration user, "
              f"{len(dup_keys)} matching multiple Contacts.")
        print(f"  Their Salesforce values are UNKNOWN, not zero. This is the same")
        print(f"  visibility gap that cost the 209-record production run — see")
        print(f"  CLAUDE.md and diagnose_duplicates.py. Grant View All (or fix the")
        print(f"  ambiguous ids) and re-run before treating any count below as final.")

    if not conflicts:
        scope = "Among the members that could be checked" if (unresolved or dup_keys) \
            else "Across every active member"
        print(f"\n  0 conflicts. {scope}, every field the source would omit is\n"
              f"  already empty in Salesforce — so omitting it changes nothing and\n"
              f"  the current behaviour is correct.")
        return

    print(f"\n  {len(conflicts)} field-level conflict(s):\n")
    per_field = Counter(sf_field for _, _, _, sf_field, _ in conflicts)
    for sf_field, n in per_field.most_common():
        print(f"    {sf_field:<32} {n:>5}")
    print()
    for key, cid, ts_field, sf_field, sf_value in conflicts[:10]:
        r = by_member.get(key) or by_member.get(int(key) if key.isdigit() else key, {})
        print(f"    PMI {key} ({r.get('Firstname')} {r.get('Lastname')}): "
              f"{sf_field} = {sf_value!r}, source says omit")
    if len(conflicts) > 10:
        print(f"    ... and {len(conflicts) - 10} more")

    os.makedirs("output", exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join("output", f"sentinel_conflicts_{stamp}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Personid", "ContactId", "Firstname", "Lastname",
                    "salesforce_field", "salesforce_value",
                    "thoughtspot_field", "thoughtspot_raw"])
        for key, cid, ts_field, sf_field, sf_value in conflicts:
            r = by_member.get(key) or by_member.get(int(key) if key.isdigit() else key, {})
            w.writerow([key, cid, r.get("Firstname"), r.get("Lastname"),
                        sf_field, sf_value, ts_field, repr(r.get(ts_field))])
    print(f"\n  Written to {path} (ContactId included so each row is actionable).")
    print("  A date field that is merely OLD is usually correct — an expired cert")
    print("  stays expired. Scrutinise the TEXT fields first: those assert a")
    print("  credential with no date attached, and nothing will ever correct them.")


if __name__ == "__main__":
    main()
