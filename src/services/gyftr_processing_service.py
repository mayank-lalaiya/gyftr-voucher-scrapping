"""
Service for processing GyFTR emails and updating Google Sheets.
"""
import base64
import re
from typing import List, Dict, Any
from datetime import datetime
from zoneinfo import ZoneInfo

from src.repositories import GmailRepository
from src.parsers.gyftr_parser import extract_vouchers_from_html
from src.config import Settings
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

class GyftrProcessingService:
    """
    Service to process GyFTR emails and update Google Sheets.
    """
    
    def __init__(self, gmail_repository: GmailRepository, settings: Settings):
        self.gmail_repo = gmail_repository

        self.settings = settings
        self._sheets_service = None

    def get_sheets_service(self):
        """Lazy initialization of Sheets service."""
        if not self._sheets_service:
            # We can reuse the credentials from the Gmail repository if they are compatible
            # Accessing credentials directly from the gmail repo
            creds = self.gmail_repo.get_credentials()
            self._sheets_service = build('sheets', 'v4', credentials=creds)
        return self._sheets_service

    def process_new_gyftr_emails(self, source: str = 'automation', max_results: int = 50, include_read: bool = False) -> dict:
        """
        Fetches new unread GyFTR emails, parses them, and appends to the sheet.
        
        Args:
            source (str): Source of execution ('backfill' or 'automation').
            max_results (int): Maximum number of emails to fetch.
            include_read (bool): If True, filters only by 'from:gifts@gyftr.com' (including read emails).
        """
        result = {
            'emails_checked': 0,
            'vouchers_found': 0,
            'rows_added': 0,
            'errors': []
        }

        try:
            # 1. Fetch emails from GyFTR
            # Query: Strictly matches emails from the official sender
            # Note: We cannot rely on Pub/Sub notification content as it does not contain the message ID.
            # We must query the mailbox for new messages.
            query = 'from:gifts@gyftr.com'
            if not include_read:
                # IMPORTANT: If we are in automation mode, we MUST filter by unread.
                # Otherwise, we will loop infinitely on old emails that failed parsing.
                query += ' is:unread'

            print(f"Fetching recent GyFTR emails with query: '{query}'")
            
            messages = self.gmail_repo.service.users().messages().list(
                userId='me', q=query, maxResults=max_results
            ).execute().get('messages', [])

            result['emails_checked'] = len(messages)
            print(f"Found {len(messages)} GyFTR emails to scan.")
            
            if not messages:
                print("No GyFTR emails found.")
                return result

            all_new_vouchers = []
            
            # IST Time
            now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")

            for msg_meta in messages:
                try:
                    msg_id = msg_meta['id']
                    # We need the snippet/internalDate to be faster? No, need payload for HTML.
                    # This might be slow for 50 emails. 
                    # Optimization: In a real world, we'd use historyId or only check recent ones.
                    # But for now, robustness > speed (within 60s limit).
                    message = self.gmail_repo.service.users().messages().get(
                        userId='me', id=msg_id, format='full'
                    ).execute()
                    
                    payload = message['payload']
                    headers = {h['name']: h['value'] for h in payload['headers']}
                    email_date = headers.get('Date', 'Unknown')
                    subject = headers.get('Subject', 'Unknown')
                    
                    # Log what we are processing to help debugging
                    print(f"Scanning email: {msg_id} | {email_date} | {subject[:50]}...")
                    
                    html = self._get_html_content(payload)
                    if not html:
                        continue
                        
                    vouchers = extract_vouchers_from_html(html)
                    
                    if vouchers:
                        print(f"  -> Found {len(vouchers)} vouchers in {msg_id}")
                        for v in vouchers:
                            v['Email Date'] = email_date
                            v['Message ID'] = msg_id
                            # Added fields
                            v['Added By'] = source
                            v['Created At'] = now_ist
                            
                            all_new_vouchers.append(v)
                        
                        # Mark as read (idempotent)
                        self.gmail_repo.mark_as_read(msg_id)
                        result['vouchers_found'] += len(vouchers)
                    else:
                        # Fallback: Mark as read even if no vouchers found to prevent infinite looping
                        # on supported emails.
                        print(f"  -> No vouchers found in {msg_id}. Marking as read to skip next time.")
                        self.gmail_repo.mark_as_read(msg_id)
                        
                except Exception as e:
                    error_msg = f"Error processing GyFTR email {msg_id}: {str(e)}"
                    print(f"✗ {error_msg}")
                    result['errors'].append(error_msg)

            # 2. Update to Google Sheet
            if all_new_vouchers:
                # If Automation: Insert at Top. If Backfill: Append to Bottom.
                insert_at_top = (source != 'backfill')
                self._update_sheet(all_new_vouchers, insert_at_top=insert_at_top)
                result['rows_added'] = len(all_new_vouchers)

        except Exception as e:
            error_msg = f"Global error in GyFTR processing: {str(e)}"
            print(f"✗ {error_msg}")
            result['errors'].append(error_msg)
            
        return result

    def _get_html_content(self, msg_payload):
        """Recursively find HTML content in message payload."""
        if 'parts' in msg_payload:
            for part in msg_payload['parts']:
                if part['mimeType'] == 'text/html':
                    data = part['body'].get('data')
                    if data:
                        return base64.urlsafe_b64decode(data).decode('utf-8')
                elif 'parts' in part:
                    res = self._get_html_content(part)
                    if res:
                        return res
        elif msg_payload['mimeType'] == 'text/html':
            data = msg_payload['body'].get('data')
            if data:
                return base64.urlsafe_b64decode(data).decode('utf-8')
        return None

    def _update_sheet(self, vouchers: List[Dict[str, Any]], insert_at_top: bool = False):
        """Updates the Google Sheet with new vouchers."""
        service = self.get_sheets_service()
        # Use settings ID
        spreadsheet_id = self.settings.gyftr_spreadsheet_id
        
        # Get current headers to ensure alignment
        sheet_metadata = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheets = sheet_metadata.get('sheets', '')
        sheet_title = sheets[0]['properties']['title']
        
        # Read header row
        header_range = f"{sheet_title}!A1:Z1"
        header_result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=header_range
        ).execute()
        headers = header_result.get('values', [[]])[0]
        
        if not headers:
            # Sheet is empty, define headers
            # Updated Headers to match simple format 'Brand', 'Value', 'Code', 'Pin', 'Expiry'
            headers = ['Brand', 'Logo', 'Value', 'Code', 'Pin', 'Expiry', 'Email Date', 'Message ID', 'Added By', 'Created At']
            # Write headers
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id, range=f"{sheet_title}!A1",
                valueInputOption="USER_ENTERED", body={'values': [headers]}
            ).execute()
            
            # Set Column Formats (Expiry -> Date)
            try:
                sheet_id = sheets[0]['properties']['sheetId']
                expiry_col_index = headers.index('Expiry')
                created_at_col_index = headers.index('Created At')
                
                requests = [
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startColumnIndex": expiry_col_index,
                                "endColumnIndex": expiry_col_index + 1
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "numberFormat": {
                                        "type": "DATE",
                                        "pattern": "d-mmm-yyyy"
                                    }
                                }
                            },
                            "fields": "userEnteredFormat.numberFormat"
                        }
                    },
                     {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startColumnIndex": created_at_col_index,
                                "endColumnIndex": created_at_col_index + 1
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "numberFormat": {
                                        "type": "DATE_TIME",
                                        "pattern": "yyyy-mm-dd hh:mm:ss"
                                    }
                                }
                            },
                            "fields": "userEnteredFormat.numberFormat"
                        }
                    }
                ]
                service.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id, body={'requests': requests}
                ).execute()
                print("✅ Applied Date formatting to 'Expiry' and 'Created At' columns.")
            except Exception as e:
                print(f"⚠️ Warning: Could not set date formatting: {e}")
        else:
             # Ensure new columns exist in headers if they don't
            updates_needed = False
            # Check for Logo and Email Date as well, as they might be missing in older sheets
            for col in ['Logo', 'Email Date', 'Added By', 'Created At']:
                if col not in headers:
                    headers.append(col)
                    updates_needed = True
            
            if updates_needed:
                print(f"Updating headers to include new columns: {headers}")
                service.spreadsheets().values().update(
                    spreadsheetId=spreadsheet_id, range=f"{sheet_title}!1:1",
                    valueInputOption="USER_ENTERED", body={'values': [headers]}
                ).execute()

        # Read existing data to prevent duplicates
        # We'll read the 'Code' column (assuming it's one of the headers)

        # Or better, read the whole sheet to check for duplicates based on Message ID or Code
        existing_data_range = f"{sheet_title}!A2:Z"
        existing_result = service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id, range=existing_data_range
        ).execute()
        existing_rows = existing_result.get('values', [])
        
        # Create a set of existing unique identifiers (e.g., Code)
        existing_codes = set()
        code_index = -1
        # Check for both "Code" and old "E-Gift Card Code" header just in case
        if 'Code' in headers:
            code_index = headers.index('Code')
        elif 'E-Gift Card Code' in headers:
            code_index = headers.index('E-Gift Card Code')
            
        for row in existing_rows:
            if code_index != -1 and len(row) > code_index:
                # Clean up the code (remove leading ' if present)
                code = str(row[code_index]).lstrip("'")
                existing_codes.add(code)

        # Prepare rows based on headers, filtering out duplicates
        rows_to_append = []
        for v in vouchers:
            # Look for 'Code' key (normalized by parser now)
            voucher_code = v.get('Code', '')
            
            if voucher_code and voucher_code in existing_codes:
                print(f"Skipping duplicate voucher: {voucher_code}...")
                continue
                
            row = []
            for header in headers:
                # Handle potential mismatch if legacy headers exist
                key = header
                if header == 'E-Gift Card Code': key = 'Code'
                if header == 'PIN': key = 'Pin'
                if header == 'Valid Till': key = 'Expiry'
                
                val = v.get(key, '')
                # Force specific fields to be treated as TEXT to prevent Sheets from
                # converting them to random serial numbers (e.g. 46354 instead of a date)
                if val and key == 'Code':
                    val = f"'{val}"
                # NOTE: We do NOT force Expiry to reference text (') anymore,
                # because we want them to be sortable Date objects in Sheets.
                # The user must ensure the column format is set to Date in Sheets.
                row.append(val)
            rows_to_append.append(row)

        if not rows_to_append:
            print("No new unique vouchers to append.")
            return

        # Strategy: Insert At Top vs Append
        if insert_at_top:
            self._insert_rows_at_top(service, spreadsheet_id, sheet_title, sheets[0]['properties']['sheetId'], rows_to_append)
        else:
            self._append_rows_at_bottom(service, spreadsheet_id, sheet_title, rows_to_append)

    def _insert_rows_at_top(self, service, spreadsheet_id, sheet_title, sheet_id, rows):
        """Inserts rows at index 1 (Row 2)."""
        print(f"Inserting {len(rows)} rows at the TOP...")
        requests = [{
            "insertDimension": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": 1,
                    "endIndex": 1 + len(rows)
                },
                "inheritFromBefore": False
            }
        }]
        
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, 
                body={'requests': requests}
            ).execute()
            
            service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=f"{sheet_title}!A2",
                valueInputOption="USER_ENTERED",
                body={'values': rows}
            ).execute()
        except Exception as e:
            print(f"Error inserting top: {e}. Fallback to append.")
            self._append_rows_at_bottom(service, spreadsheet_id, sheet_title, rows)

    def _append_rows_at_bottom(self, service, spreadsheet_id, sheet_title, rows):
        """Appends rows to the end of the sheet."""
        print(f"Appending {len(rows)} rows at the BOTTOM...")
        service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id, range=f"{sheet_title}!A1",
            valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
            body={'values': rows}
        ).execute()
        print(f"Appended {len(rows)} rows to spreadsheet {spreadsheet_id}")
