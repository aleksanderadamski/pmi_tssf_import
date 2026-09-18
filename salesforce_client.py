"""Salesforce JWT bearer auth + Contact upsert.

Field mapping (ThoughtSpot -> Salesforce Contact):
  Personid          -> Membership_ID__c  (external ID, used to match)
  Firstname         -> FirstName
  Lastname          -> LastName          (Salesforce requires this on insert;
                                           without it, upserting a Personid
                                           with no existing matching Contact
                                           fails REQUIRED_FIELD_MISSING)
  Primaryemail      -> Email             (standard Contact field)
  Startdateforterm  -> Chapter_Join_Date__c
  Enddateforterm    -> Chapter_Expiration__c
  Pmppipelinestatus     -> PMP_Status__c
  Pmpstartdate          -> PMP_Start_Date__c
  Pmpexpiredate         -> PMP_Expiration__c
  Pmporiginalgrantdate  -> PMP_Original_Grant_Date__c
  Certificationlist     -> Certifications__c

FIELD_MAP below is the authoritative copy of that mapping.

Writes in two passes, 200 records per call per Salesforce's limit:

  1. SOQL-query Membership_ID__c -> Contact Id for every Contact the
     integration user can see (fetch_existing_contact_ids).
  2. PATCH /composite/sobjects with an Id for the ones that matched (update),
     POST /composite/sobjects with a Membership_ID__c for the rest (insert).

This replaced the single-call "upsert by external ID" endpoint
(PATCH /composite/sobjects/Contact/Membership_ID__c). That endpoint resolves
the external ID itself, and on a 2,463-record production run it failed to
resolve 209 of them — attempting an insert that the unique index rejected
with DUPLICATE_VALUE, quoting the Id of the record it should have updated.
Resolving the ids ourselves makes the match explicit and inspectable; see
diagnose_duplicates.py for the read-only triage of a failure CSV.

Note that step 1 runs under the integration user's sharing rules, just as the
old endpoint's internal lookup did. If a Contact is hidden from that user, it
is missing here too and the insert still collides — that failure mode is a
permissions fix, not a code one, and the diagnostic identifies it.

This module never deletes. It only ever adds or updates:

  - WRITE_METHODS pins the HTTP method to PATCH/POST, so no delete call can be
    issued even by mistake. The integration user needs Modify All on Contact to
    write records it doesn't own, and that grant carries delete rights with it.
  - A field that is empty in ThoughtSpot is OMITTED from the payload rather
    than sent as null, so the sync cannot blank a value someone entered in the
    Salesforce UI. The tradeoff is that a value which legitimately disappears
    upstream (a lapsed certification) goes stale in Salesforce rather than
    being cleared — clearing is a deliberate manual act.

Setup required before this works:
  1. In Salesforce Setup, create a Connected App with a digital certificate
     (Setup > App Manager > New Connected App > Enable OAuth Settings >
     "Use digital signatures", upload the .pem's matching public cert).
  2. Enable the JWT bearer OAuth flow and pre-authorize SF_USERNAME for it
     (Setup > Connected App > Manage > Edit Policies > Permitted Users:
     "Admin approved users are pre-authorized", then add the integration
     user via a permission set).
  3. Keep the private key out of git. Locally, point SF_PRIVATE_KEY_FILE at the
     .pem. In a cloud run (Codespaces, mobile Claude Code, GitHub Actions) where
     no file ships, put the PEM contents in the SF_PRIVATE_KEY secret instead.
"""
import time
from urllib.parse import quote

import jwt
import requests

import config
from date_utils import to_salesforce_date

EXTERNAL_ID_FIELD = "Membership_ID__c"
CHUNK_SIZE = 200  # SObject Collections limit per call
QUERY_CHUNK_SIZE = 200  # ids per SOQL IN(...) clause, to keep the GET URL short
HTTP_TIMEOUT = 120  # seconds; a 200-record write is slow but must not hang forever

# The only HTTP methods this module may use against /composite/sobjects.
# The integration user holds Modify All on Contact (needed to update records it
# does not own), which also carries delete rights — so "this sync never deletes"
# is enforced here rather than left to code review. Salesforce deletes via
# DELETE /composite/sobjects?ids=... ; adding "DELETE" here is the only thing
# IN THIS MODULE that could make that reachable. Don't. The guard binds the
# method inside _write_collection, so it cannot police a future requests.* call
# that bypasses the helper — route every Contact write through it.
WRITE_METHODS = frozenset({"PATCH", "POST"})

