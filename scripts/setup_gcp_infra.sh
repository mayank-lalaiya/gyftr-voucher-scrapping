#!/bin/bash
set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Trap errors
trap 'echo -e "\n${RED}❌ Error: Script failed on line $LINENO. Check the error message above.${NC}"; exit 1' ERR

echo -e "${BLUE}===============================================${NC}"

echo -e "${BLUE}   GyFTR Voucher Automation - Cloud Setup      ${NC}"
echo -e "${BLUE}===============================================${NC}"

# Check gcloud
if ! command -v gcloud &> /dev/null; then
    echo -e "${RED}Error: gcloud CLI is not installed.${NC}"
    echo "Please install it from: https://cloud.google.com/sdk/docs/install"
    exit 1
fi

# Auth Login
if gcloud auth list --filter=status:ACTIVE --format="value(account)" | grep -q "@"; then
    CURRENT_USER=$(gcloud auth list --filter=status:ACTIVE --format="value(account)" | head -n 1)
    echo -e "Already logged in as: ${GREEN}$CURRENT_USER${NC}"
else
    echo -e "\n${YELLOW}Step 1: Authenticating with Google Cloud...${NC}"
    gcloud auth login --no-launch-browser --brief || gcloud auth login
fi

# Check for existing config
ENV_FILE="$(dirname "$0")/../.env"
DEFAULT_PROJECT=""
if [ -f "$ENV_FILE" ]; then
    DEFAULT_PROJECT=$(grep "^PROJECT_ID=" "$ENV_FILE" | cut -d'=' -f2)
fi

# Fallback: Check active gcloud project if not in .env
if [ -z "$DEFAULT_PROJECT" ]; then
    ACTIVE_PROJECT=$(gcloud config get-value project 2>/dev/null)
    if [ -n "$ACTIVE_PROJECT" ] && [ "$ACTIVE_PROJECT" != "(unset)" ]; then
        DEFAULT_PROJECT="$ACTIVE_PROJECT"
    fi
fi

# Project Setup
echo -e "\n${YELLOW}Step 2: Project Configuration${NC}"
if [ -n "$DEFAULT_PROJECT" ]; then
    echo -e "Found existing configuration for project: ${GREEN}$DEFAULT_PROJECT${NC}"
    read -p "Do you want to use this project? (y/n): " use_existing
    if [[ $use_existing =~ ^[Yy]$ ]]; then
        PROJECT_ID="$DEFAULT_PROJECT"
        echo "Using existing project..."
    else
        DEFAULT_PROJECT="" # Clear it to force selection
    fi
fi

