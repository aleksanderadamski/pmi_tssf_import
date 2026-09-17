"""Check which PMP IDs have certification data in ThoughtSpot."""
from thoughtspot_client import ThoughtSpotClient
from fetch_members import get_dataset_id, _column_index

ts = ThoughtSpotClient()
dataset_id = get_dataset_id(ts)

# Query cert fields + personid
cert_fields = [
    "Personid",
    "Pmppipelinestatus",
    "Pmpstartdate|daily",
    "Pmpexpiredate|daily",
    "Pmporiginalgrantdate|daily",
    "Certificationlist",
]

columns, rows = ts.search_data(dataset_id, cert_fields, record_size=5000)
idx = {f.replace("|daily", ""): _column_index(columns, f) for f in cert_fields}

print(f"\n{len(rows)} total rows fetched\n")

# Find rows with cert data
has_pmp = []
has_certs = []

for row in rows:
    pid = row[idx["Personid"]]
    pmp_status = row[idx["Pmppipelinestatus"]]
    pmp_start = row[idx["Pmpstartdate"]]
    cert_list = row[idx["Certificationlist"]]

    if pmp_status or pmp_start:
        has_pmp.append((pid, pmp_status, pmp_start))
    if cert_list:
        has_certs.append((pid, cert_list))

print(f"PMP Status/Start Date data: {len(has_pmp)} people")
print(f"Certifications Summary: {len(has_certs)} people\n")

print("=== 5 people WITH PMP cert data ===")
for pid, status, start in has_pmp[:5]:
    print(f"  PMI {pid}: status={status!r}, start={start}")

print("\n=== 5 people WITH Certification Summary ===")
for pid, cert_list in has_certs[:5]:
    print(f"  PMI {pid}: {cert_list!r}")