# ts_field -> (sf_field, optional transform applied to the raw TS value)
FIELD_MAP = {
    "Firstname": ("FirstName", None),
    "Lastname": ("LastName", None),
    "Primaryemail": ("Email", None),
    "Startdateforterm": ("Chapter_Join_Date__c", to_salesforce_date),
    "Enddateforterm": ("Chapter_Expiration__c", to_salesforce_date),
    "Pmppipelinestatus": ("PMP_Status__c", None),
    "Pmpstartdate": ("PMP_Start_Date__c", to_salesforce_date),
    "Pmpexpiredate": ("PMP_Expiration__c", to_salesforce_date),
    "Pmporiginalgrantdate": ("PMP_Original_Grant_Date__c", to_salesforce_date),
    "Certificationlist": ("Certifications__c", None),
}


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def external_id_key(value) -> str:
    """Normalize a Membership_ID for matching. Both sides of the lookup go
    through this, so a stored value with stray whitespace — or an int on one
    side and a str on the other — still matches the ThoughtSpot Personid.

    Note this is deliberately LOSSIER than Salesforce's unique index, which
    treats "123" and " 123" as two different values. Two Contacts can therefore
    collapse to one key here; fetch_existing_contact_ids detects that and
    refuses to guess (see its `ambiguous` return) rather than silently updating
    whichever row the query happened to return last.
    """
    return "" if value is None else str(value).strip()


