"""Verify a completed push actually landed, across every record (read-only).

    python verify_push.py

STRICTLY READ-ONLY. Nothing is written to Salesforce.

A clean "Wrote 2464/2464" only means Salesforce ACCEPTED the writes. It does not
mean the values are right, and spot-checking a few Contacts in the UI cannot
cover thousands of records. This closes that gap: for every value the last push
would have sent, it checks the Contact actually holds it.

The baseline is output/pushed_<UTC>.csv, the manifest the push writes of the
records it submitted (reporting.write_push_manifest). It is NOT members.csv —
that one is the whole fetched set, written before --limit/--personids filtering,
so after a partial push it names records that were never sent. If no manifest
exists the script falls back to members.csv and says so loudly.

Three checks:

  1. Sentinel leak. No Date field should hold 1900-01-01: the placeholder
     converts to None and is omitted, so this sync cannot write it. A hit means
     hand entry, a legacy load, or another integration. Queried across ALL
     Contacts, not just this run — so a PASS here is close to tautological for
     the sync itself, and is really an assertion about the org.

  2. Every value the push sent is compared against the Contact it resolves to.
     Fails on a value mismatch, on a submitted member with NO Contact, and on
     one matching MORE than one Contact (comparing against a guessed duplicate
     could pass a member the push never wrote).

     ONE EXCEPTION: a value stored exactly lower-cased is reported as INFO, not
     failed — see LOWERCASED_BY_SALESFORCE. That is a real blind spot, not just
     cosmetic tidying: for those fields an already-lower-case stored value
     compares equal whether or not the write applied, so the mixed-case records
     were the only ones that could detect a silently-ignored write. The run
     prints whether any mixed-case value survived intact, which is the evidence
     that distinguishes "normalized on save" from "never written".

  3. Coverage. How many Contacts carry each certification field, so a field
     that silently landed empty everywhere is visible as a zero.

Fields the source would OMIT are skipped by design — report_sentinel_dates.py
covers those. See "What this does NOT prove" in the output for the gap neither
script closes.
"""
import csv
import glob
import os
import sys
from collections import Counter
from datetime import datetime, timezone

import fetch_members
from date_utils import to_salesforce_date
from salesforce_client import (
    SalesforceClient, EXTERNAL_ID_FIELD, FIELD_MAP, QUERY_CHUNK_SIZE,
    external_id_key, _chunks, _soql_quote,
)

MEMBERS_CSV = os.path.join("output", "members.csv")
SENTINEL_DATE = "1900-01-01"
DATE_SF_FIELDS = [sf for _, (sf, tf) in FIELD_MAP.items() if tf is to_salesforce_date]

# Identity test above silently drops a field if FIELD_MAP ever wraps the
# transform (a partial, a lambda), which would stop check 1 scanning it while
# still reporting PASS. Fail loudly instead.
if len(DATE_SF_FIELDS) != len(fetch_members.DATE_FIELDS):
    sys.exit(
        f"Date-field derivation is out of step: found {len(DATE_SF_FIELDS)} "
        f"Salesforce date fields but fetch_members.DATE_FIELDS has "
        f"{len(fetch_members.DATE_FIELDS)}. Check 1 would silently skip the "
        f"difference — fix the derivation before trusting this script."
    )


def expected_value(ts_field, raw):
    """What the sync would have sent for this field, or None if it omitted it.

    Mirrors salesforce_client.upsert_contacts: apply the FIELD_MAP transform,
    then treat None/blank as "omitted".
    """
    transform = FIELD_MAP[ts_field][1]
    value = transform(raw) if transform else raw
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return value


# Fields observed to come back LOWER-CASED from Salesforce, where that is the
# same value and not a failed write (72 of 2,464 on the 2026-09-18 run). The
# test below accepts lower-casing specifically, not any case permutation: an
# UPPER-cased value is not the behaviour we observed and would mean the stored
# value came from somewhere other than this push, so it still fails.
LOWERCASED_BY_SALESFORCE = {"Email"}


def compare(sf_field, want, got) -> str:
    """'match', 'case-only', or 'mismatch'.

    The `got is None` branch is load-bearing: a plain str() comparison would
    equate a real Python None from Salesforce with the literal string "None",
    so a member whose Pmppipelinestatus is genuinely "None" would pass even if
    the field had never been written.

    Values are stripped before comparing because Salesforce trims text on save,
    so a trailing space we sent would otherwise read as a failed write. Dates
    need no special handling: Salesforce returns the same YYYY-MM-DD string
    to_salesforce_date produces.

    'lowercased' is reported but not failed, so a server-side normalization
    stays visible instead of being silently swallowed by a lenient compare.
    """
    if got is None:
        return "mismatch"  # we sent a value; an empty field is never a match
    want_s, got_s = str(want).strip(), str(got).strip()
    if got_s == want_s:
        return "match"
    if sf_field in LOWERCASED_BY_SALESFORCE and got_s == want_s.lower():
        return "lowercased"
    return "mismatch"


