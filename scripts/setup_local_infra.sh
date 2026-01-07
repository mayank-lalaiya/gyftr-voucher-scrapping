#!/bin/bash
set -e

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

trap 'echo -e "\n${RED}❌ Error: Script failed on line $LINENO. Check the error message above.${NC}"; exit 1' ERR

echo -e "${BLUE}===============================================${NC}"
echo -e "${BLUE}   GyFTR Voucher - Local Setup (No Cloud)      ${NC}"
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

# Project Setup
echo -e "\n${YELLOW}Step 2: Project Configuration${NC}"
echo "This requires a Google Cloud Project to access Gmail/Sheets APIs."
echo "Do you want to create a NEW project or use an EXISTING one?"
select opt in "New" "Existing"; do
    case $opt in
        New)
            read -p "Enter a unique project ID (e.g., gyftr-local-01): " PROJECT_ID
            echo "Creating project $PROJECT_ID..."
            gcloud projects create $PROJECT_ID --name="Gyftr Local"
            break
            ;;
        Existing)
            echo "Fetching available projects..."
            projects=($(gcloud projects list --format="value(projectId)"))
            
            if [ ${#projects[@]} -eq 0 ]; then
                echo "No existing projects found."
                read -p "Enter your existing Project ID: " PROJECT_ID
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
                        echo "Invalid selection."
                    fi
                done
            fi
            break
            ;;
        *) echo "Invalid option";;
    esac
done

echo "Setting active project to $PROJECT_ID..."
gcloud config set project $PROJECT_ID

# Enable APIs
echo -e "\n${YELLOW}Step 3: Enabling APIs (Gmail & Sheets only)...${NC}"
gcloud services enable \
    gmail.googleapis.com \
    sheets.googleapis.com

# Save to .env so Stage 2 can detect it
ENV_FILE="$(dirname "$0")/../.env"
if [ -f "$ENV_FILE" ]; then
    if grep -q "^PROJECT_ID=" "$ENV_FILE"; then
        # MacOS/Linux compatible sed
        sed -i.bak "s/^PROJECT_ID=.*/PROJECT_ID=$PROJECT_ID/" "$ENV_FILE" && rm "${ENV_FILE}.bak"
    else
        echo "PROJECT_ID=$PROJECT_ID" >> "$ENV_FILE"
    fi
else
    echo "PROJECT_ID=$PROJECT_ID" > "$ENV_FILE"
fi

echo -e "${GREEN}✅ Local API Setup Complete!${NC}"
echo -e "\n${BLUE}Next Steps:${NC}"
echo "1. Go to: https://console.cloud.google.com/apis/credentials?project=$PROJECT_ID"
echo "2. Create OAuth Client ID (Type: Desktop App)."
echo "3. Download JSON, rename to 'credentials.json', and place in project root."
echo "4. Run: python scripts/setup_auth.py"
echo "5. Run: python scripts/backfill_vouchers.py"
