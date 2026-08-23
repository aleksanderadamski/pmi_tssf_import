# Salesforce org setup for the membership sync

End-to-end setup of the Salesforce side so `fetch_members.py --push-salesforce`
can authenticate (JWT bearer) and upsert Contacts. Covers three things in one
place:

1. the **Connected App** (JWT digital-signature auth),
2. the **Permission Set** (object + field access, app pre-authorization),
3. the **integration User** (API-only, no full seat).

Do this **per org** — a sandbox and production are separate orgs, so production
needs its own Connected App, permission set, and user even if the sandbox is
already working (fields too, if not migrated). Steps are the same; only
`SF_LOGIN_URL` and the Consumer Key differ.

The values you collect here map to `.env` (see `.env.example`):
`SF_CLIENT_ID`, `SF_USERNAME`, `SF_LOGIN_URL`, and the JWT key
(`SF_PRIVATE_KEY_FILE` locally / `SF_PRIVATE_KEY` in the cloud).

> **Prerequisite — custom fields.** The three membership fields must already
> exist on **Contact**: `Membership_ID__c` (Text, **External ID + Unique**),
> `Chapter_Join_Date__c` (Date), `Chapter_Expiration__c` (Date). See
> `NEXT_STEPS.md` step 4. Certification fields are a **later** effort — see the
> reminder at the bottom of `NEXT_STEPS.md`.

---

## 0. The signing key pair (org-agnostic — reuse across orgs)

The RSA key pair is **not tied to any Salesforce org**. It's just `openssl`
output on your machine; you upload the **public cert** to each org's Connected
App, and the matching **private key** signs the JWTs. Reuse the same pair for
sandbox and production, or generate a fresh one per org — both are valid.

- **Private key:** `salesforce_private_key.pem` — kept local, **gitignored**,
  never committed. In the cloud, its contents go in the `SF_PRIVATE_KEY` secret.
- **Public cert:** `salesforce_public_cert.crt` — committed in this repo; this
  is the file you upload to the Connected App.

