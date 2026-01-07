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
    *   **Test Users (Critical)**: On the Test Users step, click **Add Users**, enter your Google email address, and click **Save**.
        *   *Note: Without this, you will get an "Access Blocked: Authorization Error 403" when logging in.*
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
    python scripts/setup_auth.py
    ```
    *   *This generates your local login token.*
2.  Your browser will open to a Google permission screen.
    *   Check **Select all** (to allow reading emails and writing to sheets).
    *   Click **Continue**.
    *   **"Google hasn't verified this app" Warning**: This is expected because you created the app for personal use.
        *   Click **Advanced** (small link).
        *   Click **Go to Gyftr Scraper (unsafe)**.
        *   Proceed to grant permissions.
3.  Run the backfill script:
    ```bash
    python scripts/backfill_vouchers.py
    ```
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
*   *It will automatically connect your Gmail to the Cloud bot if you have run Stage 1.*  

**Step 2: Deploy Bot**
```bash
./deploy.sh
```
*   **Result**: The logic is now deployed to Google Cloud.
*   **Test**: Reply to a GyFTR email or wait for a new one. It will appear in your sheet in ~30 seconds.

---

## 🛠️ Project Structure


```text
.
├── deploy.sh               # script to deploy to Cloud Functions
├── main.py                 # Cloud Function entry point (Event listener)
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
*   A: Your login token expired or was revoked. Run `python scripts/setup_auth.py` again to re-login.

**Q: I get "403 Permission Denied" when writing to Sheets.**
*   A: Make sure the Google Sheets API is enabled. If running in the cloud, ensure `deploy.sh` was run *after* enabling the APIs.

## 💰 Cost & Limits

For typical personal use, this project runs entirely within the **Google Cloud Free Tier**.

### Estimated Costs
| Resource | Personal Use (~100 emails/mo) | Cost | Free Tier Limit |
| :--- | :--- | :--- | :--- |
| **Cloud Functions** | ~100 invocations | **$0.00** | 2,000,000 invocations/mo |
| **Pub/Sub** | ~100 messages | **$0.00** | 10 GB/mo |
| **Cloud Build** | ~5-10 deploys/mo | **$0.00** | 120 build-minutes/day |
| **Artifact Registry** | storing docker images | **~ $0 - $0.10** | 0.5 GB/mo storage is free |

*Note: The deployment script automatically deletes old container images (keeping only the recent 2) to ensure you stay within the free storage limit.*

### System Limits

1.  **Batch Size**: The scanner checks the **last 50 emails** from `gifts@gyftr.com`. If you have more than 50 *new* unread emails at once, run the backfill script multiple times.
2.  **Execution Time**: The Cloud Function has a default timeout of 60 seconds. Scanning 50 emails takes ~20-30 seconds.
3.  **API Quotas**:
    *   **Gmail**: 1,000,000,000 units/day (This script uses ~500 units/run).
    *   **Sheets**: 60 writes/min (This script writes in a single batch).

## 🔒 Privacy & Security


*   **Your Data**: This code runs on **your** own Google Cloud project.
*   **Your Credentials**: Your `credentials.json` and `token.json` stay on your machine (or your private cloud instance).
*   **No 3rd Party**: No data is sent to any external server. It goes directly from your Gmail -> Your Script -> Your Google Sheet.

## 🤝 Contributing

Feel free to fork this repository and submit Pull Requests if you find bugs or want to support more voucher formats!



