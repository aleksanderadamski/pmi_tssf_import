# Session takeover — PMI ThoughtSpot → Salesforce sync

Handoff so a fresh session (mobile / cloud / desktop) can continue without this
session's chat history. Everything you need is in the repo. **No secrets are in
git** — `.env` and `salesforce_private_key.pem` are gitignored; supply
credentials via env vars or a local `.env` (see "Running").

## Read first
- `CLAUDE.md` — project overview + the **critical data gotchas** (authoritative).
- `SALESFORCE_SETUP.md` — full Salesforce org setup (Connected / External Client
  App, permission set, integration user) **+ a Troubleshooting section** of the
  real errors already hit and their fixes.
- `NEXT_STEPS.md` — rollout checklist, automation plan, certification reminder.
- `README.md` — data-shape notes + field mapping.

## Where things stand (done & merged to `main`)

**Membership sync is live and verified against PRODUCTION** (not just sandbox):
- Pulls currently-active chapter members from ThoughtSpot → upserts Salesforce
  Contacts, matched on `Membership_ID__c` (= PMI `Personid`). Insert **and**
  update-by-PMI-ID both proven in prod, no duplicates.
- **Fields synced:** `Personid→Membership_ID__c` is the upsert **match key**
  (`EXTERNAL_ID_FIELD` in `salesforce_client.py`, not in `FIELD_MAP`). The rest
  are `FIELD_MAP` entries: `Firstname→FirstName`, `Lastname→LastName`,
  `Primaryemail→Email`, `Startdateforterm→Chapter_Join_Date__c`,
  `Enddateforterm→Chapter_Expiration__c`.
- `Membership_ID__c` is **Text(18)** (8-digit PMI IDs failed at Text(7)).
- CLI: `--push-salesforce`, `--limit N` (push first N), `--personids a,b,c`
  (push only those PMI IDs — targeted re-sync), `--format csv|xlsx|json`,
  `--inspect`.
- `SF_PRIVATE_KEY` env var supported for cloud auth (no `.pem` file needed).
- Failure reporting in `reporting.py` (audit CSV + non-zero exit + optional
  `FAILURE_WEBHOOK_URL`).

**Production org (configured & working):**
- `SF_LOGIN_URL` = `https://pmipolandchapter.my.salesforce.com` — must be the
  **real My Domain URL**; `login.salesforce.com` gave `app_not_found`.
- Integration user `tsapi@pmi.org.pl`: **Salesforce Integration** user license +
  **Salesforce API Integration** permission-set license, profile **Minimum Access
  - API Only Integrations** (API-only, no full seat).
- **External Client App** with JWT digital-signature auth (public cert
  `salesforce_public_cert.crt` in repo; matching private `.pem` held locally,
  org-agnostic and reused across orgs).
- Permission set (License = **`Salesforce API Integration`**, NOT `--None--` and
  NOT the user license) grants Contact Read/Create/Edit + FLS on all synced
  fields (incl. `Email`) and pre-authorizes the app. Full walkthrough +
  pitfalls: `SALESFORCE_SETUP.md`.
- **Verified in prod:** 5 test members inserted, then updated with email:
  PMI `12202700, 7120616, 3295515, 5719592, 12050326`.

## Running

`config.py` reads all config from env vars (or a local gitignored `.env`). Set:
`TS_HOST`, `TS_USERNAME`, `TS_PASSWORD`, `TS_DATASET_ID`, `TS_ACTIVE_FLAG_VALUE`,
`SF_CLIENT_ID`, `SF_USERNAME`, `SF_LOGIN_URL`, `SF_API_VERSION`, and the JWT key
(`SF_PRIVATE_KEY_FILE` locally / `SF_PRIVATE_KEY` = PEM contents in cloud).
Verify auth first (read-only): `python test_sf_auth.py`.

- ⚠️ **A cloud Claude Code session cannot reach ThoughtSpot / Salesforce by
  default.** The environment network policy is `Trusted`, which blocks
  `pmi.thoughtspot.cloud` and `*.my.salesforce.com` (confirmed: connections
  reset). That's why prod testing was done **locally**. To run the sync from a
  cloud session, set the environment's network access to **Custom**, allow
  `pmi.thoughtspot.cloud` + `*.my.salesforce.com` (keep default package
  registries), and start a **new** session (network/env changes only apply to
  new sessions). Otherwise run locally.
