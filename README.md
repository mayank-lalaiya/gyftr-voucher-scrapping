# GyFTR Voucher Automation

Automated system to scan Gmail for GyFTR gift card emails (Myntra, Swiggy, Amazon, etc.), extract voucher details, and save them to a Google Sheet.

Designed for users who want to organize their vouchers automatically.

## ✨ Features
*   **Intelligent Parsing**: Extracts Brand, **Logo**, Value, Code, PIN, Expiry Date, and **Email Date** from emails.
    Supports current voucher cards and older SmartBuy tables and labeled voucher emails.
*   **Deduplication**: Checks voucher codes against existing rows and within each batch, making sequential retries safe.
*   **Dual Modes**:
    1.  **Local Backfill**: Scan your entire history of emails manually (No recurring cost).
    2.  **Cloud Automation**: A "set & forget" bot that runs 24/7 on Google Cloud (Free tier eligible).

---

### Retry-safe sheet writes

Voucher rows are appended and sorted newest-first in one atomic Sheets request.
If the request fails, its error propagates; there is no fallback append. If the
server commits but its response is lost, replay of the same request is rejected
atomically. The next invocation re-reads existing codes and skips the
already-written vouchers. Processing results report the number of rows actually
inserted, not the number of vouchers found.

Codes and PINs are literal text, preserving leading zeros and long identifiers.
Logo images remain formulas; amounts and recognized dates use native cell types.
Unrecognized amounts or dates are preserved as text with a warning. Existing
voucher rows are not rewritten or deduplicated by this change.

Empty parser results (for example, claim invitations) are distinct from parser
execution failures. Unexpected parser exceptions propagate to processing errors,
preserving pending work and the checkpoint for retry rather than skipping mail.

Every cloud processing attempt reads a revision from a hidden `_gyftr_write_guard`
tab before reading shared state. Every sheet mutation atomically deletes that
revision, creates a new revision with a never-reused sheet ID, and applies its
changes. A stale writer or transparent HTTP replay cannot delete the consumed
revision, so Sheets rejects its entire batch. This protects voucher deduplication,
header changes, pending queues, and checkpoints without a lease or extra cloud
service. Conflicts propagate for normal application-level retries; they are not
silently treated as success. Do not edit or delete the guard tab.

Large config values are stored as checksum-verified chunks of at most 40,000
characters in separate `_config` rows. The manifest and all chunks are written
atomically; old single-cell pending JSON remains readable. Shrinking or clearing
the queue clears obsolete chunks, while unrelated config rows remain unchanged.

Keep one maximum instance to limit cost and contention. Concurrent instances of
this updated processor are fenced, but older versions and external scripts do
not honor the revision protocol. Do not run legacy/manual writers concurrently.
For the first upgrade, the old processor must be quiesced and drained before the
new writer runs, followed by a Gmail history reconciliation. Simply setting
`--max-instances=1` does not prevent old/new version overlap during deployment.

### Memory and date handling

The main trigger lazily reuses one service factory per warm process. Its Sheets
client and generated `spreadsheets()` and `values()` resources are initialized
once and reused; recreating nested discovery resources can allocate substantial
memory even when the root client is already cached. Failed initialization
propagates and can be retried.

Only client objects are cached. Sheet metadata, checkpoint values, and existing
voucher rows are fetched again for each operation so retries do not use stale
state. The processor remains configured for **512 MB and one maximum instance**;
warm reuse does not make HTTP client objects thread-safe or justify
reducing the memory setting without measurement.

RFC email timestamps are converted to IST before being written as native Sheets
date/time values. The atomic write then sorts by that numeric Email Date, newest
first, retaining its date/time display format. Existing historical rows are not
reformatted by this optimization.

### Controlled trigger retries

The Gen1 handler fails the invocation when processing reports errors or
`has_more` is true. With `--retry` enabled on the main function, GCP redelivers
the **same notification**, resuming saved work in batches of up to 50 messages.
It uses platform-managed backoff, not a busy loop or another Scheduler job.
Only a completed, error-free batch is reported as successful.