if [ -z "$DEFAULT_PROJECT" ]; then
    echo "Do you want to create a NEW project or use an EXISTING one?"
    select opt in "New" "Existing"; do
        case $opt in
            New)
                read -p "Enter a unique project ID (e.g., gyftr-tracker-99): " PROJECT_ID
                echo "Creating project $PROJECT_ID..."
                gcloud projects create $PROJECT_ID --name="Gyftr Tracker"
                break
                ;;
            Existing)
                echo "Fetching available projects..."
                # Get list of projects
                projects=($(gcloud projects list --format="value(projectId)"))
                
                if [ ${#projects[@]} -eq 0 ]; then
                    echo "No existing projects found."
                    read -p "Enter your existing Project ID manually: " PROJECT_ID
                else
                    echo -e "${GREEN}Available Projects:${NC}"
                    for i in "${!projects[@]}"; do
                        echo "[$((i+1))] ${projects[$i]}"
                    done
                    
                    while true; do
                        read -p "Select a project (1-${#projects[@]}): " selection
                        if [[ "$selection" =~ ^[0-9]+$ ]] && [ "$selection" -ge 1 ] && [ "$selection" -le ${#projects[@]} ]; then
                            PROJECT_ID=${projects[$((selection-1))]}
                            break
                        else
                            echo "Invalid selection. Please try again."
                        fi
                    done
                fi
                echo "Selected Project: $PROJECT_ID"
                break
                ;;
            *) echo "Invalid option";;
        esac
    done
fi

echo "Setting active project to $PROJECT_ID..."

gcloud config set project $PROJECT_ID

# Enable APIs
echo -e "\n${YELLOW}Step 3: Enabling Required APIs...${NC}"
echo "(This may take a few minutes)"

if ! gcloud services enable \
    gmail.googleapis.com \
    sheets.googleapis.com \
    cloudfunctions.googleapis.com \
    run.googleapis.com \
    artifactregistry.googleapis.com \
    cloudbuild.googleapis.com \
    eventarc.googleapis.com \
    pubsub.googleapis.com; then

    echo -e "\n${RED}❌ Failed to enable APIs.${NC}"
    echo -e "${YELLOW}⚠️  CRITICAL: Billing is likely disabled.${NC}"
    echo "Google Cloud Functions & Cloud Run require a Billing Account (even for Free Tier usage)."
    echo "This is a Google requirement to prevent abuse."
    echo ""
    echo -e "👉 Action Required: Enable Billing for project '$PROJECT_ID' here:"
    echo -e "${BLUE}https://console.cloud.google.com/billing/linkedaccount?project=$PROJECT_ID${NC}"
    echo ""
    echo "Once you have linked a billing account, run this script again."
    exit 1
fi

# Pub/Sub Setup
echo -e "\n${YELLOW}Step 4: Creating Pub/Sub Topic...${NC}"
TOPIC_NAME="gmail-notifications"
if gcloud pubsub topics describe $TOPIC_NAME --project=$PROJECT_ID &>/dev/null; then
    echo "Topic $TOPIC_NAME already exists."
else
    gcloud pubsub topics create $TOPIC_NAME
    echo "Created topic: $TOPIC_NAME"
fi

# Add permission for Gmail to publish to this topic
echo "Granting Gmail API permission to publish to topic..."
gcloud pubsub topics add-iam-policy-binding $TOPIC_NAME \
    --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
    --role="roles/pubsub.publisher" \
    --project=$PROJECT_ID >/dev/null

# Save to .env
echo -e "\n${YELLOW}Step 5: Saving Configuration...${NC}"
ENV_FILE="$(dirname "$0")/../.env"

# Helper to update or append env var
update_env() {
    local key=$1
    local val=$2
    if grep -q "^$key=" "$ENV_FILE" 2>/dev/null; then
        # Use simple sed for Mac/Linux compatibility
        sed -i.bak "s/^$key=.*/$key=$val/" "$ENV_FILE" && rm "${ENV_FILE}.bak"
    else
        echo "$key=$val" >> "$ENV_FILE"
    fi
}

# Create .env if not exists
touch "$ENV_FILE"

update_env "PROJECT_ID" "$PROJECT_ID"
update_env "PUBSUB_TOPIC" "$TOPIC_NAME"
update_env "REGION" "us-central1"

echo -e "${GREEN}✅ Infrastructure setup complete!${NC}"
echo -e "Configuration saved to .env"

# Finalize Setup
ROOT_DIR="$(dirname "$0")/.."
CREDENTIALS_FILE="$ROOT_DIR/credentials.json"

if [ ! -f "$CREDENTIALS_FILE" ]; then
    echo -e "\n${BLUE}Next Steps (Action Required):${NC}"
    echo "1. Go to: https://console.cloud.google.com/apis/credentials?project=$PROJECT_ID"
    echo "2. Create OAuth Client ID (Type: Desktop App)."
    echo "3. Download JSON, rename to 'credentials.json', and place it in the project root."
    echo "4. Then run: python scripts/setup_auth.py"
    exit 0
fi

echo -e "\n${YELLOW}Step 6: Final Configuration${NC}"
echo -e "Found 'credentials.json'. Automating Cloud Watch setup..."

# Check if we need to run auth (Stage 1 might have been skipped)
if [ -f "$ROOT_DIR/token.json" ]; then
    echo -e "✅ Found existing 'token.json'. Skipping manual auth."
else
    echo -e "\n--- Running setup_auth.py ---"
    python3 "$ROOT_DIR/scripts/setup_auth.py"
fi

echo -e "\n--- Running enable_cloud_watch.py ---"
python3 "$ROOT_DIR/scripts/enable_cloud_watch.py"

echo -e "\n${GREEN}🎉 Cloud Setup Fully Complete!${NC}"
echo "To deploy the bot, run: ./deploy.sh"
