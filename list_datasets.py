"""Diagnostic: print every Table/Worksheet/Model name ThoughtSpot has,
so you can find the exact name/GUID of the one you want to query.

    python list_datasets.py
"""
from thoughtspot_client import ThoughtSpotClient


def main():
    client = ThoughtSpotClient()
    tables = client.list_logical_tables()
    print(f"{len(tables)} LOGICAL_TABLE objects found:\n")
    for t in sorted(tables, key=lambda t: t.get("metadata_name", "")):
        print(f"  {t.get('metadata_name')!r}  ({t.get('metadata_id')})")


if __name__ == "__main__":
    main()
