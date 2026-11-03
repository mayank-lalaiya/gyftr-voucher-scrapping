"""
Service for processing GyFTR emails and updating Google Sheets.
"""
import base64
from contextlib import contextmanager
import hashlib
import json
import math
import re
from email.utils import parseaddr, parsedate_to_datetime
from typing import List, Dict, Any, Generator
from datetime import datetime
from zoneinfo import ZoneInfo

from src.repositories import GmailRepository
from src.parsers.gyftr_parser import extract_vouchers_from_html
from src.config import Settings
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials

class GyftrProcessingService:
    """
    Service to process GyFTR emails and update Google Sheets.
    """
    
    WRITE_GUARD_TITLE = '_gyftr_write_guard'
    CONFIG_CHUNK_SIZE = 40000
    CONFIG_CHUNK_MARKER = '@gyftr-chunks-v1:'

    def __init__(self, gmail_repository: GmailRepository, settings: Settings):
        self.gmail_repo = gmail_repository

        self.settings = settings
        self._sheets_service: Resource | None = None
        self._spreadsheets_resource: Resource | None = None
        self._sheet_values_resource: Resource | None = None
        self._write_session_depth = 0
        self._revision_sheet_id: int | None = None
        self._next_revision_sheet_id = 0

    def get_sheets_service(self) -> Resource:
        """Lazy initialization of Sheets service."""
        if self._sheets_service is None:
            # We can reuse the credentials from the Gmail repository if they are compatible
            # Accessing credentials directly from the gmail repo
            creds = self.gmail_repo.get_credentials()
            self._sheets_service = build('sheets', 'v4', credentials=creds)
        return self._sheets_service

    def _get_spreadsheets_resource(self) -> Resource:
        """Cache expensive discovery-generated resources, not API responses."""
        if self._spreadsheets_resource is None:
            self._spreadsheets_resource = self.get_sheets_service().spreadsheets()
        return self._spreadsheets_resource

    def _get_sheet_values_resource(self) -> Resource:
        if self._sheet_values_resource is None:
            self._sheet_values_resource = self._get_spreadsheets_resource().values()
        return self._sheet_values_resource

    @classmethod
    def _guard_properties(cls, sheet_id: int) -> dict:
        return {
            'sheetId': sheet_id, 'title': cls.WRITE_GUARD_TITLE, 'hidden': True,
            'gridProperties': {'rowCount': 1, 'columnCount': 1},
        }

    def _start_write_session(self) -> None:
        if self._write_session_depth:
            if self._revision_sheet_id is None:
                raise RuntimeError('Sheet write session is invalid after a failed mutation; retry from fresh state')
            self._write_session_depth += 1
            return
        spreadsheet_id = self.settings.gyftr_spreadsheet_id
        if not spreadsheet_id:
            raise ValueError('Missing GYFTR_SPREADSHEET_ID')
        metadata = self._get_spreadsheets_resource().get(spreadsheetId=spreadsheet_id).execute()
        properties = [sheet['properties'] for sheet in metadata.get('sheets', [])]
        if not properties:
            raise ValueError('Spreadsheet has no sheets')
        next_id = max(prop['sheetId'] for prop in properties) + 1
        guard = next((prop for prop in properties if prop['title'] == self.WRITE_GUARD_TITLE), None)
        if guard is None:
            if next_id >= 2**31 - 1:
                raise ValueError('Spreadsheet sheet IDs exhausted; cannot initialize write guard')
            guard = self._guard_properties(next_id)
            self._get_spreadsheets_resource().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={'requests': [{'addSheet': {'properties': guard}}]},
            ).execute()
            next_id += 1
        if (
            guard.get('hidden') is not True
            or guard.get('gridProperties', {}).get('rowCount') != 1
            or guard.get('gridProperties', {}).get('columnCount') != 1
        ):
            raise ValueError('Reserved write-guard tab has unexpected properties; do not modify it manually')
        self._revision_sheet_id = guard['sheetId']
        self._next_revision_sheet_id = next_id
        self._write_session_depth = 1

    def _end_write_session(self) -> None:
        self._write_session_depth -= 1
        if not self._write_session_depth:
            self._revision_sheet_id = None

    @contextmanager
    def _sheet_write_session(self) -> Generator[None, None, None]:
        self._start_write_session()
        try:
            yield
        finally:
            self._end_write_session()

    def _guarded_batch_update(self, requests: list[dict]) -> None:
        if not self._write_session_depth or self._revision_sheet_id is None:
            raise RuntimeError('Sheet mutation requires a valid write session')
        old_id = self._revision_sheet_id
        new_id = self._next_revision_sheet_id
        if new_id >= 2**31:
            raise ValueError('Spreadsheet sheet IDs exhausted; cannot advance write guard')
        next_id = max(
            [new_id] + [
                request['addSheet']['properties']['sheetId']
                for request in requests
                if 'addSheet' in request and 'sheetId' in request['addSheet']['properties']
            ]
        ) + 1
        # A stale/replayed batch cannot delete its already-consumed revision.
        # Sheets rejects the entire batch, including voucher and config writes.
        guarded_requests = [
            {'deleteSheet': {'sheetId': old_id}},
            {'addSheet': {'properties': self._guard_properties(new_id)}},
            *requests,
        ]
        self._revision_sheet_id = None
        self._get_spreadsheets_resource().batchUpdate(
            spreadsheetId=self.settings.gyftr_spreadsheet_id, body={'requests': guarded_requests},
        ).execute()
        self._revision_sheet_id = new_id
        self._next_revision_sheet_id = next_id

    def process_new_gyftr_emails(
        self,
        source: str = 'automation',
        max_results: int = 50,
        include_read: bool = False,
        page_token: str | None = None,
    ) -> dict:
        """
        Fetches new unread GyFTR emails, parses them, and appends to the sheet.
        
        Args:
            source (str): Source of execution ('backfill' or 'automation').
            max_results (int): Maximum number of emails to fetch.
            include_read (bool): If True, filters only by 'from:gifts@gyftr.com' (including read emails).
        """
        result: dict = {
            'emails_checked': 0,
            'vouchers_found': 0,
            'rows_added': 0,
            'errors': [],
            'next_page_token': None,
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
            
            list_req = self.gmail_repo.service.users().messages().list(
                userId='me', q=query, maxResults=max_results, pageToken=page_token
            )
            list_res = list_req.execute()
            messages = list_res.get('messages', [])
            result['next_page_token'] = list_res.get('nextPageToken')

            result['emails_checked'] = len(messages)
            print(f"Found {len(messages)} GyFTR emails to scan.")
            
            if not messages:
                print("No GyFTR emails found.")
                return result

            all_new_vouchers = []
            messages_to_mark_read = []
            
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
                    email_date_raw = headers.get('Date', 'Unknown')
                    email_date = self._parse_email_date(email_date_raw)
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
                        
                        result['vouchers_found'] += len(vouchers)
                    else:
                        print(f"  -> No vouchers found in {msg_id}.")

                    messages_to_mark_read.append(msg_id)
                        
                except Exception as e:
                    error_msg = f"Error processing GyFTR email {msg_id}: {str(e)}"
                    print(f"✗ {error_msg}")
                    result['errors'].append(error_msg)
            # 2. Update to Google Sheet
            if all_new_vouchers:
                result['rows_added'] = self._update_sheet(all_new_vouchers)

            for msg_id in messages_to_mark_read:
                try:
                    self.gmail_repo.mark_as_read(msg_id)
                except Exception as e:
                    print(f"⚠️ Warning: Could not mark email {msg_id} as read after processing: {e}")

        except Exception as e:
            error_msg = f"Global error in GyFTR processing: {str(e)}"
            print(f"✗ {error_msg}")
            result['errors'].append(error_msg)
            
        return result

    def process_from_gmail_history(
        self,
        current_history_id: str,
        source: str = 'cloud_function_automation',
        max_messages: int = 50,
        fallback_newer_than_days: int = 30,
    ) -> dict:
        """Process GyFTR emails using Gmail History API.

        This is the safest way to include emails even if the user opened them,
        while avoiding repeatedly scanning old mail.

        The committed historyId advances only after discovered messages have
        been processed and their vouchers written. Pending message IDs persist
        separately so the per-invocation cap cannot discard unfinished work.

        Args:
            current_history_id: The historyId received in Pub/Sub notification.
            source: Execution source label stored in the sheet.
            max_messages: Upper bound of messages processed per invocation.
                Remaining messages are persisted for a subsequent invocation.
            fallback_newer_than_days: If startHistoryId is missing/invalid, fall back
                to a bounded Gmail search window.
        """
        result = {
            'emails_checked': 0,
            'vouchers_found': 0,
            'rows_added': 0,
            'errors': [],
            'mode': 'gmail_history',
            'has_more': False,
            'messages_skipped_deleted': 0,
        }

        def record_error(message: str) -> None:
            print(f"✗ {message}")
            result['errors'].append(message)

        if not self.settings.gyftr_spreadsheet_id:
            record_error('Missing GYFTR_SPREADSHEET_ID')
            return result
        current_history_id = str(current_history_id).strip()
        if not current_history_id.isdecimal() or int(current_history_id) <= 0:
            record_error('Invalid current Gmail history ID')
            return result
        if isinstance(max_messages, bool) or not isinstance(max_messages, int) or max_messages <= 0:
            record_error('max_messages must be a positive integer')
            return result
        if (
            isinstance(fallback_newer_than_days, bool)
            or not isinstance(fallback_newer_than_days, int)
            or fallback_newer_than_days <= 0
        ):
            record_error('fallback_newer_than_days must be a positive integer')
            return result

        try:
            with self._sheet_write_session():
                return self._process_history_batch(
                    current_history_id, source, max_messages, fallback_newer_than_days, result,
                )
        except Exception as e:
            record_error(f"Failed coordinating Gmail history processing: {e}")
            return result

    def _process_history_batch(
        self, current_history_id: str, source: str, max_messages: int,
        fallback_newer_than_days: int, result: dict,
    ) -> dict:
        def record_error(message: str) -> None:
            print(f"✗ {message}")
            result['errors'].append(message)

        history_key = 'LAST_GMAIL_HISTORY_ID'
        pending_key = 'PENDING_GMAIL_HISTORY_BATCH'
        pending_batch = None
        try:
            last_history_id = self._get_config_value(history_key)
            if last_history_id not in (None, '') and (
                not str(last_history_id).isdecimal() or int(last_history_id) <= 0
            ):
                raise ValueError('Invalid saved Gmail history ID')
            raw_pending = self._get_config_value(pending_key)
            pending_saved = bool(raw_pending)
            if raw_pending:
                pending_batch = json.loads(raw_pending)
                if not isinstance(pending_batch, dict):
                    raise ValueError('Invalid pending Gmail history batch')
                target = pending_batch.get('target_history_id')
                message_ids = pending_batch.get('message_ids')
                if (
                    not isinstance(target, str) or not target.isdecimal() or int(target) <= 0
                    or not isinstance(message_ids, list)
                    or any(not isinstance(mid, str) or not mid for mid in message_ids)
                    or pending_batch.get('mode') not in {'gmail_history', 'fallback_search'}
                ):
                    raise ValueError('Invalid pending Gmail history batch')
                # A crash can leave pending state after its cursor was committed.
                if last_history_id and int(last_history_id) >= int(target):
                    self._set_config_value(pending_key, '')
                    pending_batch = None
                    pending_saved = False
        except Exception as e:
            record_error(f"Failed loading Gmail history state: {e}")
            return result

        if pending_batch is None:
            if last_history_id and int(current_history_id) <= int(last_history_id):
                return result

            message_ids = []
            used_fallback_search = not last_history_id
            if last_history_id:
                try:
                    message_ids = self._get_message_ids_since_history(str(last_history_id))
                except HttpError as e:
                    if e.resp.status != 404:
                        record_error(f"History API failed: {e}")
                        return result
                    used_fallback_search = True
                    print("Gmail history expired; recovering with a bounded search.")
                except Exception as e:
                    record_error(f"History API failed: {e}")
                    return result

            if used_fallback_search:
                result['mode'] = 'fallback_search'
                query = f"from:gifts@gyftr.com newer_than:{fallback_newer_than_days}d"
                page_token = None
                try:
                    while True:
                        response = self.gmail_repo.service.users().messages().list(
                            userId='me', q=query, maxResults=min(max_messages, 500),
                            pageToken=page_token,
                        ).execute()
                        message_ids.extend(message['id'] for message in response.get('messages', []))
                        page_token = response.get('nextPageToken')
                        if not page_token:
                            break
                except Exception as e:
                    record_error(f"Fallback search failed: {e}")
                    return result

            pending_batch = {
                'target_history_id': current_history_id,
                'message_ids': list(dict.fromkeys(message_ids)),
                'mode': result['mode'],
            }
            if pending_batch['message_ids']:
                try:
                    self._set_config_value(pending_key, json.dumps(pending_batch, separators=(',', ':')))
                    pending_saved = True
                except Exception as e:
                    record_error(f"Failed saving pending Gmail history batch: {e}")
                    return result

        result['mode'] = pending_batch['mode']
        message_ids = list(dict.fromkeys(pending_batch['message_ids']))
        batch_message_ids = message_ids[:max_messages]
        result['emails_checked'] = len(batch_message_ids)
        result['has_more'] = bool(message_ids)

        all_new_vouchers: list[dict] = []
        messages_to_mark_read: list[str] = []
        now_ist = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")

        for msg_id in batch_message_ids:
            try:
                message = self.gmail_repo.service.users().messages().get(
                    userId='me', id=msg_id, format='full'
                ).execute()
            except HttpError as e:
                if e.resp.status == 404:
                    print(f"Skipping deleted Gmail message {msg_id} (404).")
                    result['messages_skipped_deleted'] += 1
                    continue
                record_error(f"Error fetching email {msg_id}: {e}")
                return result
            except Exception as e:
                record_error(f"Error fetching email {msg_id}: {e}")
                return result

            try:
                payload = message.get('payload', {})
                headers = {h['name'].lower(): h['value'] for h in payload.get('headers', [])}
                sender = parseaddr(headers.get('from', ''))[1].lower()

                if sender != 'gifts@gyftr.com':
                    continue

                email_date_raw = headers.get('date', 'Unknown')
                email_date = self._parse_email_date(email_date_raw)
                subject = headers.get('subject', 'Unknown')
                print(f"Scanning email (history): {msg_id} | {email_date} | {subject[:50]}...")

                html = self._get_html_content(payload)
                if not html:
                    print(f"  -> No HTML content in {msg_id}; skipping.")
                    continue

                vouchers = extract_vouchers_from_html(html)
                if vouchers:
                    print(f"  -> Found {len(vouchers)} vouchers in {msg_id}")
                    for v in vouchers:
                        v['Email Date'] = email_date
                        v['Message ID'] = msg_id
                        v['Added By'] = source
                        v['Created At'] = now_ist
                        all_new_vouchers.append(v)
                    result['vouchers_found'] += len(vouchers)
                else:
                    print(f"  -> No vouchers found in {msg_id}.")

                messages_to_mark_read.append(msg_id)
            except Exception as e:
                record_error(f"Error processing email {msg_id}: {e}")
                return result

        if all_new_vouchers:
            try:
                result['rows_added'] = self._update_sheet(all_new_vouchers)
            except Exception as e:
                record_error(f"Sheet update failed: {e}")
                return result

        remaining_ids = message_ids[len(batch_message_ids):]
        target_history_id = pending_batch['target_history_id']
        try:
            if remaining_ids:
                pending_batch['message_ids'] = remaining_ids
                self._set_config_value(pending_key, json.dumps(pending_batch, separators=(',', ':')))
            else:
                latest_history_id = self._get_config_value(history_key)
                if latest_history_id not in (None, '') and (
                    not str(latest_history_id).isdecimal() or int(latest_history_id) <= 0
                ):
                    raise ValueError('Invalid saved Gmail history ID')
                if not latest_history_id or int(target_history_id) > int(latest_history_id):
                    self._set_config_value(history_key, target_history_id)
                if pending_saved:
                    self._set_config_value(pending_key, '')
        except Exception as e:
            record_error(f"Failed saving Gmail history progress: {e}")
            return result

        result['has_more'] = bool(remaining_ids) or int(current_history_id) > int(target_history_id)
        for msg_id in messages_to_mark_read:
            try:
                self.gmail_repo.mark_as_read(msg_id)
            except Exception as e:
                print(f"⚠️ Warning: Could not mark email {msg_id} as read after processing: {e}")

        return result

    @staticmethod
    def _parse_email_date(raw_date: str) -> str:
        """Parse RFC 2822 email date into a sortable datetime string (IST)."""
        try:
            dt = parsedate_to_datetime(raw_date)
            dt_ist = dt.astimezone(ZoneInfo("Asia/Kolkata"))
            return dt_ist.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return raw_date

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

    def _update_sheet(self, vouchers: List[Dict[str, Any]]) -> int:
        """Deduplicate, atomically write and sort vouchers; return rows inserted.

        A consumed revision rejects transport replays and concurrent stale
        writers atomically. Application retries then re-read existing codes.
        """
        if not vouchers:
            return 0

        normalized_vouchers = []
        for voucher in vouchers:
            code = self._normalize_voucher_identifier(voucher.get('Code'))
            if not code:
                raise ValueError('Voucher code is missing or empty')
            normalized_vouchers.append((voucher, code))

        with self._sheet_write_session():
            return self._append_vouchers(normalized_vouchers)

    def _append_vouchers(self, normalized_vouchers: list[tuple[dict, str]]) -> int:
        spreadsheet_id = self.settings.gyftr_spreadsheet_id
        if not spreadsheet_id:
            raise ValueError('Missing GYFTR_SPREADSHEET_ID')
        sheet_metadata = self._get_spreadsheets_resource().get(spreadsheetId=spreadsheet_id).execute()
        sheets = [
            sheet for sheet in sheet_metadata.get('sheets', [])
            if sheet['properties']['title'] not in {self.WRITE_GUARD_TITLE, '_config'}
        ]
        if not sheets:
            raise ValueError('Spreadsheet has no voucher sheet')
        sheet_title = sheets[0]['properties']['title']
        sheet_id = sheets[0]['properties']['sheetId']
        sheet_range = "'" + sheet_title.replace("'", "''") + "'"
        header_result = self._get_sheet_values_resource().get(
            spreadsheetId=spreadsheet_id, range=f"{sheet_range}!A1:Z1"
        ).execute()
        header_rows = header_result.get('values', [])
        headers = header_rows[0] if header_rows else []

        if not headers:
            headers = ['Logo', 'Brand', 'Value', 'Code', 'Pin', 'Expiry', 'Email Date', 'Message ID', 'Added By', 'Created At']
            self._write_headers(sheet_id, headers)
        else:
            if 'Code' not in headers and 'E-Gift Card Code' not in headers:
                raise ValueError('Voucher sheet has no Code column; cannot safely deduplicate')
            updates_needed = False
            for col in ['Logo', 'Email Date', 'Added By', 'Created At']:
                if col not in headers:
                    headers.append(col)
                    updates_needed = True
            if updates_needed:
                print(f"Updating headers to include new columns: {headers}")
                self._write_headers(sheet_id, headers)

        headers = self._ensure_logo_first_column(spreadsheet_id, sheet_range, sheet_id, headers)
        if 'Code' in headers:
            code_index = headers.index('Code')
        else:
            code_index = headers.index('E-Gift Card Code')

        existing_result = self._get_sheet_values_resource().get(
            spreadsheetId=spreadsheet_id, range=f"{sheet_range}!A2:Z",
            valueRenderOption="FORMATTED_VALUE",
        ).execute()
        existing_codes = {
            self._normalize_voucher_identifier(row[code_index])
            for row in existing_result.get('values', []) if len(row) > code_index
        }

        rows_to_append = []
        aliases = {'E-Gift Card Code': 'Code', 'PIN': 'Pin', 'Valid Till': 'Expiry'}
        for voucher, code in normalized_vouchers:
            if code in existing_codes:
                print("Skipping duplicate voucher.")
                continue
            cells = []
            for header in headers:
                key = aliases.get(header, header)
                value = code if key == 'Code' else voucher.get(key, '')
                cells.append(self._voucher_cell(key, value))
            rows_to_append.append({'values': cells})
            existing_codes.add(code)

        if not rows_to_append:
            print("No new unique vouchers to append.")
            return 0

        # Sorting belongs to the same transaction so a sort failure cannot leave
        # written rows behind an uncommitted checkpoint.
        requests = [
            {'appendCells': {
                'sheetId': sheet_id,
                'rows': rows_to_append,
                'fields': 'userEnteredValue,userEnteredFormat.numberFormat',
            }},
            {'sortRange': {
                'range': {'sheetId': sheet_id, 'startRowIndex': 1},
                'sortSpecs': [{
                    'dimensionIndex': headers.index('Email Date'),
                    'sortOrder': 'DESCENDING',
                }],
            }},
        ]
        self._guarded_batch_update(requests)
        return len(rows_to_append)

    @staticmethod
    def _text_row(values: list[str]) -> dict:
        return {'values': [{'userEnteredValue': {'stringValue': value}} for value in values]}

    def _write_headers(self, sheet_id: int, headers: list[str]) -> None:
        self._guarded_batch_update([{'updateCells': {
            'start': {'sheetId': sheet_id, 'rowIndex': 0, 'columnIndex': 0},
            'rows': [self._text_row(headers)], 'fields': 'userEnteredValue',
        }}])

    @staticmethod
    def _normalize_voucher_identifier(value: Any) -> str:
        if value is None:
            return ''
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError('Voucher code or PIN must be text or an integer')
        return str(value).strip().lstrip("'").strip()

    @classmethod
    def _voucher_cell(cls, key: str, value: Any) -> dict:
        if value is None or value == '':
            return {}

        number_format = None
        if key in {'Code', 'Pin'}:
            entered_value = {'stringValue': cls._normalize_voucher_identifier(value)}
            number_format = {'type': 'TEXT', 'pattern': '@'}
        else:
            if isinstance(value, str) and key == 'Value':
                try:
                    amount = re.sub(r'^(?:INR|Rs\.?|\u20b9)\s*', '', value.strip(), flags=re.IGNORECASE)
                    if ',' in amount and not re.fullmatch(
                        r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d+)?',
                        amount,
                    ):
                        raise ValueError('Invalid amount grouping')
                    value = float(amount.replace(',', ''))
                except ValueError:
                    print("Warning: Unrecognized voucher Value; preserving its text.")
            if isinstance(value, str) and key in {'Expiry', 'Email Date', 'Created At'}:
                for date_format in (
                    '%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%d-%b-%Y',
                    '%d %b %Y', '%d/%m/%Y', '%d-%m-%Y', '%d%b%Y', '%d/%b/%Y',
                    '%b %d, %Y',
                ):
                    try:
                        parsed = datetime.strptime(value.strip(), date_format)
                    except ValueError:
                        continue
                    value = (parsed - datetime(1899, 12, 30)).total_seconds() / 86400
                    break
                else:
                    print(f"Warning: Unrecognized voucher {key}; preserving its text.")
            if isinstance(value, bool):
                entered_value = {'boolValue': value}
            elif isinstance(value, (int, float)):
                if not math.isfinite(value):
                    raise ValueError(f'Non-finite voucher value for {key}')
                entered_value = {'numberValue': value}
            elif isinstance(value, str):
                if key == 'Logo' and value.startswith('=IMAGE('):
                    entered_value = {'formulaValue': value}
                else:
                    entered_value = {'stringValue': value}
            else:
                raise ValueError(f'Unsupported voucher value for {key}')
            if key == 'Expiry':
                number_format = {'type': 'DATE', 'pattern': 'd-mmm-yyyy'}
            elif key in {'Email Date', 'Created At'}:
                number_format = {'type': 'DATE_TIME', 'pattern': 'yyyy-mm-dd hh:mm:ss'}

        cell = {'userEnteredValue': entered_value}
        if number_format is not None:
            cell['userEnteredFormat'] = {'numberFormat': number_format}
        return cell

    def _ensure_logo_first_column(self, spreadsheet_id: str, sheet_title: str, sheet_id: int, headers: list[str]) -> list[str]:
        """Ensure the 'Logo' column exists and is physically at column A."""

        # If Logo exists but isn't first, move the column.
        if 'Logo' in headers and headers.index('Logo') != 0:
            src_index = headers.index('Logo')
            print(f"Moving 'Logo' column from index {src_index} to 0")
            requests = [{
                'moveDimension': {
                    'source': {
                        'sheetId': sheet_id,
                        'dimension': 'COLUMNS',
                        'startIndex': src_index,
                        'endIndex': src_index + 1,
                    },
                    'destinationIndex': 0,
                }
            }]
            self._guarded_batch_update(requests)

        # If Logo is missing entirely, insert a new column A and set its header.
        if 'Logo' not in headers:
            print("Inserting missing 'Logo' column at index 0")
            requests = [{
                'insertDimension': {
                    'range': {
                        'sheetId': sheet_id,
                        'dimension': 'COLUMNS',
                        'startIndex': 0,
                        'endIndex': 1,
                    },
                    'inheritFromBefore': False,
                }
            }]
            requests.append({'updateCells': {
                'start': {'sheetId': sheet_id, 'rowIndex': 0, 'columnIndex': 0},
                'rows': [self._text_row(['Logo'])], 'fields': 'userEnteredValue',
            }})
            self._guarded_batch_update(requests)

        # Re-read headers after any structural changes.
        header_result = self._get_sheet_values_resource().get(
            spreadsheetId=spreadsheet_id, range=f"{sheet_title}!A1:Z1"
        ).execute()
        new_headers = header_result.get('values', [[]])[0]
        return new_headers

    def _ensure_config_sheet(self, spreadsheet_id: str) -> dict:
        """Ensure a small config sheet exists; return its properties."""
        config_title = '_config'
        metadata = self._get_spreadsheets_resource().get(spreadsheetId=spreadsheet_id).execute()
        for s in metadata.get('sheets', []):
            if s.get('properties', {}).get('title') == config_title:
                return s['properties']

        # Create config sheet
        config_id = self._next_revision_sheet_id + 1
        if config_id >= 2**31:
            raise ValueError('Spreadsheet sheet IDs exhausted; cannot create config tab')
        properties = {
            'title': config_title, 'sheetId': config_id,
            'gridProperties': {'rowCount': 1000, 'columnCount': 2},
        }
        self._guarded_batch_update([
            {'addSheet': {'properties': properties}},
            {'updateCells': {
                'start': {'sheetId': config_id, 'rowIndex': 0, 'columnIndex': 0},
                'rows': [self._text_row(['Key', 'Value'])], 'fields': 'userEnteredValue',
            }},
        ])
        return properties

    def _get_config_value(self, key: str) -> str | None:
        with self._sheet_write_session():
            return self._read_config_value(key)

    def _read_config_value(self, key: str) -> str | None:
        spreadsheet_id = self.settings.gyftr_spreadsheet_id
        if not spreadsheet_id:
            raise ValueError('Missing GYFTR_SPREADSHEET_ID')
        config_title = self._ensure_config_sheet(spreadsheet_id)['title']

        # Read key/value columns
        res = self._get_sheet_values_resource().get(
            spreadsheetId=spreadsheet_id,
            range=f"{config_title}!A2:B",
        ).execute()
        rows = res.get('values', [])
        for row in rows:
            if row and row[0] == key:
                value = row[1] if len(row) >= 2 else ''
                if not isinstance(value, str) or not value.startswith(self.CONFIG_CHUNK_MARKER):
                    return value
                manifest = json.loads(value[len(self.CONFIG_CHUNK_MARKER):])
                if (
                    not isinstance(manifest, dict)
                    or isinstance(manifest.get('count'), bool)
                    or not isinstance(manifest.get('count'), int)
                    or not 0 < manifest['count'] <= len(rows)
                    or not isinstance(manifest.get('sha256'), str)
                ):
                    raise ValueError('Invalid chunked config manifest')
                chunks = {}
                prefix = key + '__chunk_'
                for chunk_row in rows:
                    if chunk_row and isinstance(chunk_row[0], str) and chunk_row[0].startswith(prefix):
                        if chunk_row[0] in chunks or len(chunk_row) != 2 or not isinstance(chunk_row[1], str):
                            raise ValueError('Invalid or duplicate config chunk')
                        chunks[chunk_row[0]] = chunk_row[1]
                expected_keys = [prefix + str(index) for index in range(manifest['count'])]
                if set(chunks) != set(expected_keys):
                    raise ValueError('Missing or unexpected config chunks')
                value = ''.join(chunks[name] for name in expected_keys)
                if hashlib.sha256(value.encode()).hexdigest() != manifest['sha256']:
                    raise ValueError('Config chunk checksum mismatch')
                return value
        return None

    def _set_config_value(self, key: str, value: str) -> None:
        with self._sheet_write_session():
            self._write_config_value(key, value)

    def _write_config_value(self, key: str, value: str) -> None:
        if not isinstance(value, str):
            raise ValueError('Config values must be strings')
        spreadsheet_id = self.settings.gyftr_spreadsheet_id
        if not spreadsheet_id:
            raise ValueError('Missing GYFTR_SPREADSHEET_ID')
        properties = self._ensure_config_sheet(spreadsheet_id)
        config_title = properties['title']

        res = self._get_sheet_values_resource().get(
            spreadsheetId=spreadsheet_id,
            range=f"{config_title}!A2:B",
        ).execute()
        rows = res.get('values', [])

        prefix = key + '__chunk_'
        desired = {key: value}
        if len(value) > self.CONFIG_CHUNK_SIZE:
            chunks = [value[index:index + self.CONFIG_CHUNK_SIZE] for index in range(0, len(value), self.CONFIG_CHUNK_SIZE)]
            desired = {
                key: self.CONFIG_CHUNK_MARKER + json.dumps({
                    'count': len(chunks), 'sha256': hashlib.sha256(value.encode()).hexdigest(),
                }, separators=(',', ':')),
                **{prefix + str(index): chunk for index, chunk in enumerate(chunks)},
            }
        positions = {}
        free_rows = []
        for index, row in enumerate(rows):
            if not row or all(cell == '' for cell in row):
                free_rows.append(index)
            elif isinstance(row[0], str):
                positions.setdefault(row[0], index)
        requests = []
        for name, text in desired.items():
            index = positions.get(name)
            if index is None:
                index = free_rows.pop(0) if free_rows else len(rows)
                if index == len(rows):
                    rows.append([])
                positions[name] = index
            requests.append({'updateCells': {
                'start': {'sheetId': properties['sheetId'], 'rowIndex': index + 1, 'columnIndex': 0},
                'rows': [self._text_row([name, text])], 'fields': 'userEnteredValue',
            }})
        for name, index in positions.items():
            if name.startswith(prefix) and name not in desired:
                requests.append({'updateCells': {
                    'start': {'sheetId': properties['sheetId'], 'rowIndex': index + 1, 'columnIndex': 0},
                    'rows': [{'values': [{}, {}]}], 'fields': 'userEnteredValue',
                }})
        row_count = properties.get('gridProperties', {}).get('rowCount')
        if not isinstance(row_count, int):
            raise ValueError('Config sheet row count is missing')
        if len(rows) + 1 > row_count:
            requests.insert(0, {'appendDimension': {
                'sheetId': properties['sheetId'], 'dimension': 'ROWS',
                'length': len(rows) + 1 - row_count,
            }})
        self._guarded_batch_update(requests)

    def _get_message_ids_since_history(self, start_history_id: str) -> list[str]:
        """Return message IDs added since start_history_id using Gmail History API."""
        message_ids: list[str] = []
        page_token = None

        while True:
            req = self.gmail_repo.service.users().history().list(
                userId='me',
                startHistoryId=str(start_history_id),
                historyTypes=['messageAdded'],
                pageToken=page_token,
            )
            res = req.execute()
            for h in res.get('history', []):
                for item in h.get('messagesAdded', []):
                    msg = item.get('message', {})
                    mid = msg.get('id')
                    if mid:
                        message_ids.append(mid)

            page_token = res.get('nextPageToken')
            if not page_token:
                break
        return message_ids