Notifications are eligible for processing for **24 hours from their original
event timestamp**. At the deadline the handler logs an explicit expiry and
acknowledges the event without changing the cursor or any existing pending
batch. Expiry does not schedule recovery: a fresh Gmail notification or manual
recovery is needed. The 30-day expired-history search limit still applies.

Malformed payloads, missing/invalid `historyId` values, or missing/invalid event
timestamps are logged and acknowledged as rejected. They never trigger an
unbounded mailbox scan. Hand-written test notifications must include a valid
Gmail-style `historyId`; Pub/Sub supplies the event timestamp.

The main deployment command includes `--retry`. Deploy it together with the
durable-checkpoint and idempotent-writer code, keeping one maximum instance.
The [Gen1 platform retry window](https://cloud.google.com/functions/1stgendocs/bestpractices/retries)
is up to seven days; the 24-hour application guard can stop retries only when
the handler starts successfully. Startup failures before the guard runs still
follow the platform policy.

Regression tests use mocked Google APIs; deployment-tool tests also use the
installed `gcloud` CLI in offline preview/help/archive modes. They do not deploy
or access your mailbox or spreadsheet. For releases, run them in a
Python 3.11 virtual environment with [requirements.txt](requirements.txt)
installed, matching the deployed runtime's Python version:
```bash
python --version
python -m unittest discover -s tests -v
python -m pip check
```
The interpreter must be Python 3.11.x; syntax-only checks on a newer Python
version are not a substitute for runtime validation.

## 🚀 Usage Guide

This project is designed to grow with your needs. You can start simple (local script) and upgrade to full automation (cloud bot) seamlessly.

### Prerequisites

Run commands from the repository root. Install Python 3.11 and the
[Google Cloud CLI](https://cloud.google.com/sdk/docs/install), then create a local
environment and install dependencies:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Keep the environment active for the authentication, backfill, and release commands
below. On Windows, activate it with `.venv\Scripts\activate` instead.
Local credentials and `.env` are private: never commit them or copy them into
release source directories.

### 🟢 Stage 1: Local Backfill
*Perfect for: Getting started, scanning past emails, and verifying it works.*

**Prerequisite: Permissions**
If you are on Mac/Linux, ensure the scripts are executable:
```bash
chmod +x scripts/*.sh deploy.sh
```

**Step 1: Setup Local Environment**
```bash
./scripts/setup_local_infra.sh
```
*   *It will list your existing projects. Select one or create new.*
*   *Creates a project and enables minimum required APIs.*

**Step 2: Create Credentials**
1.  Go to [GCP Credentials](https://console.cloud.google.com/apis/credentials).
2.  **Important:** If you see a warning to "Configure Consent Screen":
    *   Click **Configure Consent Screen**.
    *   **Audience**: Choose **External** and click **Create**.
    *   **App Information**: Enter *App Name* (e.g., "Gyftr Scraper") and select your email for *User Support Email*.
    *   **Contact Information**: Enter your email address again for *Developer Contact Information*.
    *   **Finish**: Check the box for **"I agree to the Google API services user data policy"** and click **Continue**.
    *   **Publishing status**: If asked, keep it on **Testing** while you’re setting this up.
        *   Note: In **Testing** mode, Google issues refresh tokens that can expire (commonly ~7 days) for external apps.
        *   If you want long-running automation without re-auth, you can switch to **In production** later (you may still see the “unverified app” warning, but it works for personal use).
    *   **Test Users (Critical)**: On the Test Users step, click **Add Users**, add the exact Google account you’ll sign in with (e.g. `you@gmail.com`), and click **Save**.
        *   *If you skip this while in Testing, you will see: “Access blocked … can only be accessed by developer-approved testers” (403 `access_denied`).*
    *   Once created, click **Credentials** on the left sidebar to return.
3.  Click **Create Credentials** -> **OAuth Client ID**.
    *   *Note: If you see a "Metrics" dashboard with a "Create OAuth client" button, click that instead.*
4.  Application Type: Choose **Desktop App**.
    *   Name it "Local Scraper" (or anything you want).
    *   Click **Create**.
5.  Download the JSON file, rename it to `credentials.json`, and place it in this folder.

**Step 3: Connect & Initial Scan**
1.  Run the authentication script:
    ```bash
    python3 scripts/setup_auth.py
    ```
    *   *This generates your local login token.*
2.  Your browser will open to a Google permission screen.
    *   Check **Select all** (to allow reading emails and writing to sheets).
    *   Click **Continue**.
    *   **"Google hasn't verified this app" Warning**: This is expected because you created the app for personal use.
        *   Click **Advanced** (small link).
        *   Click **Go to Gyftr Scraper (unsafe)**.
        *   Proceed to grant permissions.
    *   **If you see “Access blocked … developer-approved testers”**: you are not added as a Test User on the OAuth consent screen (or you are signing in with a different Google account). Add your email under **OAuth consent screen → Test users**, wait a minute, and retry.
3.  Run the backfill script:
    ```bash
    python3 scripts/backfill_vouchers.py
    ```
    *   It will ask how many emails to scan per batch and whether to scan **all** matching emails.
    *   To recover older vouchers, choose **yes** for both including read/opened
        emails and scanning all matching emails. Automatic cloud recovery is
        limited to the last 30 days; this explicit backfill is not.
    *   Successful processing can mark matching messages as read. Review the final
        error summary; do not treat an interrupted or failed scan as complete.
*   **Result**: You now have a Google Sheet full of your past vouchers!
    *   *Columns*: Brand, Logo, Value, Code, Pin, Expiry, Email Date, etc.
*   **Stop here** if you only want to run this manually once in a while.

---

### 🔵 Stage 2: 24/7 Cloud Automation (Optional)
*Perfect for: "Set and forget". The bot wakes up instantly when a new email arrives.*

> **⚠️ Requirement**: To use Cloud Functions, you must **enable billing** on your Google Cloud Project.
> *   The Free Tier (2M invocations/month) is extremely generous and it is unlikely you will be charged for personal use.
> *   However, Google requires a credit card on file to enable the compute resources.

**Step 1: Upgrade Infrastructure**
Run this script to enable Cloud Functions and triggers.
```bash
./scripts/setup_gcp_infra.sh
```
*   *It will detect your project and create the necessary resources.*
*   *It enables the required APIs: Gmail, Sheets, Cloud Functions, Cloud Build, Pub/Sub, and **Cloud Scheduler**.*
*   *It will automatically connect your Gmail to the Cloud bot if you have run Stage 1.*  

> **Note**: If you already ran `setup_gcp_infra.sh` before the Cloud Scheduler API was added, enable it manually:
> ```bash
> gcloud services enable cloudscheduler.googleapis.com --project=YOUR_PROJECT_ID
> ```

**Step 2: Deploy Bot**
```bash
./deploy.sh
```
*   **Result**: The deploy script does three things:
    1.  Deploys the main **email processing** Cloud Function (Pub/Sub triggered, 512 MB memory).
    2.  Deploys a **watch renewal** Cloud Function (HTTP triggered, not publicly accessible).
    3.  Creates a **Cloud Scheduler** job that calls the renewal function every 6 days to keep Gmail push notifications active (they expire every 7 days).
*   **Test**: Wait for an actual new voucher email from `gifts@gyftr.com`, then
    confirm its voucher appears once in the sheet, sorted by Email Date.
    Replying to an old email is not a valid new-voucher test.
*   The processor automatically creates `_config` and the hidden
    `_gyftr_write_guard` tab. Do not edit/delete either tab or their chunk rows.

### Main-function-only upgrades

Use the full [deploy.sh](deploy.sh) for initial setup, not an isolated main-function
upgrade: it also deploys watch renewal and recreates Scheduler.

After the Python 3.11 release checks pass, prepare an upgrade without deploying:

```bash
python scripts/prepare_main_deploy.py \
  --project=YOUR_PROJECT_ID \
  --output="$HOME/gyftr-private-release"
```

The output directory must be new and outside this repository. It is private
(0700), with source archives and configuration/plan JSON files restricted to 0600.
The tool reads the deployed main source, saves non-secret configuration and
credential fingerprints (never environment values), and freezes runtime-only
candidate and rollback sources. It requires `cloudfunctions.functions.sourceCodeGet`
permission. The original source archive may contain sensitive files; keep this
directory private and never commit or upload it.

[.gcloudignore](.gcloudignore) permits only `main.py`, `requirements.txt`, and Python
modules in the explicitly listed package directories under `src/`. Virtual
environments, credentials, tests, scripts, and bytecode are excluded. Explicitly
extend the policy for new package directories or non-Python runtime assets.
The release checks validate Cloud Functions' actual ZIP creation as well as the
upload preview; those two paths handle directory exclusions differently.

The generated `deployment-plan.json` records file hashes and separate **main-only**
deployment/rollback argument lists; neither is executed. Before approving a release,
recheck the live version/configuration against `baseline.json`, verify both file
manifests, and rerun `gcloud meta list-files-for-upload` on the staged candidate.
The release command enables retries, retains Python 3.11, 512 MB and one maximum
instance, and omits environment-variable flags to preserve deployed credentials.
After deployment, verify credential fingerprints, function settings, real processing
and memory, and that watch renewal, Scheduler and artifact cleanup are unchanged.

For an installation already using this guarded writer, review the generated
`deployment-plan.json`, complete those checks, then execute its saved argument
list (not the full bootstrap script):

```bash
python - "$HOME/gyftr-private-release/deployment-plan.json" <<'PY'
import json
import subprocess
import sys

with open(sys.argv[1]) as stream:
    plan = json.load(stream)
subprocess.run(plan["deployCommand"], check=True)
PY
```

Do not use this command as an unattended deployment: the preparation tool captures
a baseline but does not enforce a version/hash preflight at execution time.
Do not edit staged files; prepare a new release if source or live settings change.
There is no need to re-run authentication, infrastructure setup, or initial
backfill for a routine source update.

**First upgrade from an older, unguarded release:** arrange a controlled
notification pause and drain all in-flight old executions before deploying.
Retain queued notifications and reconcile Gmail history after resuming; never
reset the committed cursor to the current mailbox history ID to skip the gap.
This needs a separate operator-controlled rollout, not the routine command above.
Do not casually clear the Cloud Functions-managed Pub/Sub push endpoint: restoring
its internal App Engine URL directly can fail validation. A failed deployment
must not leave delivery paused; verify that push delivery is active afterward.

Rollback rebuilds the saved old source with its previous retry setting; unpinned
dependencies can resolve differently, so this is not an exact runtime-image restore.
It does not undo sheet rows or checkpoints. Do not clear pending work or reset the
history cursor automatically; investigate outstanding work before rolling back.
Legacy source versions do not understand chunked pending state or the write guard.
Quiesce/drain writers and finish or explicitly migrate pending work before a
legacy rollback; do not run old and new writers side by side.

### Post-deployment checks

Use your project ID explicitly to avoid checking a different active project:

```bash
gcloud functions describe gyftr-automation-v1 \
  --project=YOUR_PROJECT_ID --region=us-central1 \
  --format='json(status,versionId,runtime,availableMemoryMb,maxInstances,eventTrigger)'
gcloud scheduler jobs describe gyftr-renew-gmail-watch \
  --project=YOUR_PROJECT_ID --location=us-central1 \
  --format='json(state,schedule,timeZone,lastAttemptTime,status)'
gcloud functions logs read gyftr-automation-v1 \
  --project=YOUR_PROJECT_ID --region=us-central1 --limit=50
```

Expect an ACTIVE main function, Python 3.11, 512 MB, one maximum instance, and
`eventTrigger.failurePolicy.retry` present. Scheduler should be enabled, and its
latest watch-renewal execution should return HTTP 200. Its calendar schedule runs
at 03:00 UTC on days 1, 7, 13, 19, 25, and 31 where those dates exist.

For a real purchase, match its email to the sheet: exactly one row per issued
code, newest-first native Email Date, and no processing errors. A subsequent
notification should leave already-written vouchers unchanged. An empty pending
queue and a caught-up history cursor indicate no unfinished work at that point.
Check actual Cloud Monitoring memory samples; local probes are not production
measurements. Treat logs as private and redact them before sharing.

### Deployment artifact cleanup

Artifact Registry stores deployment images and build caches separately from the
running functions. [Cloud Functions retains its own runtime image copy](https://firebase.google.com/docs/functions/manage-functions#clean_up_deployment_artifacts),
so these stored artifacts can be deleted without stopping deployed functions.
Deleting build caches can make the next deployment slower.

To automatically delete tagged and untagged artifacts older than one day in the
`gcf-artifacts` repository, apply the checked-in policy after the first deployment:

```bash
gcloud artifacts repositories set-cleanup-policies gcf-artifacts \
    --project=YOUR_PROJECT_ID \
    --location=us-central1 \
    --policy=scripts/artifact_cleanup_policy.json \
    --no-dry-run
```

Artifact Registry runs cleanup periodically, typically within one day; no Cloud
Scheduler job is needed. The policy persists across deployments and removes old
stored builds even if they have `latest` or version tags. Keep any artifacts you
need for inspection elsewhere before enabling it.

---

## 🛠️ Project Structure


```text
.
├── deploy.sh               # Deploys Cloud Functions + sets up Cloud Scheduler
├── main.py                 # Cloud Function entry points (email processing + watch renewal)
├── requirements.txt        # Python dependencies
├── scripts/
│   ├── setup_local_infra.sh   # (Option A) Setup for local running
│   ├── setup_gcp_infra.sh     # (Option B) Setup for Cloud Automation
│   ├── setup_auth.py          # Logins to Google & Creates Sheet
│   ├── enable_cloud_watch.py  # Connects Gmail to Cloud Pub/Sub
│   ├── backfill_vouchers.py   # Scans past emails manually
│   ├── prepare_main_deploy.py # Prepares main-only source, baseline and rollback
│   └── artifact_cleanup_policy.json # One-day deployment artifact retention
└── src/
    ├── config/             # Configuration & Env Vars
    ├── models/             # Data structures
    ├── parsers/            # BeautifulSoup logic to read HTML
    ├── repositories/       # Gmail API Wrapper
    └── services/           # Main logic loop
```

## ❓ Troubleshooting

**Q: I see a warning about "External Images" in Google Sheets.**
*   A: The Sheet uses links to brand logos extracted from emails. Google Sheets may ask for permission to display these external images. Click "Allow Access" if prompted.

**Q: I get "invalid_grant" or "Token has been expired" error.**
*   A: Your login token expired or was revoked. Run `python3 scripts/setup_auth.py` again to re-login.
    For cloud mode, re-authentication only changes local credentials: securely
    update the corresponding OAuth environment variables on **both** deployed
    functions as well. Never paste refresh tokens into logs or GitHub.

**Q: I get "403 Permission Denied" when writing to Sheets.**
*   A: Make sure the Google Sheets API is enabled. If running in the cloud, ensure `deploy.sh` was run *after* enabling the APIs.

**Q: I opened (read) the voucher email and it didn't get added.**
*   A: Cloud mode supports READ emails (it uses Gmail push `historyId` + Gmail History API). If it’s not updating:
    *   The Gmail Watch auto-renews every 6 days via Cloud Scheduler. If it stopped, you can manually renew:
        ```bash
        python3 scripts/enable_cloud_watch.py
        ```
    *   Or trigger the renewal function directly:
        ```bash
        gcloud scheduler jobs run gyftr-renew-gmail-watch --location us-central1
        ```
    *   Check logs:
        ```bash
        gcloud functions logs read gyftr-automation-v1 --region us-central1 --limit 50
        gcloud functions logs read gyftr-renew-watch --region us-central1 --limit 10
        ```
    *   The bot stores its last completed history window in the spreadsheet's `_config` tab (`LAST_GMAIL_HISTORY_ID`). `PENDING_GMAIL_HISTORY_BATCH` holds the target cursor, remaining message IDs, and discovery mode while work is unfinished. The cursor advances only after processing and sheet writing succeed; do not clear pending state or advance the cursor to bypass an error.
    *   Missing or expired Gmail history triggers a paginated search limited to **the last 30 days**. Older missing vouchers require an explicit backfill. Transient API failures preserve the checkpoint and pending work instead of resetting history. A message that Gmail reports as deleted (404) is logged and skipped.

**Q: Gmail Watch expired and emails stopped being processed.**
*   A: Gmail push notifications expire every 7 days. The `deploy.sh` script sets up Cloud Scheduler to auto-renew every 6 days. If it's not working:
    1.  Check the scheduler job: `gcloud scheduler jobs describe gyftr-renew-gmail-watch --location us-central1`
    2.  Run it manually: `gcloud scheduler jobs run gyftr-renew-gmail-watch --location us-central1`
    3.  As a fallback, run locally: `python3 scripts/enable_cloud_watch.py`

## 💰 Cost & Limits

Low-volume processing may fit the Google Cloud Free Tier, but a zero bill is not
guaranteed. Billing must be enabled, and allowances can be shared with other
projects on the same billing account.

Monitor function compute/invocations, Pub/Sub, builds, logs, Scheduler, and
Artifact Registry storage in Cloud Billing. This setup creates one watch-renewal
Scheduler job; its executions are not separate jobs. Deployment images and build
caches can consume hundreds of MB even when email traffic is tiny. One-day
cleanup reduces storage, but it is periodic, not immediate, and does not
guarantee remaining within a shared allowance. Check the
[current free-tier terms](https://cloud.google.com/free/docs/free-cloud-features)
and configure a billing budget alert.

### System Limits

1.  **Batch Size**: Cloud mode processes up to 50 discovered messages per invocation and saves remaining IDs for resumption. The handler requests redelivery when `has_more` is true; this requires Gen1 retries enabled at deployment and remains subject to the 24-hour notification window. Saving pending work alone does not schedule another invocation. Local backfill can scan larger ranges in batches.
2.  **Execution Time**: The Cloud Function has a default timeout of 60 seconds. Scanning 50 emails takes ~20-30 seconds.
3.  **API Quotas**:
    *   **Gmail**: 1,000,000,000 units/day (This script uses ~500 units/run).
    *   **Sheets**: 60 writes/min per user/project; voucher writes and `_config` progress updates both consume this quota.

## 🔒 Privacy & Security


*   **Your Data**: This code runs on **your** own Google Cloud project.
*   **Your Credentials**: Your `credentials.json` and `token.json` stay on your machine (or your private cloud instance).
*   **No 3rd Party**: No data is sent to any external server. It goes directly from your Gmail -> Your Script -> Your Google Sheet.

### Publishing to GitHub (Important)

*   This repo uses `.gitignore` to prevent committing secrets like `.env`, `token.json`, and `credentials.json`.
*   Double-check these files are NOT tracked before pushing:
    `git ls-files token.json credentials.json .env`
*   Avoid pasting raw `gcloud functions deploy ...` output into issues/chats. GCP can print environment variables in CLI output.
*   If you accidentally exposed `CLIENT_SECRET` or `REFRESH_TOKEN`, rotate them in Google Cloud Console and re-run `python3 scripts/setup_auth.py`.

## 🤝 Contributing

Feel free to fork this repository and submit Pull Requests if you find bugs or want to support more voucher formats!
