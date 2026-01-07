"""
Cloud Function to process GyFTR Gmail notifications.
Triggered by Pub/Sub when new emails arrive.
"""

import sys
import os
import base64
import json
from datetime import datetime
import functions_framework

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

@functions_framework.cloud_event
def process_pubsub_message(cloud_event):
    """
    Triggered from a message on a Cloud Pub/Sub topic (Gen 2 / CloudEvent).
    
    Args:
        cloud_event: The CloudEvent object.
    """
    try:
        # Initialize factory and dependencies
        factory = ServiceFactory()
        
        # Log execution context
        print(f"Function triggered at {datetime.utcnow().isoformat()}")
        print(f"Event ID: {cloud_event['id']}")
        
        # Decode the Pub/Sub message
        # Gen 2 CloudEvent data structure for Pub/Sub:
        # cloud_event.data = {"message": {"data": "base64...", "attributes": ...}, "subscription": ...}
        data = cloud_event.data
        history_id = None
        if "message" in data and "data" in data["message"]:
            pubsub_message = base64.b64decode(data["message"]["data"]).decode('utf-8')
            try:
                message_data = json.loads(pubsub_message)
                history_id = message_data.get('historyId')
                print(f"Received notification: historyId={message_data.get('historyId', 'N/A')}, email={message_data.get('emailAddress', 'N/A')}")
            except json.JSONDecodeError:
                print(f"Received raw message: {pubsub_message}")
        
        # Validate configuration (will raise error if missing env vars)

        factory.validate_configuration()

        print("--- Processing GyFTR Emails ---")
        
        # Core Logic: Fetch and Process GyFTR Emails
        # Use Gmail History API (historyId from Pub/Sub) so even READ emails get captured.
        gyftr_service = factory.get_gyftr_processing_service()
        if history_id:
            result = gyftr_service.process_from_gmail_history(
                current_history_id=str(history_id),
                source="cloud_function_automation",
            )
        else:
            # Fallback if notification payload doesn't include historyId for any reason
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
        # Re-raise to flag execution failure in Cloud Console
        raise e