- A full `--push-salesforce` writes **real** data — use sandbox creds for
  experiments; test with `--limit 5` / `--personids` first.

## THE ACTIVE TASK (next): certifications → flat Contact fields

**Goal (decided with the user):** put certifications on the Contact as **flat
per-credential fields** (not a child object). First build: **PMP structured
fields + a `Certifications` summary field.** Investigation is done — reproduce
with `investigate_certifications.py`. Key findings:
- Person-level cert columns return **one row per person, no fan-out** (safe to
  flatten, no dedup needed).
- **PMP fully available**: `Pmppipelinestatus` (17 values), `Pmpstartdate`,
  `Pmpexpiredate`, `Pmporiginalgrantdate` (~2,600 members).
- `Certificationlist` = ready-made per-person summary string (e.g. `"PMI-ACP, PMP"`).
- CAPM/ACP/RMP: only an ID column each; other credentials: no dedicated columns.
  Generic per-cert fields come back NULL at person grain (cert-grain query, a
  later separate effort).
- Cert dates are epoch seconds; some carry the `1900-01-01` sentinel
  (`-2208988800`) = "no expiry" → `date_utils.to_salesforce_date` maps to `None`.

**Do these in order:**
1. **Create the Salesforce Contact fields FIRST** (sandbox → prod, separate orgs)
   — or the next `--push-salesforce` breaks on unknown fields:
   | API name | Type | ThoughtSpot source |
   |---|---|---|
   | `PMP_Status__c` | Text(40) | `Pmppipelinestatus` |
   | `PMP_Start_Date__c` | Date | `Pmpstartdate` |
   | `PMP_Expiration__c` | Date | `Pmpexpiredate` |
   | `PMP_Original_Grant_Date__c` | Date | `Pmporiginalgrantdate` |
   | `Certifications__c` | Text(255) | `Certificationlist` |
   Then **add Read+Edit FLS on all 5 to the permission set** (`SALESFORCE_SETUP.md`
   step 2b). Confirm exact API names with the user before wiring code.
2. **Wire the code** (once fields exist):
   - `fetch_members.py`: add the 5 source columns to `FIELDS` and `QUERY_FIELDS`
     (the 3 PMP **date** columns need the `|daily` binding; `Pmppipelinestatus`
     and `Certificationlist` are plain); add the 3 PMP dates to `DATE_FIELDS`;
     carry all 5 **first-non-null** in `dedupe_by_person()` (person-stable, like
     `Firstname`/`Primaryemail` — NOT min/max like the term dates).
   - `salesforce_client.py` `FIELD_MAP`: add 5 entries —
     `Pmppipelinestatus→("PMP_Status__c", None)`,
     `Pmpstartdate→("PMP_Start_Date__c", to_salesforce_date)`,
     `Pmpexpiredate→("PMP_Expiration__c", to_salesforce_date)`,
     `Pmporiginalgrantdate→("PMP_Original_Grant_Date__c", to_salesforce_date)`,
     `Certificationlist→("Certifications__c", None)`.
3. **Test:** `python fetch_members.py --push-salesforce --limit 5` (or
   `--personids <known ids>`) against sandbox, then spot-check a Contact.

## Remaining after certifications
- Full prod run (`--push-salesforce`, no `--limit`), spot-check broadly.
- Automation: GitHub Actions scheduled run + repo secrets + failure webhook
  (`NEXT_STEPS.md` step 10 has the mechanism).

## Workflow conventions (from CLAUDE.md)
- **Before any `git commit`, run the `code-reviewer` subagent** on the diff;
  resolve blockers and apply any flagged doc updates in the same commit.
- Changes go via a branch → PR into `main`. **Never push without the user's
  say-so.** `main` is the default branch.
- If a change adds/changes an invariant, update `CLAUDE.md` in the same commit.

## Environment note
Prefer a **non-synced** local path (`C:\dev\...`). The desktop repo lived in a
cloud-synced folder (Google Drive, then OneDrive) which repeatedly corrupted
`.git`. Cloud/mobile sessions clone fresh from GitHub each time.
