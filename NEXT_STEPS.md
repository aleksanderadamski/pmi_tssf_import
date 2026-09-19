# Next steps

Work through these in order. Each step tells you how to know it worked
before moving to the next.

## 1. Install dependencies

```
pip install -r requirements.txt
```

## 2. Configure ThoughtSpot access

1. `cp .env.example .env`
2. Fill in `TS_HOST`, `TS_USERNAME`, `TS_PASSWORD` in `.env`.
3. Run:
   ```
   python fetch_members.py --inspect
   ```
   **Success looks like:** it prints the resolved dataset GUID (copy it into
   `TS_DATASET_ID` in `.env` so future runs skip the lookup), then prints
   `Distinct Isactive values in sample of N rows: {...}`.
4. Set `TS_ACTIVE_FLAG_VALUE` in `.env` to whichever value from that set
   means "active" (e.g. `1`, `true`, `Yes`).

## 3. Pull the real data and eyeball it

*(Done as of this writing — steps 1–4 are complete: dataset resolved,
response shape confirmed, dates confirmed as epoch seconds, dedup rule
implemented. Re-run if you want to double check.)*

```
python fetch_members.py --format xlsx
```

**Success looks like:** `output/members.xlsx` with `Personid,
Startdateforterm, Enddateforterm` columns, one row per active member
(5,011 as of the last run) — dates as epoch-seconds ints (e.g.
`1546300800` = 2019-01-01).

## 4. Confirm the Salesforce Contact fields exist

In your Salesforce **sandbox** first (Setup > Object Manager > Contact >
Fields & Relationships), confirm these three custom fields exist with the
right types:
- `Membership_ID__c` — Text, marked **External ID** and **Unique**
- `Chapter_Join_Date__c` — Date
- `Chapter_Expiration__c` — Date

If any are missing, create them now — the upsert will fail on the first
record otherwise, and the error will be about a field name Salesforce
doesn't recognize.

## 5. Set up a Salesforce Connected App for JWT auth (sandbox)

1. **Generate a key pair and self-signed certificate** (run locally, do not
   commit these):
   ```
   openssl req -x509 -sha256 -nodes -days 365 -newkey rsa:2048 \
     -keyout salesforce_private_key.pem -out salesforce_public_cert.crt
   ```
   Any answers are fine for the certificate prompts (org name, etc.) — it's
   only used to prove the JWT came from you.
2. In the sandbox: **Setup > App Manager > New Connected App**.
   - Enable OAuth Settings.
   - Callback URL: any placeholder, e.g. `https://login.salesforce.com/services/oauth2/callback` (not actually used by JWT flow).
   - OAuth Scopes: add **both** **Manage user data via APIs (api)** and
     **Perform requests at any time (refresh_token, offline_access)** — JWT
     bearer needs the refresh/offline scope too (see `SALESFORCE_SETUP.md`
     and CLAUDE.md; with `api` alone auth fails `invalid_grant`).
   - Check **Use digital signatures**, upload `salesforce_public_cert.crt`.
   - Save. Wait ~10 minutes for it to propagate.
3. Copy the **Consumer Key** shown on the app page → `SF_CLIENT_ID` in `.env`.
4. **Setup > App Manager > (your app) > Manage > Edit Policies**:
   - Permitted Users: **Admin approved users are pre-authorized**.
