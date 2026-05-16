#!/bin/bash

# Deployment Script for GyFTR Automation Cloud Function
set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}===============================================${NC}"
echo -e "${BLUE}   GyFTR Voucher Automation - Deployment       ${NC}"
echo -e "${BLUE}===============================================${NC}"

# 1. Check Prerequisites
if ! command -v gcloud &> /dev/null; then
    echo -e "${RED}❌ Error: gcloud CLI is not installed.${NC}"
    exit 1
fi

# 2. Load Configuration
if [ -f .env ]; then
    # More robust than: export $(grep ... | xargs)
    # - supports values with '='
    # - avoids word-splitting surprises
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
    echo -e "${GREEN}✅ Loaded configuration from .env${NC}"
else
    echo -e "${RED}❌ Error: .env file not found.${NC}"
    echo "   Please run the following commands first:"
    echo "   1. ./scripts/setup_gcp_infra.sh"
    echo "   2. python scripts/setup_auth.py"
    exit 1
fi

# 3. Validate Critical Variables
MISSING_VARS=0
check_var() {
    if [ -z "${!1}" ]; then
        echo -e "${RED}❌ Missing Config: $1${NC}"
        MISSING_VARS=1
    fi
}

check_var "PROJECT_ID"
check_var "PUBSUB_TOPIC"
check_var "CLIENT_ID"
check_var "CLIENT_SECRET"
check_var "REFRESH_TOKEN"
check_var "GYFTR_SPREADSHEET_ID"

if [ $MISSING_VARS -eq 1 ]; then
    echo -e "\n${YELLOW}⚠️  Configuration is incomplete.${NC}"
    echo "   Please re-run 'python scripts/setup_auth.py' to generate valid credentials."
    exit 1
fi

# Defaults (Gen1 only)
FUNCTION_NAME="gyftr-automation-v1"
WATCH_FUNCTION_NAME="gyftr-renew-watch"
SCHEDULER_JOB_NAME="gyftr-renew-gmail-watch"
REGION="${REGION:-us-central1}"
RUNTIME="python311"
ENTRY_POINT="process_pubsub_message_gen1"
WATCH_ENTRY_POINT="renew_gmail_watch"

echo -e "\n${YELLOW}🚀 Deploying to Google Cloud Functions (Gen1)...${NC}"
echo "   - Project:  $PROJECT_ID"
echo "   - Region:   $REGION"
echo "   - Trigger:  $PUBSUB_TOPIC"

# 4. Deploy
DEPLOY_LOG_FILE="$(mktemp -t gyftr_deploy_XXXXXX.log)"

DEPLOY_CMD=(gcloud functions deploy "$FUNCTION_NAME" \
    --no-gen2 \
    --region="$REGION" \
    --runtime="$RUNTIME" \
    --source=. \
    --entry-point="$ENTRY_POINT" \
    --trigger-topic="$PUBSUB_TOPIC" \
    --project="$PROJECT_ID" \
    --timeout=60s \
    --memory=256MB \
    --max-instances=1 \
    --set-env-vars GYFTR_SPREADSHEET_ID="$GYFTR_SPREADSHEET_ID" \
    --set-env-vars CLIENT_ID="$CLIENT_ID" \
    --set-env-vars CLIENT_SECRET="$CLIENT_SECRET" \
    --set-env-vars REFRESH_TOKEN="$REFRESH_TOKEN" \
    --quiet)

if "${DEPLOY_CMD[@]}" >"$DEPLOY_LOG_FILE" 2>&1; then

    echo -e "\n${GREEN}🎉 Deployment Successful!${NC}"
    echo "   Your bot is now live and listening for emails."
    echo "   View in Cloud Console: https://console.cloud.google.com/functions/details/$REGION/$FUNCTION_NAME?project=$PROJECT_ID"
    echo "   (Deploy logs saved to: $DEPLOY_LOG_FILE)"

