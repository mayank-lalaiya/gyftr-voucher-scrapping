"""
Authentication Setup Script
This script handles the OAuth flow for Gmail & Sheets API access.
Run this locally to generate token.json file for local development.

Usage:
    python scripts/setup_auth.py
"""

import os.path
import sys
import json

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Scopes required for the application
# We need read/modify for Gmail (to mark as read) and Sheets access
SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/spreadsheets'
]

def save_env_file(key_values: dict):
    """Saves or updates variables in a local .env file."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    existing_vars = {}
    
    # Read existing
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                if '=' in line:
                    k, v = line.strip().split('=', 1)
                    existing_vars[k] = v
    
    # Update
    existing_vars.update(key_values)
    
    # Write back
    with open(env_path, 'w') as f:
        for k, v in existing_vars.items():
            f.write(f"{k}={v}\n")
    print(f"✅ Configuration saved to {env_path}")

def create_spreadsheet(creds):
    """Creates a new Google Sheet and sets up headers."""
    try:
        service = build('sheets', 'v4', credentials=creds)
        spreadsheet = {
            'properties': {
                'title': 'GyFTR Vouchers Automated'
            }
        }
        print("   Creating spreadsheet...")
        spreadsheet = service.spreadsheets().create(body=spreadsheet, fields='spreadsheetId').execute()
        spreadsheet_id = spreadsheet.get('spreadsheetId')
        print(f"✅ Created new Spreadsheet: https://docs.google.com/spreadsheets/d/{spreadsheet_id}")
        
        # Add Headers (canonical schema used by the processing service)
        print("   Adding headers...")
        headers = [[
            "Logo",
            "Brand",
            "Value",
            "Code",
            "Pin",
            "Expiry",
            "Email Date",
            "Message ID",
            "Added By",
            "Created At",
        ]]
        body = {'values': headers}
        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range="Sheet1!A1",

            valueInputOption="RAW", body=body
        ).execute()
        
        return spreadsheet_id
    except Exception as e:
        print(f"❌ Failed to create spreadsheet: {e}")
        print("   Ensure the 'Google Sheets API' is enabled in your Google Cloud Project.")
        return None

def authenticate():
    """
    Authenticates with Google APIs.
    On first run, opens browser for OAuth consent.
    Saves token.json for future use.
    """
    print("\n🔐 Starting Authentication Process...")
    
    creds = None
    # We look for files in the project root
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    token_path = os.path.join(root_dir, 'token.json')
    creds_path = os.path.join(root_dir, 'credentials.json')

    if not os.path.exists(creds_path):
        print("❌ Error: credentials.json not found in root folder.")
        print(f"   Expected path: {creds_path}")
        print("   Please download your OAuth Client credentials from Google Cloud Console and save them as credentials.json")
        return
    
    # Check if credentials.json is valid JSON
    try:
        with open(creds_path, 'r') as f:
            json.load(f)
    except json.JSONDecodeError:
        print("❌ Error: credentials.json is not a valid JSON file.")
        return

    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception as e:
            print(f"⚠️  Warning: Existing token.json is corrupt ({e}). Re-authenticating...")
            creds = None

    # If there are no (valid) credentials available, let the user log in
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("🔄 Refreshing expired credentials...")
            try:
                creds.refresh(Request())
            except Exception as e:
                print(f"⚠️  Token refresh failed: {e}. Re-authenticating...")
                creds = None

    if not creds:
        try:
            print("🌐 Opening browser for authentication...")
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            # prompt='consent' forces a refresh_token to be returned
            creds = flow.run_local_server(port=0, prompt='consent', access_type='offline')
            
            # Save the credentials for the next run
            with open(token_path, 'w') as token:
                token.write(creds.to_json())
                print(f"✅ Saved authentication token to {token_path}")
        except Exception as e:
            print(f"❌ Authentication failed: {e}")
            return

    # --- Setup Configuration ---
    print("\n--- ⚙️  Configuration Setup ---")
    
    try:
        # Load client info for .env
        with open(creds_path) as f:
            client_config = json.load(f)
            # Handle both installed and web types
            client_type = 'installed' if 'installed' in client_config else 'web'
            client_id = client_config[client_type]['client_id']
            client_secret = client_config[client_type]['client_secret']

        # Extract Refresh Token
        refresh_token = creds.refresh_token
        if not refresh_token:
            # Sometimes refresh token is in the file but not memory if loaded from file
            with open(token_path) as f:
                t = json.load(f)
                refresh_token = t.get('refresh_token')

        env_vars = {
            'CLIENT_ID': client_id,
            'CLIENT_SECRET': client_secret,
            'REFRESH_TOKEN': refresh_token or ''
        }
    except Exception as e:
        print(f"❌ Error extracting credentials details: {e}")
        return

    # Sheet Setup
    while True:
        sheet_choice = input("Do you want to create a NEW Google Sheet for vouchers? (y/n): ").lower()
        if sheet_choice in ['y', 'n']:
            break
            
    if sheet_choice == 'y':
        sid = create_spreadsheet(creds)
        if sid:
            env_vars['GYFTR_SPREADSHEET_ID'] = sid
        else:
            print("⚠️  Skipping sheet creation due to error.")
    else:
        sid = input("Enter existing Google Sheet ID: ").strip()
        if sid:
            env_vars['GYFTR_SPREADSHEET_ID'] = sid
    
    save_env_file(env_vars)
    print("\n✅ Setup Complete! Environment variables saved to .env")
    
    return creds

if __name__ == '__main__':
    creds = authenticate()
    if creds:
        print("\n✅ Authentication successful!")
        # Removed Push Notification setup from here to avoid confusion for local users.
        # It is now a separate step in the Cloud Deployment workflow.

