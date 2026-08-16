"""Lists every column ThoughtSpot has for the configured dataset, straight
from live metadata — not the xlsx mapping sheet, which we already know
drifted from reality once (the dataset's real name differed from what the
sheet said). Useful for picking the final field list to sync, and for
finding candidate fields (e.g. a "has chapter membership" flag) to
investigate why the active-member count came out higher than expected.

    python list_columns.py
"""
import json
import os

from thoughtspot_client import ThoughtSpotClient
from fetch_members import get_dataset_id


def main():
    client = ThoughtSpotClient()
    dataset_id = get_dataset_id(client)
    detail = client.get_table_detail(dataset_id)

    os.makedirs("output", exist_ok=True)
    out_path = os.path.join("output", "dataset_detail.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(detail, f, indent=2, default=str)
    print(f"Full metadata detail written to {out_path}")

    # Best-effort column extraction — try a few known ThoughtSpot v2 shapes;
    # if none match, the full dump above still has everything.
    columns = None
    md_detail = detail.get("metadata_detail", detail)
    for path in (
        ("columns",),
        ("logicalTableContent", "columns"),
        ("columnDetails",),
    ):
        node = md_detail
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                break
        if node:
            columns = node
            break

    if columns:
        print(f"\n{len(columns)} columns found:\n")
        for c in columns:
            name = c.get("name") or c.get("header", {}).get("name") or c.get("columnName")
            dtype = c.get("dataType") or c.get("type")
            print(f"  {name!r}  ({dtype})")
    else:
        print("\nCouldn't auto-locate the column list in the response shape — "
              f"open {out_path} and look for it manually.")


if __name__ == "__main__":
    main()