5. Create a **Permission Set** that grants access to this Connected App
   (Setup > Permission Sets > New > assign the Connected App under
   "Assigned Connected Apps" — or via the app's "Manage Profiles/Permission
   Sets" button), and assign it to the integration user you'll use as
   `SF_USERNAME`.
6. Fill in `.env`:
   ```
   SF_CLIENT_ID=<consumer key from step 3>
   SF_USERNAME=<integration user's Salesforce username>
   SF_PRIVATE_KEY_FILE=./salesforce_private_key.pem
   # Sandbox's REAL My Domain URL, NOT test.salesforce.com — Salesforce dropped
   # legacy hostname support for External Client Apps as of Spring '26.
   SF_LOGIN_URL=https://<org>--<sandbox>.sandbox.my.salesforce.com
   ```

## 6. Sanity-check auth before touching real data

```
python test_sf_auth.py
```

**Success looks like:** `Authenticated. Instance URL: ...` followed by
`Query ok. totalSize=...`. If this fails, fix it here — don't move on to
`--push-salesforce` with a broken auth chain.

Common failures:
- `invalid_grant` → user not pre-authorized (redo step 5.4/5.5), or the
  Connected App hasn't finished propagating yet (wait longer).
- `invalid_client_id` → wrong `SF_CLIENT_ID`.
- Signature/JWT errors → cert uploaded to the Connected App doesn't match
  `SF_PRIVATE_KEY_FILE`.

## 7. Test the push with a handful of records (sandbox)

```
python fetch_members.py --push-salesforce --limit 5
```

**Success looks like:** `Upserted 5/5 Contacts.` Then, in the sandbox UI,
search for those 5 `Membership_ID__c` values under Contacts and confirm
`Chapter_Join_Date__c` / `Chapter_Expiration__c` look right.

If a record fails, the printed failure detail names the field/reason —
fix and re-run with `--limit 5` again before scaling up.

## 8. Full sandbox run

```
python fetch_members.py --push-salesforce
```

Spot-check a broader sample of Contacts in the sandbox.

## 9. Repeat for production

Production is a **separate Salesforce org** — steps 4 and 5 (fields +
Connected App + cert + permission set) need to be redone there (you can
reuse the same `.pem`/`.crt` pair, or generate a fresh one). Then:

```
# Production's REAL My Domain URL, NOT login.salesforce.com
SF_LOGIN_URL=https://<org>.my.salesforce.com
```

in `.env`, re-run step 6 (`test_sf_auth.py`) against prod, then step 7
(`--limit 5`) before a full `--push-salesforce` run.

## 10. Automate it

Once a full production run has been verified by hand:
- `git init` is done and `origin` is set; push to GitHub (`.env`, `*.pem`,
  `settings.local.json` are gitignored — double check `git status` before the
  first commit).
- Move `.env`'s values into GitHub Actions repository secrets.
- Add a scheduled workflow (`on: schedule`, cron) that installs
  dependencies and runs `python fetch_members.py --push-salesforce`.
- Decide a cadence (daily? weekly?) based on how often chapter membership
  data actually changes.

### Failure reporting for the scheduled job (mechanism already built)

`reporting.py` already handles failures three ways: a
`output/upsert_failures_<UTC>.csv` audit file, a non-zero exit code, and an
optional webhook POST. Chosen alerting for the scheduled run: **GitHub email +
Slack/Teams webhook.** When building the Actions workflow:
- The non-zero exit makes the job fail → GitHub emails the repo owner
  automatically (no config needed).
- Add an `actions/upload-artifact` step with `if: failure()` to upload
  `output/upsert_failures_*.csv`, so failed records are downloadable per run.
- Create a Slack/Teams **incoming webhook** and store its URL as a repo secret
  `FAILURE_WEBHOOK_URL`; pass it into the job's env so `reporting.notify_failures`
  posts a summary the moment a partial failure happens.

## 11. Run from the cloud instead of per-machine (TODO)

Motivation: the project is now worked on from **two computers**. Git syncs the
code, but never `.env`, never the `.pem`, and never the installed packages — so
each machine needs its own setup and the two drift. Moving execution to GitHub
removes that entirely: secrets live in one place and any browser can trigger a
run.

Already true, so this is less work than it looks:
- `salesforce_client._load_private_key` reads `SF_PRIVATE_KEY` (the PEM
  *contents*) when no file is available — built precisely for cloud runners.
- `reporting.py` already drives a non-zero exit, a failure CSV, and an optional
  webhook (see §10 above).

Do it in this order — read-only first, so a secrets mistake cannot touch
Salesforce data:

1. **Add repo secrets** (Settings → Secrets and variables → Actions), one per
   `.env` key: `TS_HOST`, `TS_USERNAME`, `TS_PASSWORD`, `TS_ORG_ID`,
   `TS_DATASET_NAME`, `TS_DATASET_ID`, `TS_ACTIVE_FLAG_VALUE`, `SF_CLIENT_ID`,
   `SF_USERNAME`, `SF_LOGIN_URL`, `SF_API_VERSION`, and `SF_PRIVATE_KEY` (paste
   the whole PEM, `BEGIN`/`END` lines included — **not** `SF_PRIVATE_KEY_FILE`,
   since no file ships to a runner). `FAILURE_WEBHOOK_URL` too if §10's
   alerting is wanted.
2. **Manual-trigger workflow** (`on: workflow_dispatch`) running only the
   read-only scripts: `test_sf_auth.py`, then `report_sentinel_dates.py`. This
   proves the secrets work with zero risk to Salesforce data.

   **Upload `output/sentinel_conflicts_*.csv` only — never `output/*.csv`.**
   That glob would sweep in `members.csv`, which the report always refreshes:
   ~2,464 members' names, emails and PMI ids, uploaded as an artifact anyone
   with repo read access can download, retained 90 days by default. `output/`
   is gitignored precisely to keep that data off GitHub; a wildcard artifact
   path quietly undoes it. Set an explicit short `retention-days` too.
3. **Then** the scheduled `--push-salesforce` job from §10, reusing the same
   secrets, plus `diagnose_duplicates.py` — it needs an
   `output/upsert_failures_*.csv` to read and exits immediately on a fresh
   runner, so it only becomes useful once a push has run.

Alternative if an interactive shell is wanted rather than a button:
**Codespaces** runs a full Linux VM in the browser (`pip install -r
requirements.txt` then run anything) with the same secrets mechanism. Note that
`github.dev` — pressing `.` on the repo — is an editor only and **cannot** run
Python; it is not an option here.

## 12. One member did not resolve to a Contact — RESOLVED 2026-09-18

`report_sentinel_dates.py` resolved 1,087 of 1,088 members and labelled the
last one "NOT VISIBLE". That label is broader than it sounds — the SOQL simply
returned nothing, which is equally true of a Contact hidden by record-level
sharing (the cause of the 209 `DUPLICATE_VALUE` failures) and of a member with
no Contact at all.

It was the second, now confirmed twice over. The first full push reported
`2463 updates, 1 inserts` — and because `Membership_ID__c` is a **unique**
external id whose index is org-wide, an insert that *succeeded* proves no
Contact held that id: not visible, not hidden, not in the Recycle Bin. That
rules out the sharing explanation outright.

The next run settled it: `2464 updates, 0 inserts`, and `verify_push.py` —
which fails on any submitted member without a Contact — reported `2464 of 2464
submitted members resolved in Salesforce`. The 1,088 members the earlier report
covered are a subset of those 2,464, so nothing it flagged remains unresolved.

(For anyone cross-checking the log: that verify run exited 1, on 72 Email values
stored lower-cased. Unrelated to this — the resolution count is what closes §12.)

## 13. `_min_ignore_none()` let a sentinel beat a real date — FIXED 2026-09-18

Found during the 2026-09-18 review. **Fixed the same day**; kept here as the
record of what was wrong and what to re-check if dedupe is ever touched.

Fix: `_min_ignore_none`/`_max_ignore_none` became `_earliest_date`/
`_latest_date`, both routing each operand through `_real_date_or_none()` so a
sentinel drops out of the comparison exactly as `None` does. Verified: a member
with one sentinel row and one real row now keeps the real date in both field
orders, and a normal multi-term member still gets earliest-join / latest-expiry.

The original problem, for context:

`-2208988800` is smaller than every plausible epoch, so in `dedupe_by_person()`
(`fetch_members.py:219` and `:231`) the two MIN merges pick the **sentinel** over
a real value:

```python
_min_ignore_none(-2208988800, 1546300800)  ->  -2208988800   # sentinel wins
_max_ignore_none(-2208988800, 1546300800)  ->   1546300800   # real date wins
```

Affected: `Startdateforterm` → `Chapter_Join_Date__c`, and
`Pmporiginalgrantdate` → `PMP_Original_Grant_Date__c`. A member with one
sentinel term row and one real row is deduped to the sentinel, the true date is
discarded, and since empties are omitted the field is then never written —
permanently. The MAX merges (`Enddateforterm`, `Pmpstartdate`, `Pmpexpiredate`)
are unaffected.

It had not bitten yet only because the 9 sentinel rows belong to members the
currency filter drops. That was luck, not design — and it violated the rule in
CLAUDE.md that *any raw-epoch comparison must gate on
`is_plausible_date_epoch()`*.

**Expect no observable change from this fix against today's data** — the only
sentinel-carrying members are ones the currency filter drops, so they never
reach a push at all. The fix matters for the future: *if* a currently-active
member ever carries a sentinel row alongside a real one, the dedupe now keeps
the real date and the next push writes it, instead of silently discarding it.

Re-check with `report_sentinel_dates.py` §1 whenever the source changes: a
per-member `omitted` count above zero is now a source fact (every row that
member has is sentinel), no longer something the merge could manufacture.

## 14. "Salesforce mangles Polish names" — investigated 2026-09-19, NOT reproduced

Reported: names hold Polish letters in ThoughtSpot but arrive transliterated in
Salesforce (`Ą`→`A`, `Ł`→`L`). Recorded so the next person who hears this has a
dated result instead of re-running the whole investigation.

Evidence, strongest first:

- `verify_push.py` compares `FirstName`/`LastName` (and every other mapped
  field) for **all 2,464** records the last push sent. It reported mismatches in
  `Email` only. Nothing is folding what we send.
- `diagnose_diacritics.py` then traced accented members end to end: sent and
  stored equal at every stage.
- Nothing in the fetch → transform → payload path calls `unicodedata`,
  `normalize`, or encodes to ASCII, and `requests`' `json=` escapes non-ASCII
  losslessly as `\uXXXX`. (The read-only checkers do: `verify_push.py`
  NFC-normalizes its *comparison* and `diagnose_diacritics.py` uses NFKD to
  classify a fold. Neither touches what is written.)

What was measured upstream: **182 of 2,467** currently-active members carry a
non-ASCII character in a name field in ThoughtSpot; the rest arrive as ASCII
already. Corroborating that, a human spot-check of the ThoughtSpot UI on
2026-09-19 found a common Polish given name present both ways across two
records — accented on one, plain on the other. Eyeballed, n=2, and two records
bearing the same given name may simply be two people who spell their own names
differently, so it supports the reading without establishing it.

**Closed on the full-population evidence** (`verify_push.py`, 2,464 records),
with the UI check as corroboration. That is also the standing guarantee going
forward — it compares `FirstName`/`LastName` on every pushed record,
NFC-normalized so a composed/decomposed byte difference does not cry wolf
(reported as INFO, since it would still mean something re-encodes names) while
a genuinely folded accent fails.

**What it does not cover:** the baseline is the push manifest, written *after*
the ThoughtSpot fetch, so it verifies the manifest → Salesforce leg only. If
`/searchdata` itself began folding characters, the manifest and Salesforce would
agree and it would pass indefinitely. Only `diagnose_diacritics.py` looks at
that leg — which is why the open hypothesis above is about the source, and why a
recurrence should be traced with a specific PMI id rather than re-run in bulk.

If it is reported again, do this rather than re-deriving: get the **specific**
PMI id and run `python diagnose_diacritics.py <id>`. A sample cannot refute a
claim about one person — a Salesforce Flow scoped by owner, record type or
created-date would fold some records and not others. If that trace shows
ThoughtSpot supplying an accented name and Salesforce storing a folded one,
this conclusion is overturned and the culprit is org-side.

## Reminder: certification fields (later effort)

When the certification sync is built (full plan in `TAKEOVER.md` → "certification
data → flat Contact fields"), two Salesforce-side steps must happen **before**
the next `--push-salesforce`, or it breaks on unknown/inaccessible fields:

1. **Create the new certification custom fields on Contact** (sandbox first,
   then production — separate orgs). Planned first build: `PMP_Status__c`
   (Text 40), `PMP_Start_Date__c` (Date), `PMP_Expiration__c` (Date),
   `PMP_Original_Grant_Date__c` (Date), `Certifications__c` (Text 255). See
   `TAKEOVER.md` for the ThoughtSpot source columns and exact mapping.
2. **Extend the `ThoughtSpot Sync - Contact Access` permission set** with
   **Read + Edit FLS** on each of those new fields (same as the membership
   fields — see `SALESFORCE_SETUP.md` step 2b). Do it in every org the sync
   writes to. Missing FLS surfaces only at push time as `INSUFFICIENT_ACCESS`.
