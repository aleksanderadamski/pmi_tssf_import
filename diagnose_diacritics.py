"""Find where accented characters are lost between ThoughtSpot and Salesforce.

    python diagnose_diacritics.py

STRICTLY READ-ONLY. Nothing is written to Salesforce.

Reported symptom: names hold Polish letters in ThoughtSpot but land in
Salesforce transliterated - A-ogonek becomes A, L-stroke becomes L. The right
fix depends entirely on WHERE that happens, and the candidates need completely
different responses:

  ThoughtSpot already returns ASCII  -> source-side. No change here can restore
                                        what we were never given.
  Our pipeline drops them            -> a bug here.
  Salesforce folds them on save      -> org-side: a before-save Flow, an Apex
                                        trigger, or a data-cleansing package.

What the existing evidence already settles, and what it does not:

  verify_push.py compares the pushed manifest against Salesforce - that is
  stage 2 against stage 3. Its 2026-09-18 run reported mismatches in Email
  ONLY, across the whole population. That RULES OUT Salesforce folding what we
  send. It says nothing about stage 1 -> stage 2.

  Separately, nothing in this codebase calls unicodedata, normalize, or encodes
  to ASCII, and requests' json= escapes non-ASCII losslessly as \\uXXXX. That
  argues against candidate 2.

So the prior is "ThoughtSpot returns ASCII", but the two arguments are
different evidence and are kept separate below rather than merged into a guess.

A note that narrows the culprit when folding IS found: U+0141 (L with stroke)
has NO Unicode decomposition, so NFKD leaves it alone, while A-ogonek NFKD-folds
to A. Naive accent-stripping therefore cannot produce L-stroke -> L; that needs a
real transliteration table (Unidecode, iconv //TRANSLIT, an ICU Latin-ASCII
transform, a cleansing package). This script reports which kind it sees, because
they point at different culprits.

Alphabet-agnostic by construction: every test is "is this codepoint > 127",
never a list of Polish letters. Czech, Hungarian, Turkish, Greek and Cyrillic
are handled identically.
"""
import csv
import glob
import os
import sys
import unicodedata
from datetime import datetime, timezone

import config
from thoughtspot_client import ThoughtSpotClient
from fetch_members import (
    get_dataset_id, dedupe_by_person, filter_currently_active,
    QUERY_FIELDS, FIELDS, DATE_FIELDS, ACTIVE_FIELD,
    _column_index, _is_active,
)
from date_utils import to_epoch_seconds
from salesforce_client import (
    SalesforceClient, EXTERNAL_ID_FIELD, FIELD_MAP, QUERY_CHUNK_SIZE,
    external_id_key, _chunks, _soql_quote,
)

# Text fields that could carry accents. Dates cannot.
TEXT_FIELDS = ["Firstname", "Lastname", "Primaryemail",
               "Pmppipelinestatus", "Certificationlist"]
# Only these reach Salesforce as names, so only these can be compared in stage 3.
NAME_FIELDS = ["Firstname", "Lastname"]
SAMPLE = 5

# A Windows console is typically cp1252 and raises UnicodeEncodeError on exactly
# the characters under investigation - which would crash the tool built to
# investigate them. Values are printed via show() (pure ASCII) as well, so this
# failing is survivable; every literal below is ASCII-only for the same reason.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass  # stream already wrapped (pytest capture, CI shim) - carry on


def has_accent(value) -> bool:
    return bool(value) and any(ord(c) > 127 for c in str(value))


def show(value) -> str:
    """Console-safe rendering: escaped text plus Unicode names.

    ascii() escapes non-ASCII to \\uXXXX so this cannot fail on a cp1252
    console, and the codepoint names make 'L' vs 'L-with-stroke' unambiguous in
    a log someone pastes back.
    """
    if value is None:
        return "None"
    text = str(value)
    names = [unicodedata.name(c, f"U+{ord(c):04X}") for c in text if ord(c) > 127]
    return f"{ascii(text)}  [{', '.join(names)}]" if names else ascii(text)


def nfc(value):
    """Normalize so a mere NFC/NFD difference isn't reported as folding."""
    return unicodedata.normalize("NFC", str(value)) if value is not None else None


def folding_kind(sent: str, stored: str) -> str:
    """Whether stripping accents could explain this, or a table was needed."""
    stripped = "".join(
        c for c in unicodedata.normalize("NFKD", sent)
        if not unicodedata.combining(c)
    )
    if stored == stripped:
        return "NFKD accent-strip (no transliteration table needed)"
    return ("TRANSLITERATION TABLE - some characters folded have no Unicode "
            "decomposition, so accent-stripping alone cannot produce this")


