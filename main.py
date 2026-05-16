"""
Cloud Function to process GyFTR Gmail notifications.
Triggered by Pub/Sub when new emails arrive.

Also includes a scheduled HTTP function to renew the Gmail Watch,
which expires every 7 days.
"""

import sys
import os
import base64
import json
from datetime import datetime

# Logic to add root to path if running mostly for local debug, 
# but in Cloud Functions, root is already CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import Settings
from src.factory import ServiceFactory

try:
    from urllib3.util import connection as urllib3_connection
    # Force IPv4 for outbound requests if configured
    settings = Settings()
    if settings.force_ipv4:
        urllib3_connection.HAS_IPV6 = False
except ImportError:
    pass

def process_pubsub_message_gen1(event, context):
    """Gen1 Cloud Function entrypoint (Pub/Sub background function).

    Args:
        event: Pub/Sub event payload dict. `event['data']` is base64-encoded bytes.
        context: Event metadata (unused).
    """
    try:
        factory = ServiceFactory()
        factory.validate_configuration()

        print(f"Function (gen1) triggered at {datetime.utcnow().isoformat()}")

        history_id = None
        try:
            data_b64 = event.get('data')
            if data_b64:
                decoded = base64.b64decode(data_b64).decode('utf-8')
                message_data = json.loads(decoded)
                history_id = message_data.get('historyId')
                print(
                    f"Received notification: historyId={message_data.get('historyId', 'N/A')}, "
                    f"email={message_data.get('emailAddress', 'N/A')}"
                )
            else:
                print("Received Pub/Sub event with no data")
        except Exception as e:
            print(f"Warning: failed to decode Pub/Sub data: {e}")

        print("--- Processing GyFTR Emails ---")
        gyftr_service = factory.get_gyftr_processing_service()

        if history_id:
            result = gyftr_service.process_from_gmail_history(
                current_history_id=str(history_id),
                source="cloud_function_automation",
            )
        else:
            # Fallback if payload doesn't include historyId
            result = gyftr_service.process_new_gyftr_emails(
                source="cloud_function_automation",
                include_read=True,
            )

        print(f"Result: {json.dumps(result)}")
        print("Processing complete")
        print("==================================================")
        return result

    except Exception as e:
        import traceback
        error_msg = f"Critical error in execution: {str(e)}\n{traceback.format_exc()}"
        print(error_msg)
        raise


def renew_gmail_watch(request):
    """HTTP Cloud Function to renew Gmail push notifications.

    Gmail watch() expires every 7 days. This function is called by
    Cloud Scheduler to renew it automatically.

    Args:
        request: Flask request object (unused).

    Returns:
        Tuple of (response_body, status_code).
    """
    try:
        from googleapiclient.discovery import build
        from google.oauth2.credentials import Credentials

        settings = Settings()
        valid, missing = settings.validate()
        if not valid:
            msg = f"Missing config: {', '.join(missing)}"
            print(f"❌ {msg}")
            return (json.dumps({"error": msg}), 500)

        creds = Credentials.from_authorized_user_info(settings.gmail_credentials)
        service = build('gmail', 'v1', credentials=creds)

        project_id = os.environ.get('PROJECT_ID', '')
        topic_name = os.environ.get('PUBSUB_TOPIC', 'gmail-notifications')
        full_topic = f"projects/{project_id}/topics/{topic_name}"

        result = service.users().watch(
            userId='me',
            body={'labelIds': ['INBOX'], 'topicName': full_topic},
        ).execute()

        expiration = result.get('expiration', 'unknown')
        print(f"✅ Gmail Watch renewed. Expiration: {expiration}")
        return (json.dumps({"status": "renewed", "expiration": expiration}), 200)

    except Exception as e:
        import traceback
        error_msg = f"Failed to renew Gmail Watch: {str(e)}\n{traceback.format_exc()}"
        print(error_msg)
        return (json.dumps({"error": str(e)}), 500)
