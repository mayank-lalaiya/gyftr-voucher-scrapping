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
    export $(grep -v '^#' .env | xargs)
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

# Defaults
FUNCTION_NAME="gyftr-automation-v1"
RUNTIME="python311"
ENTRY_POINT="process_pubsub_message"
REGION="${REGION:-us-central1}"

echo -e "\n${YELLOW}🚀 Deploying to Google Cloud Functions...${NC}"
echo "   - Project:  $PROJECT_ID"
echo "   - Region:   $REGION"
echo "   - Trigger:  $PUBSUB_TOPIC"

# 4. Deploy
DEPLOY_LOG_FILE="$(mktemp -t gyftr_deploy_XXXXXX.log)"

if gcloud functions deploy $FUNCTION_NAME \
    --gen2 \
    --region=$REGION \
    --runtime=$RUNTIME \
    --source=. \
    --entry-point=$ENTRY_POINT \
    --trigger-topic=$PUBSUB_TOPIC \
    --project=$PROJECT_ID \
    --set-env-vars GYFTR_SPREADSHEET_ID="$GYFTR_SPREADSHEET_ID" \
    --set-env-vars CLIENT_ID="$CLIENT_ID" \
    --set-env-vars CLIENT_SECRET="$CLIENT_SECRET" \
    --set-env-vars REFRESH_TOKEN="$REFRESH_TOKEN" \
    --quiet >"$DEPLOY_LOG_FILE" 2>&1; then

    echo -e "\n${GREEN}🎉 Deployment Successful!${NC}"
    echo "   Your bot is now live and listening for emails."
    echo "   View in Cloud Console: https://console.cloud.google.com/functions/details/$REGION/$FUNCTION_NAME?project=$PROJECT_ID"
    echo "   (Deploy logs saved to: $DEPLOY_LOG_FILE)"

    # 5. Cleanup Old Images (Cost Optimization)
    echo -e "\n${YELLOW}🧹 Cleaning up old container images (keeping latest 2)...${NC}"
    
    # Define repository details (Standard for Gen 2)
    REPO_NAME="gcf-artifacts"
    PACKAGE_NAME="$FUNCTION_NAME" # Usually matches function name directory
    
    # Check if we can list versions
    if gcloud artifacts versions list --package=$PACKAGE_NAME --repository=$REPO_NAME --location=$REGION --project=$PROJECT_ID --limit=1 &>/dev/null; then
        
        # List all versions, sort by update time descending, skip top 2, and delete the rest
        # We keep 2 just to be safe for immediate rollbacks if needed
        gcloud artifacts versions list \
            --package=$PACKAGE_NAME \
            --repository=$REPO_NAME \
            --location=$REGION \
            --project=$PROJECT_ID \
            --sort-by="~UPDATE_TIME" \
            --format="value(name)" | \
            tail -n +3 | \
            while read -r version; do
                echo "   Deleting old image version: $version"
                gcloud artifacts versions delete "$version" \
                    --package=$PACKAGE_NAME \
                    --repository=$REPO_NAME \
                    --location=$REGION \
                    --project=$PROJECT_ID \
                    --quiet || echo "   (Skipped or failed to delete $version)"
            done
            echo -e "${GREEN}✅ Cleanup complete.${NC}"
    else
        echo "   (Skipping cleanup: Repository or package not found/accessible yet)"
    fi

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