def build_records(columns, rows):
    """Reproduce the exact pipeline fetch_active() runs, so every stage below
    talks about the same population. Filtering on Isactive alone would not:
    per CLAUDE.md it means 'was active during that term row', and roughly half
    of those people have an expired term the push never touches.
    """
    idx = {f: _column_index(columns, f) for f in FIELDS}
    active_idx = _column_index(columns, ACTIVE_FIELD)
    raw = []
    for row in rows:
        if not _is_active(row[active_idx], config.TS_ACTIVE_FLAG_VALUE):
            continue
        rec = {}
        for field in FIELDS:
            v = row[idx[field]]
            rec[field] = to_epoch_seconds(v) if field in DATE_FIELDS else v
        raw.append(rec)
    return raw, filter_currently_active(dedupe_by_person(raw))


def main():
    ts = ThoughtSpotClient()
    columns, rows = ts.search_data(get_dataset_id(ts), QUERY_FIELDS)
    raw, records = build_records(columns, rows)

    print("=" * 72)
    print("Tracing accented characters through each stage")
    print("=" * 72)

    # --- Stage 1 ------------------------------------------------------------
    raw_hits = [r for r in raw if any(has_accent(r.get(f)) for f in TEXT_FIELDS)]
    live_hits = [r for r in records if any(has_accent(r.get(f)) for f in TEXT_FIELDS)]
    named_hits = [r for r in records if any(has_accent(r.get(f)) for f in NAME_FIELDS)]

    print(f"\n  STAGE 1 - raw /searchdata response")
    print(f"    {len(raw_hits)} of {len(raw)} term rows carry a non-ASCII character")
    print(f"    {len(live_hits)} of {len(records)} currently-active members do "
          f"(this is the set a push actually writes)")
    print(f"    {len(named_hits)} of those have the accent in a NAME field")
    for r in named_hits[:SAMPLE]:
        for f in NAME_FIELDS:
            if has_accent(r.get(f)):
                print(f"      PMI {r['Personid']}: {f} = {show(r[f])}")

    if not live_hits:
        print("\n" + "=" * 72)
        print("VERDICT: ThoughtSpot returns ASCII-only text for currently-active "
              "members.")
        print("=" * 72)
        print("\n  The accents are gone before this sync sees them, so no change here")
        print("  can restore them - we cannot write what we were never given.")
        if raw_hits:
            print(f"\n  NOTE: {len(raw_hits)} raw term row(s) DO carry accents, but")
            print("  none belongs to a currently-active member, so none is ever")
            print("  pushed. Worth a look - it suggests the source holds both forms.")
        print("\n  Next steps, in order:")
        print("    1. Compare a known member in the ThoughtSpot UI against this run.")
        print("       UI shows accents but the API does not -> the dataset exposes a")
        print("       transliterated copy of the column.")
        print("    2. python list_columns.py - look for a separate column holding the")
        print("       original spelling, then point FIELDS/QUERY_FIELDS at it.")
        print("    3. Otherwise it is a PMI/source-data question, not a code one.")
        return

    if not named_hits:
        print("\n" + "=" * 72)
        print("INCONCLUSIVE: accents exist, but not in FirstName/LastName.")
        print("=" * 72)
        print("\n  Only name fields can be compared against Salesforce here, and none")
        print("  of the accented members has an accented name. Nothing to trace.")
        return

    sample = named_hits[:SAMPLE]
    sample_ids = [external_id_key(r["Personid"]) for r in sample]

    # --- Stage 2: these exact members in the local artifact ------------------
    manifest = sorted(glob.glob(os.path.join("output", "pushed_*.csv")))
    local_path = manifest[-1] if manifest else os.path.join("output", "members.csv")
    local_by_id, local_rows = {}, []
    if os.path.exists(local_path):
        with open(local_path, newline="", encoding="utf-8") as f:
            local_rows = list(csv.DictReader(f))
        local_by_id = {external_id_key(r.get("Personid")): r for r in local_rows}
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(
            os.path.getmtime(local_path), tz=timezone.utc)
        print(f"\n  STAGE 2 - {local_path}")
        print(f"    {len(local_rows)} record(s), written "
              f"{int(age.total_seconds() // 60)} min ago")
        if len(local_rows) < len(records):
            print(f"    WARNING: narrower than the {len(records)} currently-active")
            print(f"    members - probably a --limit/--personids run. A sampled member")
            print(f"    missing from it means nothing.")
        found = 0
        for r in sample:
            key = external_id_key(r["Personid"])
            local = local_by_id.get(key)
            if local is None:
                print(f"      PMI {key}: not in this file")
                continue
            found += 1
            for f in NAME_FIELDS:
                if has_accent(r.get(f)):
                    same = nfc(local.get(f)) == nfc(r[f])
                    print(f"      PMI {key}: {f} {'kept' if same else 'CHANGED'}"
                          f" -> {show(local.get(f))}")
                    if not same:
                        print(f"          fetched {show(r[f])}")
        if found and all(
            not has_accent(local_by_id[external_id_key(r['Personid'])].get(f))
            for r in sample if external_id_key(r["Personid"]) in local_by_id
            for f in NAME_FIELDS if has_accent(r.get(f))
        ):
            print("\n  >>> LOST HERE: fetched with accents, written to the local file")
            print("      without them. That is a bug in this codebase.")
            return
    else:
        print(f"\n  STAGE 2 - skipped, no {local_path} yet")

    # --- Stage 3: these exact members in Salesforce --------------------------
    sf = SalesforceClient()
    sf_name_fields = [FIELD_MAP[f][0] for f in NAME_FIELDS]
    stored, ambiguous = {}, set()
    for chunk in _chunks([k for k in sample_ids if k], QUERY_CHUNK_SIZE):
        soql = (
            f"SELECT Id, {EXTERNAL_ID_FIELD}, {', '.join(sf_name_fields)} "
            f"FROM Contact WHERE {EXTERNAL_ID_FIELD} IN "
            f"({','.join(_soql_quote(k) for k in chunk)})"
        )
        for rec in sf.query(soql):
            key = external_id_key(rec.get(EXTERNAL_ID_FIELD))
            if not key:
                continue
            # Same refusal to guess as fetch_existing_contact_ids: comparing a
            # name against the wrong duplicate would read as transliteration.
            if key in stored and stored[key]["Id"] != rec["Id"]:
                ambiguous.add(key)
                stored.pop(key, None)
                continue
            if key not in ambiguous:
                stored[key] = rec

    print(f"\n  STAGE 3 - Salesforce, for those {len(sample)} member(s)")
    compared = changed = 0
    kinds = set()
    for r in sample:
        key = external_id_key(r["Personid"])
        if key in ambiguous:
            print(f"      PMI {key}: matches MORE than one Contact - not compared")
            continue
        contact = stored.get(key)
        if contact is None:
            print(f"      PMI {key}: no Contact found - not compared")
            continue
        for ts_field in NAME_FIELDS:
            sent = r.get(ts_field)
            if not has_accent(sent):
                continue
            sf_field = FIELD_MAP[ts_field][0]
            got = contact.get(sf_field)
            compared += 1
            sent_n, got_n = nfc(sent), nfc(got)
            print(f"      PMI {key}: {sf_field}")
            print(f"          sent   {show(sent)}")
            print(f"          stored {show(got)}")
            if sent_n == got_n:
                print("          identical")
            elif got_n is not None and not has_accent(got_n):
                changed += 1
                kinds.add(folding_kind(sent_n, got_n))
                print("          >>> FOLDED ON SAVE")
            else:
                changed += 1
                print("          >>> CHANGED ON SAVE (partially, or differently)")

    # --- Verdict, driven by counted comparisons ------------------------------
    print("\n" + "=" * 72)
    if compared == 0:
        print("INCONCLUSIVE: no accented name could be compared.")
        print("=" * 72)
        print("\n  None of the sampled members resolved to exactly one Contact, so")
        print("  nothing was actually checked. This is NOT evidence about Salesforce.")
        print("  Push first (a member absent here may simply never have been written),")
        print("  then re-run.")
    elif changed == 0:
        print("VERDICT: accents survive end to end. Nothing here to fix.")
        print("=" * 72)
        print(f"\n  {compared} accented name(s) compared, all identical in Salesforce.")
        print("  If the Salesforce UI still looks wrong, it is a display or export")
        print("  issue (font, report encoding, the viewing app) rather than the")
        print("  stored data - trust the API value above, not the browser.")
    elif changed == compared:
        print("VERDICT: Salesforce is folding accents on save.")
        print("=" * 72)
        print(f"\n  All {compared} compared name(s) changed between send and store,")
        print("  so this is org-side, not a bug here - the same shape as the Email")
        print("  lower-casing in CLAUDE.md.")
        for k in sorted(kinds):
            print(f"\n  How: {k}")
        print("\n  Look for, in this order:")
        print("    1. A before-save Flow on Contact touching FirstName/LastName.")
        print("    2. An Apex trigger on Contact (Setup > Object Manager > Triggers).")
        print("    3. A managed package doing data cleansing or deduplication.")
        print("\n  NOTE this contradicts verify_push.py, which compares the same")
        print("  fields across the WHOLE population and reported no name mismatches.")
        print("  Re-run verify_push.py before acting: a 5-record sample must not")
        print("  override a full-population check.")
    else:
        print(f"INCONCLUSIVE: {changed} of {compared} compared name(s) changed.")
        print("=" * 72)
        print("\n  A partial result does not fit any single cause. Widen the sample")
        print("  and check whether the changed members share something - an owner, a")
        print("  record type, a creation date - that a scoped Flow would key on.")


if __name__ == "__main__":
    main()