Generate a fresh pair (only if you don't reuse an existing one):

```bash
openssl req -x509 -sha256 -nodes -days 365 -newkey rsa:2048 \
  -keyout salesforce_private_key.pem -out salesforce_public_cert.crt
```

**Verify the `.pem` and `.crt` are a matching pair** before uploading:

```bash
openssl rsa  -in salesforce_private_key.pem -noout -modulus | openssl md5
openssl x509 -in salesforce_public_cert.crt -noout -modulus | openssl md5
```

Identical hashes = a matching pair (safe to reuse). Different = the cert came
from a different key; generate a fresh pair and upload its `.crt`.

---

## 1. Connected App (JWT bearer auth)

In **Setup → App Manager → New Connected App** (classic Connected App; if your
org steers you to **New External Client App**, the equivalents are noted below).

1. **Basic info:** name it, e.g. `ThoughtSpot Membership Sync`.
2. **Enable OAuth Settings.**
3. **Callback URL:** any placeholder — the JWT flow doesn't use it, e.g.
   `https://login.salesforce.com/services/oauth2/callback`.
4. **OAuth Scopes** — add **both**:
   - **Manage user data via APIs (`api`)**
   - **Perform requests at any time (`refresh_token`, `offline_access`)**
   (JWT bearer needs the refresh/offline scope in addition to `api`.)
5. **Use digital signatures:** check it, and **upload `salesforce_public_cert.crt`**.
6. **Save**, then **wait ~10 minutes** for the app to propagate before testing.
7. Open the app and copy the **Consumer Key** → this is `SF_CLIENT_ID` in `.env`
   (it is org-specific: sandbox and production have different keys).
8. **Manage → Edit Policies → Permitted Users:**
   **"Admin approved users are pre-authorized."**
   (This is what makes JWT bearer authorize only pre-approved users — the
   permission set in step 2 is how you pre-approve the integration user.)

> **External Client App variant:** pre-authorization lives under the app's
> **Policies → OAuth Policies → Permitted Users: Admin approved users**, then
> attach the permission set there. Same trust model (per-app cert + permission
> set), different menu.

---

## 2. Permission Set

Grants the integration user object/field access and pre-authorizes it for the
Connected App.

### 2a. Create it with the right License

**Setup → Permission Sets → New.**

- **Label:** `ThoughtSpot Sync - Contact Access`
- **License:** this depends on the integration user's license (step 3):
  - If the user has the **Salesforce Integration** license →
    set **License = `Salesforce API Integration`**. This is required: a
    `--None--` permission set validates a permission like *Create Contacts*
    against the bare Integration license, which alone doesn't allow it, and the
    assignment fails with
    *"The user license doesn't allow the permission: Create Contacts."*
  - If the user has a full **Salesforce** or **Salesforce Platform** license →
    `--None--` is fine.

> The `License` field generally can't be changed after the permission set is
> created. If you picked the wrong one, create a new permission set.

### 2b. Contact object + field permissions

The sync **upserts** Contacts (creates new + edits existing), so it needs
Create **and** Edit. It does **not** read Contact data (matching on
`Membership_ID__c` happens server-side in the upsert), but Read is a
platform requirement alongside Create/Edit, so it stays checked.

**Object Settings → Contacts → Edit:**

- Object Permissions: ✅ **Read**, ✅ **Create**, ✅ **Edit**
  (leave Delete / View All / Modify All off).
- Field Permissions (Read + Edit):

  | Field | Read | Edit |
  |---|---|---|
  | `Membership_ID__c` | ✅ | ✅ |
  | `Chapter_Join_Date__c` | ✅ | ✅ |
  | `Chapter_Expiration__c` | ✅ | ✅ |
  | `Email` (standard) | ✅ | ✅ |
  | `First Name` (standard) | ✅ | ✅ |
  | `Last Name` (standard) | ✅ | ✅ |

  (`FirstName`/`LastName` are written because a new Contact needs `LastName`;
  granting Edit here avoids `REQUIRED_FIELD_MISSING` / `INSUFFICIENT_ACCESS`
  on insert.)

**Save.**

### 2c. API Enabled

- Full **Salesforce** / **Salesforce Platform** license: **System Permissions →
  Edit → ✅ API Enabled → Save**.
- **Salesforce Integration** license: **skip it** — that license is API-only by
  definition, so `API Enabled` is inherent and doesn't appear as a toggle in a
  `Salesforce API Integration`-licensed permission set. This is expected.

### 2d. Assigned Connected App (pre-authorization)

**Assigned Connected Apps → Edit →** move the Connected App from step 1 into
**Enabled**, **Save**. (Equivalent: from the app, **Manage → Manage
Profiles/Permission Sets → add this permission set**.)

---

## 3. Integration User (API-only, no full seat)

Use a **Salesforce Integration** license so you don't consume a full user seat.
Each org includes 5 of these free. The user is **API-only — it cannot log into
the UI** — which is exactly what an unattended sync wants.

- **User License:** `Salesforce Integration`
- **Profile:** `Minimum Access - API Only Integrations`

Create it under **Setup → Users → New User** (or reuse an existing integration
user), set a username → this is `SF_USERNAME` in `.env`.

### 3a. Permission Set License

The Salesforce Integration license carries the **`Salesforce API Integration`**
permission set license (PSL), and that PSL is what actually unlocks the object
CRUD granted in step 2. Confirm it's assigned:

**Setup → Users → (the user) → Permission Set License Assignments** — ensure
**`Salesforce API Integration`** is present. If missing, **Edit Assignments /
Assign Licenses → enable it → Save.** (Usually auto-assigned with the license,
but verify — its absence is the usual cause of the "Create Contacts" error.)

### 3b. Assign the Permission Set

From the permission set: **Manage Assignments → Add Assignment →** select the
integration user → **Assign → Done.** (Or from the user: **Permission Set
Assignments → Edit Assignments →** add it.)

With the `Salesforce API Integration`-licensed permission set (step 2a), this
assignment now succeeds instead of erroring on *Create Contacts*.

> **Need a full seat instead?** If your org's Integration-license config won't
> allow Contact CUD even after 3a, fall back to a **Salesforce Platform**
> license (covers Accounts/Contacts, consumes a seat) with a `--None--` license
> permission set. Only do this if the Integration route genuinely fails.

---

## 4. Configure `.env` and verify

Fill `.env` (see `.env.example`; `.env` is gitignored — never commit):

```ini
SF_CLIENT_ID=<Consumer Key from step 1.7>
SF_USERNAME=<integration user's username>
SF_PRIVATE_KEY_FILE=./salesforce_private_key.pem   # local; leave SF_PRIVATE_KEY blank
# Real My Domain URL — NOT test/login.salesforce.com, NOT the my.salesforce-setup.com host:
#   Sandbox:    https://<org>--<sandbox>.sandbox.my.salesforce.com
#   Production: https://<org>.my.salesforce.com
SF_LOGIN_URL=https://<org>.my.salesforce.com
SF_API_VERSION=v61.0
```

In the cloud (no `.pem` file): leave `SF_PRIVATE_KEY_FILE` blank and put the PEM
contents in `SF_PRIVATE_KEY` (it wins if both are set).

**Verify (read-only — safe against production):**

```bash
python test_sf_auth.py
```

Success: `Authenticated. Instance URL: ...` + `Query ok. totalSize=...`. This
proves the whole chain: Connected App, cert, pre-authorization, and permission
set all work.

Common failures:

| Symptom | Likely cause |
|---|---|
| `invalid_grant` | user not pre-authorized (step 1.8 / 2d / 3b), or the Connected App hasn't finished propagating (wait ~10 min) |
| `invalid_client_id` | wrong `SF_CLIENT_ID` (using the other org's key?) |
| signature / JWT error | cert uploaded to the app doesn't match the private key (re-run the step 0 modulus check) |
| `INSUFFICIENT_ACCESS` on a field, at push time | FLS missing for that field in the permission set (step 2b) |

Once `test_sf_auth.py` passes, continue with the push test in `NEXT_STEPS.md`
(steps 7–8: `--push-salesforce --limit 5`, then a full run).

---

## Troubleshooting (real cases hit during production setup)

These are the exact errors seen while first wiring up the production org, with
the root cause and fix, most-common first.

### `app_not_found` — "External client app is not installed in this org"
Auth (`test_sf_auth.py`) fails 400 before any signature check.
**Cause:** `SF_LOGIN_URL` was the legacy generic host (`https://login.salesforce.com`
/ `https://test.salesforce.com`). Salesforce dropped legacy hostname support for
External Client Apps, so JWT sent there can't find the app.
**Fix:** set `SF_LOGIN_URL` to the org's **real My Domain URL**
(`https://<org>.my.salesforce.com`), found under **Setup → My Domain → Current
My Domain URL**. Not `login/test.salesforce.com`, not the `my.salesforce-setup.com`
Setup UI host, not the `.lightning.force.com` host. `SF_LOGIN_URL` is used both as
the token endpoint and the JWT `aud`, so this one value must be correct.

### `INVALID_TYPE` — "sObject type 'Contact' is not supported"
Auth succeeds, but a query/upsert against Contact fails 400.
**Cause:** the integration user has **no Read** on the Contact object. Usually the
object permissions were configured in a permission set that is *not the one
assigned* (e.g. an early `--None--`-licensed set whose assignment failed), or the
object-level **Read** box was never checked (only field permissions were set).
**Fix:** in the **assigned** permission set, **Object Settings → Contacts → Read**
(plus Create/Edit) — see step 2b. Confirm which set is actually assigned under
**Setup → Users → (user) → Permission Set Assignments**, and remove stray
experimental sets so there's no ambiguity.

### "The user license doesn't allow the permission: Create Contacts"
Assigning the permission set to the Salesforce Integration user fails.
**Cause:** the permission set's **License** was `--None--`, so a permission like
*Create Contacts* is validated against the bare Salesforce Integration user
license, which alone doesn't allow it.
**Fix:** create the permission set with **License = `Salesforce API Integration`**
(the PSL bundled with the Integration user license) — see step 2a — and confirm
that PSL is assigned to the user (step 3a).

### Contact not selectable in a permission set's Object Settings
You set the permission set License and now can't add Contact field/object perms.
**Cause:** the License was set to the **user license** `Salesforce Integration`
instead of the **permission set license** `Salesforce API Integration`. The user
license option is more restrictive and doesn't expose Contact. These two
similarly-named things are different: the user license goes on the *user*; the
PSL goes in the permission set's *License* field.
**Fix:** use **`Salesforce API Integration`** as the permission set License.

### `API Enabled` missing from System Permissions
In a `Salesforce API Integration`-licensed permission set, there is no
`API Enabled` checkbox to turn on.
**Cause / fix:** none needed — the Salesforce Integration license is API-only by
definition, so API access is inherent and not a togglable permission. Skip it
(see step 2c). It's only a real step for full Salesforce / Platform licenses.

### `INSUFFICIENT_ACCESS` on a field, only at push time
A field-specific error appears during `--push-salesforce`, not during auth.
**Cause:** the permission set is missing **FLS** for that field. (Hit when adding
`Email` to the sync — the standard `Contact.Email` field still needs FLS for the
API-only user.)
**Fix:** add **Read+Edit FLS** for that field in the assigned permission set
(step 2b), same as the membership fields.
