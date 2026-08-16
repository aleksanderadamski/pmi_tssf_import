import os
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name} (see .env.example)")
    return value


TS_HOST = _require("TS_HOST")
TS_USERNAME = _require("TS_USERNAME")
TS_PASSWORD = _require("TS_PASSWORD")
TS_ORG_ID = int(os.environ.get("TS_ORG_ID", "0"))
TS_DATASET_NAME = os.environ.get("TS_DATASET_NAME", "Chapter Membership Dataset v2")
TS_DATASET_ID = os.environ.get("TS_DATASET_ID") or None
TS_ACTIVE_FLAG_VALUE = os.environ.get("TS_ACTIVE_FLAG_VALUE", "1")

SF_CLIENT_ID = os.environ.get("SF_CLIENT_ID")
SF_USERNAME = os.environ.get("SF_USERNAME")
# Two ways to supply the JWT signing key. Local runs use SF_PRIVATE_KEY_FILE (a
# path to the .pem). Cloud runs (Codespaces, mobile Claude Code, GitHub Actions)
# can't ship a file, so SF_PRIVATE_KEY holds the PEM contents directly as a
# secret. If both are set, SF_PRIVATE_KEY wins. See salesforce_client._load_private_key.
SF_PRIVATE_KEY_FILE = os.environ.get("SF_PRIVATE_KEY_FILE")
SF_PRIVATE_KEY = os.environ.get("SF_PRIVATE_KEY")
# No default host on purpose: it must be the org's real My Domain URL
# (test.salesforce.com / login.salesforce.com no longer work for External
# Client Apps as of Spring '26), so a wrong default would silently misauth.
SF_LOGIN_URL = os.environ.get("SF_LOGIN_URL", "")
SF_API_VERSION = os.environ.get("SF_API_VERSION", "v61.0")
