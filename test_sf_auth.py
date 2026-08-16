"""Sanity check: does the JWT bearer setup work at all?

Authenticates and runs a trivial SOQL query. Run this before ever touching
--push-salesforce, so a Connected App / cert / pre-authorization problem
shows up here instead of mid-upsert.

    python test_sf_auth.py
"""
import requests

import config
from salesforce_client import SalesforceClient


def main():
    client = SalesforceClient()
    client.authenticate()
    print(f"Authenticated. Instance URL: {client._instance_url}")

    resp = requests.get(
        f"{client._instance_url}/services/data/{config.SF_API_VERSION}/query",
        headers=client._headers(),
        params={"q": "SELECT Id, Name FROM Contact LIMIT 1"},
    )
    resp.raise_for_status()
    result = resp.json()
    print(f"Query ok. totalSize={result['totalSize']}")
    if result["records"]:
        print(f"Sample: {result['records'][0]}")


if __name__ == "__main__":
    main()
