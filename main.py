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
from datetime import datetime, timedelta, timezone
from functools import lru_cache

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


MAX_NOTIFICATION_AGE = timedelta(hours=24)


class RetryableProcessingError(RuntimeError):
    """Request Gen1 redelivery when retry-on-failure is enabled."""


@lru_cache(maxsize=1)
def _get_service_factory() -> ServiceFactory:
    return ServiceFactory()


def _notification_created_at(context) -> datetime:
    timestamp = getattr(context, 'timestamp', None)
    if not isinstance(timestamp, str):
        raise ValueError('Missing Gmail event timestamp')
    try:
        created_at = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    except ValueError as e:
        raise ValueError('Invalid Gmail event timestamp') from e
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError('Gmail event timestamp must include a timezone')
    return created_at


def _notification_history_id(event) -> str:
    if not isinstance(event, dict):
        raise ValueError('Pub/Sub event must be an object')
    data = event.get('data')
    if not isinstance(data, (str, bytes)) or not data:
        raise ValueError('Missing base64-encoded Pub/Sub data')
    try:
        payload = json.loads(base64.b64decode(data, validate=True).decode('utf-8'))
    except (ValueError, TypeError) as e:
        raise ValueError('Pub/Sub data must be base64-encoded UTF-8 JSON') from e
    if not isinstance(payload, dict):
        raise ValueError('Gmail notification must be a JSON object')
    history_id = payload.get('historyId')
    if isinstance(history_id, bool) or not isinstance(history_id, (str, int)):
        raise ValueError('Gmail notification must include a positive historyId')
    history_id = str(history_id).strip()
    if not history_id.isascii() or not history_id.isdecimal() or int(history_id) <= 0:
        raise ValueError('Gmail notification must include a positive historyId')
    return history_id


def process_pubsub_message_gen1(event, context):
    """Gen1 Cloud Function entrypoint (Pub/Sub background function).

    Failures and unfinished batches request redelivery through Gen1 retries.
    Events at least 24 hours old are acknowledged without processing, leaving
    any pending work saved for a fresh notification or manual recovery.

    Args:
        event: Pub/Sub event payload dict. `event['data']` is base64-encoded bytes.
        context: Event metadata, including its original creation timestamp.
    """
    try:
        created_at = _notification_created_at(context)
        history_id = _notification_history_id(event)
    except ValueError as e:
        print(f"ERROR: Discarding invalid Gmail notification without processing: {e}")
        return {'status': 'rejected', 'reason': str(e)}

    now = datetime.now(timezone.utc)
    if now - created_at >= MAX_NOTIFICATION_AGE:
        print(
            "ERROR: Gmail notification expired after its 24-hour retry window. "
            "Acknowledging without processing; existing pending work is unchanged. "
            "A fresh notification or manual recovery is required."
        )
        return {'status': 'expired', 'reason': '24-hour retry window exhausted'}

    try:
        factory = _get_service_factory()
        factory.validate_configuration()

        print(f"Function (gen1) triggered at {now.isoformat()}")
        print(f"Received Gmail notification: historyId={history_id}")

        print("--- Processing GyFTR Emails ---")
        gyftr_service = factory.get_gyftr_processing_service()
        result = gyftr_service.process_from_gmail_history(
            current_history_id=history_id,
            source="cloud_function_automation",
        )
        if (
            not isinstance(result, dict)
            or not isinstance(result.get('errors'), list)
            or not isinstance(result.get('has_more'), bool)
        ):
            raise RetryableProcessingError('Invalid Gmail processing result; retry required')

        print(f"Result: {json.dumps(result)}")
        if result['errors']:
            raise RetryableProcessingError('Gmail processing reported errors; retry required')
        if result['has_more']:
            raise RetryableProcessingError('Gmail batch incomplete; retrying saved pending work')
        print("Processing complete")
        print("==================================================")
        return result

    except RetryableProcessingError as e:
        print(f"Retry requested: {e}")
        raise
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
