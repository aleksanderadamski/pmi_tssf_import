"""Investigate certification data in the ThoughtSpot dataset (read-only).

Goal: understand how a person's (possibly multiple) certifications are
represented, so we can design flat per-credential fields on the Salesforce
Contact. Examines BOTH representations:

  - Generic per-cert rows: Certificationid, Certificationtypename,
    Certificationstatusname, Effectivestartdate, Effectiveenddate,
    Originalgrantdate, plus the per-person Certificationlist / Iscurrentlycertified.
  - Dedicated per-credential columns: Pmpcertificationid, Pmpstartdate,
    Pmpexpiredate, Pmporiginalgrantdate, Pmppipelinestatus, and the
    Capm/Acp/Rmp certification-id columns.

No production changes, no Salesforce writes.

    python investigate_certifications.py
"""
from collections import defaultdict, Counter

from thoughtspot_client import ThoughtSpotClient
from fetch_members import get_dataset_id, _column_index
from date_utils import to_salesforce_date

# Plain field names (dict keys / matching). Date fields get a |daily binding in
# the query so ThoughtSpot returns day-level values instead of a monthly bucket.
GENERIC_FIELDS = [
    "Personid",
    "Certificationlist",
    "Certificationid",
    "Certificationtypename",
    "Certificationstatusname",
    "Iscurrentlycertified",
    "Effectivestartdate",
    "Effectiveenddate",
    "Originalgrantdate",
]
DEDICATED_FIELDS = [
    "Pmpcertificationid",
    "Pmpstartdate",
    "Pmpexpiredate",
    "Pmporiginalgrantdate",
    "Pmppipelinestatus",
    "Capmcertificationid",
    "Acpcertificationid",
    "Rmpcertificationid",
]
DATE_FIELDS = {
    "Effectivestartdate", "Effectiveenddate", "Originalgrantdate",
    "Pmpstartdate", "Pmpexpiredate", "Pmporiginalgrantdate",
}

ALL_FIELDS = GENERIC_FIELDS + DEDICATED_FIELDS


def query_fields():
    """Bind date fields to |daily; leave the rest plain."""
    return [f"{f}|daily" if f in DATE_FIELDS else f for f in ALL_FIELDS]


def main():
    ts = ThoughtSpotClient()
    dataset_id = get_dataset_id(ts)
    columns, rows = ts.search_data(dataset_id, query_fields())
    idx = {f: _column_index(columns, f) for f in ALL_FIELDS}
    print(f"\n{len(rows)} total rows returned.")
    print(f"Response columns: {columns}\n")

    def g(r, f):
        return r[idx[f]]

    persons = {g(r, "Personid") for r in rows}
    print(f"Distinct Personids: {len(persons)}")

    # --- 1. Credential catalog -------------------------------------------------
    print("\n=== 1. Credential catalog (Certificationtypename) ===")
    type_counts = Counter(
        g(r, "Certificationtypename") for r in rows if g(r, "Certificationtypename")
    )
    for name, n in type_counts.most_common():
        print(f"  {name!r}: {n} rows")
    none_type = sum(1 for r in rows if not g(r, "Certificationtypename"))
    print(f"  (rows with no Certificationtypename: {none_type})")

    # --- 2. Certs per person + Certificationid uniqueness ----------------------
    print("\n=== 2. Certifications per person ===")
    certs_by_person = defaultdict(set)      # personid -> {certificationid}
    person_by_cert = defaultdict(set)       # certificationid -> {personid}
    for r in rows:
        pid, cid = g(r, "Personid"), g(r, "Certificationid")
        if cid is not None:
            certs_by_person[pid].add(cid)
            person_by_cert[cid].add(pid)

    dist = Counter(len(v) for v in certs_by_person.values())
    people_with_no_cert = len(persons) - len(certs_by_person)
    print(f"  People with 0 certs: {people_with_no_cert}")
    for k in sorted(dist):
        print(f"  People with {k} distinct Certificationid(s): {dist[k]}")

    multi_cert_ids = [cid for cid, pids in person_by_cert.items() if len(pids) > 1]
    print(f"\n  Distinct Certificationids: {len(person_by_cert)}")
    print(f"  Certificationids shared by >1 Personid: {len(multi_cert_ids)} "
          f"({'NOT unique per person' if multi_cert_ids else 'unique per person — usable as external ID'})")

    # --- 3. How multiple certs appear: dump a few multi-cert people ------------
    print("\n=== 3. Sample multi-cert people (both representations) ===")
    multi_people = [pid for pid, c in certs_by_person.items() if len(c) >= 2][:3]
    for pid in multi_people:
        prows = [r for r in rows if g(r, "Personid") == pid]
        print(f"\n  Personid {pid} — {len(prows)} total rows, "
              f"{len(certs_by_person[pid])} distinct certs")
        print(f"    Certificationlist: {g(prows[0], 'Certificationlist')!r}")
        seen = set()
        print("    Generic per-cert rows (cid | type | status | start | end | grant):")
        for r in prows:
            cid = g(r, "Certificationid")
            if cid is None or cid in seen:
                continue
            seen.add(cid)
            print(f"      {cid} | {g(r,'Certificationtypename')} | {g(r,'Certificationstatusname')} | "
                  f"{to_salesforce_date(g(r,'Effectivestartdate'))} | "
                  f"{to_salesforce_date(g(r,'Effectiveenddate'))} | "
                  f"{to_salesforce_date(g(r,'Originalgrantdate'))}")
        ded = {tuple(g(r, f) for f in DEDICATED_FIELDS) for r in prows}
        print(f"    Dedicated per-credential columns {DEDICATED_FIELDS}:")
        for tup in ded:
            print(f"      {tup}")

    # --- 4. Per-person stability of dedicated columns + Certificationlist ------
    print("\n=== 4. Per-person stability (0 varying = constant per person, good to flatten) ===")
    for field in DEDICATED_FIELDS + ["Certificationlist", "Iscurrentlycertified"]:
        by_person = defaultdict(set)
        for r in rows:
            by_person[g(r, "Personid")].add(g(r, field))
        unstable = sum(1 for v in by_person.values() if len(v) > 1)
        populated = sum(1 for v in by_person.values() if any(x not in (None, "") for x in v))
        print(f"  {field}: {unstable} of {len(by_person)} persons vary; "
              f"{populated} have a non-null value")

    # --- 5. Date semantics -----------------------------------------------------
    print("\n=== 5. Date field samples (raw -> converted) ===")
    for field in ("Effectivestartdate", "Effectiveenddate", "Originalgrantdate",
                  "Pmpstartdate", "Pmpexpiredate", "Pmporiginalgrantdate"):
        sample = next((g(r, field) for r in rows if g(r, field) not in (None, "")), None)
        midnight = (sample % 86400 == 0) if isinstance(sample, int) else None
        tag = ("(midnight-aligned)" if midnight else
               "(HAS time-of-day component!)" if sample is not None else "")
        print(f"  {field}: raw={sample!r} -> {to_salesforce_date(sample)} {tag}")

    print("\nDone. Use the above to decide: which credentials become Contact "
          "fields, whether to source them from the dedicated columns or by "
          "pivoting the generic rows on Certificationtypename, and what dedup "
          "rule the fan-out needs.")


if __name__ == "__main__":
    main()
