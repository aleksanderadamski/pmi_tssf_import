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
   - OAuth Scopes: add **Manage user data via APIs (api)**.
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
