"""
Gmail repository for email operations.
Abstracts Gmail API interactions following Repository pattern.
"""

import base64
from typing import List, Optional
from datetime import datetime

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from src.models import Email
from src.config import Settings


class GmailRepository:
    """
    Repository for Gmail operations.
    Handles all interactions with Gmail API.
    """
    
    def __init__(self, settings: Settings):
        """
        Initialize Gmail repository.
        
        Args:
            settings: Application settings instance
        """
        self.settings = settings
        self._service = None
    
    @property
    def service(self):
        """Lazy-load Gmail service"""
        if self._service is None:
            creds = self.get_credentials()
            self._service = build('gmail', 'v1', credentials=creds)
        return self._service

    def get_credentials(self) -> Credentials:
        """
        Get the credentials object used for Gmail service.
        Useful for sharing credentials with other services (e.g. Sheets).
        """
        return Credentials.from_authorized_user_info(
            self.settings.gmail_credentials
        )
    
    def get_recent_unread_emails(
        self,
        max_results: int = 1,
        window_minutes: int = 5
    ) -> List[Email]:
        """
        Get recent unread emails from inbox.
        
        Args:
            max_results: Maximum number of emails to retrieve
            window_minutes: Only get emails from last N minutes
            
        Returns:
            List of Email objects
        """
        query = f'is:unread in:inbox newer_than:{window_minutes}m'
        
        results = self.service.users().messages().list(
            userId='me',
            maxResults=max_results,
            q=query
        ).execute()
        
        messages = results.get('messages', [])
        
        if not messages:
            return []
        
        emails = []
        for msg_ref in messages:
            email = self.get_email_by_id(msg_ref['id'])
            if email:
                emails.append(email)
        
        return emails
    
    def get_email_by_id(self, email_id: str) -> Optional[Email]:
        """
        Get a specific email by ID.
        
        Args:
            email_id: Gmail message ID
            
        Returns:
            Email object or None if not found
        """
        try:
            msg = self.service.users().messages().get(
                userId='me',
                id=email_id,
                format='full'
            ).execute()
            
            return self._parse_email(msg)
        except Exception as e:
            print(f"Error fetching email {email_id}: {str(e)}")
            return None
    
    def _parse_email(self, msg: dict) -> Email:
        """
        Parse Gmail API message into Email object.
        
        Args:
            msg: Gmail API message dict
            
        Returns:
            Email object
        """
        # Extract headers
        headers = {
            h['name']: h['value']
            for h in msg['payload']['headers']
        }
        
        subject = headers.get('Subject', '')
        sender = headers.get('From', '')
        
        # Extract body
        body = self._extract_email_body(msg)
        snippet = msg.get('snippet', '')
        
        # Get labels
        labels = msg.get('labelIds', [])
        is_read = 'UNREAD' not in labels
        
        return Email(
            id=msg['id'],
            subject=subject,
            sender=sender,
            body=body,
            snippet=snippet,
            is_read=is_read,
            labels=labels,
            headers=headers
        )
    
    def _extract_email_body(self, msg: dict) -> str:
        """
        Extract email body text from Gmail message.
        Handles both plain text and HTML content.
        
        Args:
            msg: Gmail API message dict
            
        Returns:
            Email body text
        """
        payload = msg.get('payload', {})
        body_text = ""
        
        def get_body_from_parts(parts):
            plain_text = ""
            html_text = ""
            
            for part in parts:
                mime_type = part.get('mimeType', '')
                part_body = part.get('body', {})
                data = part_body.get('data', '')
                
                if 'parts' in part:
                    # Recursively process nested parts
                    nested = get_body_from_parts(part['parts'])
                    if nested:
                        return nested
                elif mime_type == 'text/plain' and data:
                    decoded = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                    plain_text = decoded
                elif mime_type == 'text/html' and data:
                    # We don't need markdown conversion for GyFTR, just raw content if possible
                    # But if we want to keep it generic, we can keep the markdown logic or strip it.
                    # Since we use parsing logic that often looks at HTML directly in the service,
                    # this helper might be less critical for GyFTR but good for generic debugging.
                    # I'll strip the markdownify dependency to make the new repo lighter.
                    
                    decoded = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                    html_text = decoded
            
            # Prefer HTML
            return html_text if html_text else plain_text
        
        # Try to get body from parts
        if 'parts' in payload:
            body_text = get_body_from_parts(payload['parts'])
        else:
            # Single part message
            body_data = payload.get('body', {}).get('data', '')
            if body_data:
                body_text = base64.urlsafe_b64decode(body_data).decode('utf-8', errors='ignore')
        
        # Get snippet as fallback
        if not body_text:
            body_text = msg.get('snippet', '')
        
        return body_text
    
    def mark_as_read(self, email_id: str) -> bool:
        """
        Mark an email as read.
        
        Args:
            email_id: Gmail message ID
            
        Returns:
            True if successful, False otherwise
        """
        try:
            self.service.users().messages().modify(
                userId='me',
                id=email_id,
                body={'removeLabelIds': ['UNREAD']}
            ).execute()
            return True
        except Exception as e:
            print(f"Error marking email {email_id} as read: {str(e)}")
            return False
