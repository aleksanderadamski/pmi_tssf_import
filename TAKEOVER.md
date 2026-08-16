# Session takeover — continue the certification sync

This is a handoff so a fresh session (e.g. mobile Claude Code) can pick up the
work without the previous session's chat history. Everything you need is in this
repo. **No secrets are in git** — `.env` and `salesforce_private_key.pem` are
gitignored; a cloud session gets credentials from environment variables instead
(see "Running", below).

## Read these first
- `CLAUDE.md` — project overview + the **critical data gotchas** (authoritative).
- `NEXT_STEPS.md` — the rollout checklist and automation plan.
- `README.md` — setup + data-shape notes.

## Where things stand (done & merged)
- **Membership sync works end-to-end, verified against the Salesforce sandbox.**
  Pull currently-active chapter members from ThoughtSpot → upsert into Salesforce
  Contacts, matched on `Membership_ID__c` (= PMI ID). Insert + update-by-PMI-ID
  both proven, no duplicates. (`fetch_members.py --push-salesforce`.)
- `Membership_ID__c` in Salesforce is **Text(18)** (8-digit PMI IDs failed at the
  original Text(7) — ~43% of members).
- Failure reporting exists (`reporting.py`): audit CSV + non-zero exit + optional
  `FAILURE_WEBHOOK_URL`.
- `SF_PRIVATE_KEY` env var is now supported (PR #2) so cloud/mobile sessions can
  authenticate without a `.pem` file.

## THE ACTIVE TASK: certification data → flat Contact fields

**Goal (decided with the user):** put certifications on the Contact as **flat
per-credential fields** (not a child object). Chosen scope for the first build:
**PMP structured fields + a `Certifications` summary field.**

**Investigation is already done** — see `investigate_certifications.py` (run it
to reproduce). Key findings:
- Querying only person-level cert columns returns **one row per person, no
  fan-out**, and the dedicated per-credential columns are **stable per person**
  (safe to flatten, no dedup needed for them).
- **PMP is fully available** via dedicated columns: `Pmppipelinestatus`
  (Certified/Expired/Suspended/… 17 values), `Pmpstartdate`, `Pmpexpiredate`,
  `Pmporiginalgrantdate`. ~2,600 members have PMP data.
- **CAPM / ACP / RMP**: only an ID column each (`Capmcertificationid`,
  `Acpcertificationid`, `Rmpcertificationid`) — no dates/status.
- **Other credentials** (PgMP, PfMP, PMI-SP, PMI-PBA, CPMAI, …): **no dedicated
  columns** at all.
- `Certificationlist` = a ready-made **per-person summary string**, e.g.
  `"PMI-ACP, PMP"`, `"CAPM, PMP"` (70 distinct combos). Covers all credentials at
  once, but no per-cert dates/status.
- The **generic per-cert fields** (`Certificationid`, `Certificationtypename`,
  `Effectivestartdate/endate`, `Originalgrantdate`) come back **NULL at person
  grain** — they live at cert grain and get aggregated away. Structured data for
  *non-PMP* credentials would need a separate cert-grain query (keyed on
  `Personid-Cert`, which fans out) — a later, separate effort.
- Cert dates are epoch seconds, midnight-aligned. Some carry the **1900-01-01
  sentinel** (`-2208988800`) meaning "no expiry" — `date_utils.to_salesforce_date`
  already maps these to `None` (do not push a bogus 1900 date).

## NEXT ACTION (in order)

### 1. Create the Salesforce Contact fields FIRST (blocker)
Do NOT wire the code before these exist, or the next `--push-salesforce` breaks on
unknown fields. Create on **Contact** (sandbox first):

| API name | Type | Source (ThoughtSpot) |
|---|---|---|
| `PMP_Status__c` | Text(40) | `Pmppipelinestatus` |
| `PMP_Start_Date__c` | Date | `Pmpstartdate` |
| `PMP_Expiration__c` | Date | `Pmpexpiredate` |
| `PMP_Original_Grant_Date__c` | Date | `Pmporiginalgrantdate` |
| `Certifications__c` | Text(255) | `Certificationlist` |

Also grant **field-level security** (Read+Edit) on these to the
`ThoughtSpot Sync - Contact Access` permission set, same as the membership fields.
Confirm the exact API names with the user before wiring the code.

### 2. Wire the code (once fields exist)
- `fetch_members.py`:
  - Add the 5 source fields to `FIELDS`.
  - Add to `QUERY_FIELDS` (the 3 PMP **date** fields need the `|daily` binding;
    `Pmppipelinestatus` and `Certificationlist` are plain).
  - Add the 3 PMP date fields to `DATE_FIELDS` (so they convert to epoch).
  - In `dedupe_by_person()`, carry the 5 new fields **first-non-null** (they're
    person-stable — like `Firstname`/`Lastname`, NOT min/max like the term dates).
- `salesforce_client.py` `FIELD_MAP`: add 5 entries —
  `Pmppipelinestatus -> ("PMP_Status__c", None)`,
  `Pmpstartdate -> ("PMP_Start_Date__c", to_salesforce_date)`,
  `Pmpexpiredate -> ("PMP_Expiration__c", to_salesforce_date)`,
  `Pmporiginalgrantdate -> ("PMP_Original_Grant_Date__c", to_salesforce_date)`,
  `Certificationlist -> ("Certifications__c", None)`.

### 3. Test
`python fetch_members.py --push-salesforce --limit 5` against the sandbox, then
spot-check a Contact in the SF UI (PMP fields + Certifications populated).

## Running (cloud/mobile session)
`config.py` reads everything from environment variables, so set these as env vars
(no `.env` file needed in a cloud env): `TS_HOST`, `TS_USERNAME`, `TS_PASSWORD`,
`TS_DATASET_ID`, `TS_ACTIVE_FLAG_VALUE`, `SF_CLIENT_ID`, `SF_USERNAME`,
`SF_LOGIN_URL`, `SF_API_VERSION`, and **`SF_PRIVATE_KEY`** (the .pem contents;
leave `SF_PRIVATE_KEY_FILE` blank). Use **sandbox** credentials — a full
`--push-salesforce` writes real data. Verify auth first: `python test_sf_auth.py`.

## Workflow conventions (from CLAUDE.md)
- **Before any `git commit`, invoke the `code-reviewer` subagent** on the diff;
  resolve blockers and apply any doc updates it flags in the same commit.
- Changes go via a branch → PR into `main`. **Never push without the user's
  say-so.** `main` is the default branch.
- If a change adds/changes an invariant, update `CLAUDE.md` in the same commit —
  it's the single source of truth the reviewer reads.

## Environment note
The desktop local repo was in a cloud-synced folder (Google Drive, then OneDrive)
which repeatedly corrupted `.git` (injected `desktop.ini`, broke refs). Prefer a
non-synced path (`C:\dev\...`) on desktop, or just use cloud/mobile sessions that
clone fresh from GitHub each time.
