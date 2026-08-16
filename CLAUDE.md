# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Pulls currently-active chapter members from ThoughtSpot (`/searchdata` REST API v2.0) and upserts them into Salesforce Contacts (SObject Collections PATCH endpoint). The entry point is `fetch_members.py`.

## Setup

```
pip install -r requirements.txt
cp .env.example .env   # fill in TS_HOST, TS_USERNAME, TS_PASSWORD, SF_CLIENT_ID, etc.
```

## Commands

```bash
# Confirm what "active" looks like in the raw data (run once when source changes)
python fetch_members.py --inspect

# Pull active members to a file (csv/xlsx/json)
python fetch_members.py --format xlsx

# Verify Salesforce auth before any push
python test_sf_auth.py

# Test push: 5 records to sandbox, inspect them by hand in Salesforce UI
python fetch_members.py --push-salesforce --limit 5

# Full push
python fetch_members.py --push-salesforce

# Diagnostics
python list_datasets.py       # list all ThoughtSpot dataset names + GUIDs
python list_columns.py        # list columns on the configured dataset
python debug_searchdata.py    # dump raw 5-row /searchdata response to output/
```

## Architecture

All config is loaded via `config.py` from `.env`. The two clients authenticate lazily (first call triggers auth):

- **`thoughtspot_client.ThoughtSpotClient`** — wraps `/auth/token/full`, `/metadata/search`, and `/searchdata`. All API request shapes live here. Field names in `search_data()` use `"Fieldname|daily"` syntax to force day-level date buckets (ThoughtSpot defaults to monthly otherwise).

- **`salesforce_client.SalesforceClient`** — wraps JWT bearer auth (RS256, via `PyJWT`) and SObject Collections upsert (`PATCH /composite/sobjects/Contact/Membership_ID__c`). The field mapping from ThoughtSpot names to Salesforce field names is `FIELD_MAP` in this file.

- **`date_utils.py`** — single source of truth for date conversions. ThoughtSpot returns dates as Unix epoch seconds (confirmed: `1546300800` = 2019-01-01). `to_epoch_seconds()` normalizes any incoming format; `to_salesforce_date()` converts to `YYYY-MM-DD` for Salesforce. Out-of-range / sentinel epochs (e.g. `1900-01-01` = `-2208988800`, the source's "no expiry" marker) convert to `None`, not a bogus date, and the conversion avoids `datetime.fromtimestamp()` so it can't raise `OSError` on Windows. Any code that compares raw epochs must use `is_plausible_date_epoch()` so it treats sentinels the same way (see the gotcha below).

- **`fetch_members.py`** — orchestrates the pipeline: fetch → `dedupe_by_person()` (one row per Personid: earliest start, latest expiration) → `filter_currently_active()` (keep only rows where `Enddateforterm` ≥ today) → write file → optionally push. On `--push-salesforce` it exits non-zero if any record failed to upsert, so a scheduler flags the run.

- **`reporting.py`** — failure reporting for upsert runs, so an unattended/scheduled run can't fail silently: writes failed records + their Salesforce errors to `output/upsert_failures_<UTC>.csv`, drives the non-zero exit code, and (if `FAILURE_WEBHOOK_URL` is set) POSTs a Slack/Teams-compatible JSON summary. `report_upsert(records, results)` is the entry point.

## Critical data gotchas

- **`Isactive` does not mean "active right now."** It means "was active during that historical term row." After `dedupe_by_person()`, use `filter_currently_active()` — which checks that `Enddateforterm` hasn't passed — as the real currency filter. Using `Isactive` alone doubles the synced population with lapsed members.

- **Dataset grain is (person, term).** A member with 11 annual renewals has 11 rows. `dedupe_by_person()` collapses to one row: `Startdateforterm = MIN` (original join), `Enddateforterm = MAX` (current expiry).

- **Date columns need `.daily` binding.** Without `"Fieldname|daily"` in `QUERY_FIELDS`, ThoughtSpot returns a monthly bucket instead of the real date.

- **Sentinel dates exist in the source.** Some records carry `1900-01-01` (`-2208988800`) as a "no expiry" marker (confirmed on PMP cert expiries). `to_salesforce_date()` maps these to `None`; any raw-epoch comparison (like `filter_currently_active()`) must gate on `is_plausible_date_epoch()` so the two paths agree, or you get members silently dropped / `None` written where the filter kept them.

## Before committing

Before running `git commit`, invoke the `code-reviewer` subagent
(`subagent_type: "code-reviewer"`) on the pending diff. Then:

1. Resolve any **BLOCK** findings — never commit a secret leak (`.env`, `*.pem`,
   a hardcoded credential) or a correctness regression against the gotchas
   below.
2. Apply any documentation updates the reviewer flags: if a change outdates or
   adds to the "Critical data gotchas" / "Salesforce authentication notes"
   sections, update this file **in the same commit**. This file is the single
   source of truth for the project's invariants — the reviewer reads its gotcha
   list at review time, so keeping it current is what keeps the review current.

Pushing to a remote is always a separate, explicitly-approved step — never push
without the user's say-so.

## Salesforce authentication notes

- Use the org's real My Domain URL (`https://<org>.sandbox.my.salesforce.com`) as `SF_LOGIN_URL`, not `test.salesforce.com` — Salesforce dropped legacy hostname support for External Client Apps as of Spring '26.
- The Connected App needs `refresh_token`/`offline_access` OAuth scope in addition to `api` for JWT bearer to work.
- The integration user needs a Permission Set both assigned to the user AND added to the app's pre-authorized list.
- The JWT signing key can come from either `SF_PRIVATE_KEY_FILE` (path to the `.pem`, for local runs) or `SF_PRIVATE_KEY` (the PEM contents, for cloud runs like Codespaces / mobile Claude Code / GitHub Actions where no file ships). `SF_PRIVATE_KEY` wins if both are set; see `salesforce_client._load_private_key`.
