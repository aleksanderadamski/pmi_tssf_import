"""Thin wrapper around the ThoughtSpot REST API v2.0 endpoints this project needs.

Endpoint field names below follow the published v2.0 docs
(https://developers.thoughtspot.com/docs/rest-apiv2-reference). If ThoughtSpot
returns a 400 mentioning an unknown field, the error body names the bad field —
fix it here, this is the only place request shapes live.
"""
import requests

import config

BASE_URL = f"https://{config.TS_HOST}/api/rest/2.0"


class ThoughtSpotClient:
    def __init__(self):
        self._token = None

    def authenticate(self) -> str:
        """POST /auth/token/full — full-session token via username/password."""
        resp = requests.post(
            f"{BASE_URL}/auth/token/full",
            json={
                "username": config.TS_USERNAME,
                "password": config.TS_PASSWORD,
                "org_id": config.TS_ORG_ID,
                "validity_time_in_sec": 3600,
            },
        )
        resp.raise_for_status()
        self._token = resp.json()["token"]
        return self._token

    def _headers(self):
        if not self._token:
            self.authenticate()
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def list_logical_tables(self, record_size: int = 1000) -> list[dict]:
        """POST /metadata/search — all Tables/Worksheets/Models, unfiltered.

        Used both as a diagnostic (see list_datasets.py) and as the fallback
        for resolve_dataset_id, since an exact name match via the API's
        `identifier` field isn't reliable — case, whitespace, or the object
        actually being a Worksheet vs. Model subtype can all cause a miss.
        """
        resp = requests.post(
            f"{BASE_URL}/metadata/search",
            headers=self._headers(),
            json={"metadata": [{"type": "LOGICAL_TABLE"}], "record_size": record_size},
        )
        resp.raise_for_status()
        data = resp.json()
        # Response shape wasn't confirmed against a live call ahead of time;
        # handle both a bare list and a {"metadata": [...]}-style wrapper.
        if isinstance(data, dict):
            data = data.get("metadata", data.get("results", []))
        return data

    def resolve_dataset_id(self, name: str) -> str:
        """Find a LOGICAL_TABLE's GUID by name (case-insensitive, with a
        substring fallback) among everything list_logical_tables() returns.
        """
        tables = self.list_logical_tables()
        target = name.strip().lower()

        exact = [t for t in tables if t.get("metadata_name", "").strip().lower() == target]
        if len(exact) == 1:
            return exact[0]["metadata_id"]
        if len(exact) > 1:
            raise RuntimeError(
                f"Multiple objects exactly named {name!r}: "
                f"{[t.get('metadata_id') for t in exact]}"
            )

        partial = [t for t in tables if target in t.get("metadata_name", "").strip().lower()]
        if len(partial) == 1:
            print(f"No exact match for {name!r}; using closest match "
                  f"{partial[0]['metadata_name']!r} instead.")
            return partial[0]["metadata_id"]

        available = sorted(t.get("metadata_name", "") for t in tables)
        raise RuntimeError(
            f"No LOGICAL_TABLE matched {name!r} "
            f"({'multiple partial matches: ' + str([t['metadata_name'] for t in partial]) if partial else 'no partial matches either'}).\n"
            f"Available tables/worksheets/models ({len(available)}):\n  " + "\n  ".join(available)
        )

    def get_table_detail(self, dataset_id: str) -> dict:
        """POST /metadata/search with include_details=True for one object
        by GUID — returns its full metadata_detail, which includes column
        info. Exact detail shape not confirmed live yet; callers should
        inspect the raw structure (see list_columns.py) rather than assume.
        """
        resp = requests.post(
            f"{BASE_URL}/metadata/search",
            headers=self._headers(),
            json={
                "metadata": [{"type": "LOGICAL_TABLE", "identifier": dataset_id}],
                "include_details": True,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("metadata", data.get("results", []))
        if not data:
            raise RuntimeError(f"No object found for id {dataset_id!r}")
        return data[0]

    def search_data(
        self,
        dataset_id: str,
        columns: list[str],
        runtime_filter: dict | None = None,
        record_offset: int = 0,
        record_size: int = -1,
    ) -> tuple[list[str], list[list]]:
        """POST /searchdata — query a Model/Worksheet/Table for arbitrary columns.

        `columns` are field names as shown in ThoughtSpot, e.g. ["Personid"].
        A date-type column defaults to a MONTHLY bucket unless you bind one
        explicitly with "Fieldname|bucket" (bucket: daily/weekly/monthly/
        quarterly/yearly), e.g. "Startdateforterm|daily" -> query token
        "[Startdateforterm].daily".

        Returns (column_names, data_rows) unpacked from the real v2 response
        shape: {"contents": [{"column_names": [...], "data_rows": [...]}]}.
        """
        tokens = []
        for c in columns:
            if "|" in c:
                name, bucket = c.split("|", 1)
                tokens.append(f"[{name}].{bucket}")
            else:
                tokens.append(f"[{c}]")
        query_string = " ".join(tokens)

        body = {
            "query_string": query_string,
            "logical_table_identifier": dataset_id,
            "data_format": "COMPACT",
            "record_offset": record_offset,
            "record_size": record_size,
        }
        if runtime_filter:
            body["runtime_filter"] = runtime_filter

        resp = requests.post(
            f"{BASE_URL}/searchdata", headers=self._headers(), json=body
        )
        if not resp.ok:
            raise RuntimeError(
                f"searchdata failed ({resp.status_code}): {resp.text}"
            )
        contents = resp.json()["contents"][0]
        return contents["column_names"], contents["data_rows"]