def newest_manifest():
    """The most recent push manifest, or None if no push has written one."""
    found = sorted(glob.glob(os.path.join("output", "pushed_*.csv")))
    return found[-1] if found else None


def load_sent():
    """The records the last push submitted, preferring its manifest.

    Falls back to output/members.csv, which is the whole FETCHED set written
    before --limit/--personids filtering — so after a partial push it names
    records that were never sent, and every one of them would be reported as a
    mismatch. Warn loudly rather than quietly compare against the wrong thing.
    """
    path = newest_manifest()
    if path is None:
        if not os.path.exists(MEMBERS_CSV):
            sys.exit("No output/pushed_*.csv manifest and no output/members.csv "
                     "— run a push first.")
        path = MEMBERS_CSV
        print(f"WARNING: no push manifest found, falling back to {MEMBERS_CSV}.\n"
              f"  That file is the whole fetched set, written BEFORE the push and\n"
              f"  before --limit/--personids filtering. If the last push was\n"
              f"  partial, records that were never sent will be reported as\n"
              f"  mismatches. Re-run the push to get a manifest.\n")
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(
        os.path.getmtime(path), tz=timezone.utc)
    print(f"Baseline: {path} (written {int(age.total_seconds() // 60)} min ago)")
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    sent = load_sent()
    print(f"{len(sent)} record(s) submitted by that run\n")
    sf = SalesforceClient()

    # --- 1. Sentinel leak, across every Contact in the org -------------------
    print("### 1. Sentinel dates in Salesforce\n")
    clauses = " OR ".join(f"{f} = {SENTINEL_DATE}" for f in DATE_SF_FIELDS)
    leaked = sf.query(
        f"SELECT Id, {EXTERNAL_ID_FIELD}, {', '.join(DATE_SF_FIELDS)} "
        f"FROM Contact WHERE {clauses}"
    )
    if not leaked:
        print(f"  PASS — no Contact holds {SENTINEL_DATE} in any of: "
              f"{', '.join(DATE_SF_FIELDS)}")
    else:
        print(f"  FAIL — {len(leaked)} Contact(s) hold the {SENTINEL_DATE} "
              f"placeholder. This sync structurally cannot write it (to_salesforce_date "
              f"maps it to None and the write omits None), so look first at hand "
              f"entry in the UI, a legacy data load, or another integration:")
        for r in leaked[:10]:
            hits = [f for f in DATE_SF_FIELDS if r.get(f) == SENTINEL_DATE]
            print(f"      PMI {r.get(EXTERNAL_ID_FIELD)} ({r['Id']}): {', '.join(hits)}")

    # --- 2/3. Compare what was sent against what is stored -------------------
    print("\n### 2. Does Salesforce hold what the run sent?\n")
    by_key, blank_id = {}, 0
    for row in sent:
        key = external_id_key(row.get("Personid"))
        if not key:
            blank_id += 1
            continue
        by_key[key] = row
    dropped = len(sent) - len(by_key) - blank_id
    if blank_id or dropped:
        print(f"  NOTE: {blank_id} row(s) had no Personid and {dropped} duplicate "
              f"Personid row(s) collapsed — {len(by_key)} unique of {len(sent)}.")

    sf_fields = sorted({sf_field for sf_field, _ in FIELD_MAP.values()})
    stored, ambiguous = {}, set()
    for chunk in _chunks(sorted(by_key), QUERY_CHUNK_SIZE):
        soql = (
            f"SELECT Id, {EXTERNAL_ID_FIELD}, {', '.join(sf_fields)} FROM Contact "
            f"WHERE {EXTERNAL_ID_FIELD} IN ({','.join(_soql_quote(k) for k in chunk)})"
        )
        for rec in sf.query(soql):
            key = external_id_key(rec.get(EXTERNAL_ID_FIELD))
            if not key:
                continue
            # Same refusal-to-guess as fetch_existing_contact_ids: comparing
            # against whichever duplicate came back last could "pass" a member
            # the push never wrote.
            if key in stored and stored[key]["Id"] != rec["Id"]:
                ambiguous.add(key)
                stored.pop(key, None)
                continue
            if key not in ambiguous:
                stored[key] = rec

    missing = [k for k in by_key if k not in stored and k not in ambiguous]
    mismatches, lowercased, checked = [], [], 0
    # Evidence for/against "something lower-cases these fields": if a value we
    # sent with an uppercase letter came back byte-identical, then nothing is
    # normalizing, and the lower-cased ones need a different explanation.
    mixed_case_survived = []
    for key, row in by_key.items():
        contact = stored.get(key)
        if not contact:
            continue
        for ts_field, (sf_field, transform) in FIELD_MAP.items():
            raw = row.get(ts_field)
            # CSV round-trips everything as strings; restore ints for the date
            # fields only — a text value like "007" must not become 7.
            if (transform is to_salesforce_date and raw not in (None, "")
                    and str(raw).lstrip("-").isdigit()):
                raw = int(raw)
            want = expected_value(ts_field, raw)
            if want is None:
                continue  # omitted by design — see the coverage note below
            checked += 1
            got = contact.get(sf_field)
            verdict = compare(sf_field, want, got)
            if verdict == "mismatch":
                mismatches.append((key, sf_field, want, got))
            elif verdict == "lowercased":
                lowercased.append((key, sf_field, want, got))
            elif (verdict == "match" and sf_field in LOWERCASED_BY_SALESFORCE
                  and any(c.isupper() for c in str(want))):
                mixed_case_survived.append((key, sf_field, want))

    print(f"  {len(stored)} of {len(by_key)} submitted members resolved in Salesforce.")
    print(f"  {checked} written field value(s) compared.")
    if missing:
        print(f"\n  FAIL — {len(missing)} submitted member(s) have NO Contact at all: "
              f"{missing[:10]}" + (" ..." if len(missing) > 10 else ""))
        print("  The push reported writing them; Salesforce has no such record.")
    if ambiguous:
        print(f"\n  FAIL — {len(ambiguous)} member(s) match MORE than one Contact: "
              f"{sorted(ambiguous)[:10]}")
        print("  Not compared: whichever duplicate we picked could pass or fail "
              "by accident. Resolve the duplicates in Salesforce.")
    if mismatches:
        print(f"\n  FAIL — {len(mismatches)} value mismatch(es):")
        for key, field, want, got in mismatches[:15]:
            print(f"      PMI {key}: {field} sent {want!r}, stored {got!r}")
        if len(mismatches) > 15:
            print(f"      ... and {len(mismatches) - 15} more")
        print(f"\n  By field: {dict(Counter(f for _, f, _, _ in mismatches))}")
    if lowercased:
        by_f = Counter(f for _, f, _, _ in lowercased)
        print(f"\n  INFO — {len(lowercased)} value(s) stored LOWER-CASED, not failed: "
              f"{dict(by_f)}")
        for key, field, want, got in lowercased[:3]:
            print(f"      PMI {key}: {field} sent {want!r}, stored {got!r}")
        if len(lowercased) > 3:
            print(f"      ... and {len(lowercased) - 3} more")
        print("  Same address either way. Note this does NOT prove the write landed:")
        print("  a silently discarded write against an already-lower-case stored value")
        print("  looks identical. See the evidence line below.")

        if mixed_case_survived:
            print(f"\n  EVIDENCE: {len(mixed_case_survived)} mixed-case value(s) came "
                  f"back byte-identical, e.g. {mixed_case_survived[0][1]} for PMI "
                  f"{mixed_case_survived[0][0]}.")
            print("  So nothing normalizes these universally — the lower-cased ones "
                  "above need a real explanation. Investigate before trusting them.")
        else:
            print(f"\n  EVIDENCE: no mixed-case value survived intact, consistent with "
                  f"something lower-casing {'/'.join(sorted(LOWERCASED_BY_SALESFORCE))} "
                  f"on save (a before-save flow or trigger in this org, most likely — "
                  f"stock Salesforce preserves Contact.Email case).")

    if not (missing or ambiguous or mismatches):
        print("\n  PASS — every submitted member resolved to exactly one Contact, "
              "and every value sent matches what Salesforce holds"
              + (f" ({len(lowercased)} counted as matching after lower-casing)."
                 if lowercased else "."))

    print("\n### 3. Certification field coverage\n")
    cert_fields = [FIELD_MAP[f][0] for f in
                   ("Pmppipelinestatus", "Pmpstartdate", "Pmpexpiredate",
                    "Pmporiginalgrantdate", "Certificationlist") if f in FIELD_MAP]
    print(f"  {'field':<32} {'populated':>10}  of {len(stored)} Contacts")
    for f in cert_fields:
        n = sum(1 for c in stored.values() if c.get(f) not in (None, ""))
        flag = "   <- zero: check FLS / field mapping" if n == 0 else ""
        print(f"  {f:<32} {n:>10}{flag}")
    print("\n  A field empty for many members is expected — not everyone holds a")
    print("  PMP, and the sync omits empties rather than blanking them. A field")
    print("  populated for ZERO members is the suspicious case.")

    print("\n### What this does NOT prove\n")
    print("  A field the sync BLANKED is invisible to both checkers. This script")
    print("  skips fields the source would omit, and report_sentinel_dates.py")
    print("  counts an already-empty field as '0 conflicts' — which reads as a")
    print("  pass. Neither holds a before-snapshot, so a regression that started")
    print("  writing nulls would show up as clean in both. The guard against that")
    print("  is the omit rule in salesforce_client itself, not this script.")

    failed = bool(leaked) or bool(mismatches) or bool(missing) or bool(ambiguous)
    if failed:
        print("\nVERIFICATION FAILED — see above.")
    else:
        # Carry the caveat into the summary line and the exit path, so a skimmer
        # or a scheduler sees that some values were not matched byte-for-byte.
        note = (f" ({len(lowercased)} value(s) matched only after lower-casing)"
                if lowercased else "")
        print(f"\nVERIFICATION PASSED{note}.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
