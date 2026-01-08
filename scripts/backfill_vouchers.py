"""
Script to manually trigger GyFTR email processing.
Useful for backfilling or testing local logic.
"""


import os
import sys
import json
import traceback

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.factory import ServiceFactory


def load_env_vars():
    """Manually load .env variables into os.environ"""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                if '=' in line and not line.startswith('#'):
                    key, val = line.strip().split('=', 1)
                    os.environ[key] = val
        print(f"✅ Loaded configuration from .env")

def manual_process():
    """Run the GyFTR processing service manually."""
    print("\n🚀 Starting manual GyFTR processing script...")
    print("---------------------------------------------")
    
    # Load env vars first (critical for SPREADSHEET_ID)
    load_env_vars()

    # Check for authentication
    if not os.path.exists('token.json'):
        print("❌ Error: 'token.json' not found.")
        print("   This file is required for local execution.")
        print("   Please run 'python scripts/setup_auth.py' first to authenticate.")
        return

    try:
        # Initialize factory and service
        print("🔧 Initializing services...")
        factory = ServiceFactory()
        
        # Override credential dictionary in settings to use local token.json if available.
        # This ensures we use the local user token which might be different from
        # what's configured in env vars for cloud execution.
        try:
            with open('token.json', 'r') as f:
                token_data = json.load(f)
                if not token_data:
                    raise ValueError("token.json is empty")
                factory.settings.gmail_credentials = token_data
                print("✅ Global settings updated with local 'token.json'")
        except (json.JSONDecodeError, ValueError) as e:
            print(f"❌ Error reading 'token.json': {e}")
            print("   Please re-run 'python scripts/setup_auth.py'")
            return
            
        service = factory.get_gyftr_processing_service()
        
        # User Configuration
        print("\n--- 🔧 Scan Settings ---")
        try:
            limit_input = input("How many emails per batch should we scan? (Default: 50): ").strip()
            max_results = int(limit_input) if limit_input else 50
            
            read_input = input("Should we include READ/OPENED emails? (y/n, Default: n): ").lower().strip()
            include_read = read_input == 'y'

            all_input = input("Scan ALL matching emails (may take time)? (y/n, Default: n): ").lower().strip()
            scan_all = all_input == 'y'
        except ValueError:
            print("❌ Invalid number. Using default: 50")
            max_results = 50
            include_read = False
            scan_all = False

        total = {
            'emails_checked': 0,
            'vouchers_found': 0,
            'rows_added': 0,
            'errors': [],
        }

        page_token = None
        batch_num = 0

        while True:
            batch_num += 1
            print(f"\n📥 Scanning emails (batch {batch_num})...")
            result = service.process_new_gyftr_emails(
                source="backfill",
                max_results=max_results,
                include_read=include_read,
                page_token=page_token,
            )

            total['emails_checked'] += int(result.get('emails_checked', 0) or 0)
            total['vouchers_found'] += int(result.get('vouchers_found', 0) or 0)
            total['rows_added'] += int(result.get('rows_added', 0) or 0)
            total['errors'].extend(result.get('errors', []) or [])

            page_token = result.get('next_page_token')
            if not scan_all or not page_token:
                break
        
        print("\n✅ Processing Complete!")

        print(f"   - Emails Scanned: {total['emails_checked']}")
        print(f"   - Vouchers Found: {total['vouchers_found']}")
        print(f"   - Rows Added:     {total['rows_added']}")
        
        if total['errors']:
            print("\n⚠️  Errors encountered during processing:")
            for err in total['errors']:
                print(f"   - {err}")
        else:
            print("\n✨ No errors encountered.")

    except ImportError as e:
         print(f"\n❌ Import Error: {e}")
         print("   Did you run 'pip install -r requirements.txt'?")
    except Exception as e:
        print(f"\n❌ Critical Unexpected Error: {e}")
        print("   Stack trace:")
        traceback.print_exc()

if __name__ == "__main__":
    manual_process()

