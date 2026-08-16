# ThoughtSpot → Salesforce member sync

Pulls Personid + name + chapter membership dates for **currently active**
members out of ThoughtSpot and upserts them into Salesforce Contacts,
matched on `Personid`.

## Status

- **ThoughtSpot extraction: confirmed working against the live instance,**
  including the currency fix below. Dataset: "Chapter Membership Dataset v2
  with Cert Details" (`6aa8c1ff-32b7-41ab-b990-e92c05b04582`, pinned in
  `.env` as `TS_DATASET_ID`).
- **Salesforce upsert: confirmed working against the sandbox.** Mapped as:
  - `Personid` → `Contact.Membership_ID__c` (external ID, used to match)
  - `Firstname` → `Contact.FirstName`
  - `Lastname` → `Contact.LastName` (required by Salesforce whenever the
    upsert creates a new Contact rather than updating an existing one)
  - `Startdateforterm` (deduped, see below) → `Contact.Chapter_Join_Date__c`
  - `Enddateforterm` (deduped) → `Contact.Chapter_Expiration__c`

### Data shape (confirmed from live pulls)

This dataset's grain is **(person, membership term)** — a long-tenured
member has one row per renewal cycle, e.g. one real person had 11
consecutive annual terms from 2016–2027. `dedupe_by_person()` in
`fetch_members.py` collapses that to one row per person:
- `Startdateforterm` = earliest term start across their rows (original
  join date)
- `Enddateforterm` = latest term end (current term's expiration)

**`Isactive` does not mean "active right now"** — confirmed live, it means
"was active during that historical term row." ~5,000 people have some row
with `Isactive=True`, but only ~2,500 have a term that hasn't actually
expired yet (matching the ThoughtSpot UI's active-member count exactly).
`filter_currently_active()` is the real filter: after dedup, keep only
people whose (max) `Enddateforterm` is still in the future. Relying on
`Isactive` alone silently doubled the synced population with lapsed members.

Date columns default to a **monthly bucket** in ThoughtSpot search unless
bound explicitly — `QUERY_FIELDS` requests `.daily` to get the real date.
Confirmed representation: **Unix epoch seconds** (e.g. `1546300800` =
2019-01-01), not milliseconds. `date_utils.py` is the single source of
truth for that conversion, used both for the exported file and for the
Salesforce `YYYY-MM-DD` format.

The xlsx field-mapping sheet this project started from has **drifted from
the live object** in more than one way — the dataset's real name has an
extra " with Cert Details" suffix, and fields like `Participationstatus`
it references don't exist on the live object at all. `list_columns.py`
pulls the authoritative live column list instead.

## Setup

1. `pip install -r requirements.txt`
2. `cp .env.example .env` and fill in `TS_USERNAME` / `TS_PASSWORD` (and the
   Salesforce values once you're ready for that part). `TS_DATASET_ID` is
   already pinned above.
3. Confirm what "active" looks like in the data (already done — boolean
   `true`/`false` — but re-run if anything about the source changes):
   ```
   python fetch_members.py --inspect
   ```
4. Pull the real data:
   ```
   python fetch_members.py --format xlsx
   ```
   (`--format` is `csv` by default; `xlsx` or `json` also available.) Writes
   `output/members.<ext>` with one deduped, currently-active row per member:
   `Personid, Firstname, Lastname, Startdateforterm, Enddateforterm` (dates
   as epoch seconds).
5. Once that file looks right and Salesforce is configured (see
   `salesforce_client.py` docstring for the Connected App setup), sync it in:
   ```
   python fetch_members.py --push-salesforce --limit 5
   ```
   **Test against a Salesforce sandbox first** — set `SF_LOGIN_URL` to the
   sandbox's real My Domain URL (`https://<org>--<sandbox>.sandbox.my.salesforce.com`),
   **not** `test.salesforce.com` (see the Salesforce auth note below). Check
   those 5 Contacts by hand, then drop `--limit` for a full run.

## Diagnostics

- `python list_datasets.py` — lists every Table/Worksheet/Model name +
  GUID visible to your credentials.
- `python list_columns.py` — lists every column on the configured dataset
  (live from ThoughtSpot metadata), for picking additional fields to sync.
- `python debug_searchdata.py` — dumps a raw 5-row `/searchdata` result
  (columns + rows) to `output/debug_searchdata_response.json`, for when
  something about the response shape needs re-checking.
- `python investigate_fields.py` / `python investigate_currency.py` —
  one-off scripts used to diagnose the `Isactive`-vs-currency issue above;
  kept around in case a similar "does this field mean what it sounds like"
  question comes up for a new field later.
- `python test_sf_auth.py` — authenticates to Salesforce and runs a trivial
  SOQL query, independent of the upsert logic. Run this before ever using
  `--push-salesforce`.

## Notes

- ThoughtSpot's public rate limit is 100 req/sec per IP with a burst of 10,
  and a single call caps at 100,000 rows — the ~13k-row term-level pull
  needs no pagination.
- The Connected App (an External Client App in this org) needed the
  `refresh_token`/`offline_access` OAuth scope in addition to `api` for the
  JWT bearer flow to work, and its integration user needed a Permission Set
  (License = `Salesforce API Integration`) both assigned to the user AND
  added to the app's pre-authorized list — assigning it to the user alone
  isn't enough.
- Sandbox/production API calls must use the org's real My Domain URL
  (`https://<org>.sandbox.my.salesforce.com`), not the generic
  `test.salesforce.com`/`login.salesforce.com` — Salesforce dropped legacy
  hostname support for External Client Apps as of Spring '26. Also don't
  confuse this with `*.my.salesforce-setup.com`, which is Setup-UI-only.
- The dedup + currency filter only see rows ThoughtSpot returns for this
  object — if someone lapsed and rejoined, "earliest join" means earliest
  within their current unbroken active streak, not their all-time-first
  join.

## Next steps

- Run `--push-salesforce --limit 5` against a **sandbox**, verify those
  Contacts by hand, then a full sandbox run, then repeat against production
  (separate org — separate Connected App/cert/permission set).
- Eventually: `git init`, push to GitHub, move `.env` values to Actions
  secrets, add a scheduled workflow (cron) to run this unattended.