else
    echo -e "\n${RED}❌ Deployment Failed.${NC}"

    echo "   Deploy logs saved to: $DEPLOY_LOG_FILE"
    echo "   --- Last 40 lines ---"
    tail -n 40 "$DEPLOY_LOG_FILE" || true
    echo "   Common issues:"
    echo "   - Cloud Build API not enabled (Run setup_gcp_infra.sh)"
    echo "   - Billing not enabled on project"
    exit 1
fi

# ─── 5. Deploy Watch Renewal Function (HTTP-triggered) ───
echo -e "\n${YELLOW}🔄 Deploying Gmail Watch renewal function...${NC}"

WATCH_LOG_FILE="$(mktemp -t gyftr_watch_deploy_XXXXXX.log)"

WATCH_CMD=(gcloud functions deploy "$WATCH_FUNCTION_NAME" \
    --no-gen2 \
    --region="$REGION" \
    --runtime="$RUNTIME" \
    --source=. \
    --entry-point="$WATCH_ENTRY_POINT" \
    --trigger-http \
    --no-allow-unauthenticated \
    --project="$PROJECT_ID" \
    --timeout=30s \
    --memory=128MB \
    --max-instances=1 \
    --set-env-vars PROJECT_ID="$PROJECT_ID" \
    --set-env-vars PUBSUB_TOPIC="$PUBSUB_TOPIC" \
    --set-env-vars CLIENT_ID="$CLIENT_ID" \
    --set-env-vars CLIENT_SECRET="$CLIENT_SECRET" \
    --set-env-vars REFRESH_TOKEN="$REFRESH_TOKEN" \
    --quiet)

if "${WATCH_CMD[@]}" >"$WATCH_LOG_FILE" 2>&1; then
    echo -e "${GREEN}✅ Watch renewal function deployed.${NC}"
else
    echo -e "${RED}❌ Watch renewal function deployment failed.${NC}"
    echo "   --- Last 20 lines ---"
    tail -n 20 "$WATCH_LOG_FILE" || true
    exit 1
fi

# ─── 6. Setup Cloud Scheduler (every 6 days) ───
echo -e "\n${YELLOW}⏰ Setting up Cloud Scheduler for automatic watch renewal...${NC}"

# Ensure the API is enabled
gcloud services enable cloudscheduler.googleapis.com --project="$PROJECT_ID" --quiet 2>/dev/null || true

# Get the function URL
WATCH_URL=$(gcloud functions describe "$WATCH_FUNCTION_NAME" \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --format="value(httpsTrigger.url)" 2>/dev/null)

if [ -z "$WATCH_URL" ]; then
    echo -e "${RED}❌ Could not retrieve watch function URL.${NC}"
    exit 1
fi

# Get the default service account for authentication
SA_EMAIL="${PROJECT_ID}@appspot.gserviceaccount.com"

# Delete existing job if present (idempotent re-deploy)
gcloud scheduler jobs delete "$SCHEDULER_JOB_NAME" \
    --location="$REGION" \
    --project="$PROJECT_ID" \
    --quiet 2>/dev/null || true

# Create scheduler job: runs every 6 days (buffer before 7-day expiry)
# Cron: "0 3 */6 * *" = at 03:00 UTC every 6th day
gcloud scheduler jobs create http "$SCHEDULER_JOB_NAME" \
    --location="$REGION" \
    --project="$PROJECT_ID" \
    --schedule="0 3 */6 * *" \
    --uri="$WATCH_URL" \
    --http-method=POST \
    --oidc-service-account-email="$SA_EMAIL" \
    --oidc-token-audience="$WATCH_URL" \
    --quiet 2>&1

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✅ Cloud Scheduler job created: runs every 6 days.${NC}"
    echo "   Job name:  $SCHEDULER_JOB_NAME"
    echo "   Schedule:  Every 6 days at 03:00 UTC"
    echo "   Target:    $WATCH_URL"
else
    echo -e "${YELLOW}⚠️  Cloud Scheduler setup failed. You may need to enable the Cloud Scheduler API:${NC}"
    echo "   gcloud services enable cloudscheduler.googleapis.com --project=$PROJECT_ID"
fi

echo -e "\n${GREEN}🎉 All done! Your bot is live with automatic watch renewal.${NC}"


