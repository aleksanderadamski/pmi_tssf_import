"""Salesforce JWT bearer auth + Contact upsert.

Field mapping (ThoughtSpot -> Salesforce Contact):
  Personid          -> Membership_ID__c  (external ID, used to match)
  Firstname         -> FirstName
  Lastname          -> LastName          (Salesforce requires this on insert;
                                           without it, upserting a Personid
                                           with no existing matching Contact
                                           fails REQUIRED_FIELD_MISSING)
  Startdateforterm  -> Chapter_Join_Date__c
  Enddateforterm    -> Chapter_Expiration__c

Upserts via the SObject Collections "upsert by external ID" endpoint
(PATCH /composite/sobjects/Contact/Membership_ID__c), 200 records per call
per Salesforce's limit. Untested against a live org — if the first call
errors, the response body names the bad field/record; this is the only
place the request shape lives.

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

import jwt
import requests

import config
from date_utils import to_salesforce_date

EXTERNAL_ID_FIELD = "Membership_ID__c"
CHUNK_SIZE = 200  # SObject Collections limit per call

# ts_field -> (sf_field, optional transform applied to the raw TS value)
FIELD_MAP = {
    "Firstname": ("FirstName", None),
    "Lastname": ("LastName", None),
    "Startdateforterm": ("Chapter_Join_Date__c", to_salesforce_date),
    "Enddateforterm": ("Chapter_Expiration__c", to_salesforce_date),
}


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i : i + size]


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

    def upsert_contacts(self, records: list[dict]) -> list[dict]:
        """records: list of {"Personid": ..., "Firstname": ..., "Lastname": ...,
        "Startdateforterm": ..., "Enddateforterm": ...} as pulled from
        ThoughtSpot (raw column names).

        Returns the flat list of per-record results Salesforce returns
        (in input order): {"id":..., "success": bool, "errors": [...]}.
        """
        payload_records = []
        for r in records:
            sf_record = {
                "attributes": {"type": "Contact"},
                EXTERNAL_ID_FIELD: r["Personid"],
            }
            for ts_field, (sf_field, transform) in FIELD_MAP.items():
                if ts_field in r:
                    value = r[ts_field]
                    sf_record[sf_field] = transform(value) if transform else value
            payload_records.append(sf_record)

        headers = self._headers()  # ensures authenticate() has run, sets _instance_url
        url = (
            f"{self._instance_url}/services/data/{config.SF_API_VERSION}"
            f"/composite/sobjects/Contact/{EXTERNAL_ID_FIELD}"
        )

        all_results = []
        for chunk in _chunks(payload_records, CHUNK_SIZE):
            resp = requests.patch(
                url,
                headers=headers,
                json={"allOrNone": False, "records": chunk},
            )
            if not resp.ok:
                raise RuntimeError(
                    f"Salesforce upsert failed ({resp.status_code}): {resp.text}"
                )
            all_results.extend(resp.json())

        failures = [r for r in all_results if not r.get("success")]
        print(f"Upserted {len(all_results) - len(failures)}/{len(all_results)} Contacts.")
        if failures:
            print(f"{len(failures)} failures, e.g.: {failures[:3]}")
        return all_results
