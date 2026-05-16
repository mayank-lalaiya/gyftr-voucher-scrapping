# GyFTR Voucher Automation

Automated system to scan Gmail for GyFTR gift card emails (Myntra, Swiggy, Amazon, etc.), extract voucher details, and save them to a Google Sheet.

Designed for users who want to organize their vouchers automatically.

## ✨ Features
*   **Intelligent Parsing**: Extracts Brand, **Logo**, Value, Code, PIN, Expiry Date, and **Email Date** from emails.
*   **Deduplication**: Never adds the same voucher twice.
*   **Dual Modes**:
    1.  **Local Backfill**: Scan your entire history of emails manually (No recurring cost).
    2.  **Cloud Automation**: A "set & forget" bot that runs 24/7 on Google Cloud (Free tier eligible).

---

## 🚀 Usage Guide

This project is designed to grow with your needs. You can start simple (local script) and upgrade to full automation (cloud bot) seamlessly.

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
    1.  Deploys the main **email processing** Cloud Function (Pub/Sub triggered).
    2.  Deploys a **watch renewal** Cloud Function (HTTP triggered, not publicly accessible).
    3.  Creates a **Cloud Scheduler** job that calls the renewal function every 6 days to keep Gmail push notifications active (they expire every 7 days).
*   **Test**: Reply to a GyFTR email or wait for a new one. It will appear in your sheet in ~30 seconds.

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
│   └── backfill_vouchers.py   # Scans past emails manually
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
    *   The bot stores a cursor in the spreadsheet under a `_config` tab (`LAST_GMAIL_HISTORY_ID`).

**Q: Gmail Watch expired and emails stopped being processed.**
*   A: Gmail push notifications expire every 7 days. The `deploy.sh` script sets up Cloud Scheduler to auto-renew every 6 days. If it's not working:
    1.  Check the scheduler job: `gcloud scheduler jobs describe gyftr-renew-gmail-watch --location us-central1`
    2.  Run it manually: `gcloud scheduler jobs run gyftr-renew-gmail-watch --location us-central1`
    3.  As a fallback, run locally: `python3 scripts/enable_cloud_watch.py`

## 💰 Cost & Limits

For typical personal use, this project runs entirely within the **Google Cloud Free Tier**.

### Estimated Costs
| Resource | Personal Use (~100 emails/mo) | Cost | Free Tier Limit |
| :--- | :--- | :--- | :--- |
| **Cloud Functions** | ~100 invocations | **$0.00** | 2,000,000 invocations/mo |
| **Pub/Sub** | ~100 messages | **$0.00** | 10 GB/mo |
| **Cloud Build** | ~5-10 deploys/mo | **$0.00** | 120 build-minutes/day || **Cloud Scheduler** | ~5 jobs/mo (watch renewal) | **$0.00** | 3 free jobs/account |
### System Limits

1.  **Batch Size**: Cloud mode processes incrementally (Gmail History API) and caps the number of messages processed per run. Local backfill can scan larger ranges in batches.
2.  **Execution Time**: The Cloud Function has a default timeout of 60 seconds. Scanning 50 emails takes ~20-30 seconds.
3.  **API Quotas**:
    *   **Gmail**: 1,000,000,000 units/day (This script uses ~500 units/run).
    *   **Sheets**: 60 writes/min (This script writes in a single batch).

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



