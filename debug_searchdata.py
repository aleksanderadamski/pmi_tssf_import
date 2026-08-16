"""Dumps a raw /searchdata result (columns + rows) for eyeballing.

    python debug_searchdata.py
"""
import json
import os

from thoughtspot_client import ThoughtSpotClient
from fetch_members import QUERY_FIELDS, get_dataset_id


def main():
    client = ThoughtSpotClient()
    dataset_id = get_dataset_id(client)
    columns, rows = client.search_data(dataset_id, QUERY_FIELDS, record_size=5)

    print(f"Columns: {columns}\n")

    os.makedirs("output", exist_ok=True)
    out_path = os.path.join("output", "debug_searchdata_response.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"columns": columns, "rows": rows}, f, indent=2, default=str)
    print(f"Full result written to {out_path}")

    print("\nRows:")
    print(json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    main()