def _soql_quote(value: str) -> str:
    """Escape a value for a SOQL string literal. Membership_IDs are numeric in
    practice, but this interpolates record data into a query, so escape rather
    than trust the shape of the source data.
    """
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class SalesforceClient:
    def __init__(self):
        self._access_token = None
        self._instance_url = None

    @staticmethod
    def _load_private_key() -> bytes:
        """The RS256 signing key, from SF_PRIVATE_KEY (PEM contents, for cloud
        runs) if set, else from the SF_PRIVATE_KEY_FILE path (local runs).
        Tolerates a secret stored with escaped "\\n" line breaks — some secret
        stores mangle real newlines — since PEM base64 never contains a
        backslash, so that substitution is safe.
        """
        if config.SF_PRIVATE_KEY:
            key = config.SF_PRIVATE_KEY
            if "-----BEGIN" in key and "\\n" in key:
                # Repair a secret whose newlines were escaped (\n or \r\n).
                key = key.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "")
            return key.encode() if isinstance(key, str) else key
        if config.SF_PRIVATE_KEY_FILE:
            with open(config.SF_PRIVATE_KEY_FILE, "rb") as f:
                return f.read()
        raise RuntimeError(
            "No Salesforce private key: set SF_PRIVATE_KEY (PEM contents) or "
            "SF_PRIVATE_KEY_FILE (path to the .pem)."
        )

    def authenticate(self):
        private_key = self._load_private_key()

        claim = {
            "iss": config.SF_CLIENT_ID,
            "sub": config.SF_USERNAME,
            "aud": config.SF_LOGIN_URL,
            "exp": int(time.time()) + 300,
        }
        assertion = jwt.encode(claim, private_key, algorithm="RS256")

        resp = requests.post(
            f"{config.SF_LOGIN_URL}/services/oauth2/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        if not resp.ok:
            raise RuntimeError(
                f"Salesforce JWT auth failed ({resp.status_code}): {resp.text}"
            )
        payload = resp.json()
        self._access_token = payload["access_token"]
        self._instance_url = payload["instance_url"]
        return self._access_token

    def _headers(self):
        if not self._access_token:
            self.authenticate()
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

    def query(self, soql: str, include_deleted: bool = False) -> list[dict]:
        """Run a SOQL query, following nextRecordsUrl pagination to the end.

        include_deleted switches to /queryAll, which also returns soft-deleted
        (Recycle Bin) rows — those stay in the Membership_ID__c unique index
        while being invisible to a normal query, which is one of the ways an
        external-ID upsert can miss a match and then fail as a duplicate.
        """
        headers = self._headers()  # ensures authenticate() ran, sets _instance_url
        endpoint = "queryAll" if include_deleted else "query"
        url = (
            f"{self._instance_url}/services/data/{config.SF_API_VERSION}"
            f"/{endpoint}?q={quote(soql)}"
        )
        records = []
        seen_pages = set()
        while True:
            resp = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            if not resp.ok:
                raise RuntimeError(
                    f"Salesforce query failed ({resp.status_code}): {resp.text}"
                )
            payload = resp.json()
            records.extend(payload.get("records", []))
            next_url = payload.get("nextRecordsUrl")
            if not next_url:
                return records
            if next_url in seen_pages:
                raise RuntimeError(
                    f"Salesforce query pagination looped on {next_url!r} — "
                    f"aborting after {len(records)} records."
                )
            seen_pages.add(next_url)
            url = f"{self._instance_url}{next_url}"

    def fetch_existing_contact_ids(self, keys) -> tuple[dict[str, str], set[str]]:
        """Resolve the given Membership_IDs to Contact Ids.

        Returns (by_external_id, ambiguous). `ambiguous` holds keys that more
        than one Contact answered to — possible because external_id_key strips
        whitespace while Salesforce's unique index does not, so "123" and " 123"
        are two real rows that normalize to one key. Updating either would be a
        guess, so the caller fails those records loudly instead.

        Scoped with IN(...) to just the ids being written: a `--limit 5` smoke
        test or a --personids retry must not drag every Contact in the org
        across the wire before it can write anything.

        Note "resolve": this runs under the integration user's sharing rules,
        exactly as the external-ID upsert's own lookup did. A Contact hidden
        from that user is absent here too — diagnose_duplicates.py tells the
        two cases apart.
        """
        wanted = {external_id_key(k) for k in keys}
        wanted.discard("")

        by_external_id: dict[str, str] = {}
        ambiguous: set[str] = set()
        for chunk in _chunks(sorted(wanted), QUERY_CHUNK_SIZE):
            soql = (
                f"SELECT Id, {EXTERNAL_ID_FIELD} FROM Contact "
                f"WHERE {EXTERNAL_ID_FIELD} IN "
                f"({','.join(_soql_quote(k) for k in chunk)})"
            )
            for rec in self.query(soql):
                key = external_id_key(rec.get(EXTERNAL_ID_FIELD))
                if not key:
                    continue
                prior = by_external_id.get(key)
                if prior is not None and prior != rec["Id"]:
                    # Drop it entirely rather than keeping whichever row arrived
                    # first: the map must never hand a caller a guess.
                    ambiguous.add(key)
                    by_external_id.pop(key, None)
                    continue
                if key not in ambiguous:
                    by_external_id[key] = rec["Id"]
        return by_external_id, ambiguous

    def _write_collection(self, method: str, indexed_payloads, results: list,
                          created: bool) -> None:
        """PATCH (update) or POST (insert) a batch through SObject Collections.

        indexed_payloads is [(original_index, payload)]; each result is written
        back to results[original_index] so the caller's list stays aligned with
        its input regardless of how records were split across the two passes.

        These endpoints return a bare {id, success, errors} with no `created`
        flag — only the external-ID upsert endpoint reports one. We know which
        pass each record went through, so we set it here: callers (and
        test_upsert_update.py) still get insert-vs-update per record.
        """
        if method not in WRITE_METHODS:
            raise ValueError(
                f"Refusing to issue {method!r} against Contact records. This "
                f"sync only ever updates or inserts; see WRITE_METHODS."
            )
        if not indexed_payloads:
            return
        headers = self._headers()
        url = (
            f"{self._instance_url}/services/data/{config.SF_API_VERSION}"
            f"/composite/sobjects"
        )
        for chunk in _chunks(indexed_payloads, CHUNK_SIZE):
            resp = requests.request(
                method,
                url,
                headers=headers,
                json={"allOrNone": False, "records": [p for _, p in chunk]},
                timeout=HTTP_TIMEOUT,
            )
            if not resp.ok:
                raise RuntimeError(
                    f"Salesforce {method} failed ({resp.status_code}): {resp.text}"
                )
            for (original_index, _), result in zip(chunk, resp.json()):
                if result.get("success"):
                    result["created"] = created
                results[original_index] = result

    def upsert_contacts(self, records: list[dict]) -> list[dict]:
        """records: ThoughtSpot rows keyed by raw column name (Personid,
        Firstname, Lastname, Primaryemail, the term dates, the cert fields).

        Resolves Membership_ID__c -> Contact Id up front and then writes in two
        passes: a plain update for ids that already exist, an insert for the
        rest. The single-call external-ID upsert this replaces resolved matches
        itself, and for ~8% of the population it silently failed to — attempting
        an insert that the unique index then rejected with DUPLICATE_VALUE,
        naming the very record it should have updated.

        Returns per-record results in INPUT ORDER: reporting.summarize_results
        pairs records[i] with results[i] positionally, so a misaligned list
        would attribute every failure to the wrong person.
        """
        existing, ambiguous = self.fetch_existing_contact_ids(
            r["Personid"] for r in records
        )

        results: list = [None] * len(records)
        updates, inserts = [], []
        for i, r in enumerate(records):
            key = external_id_key(r["Personid"])
            if not key:
                # Without an external id an insert would create a Contact with a
                # null Membership_ID__c that nothing can ever match — so the next
                # run inserts another one, and so on. Refuse instead.
                results[i] = {
                    "success": False,
                    "errors": [{
                        "statusCode": "MISSING_EXTERNAL_ID",
                        "message": "record has no Personid; refusing to write a "
                                   "Contact with no Membership_ID__c",
                    }],
                }
                continue

            if key in ambiguous:
                # More than one Contact answers to this id. Writing to either
                # could overwrite the wrong person, so report instead of guess.
                results[i] = {
                    "success": False,
                    "errors": [{
                        "statusCode": "AMBIGUOUS_EXTERNAL_ID",
                        "message": f"{EXTERNAL_ID_FIELD} {key!r} matches more than "
                                   f"one Contact; resolve the duplicates in "
                                   f"Salesforce before syncing this member",
                    }],
                }
                continue

            payload = {"attributes": {"type": "Contact"}}
            for ts_field, (sf_field, transform) in FIELD_MAP.items():
                if ts_field not in r:
                    continue
                value = r[ts_field]
                if transform:
                    value = transform(value)
                # Omit empties instead of sending an explicit null. A null would
                # blank whatever Salesforce holds, and with Modify All this runs
                # against Contacts the chapter maintains by hand. Whitespace-only
                # counts as empty because Salesforce trims text on save, so " "
                # would blank the field just as effectively as null. Tested by
                # type rather than falsiness, so a legitimate 0 or False writes.
                if value is None or (isinstance(value, str) and not value.strip()):
                    continue
                payload[sf_field] = value

            contact_id = existing.get(key)
            if contact_id:
                # Update by Id. Membership_ID__c is deliberately left out: it
                # already holds this value, and rewriting a unique field is
                # needless index churn.
                payload["Id"] = contact_id
                updates.append((i, payload))
            else:
                # Normalized (str) rather than the raw Personid, so the value we
                # write is byte-identical to the key we later match on.
                payload[EXTERNAL_ID_FIELD] = key
                inserts.append((i, payload))

        print(
            f"Resolved {len(existing)} of {len(records)} Membership_IDs: "
            f"{len(updates)} updates, {len(inserts)} inserts."
        )
        if ambiguous:
            print(f"WARNING: {len(ambiguous)} Membership_ID(s) match multiple "
                  f"Contacts and were NOT written: {sorted(ambiguous)}")

        self._write_collection("PATCH", updates, results, created=False)
        self._write_collection("POST", inserts, results, created=True)

        # Salesforce returns one result per submitted record; if a response ever
        # comes back short, the leftover slot stays None and would crash the
        # reporter. Make it an explicit failure so the run is flagged instead.
        for i, res in enumerate(results):
            if res is None:
                results[i] = {
                    "success": False,
                    "errors": [{"statusCode": "NO_RESULT",
                                "message": "no result returned by Salesforce"}],
                }

        failures = [r for r in results if not r.get("success")]
        print(f"Wrote {len(results) - len(failures)}/{len(results)} Contacts.")
        if failures:
            print(f"{len(failures)} failures, e.g.: {failures[:3]}")
        return results
