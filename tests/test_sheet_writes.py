import base64
import copy
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from googleapiclient.errors import HttpError
from httplib2 import Response

import main
from src.services.gyftr_processing_service import GyftrProcessingService


MODERN_HTML = """
<table><tr>
  <td width="100px"><img src="https://example.com/logo/510.png">
      <div style="text-align:center">Amazon Shopping Voucher</div></td>
  <td>
    <div style="font-size: 11px">Gift Voucher Code</div><div>TEST-MODERN-001</div>
    <div style="font-size: 11px">Gift Voucher Value</div><div>1000</div>
    <div style="font-size: 11px">PIN</div><div>0007</div>
    <div style="font-size: 11px">Valid Till</div><div>31 Oct 2027</div>
  </td>
</tr></table>
"""

LEGACY_HTML = """
<p>Thank you for buying Myntra Gift Voucher from Gyftr.</p>
<table>
  <tr><td>E-Gift Card Code</td><td>Face Value</td><td>PIN</td><td>Expiry Date</td></tr>
  <tr><td>0001234567890123456789</td><td>250</td><td>001234</td><td>Mar 07, 2019</td></tr>
</table>
"""


class SheetWriteTests(unittest.TestCase):
    def setUp(self):
        self.repo = Mock()
        self.service = GyftrProcessingService(
            self.repo, SimpleNamespace(gyftr_spreadsheet_id='test-sheet'),
        )
        self.api = Mock()
        self.service.get_sheets_service = Mock(return_value=self.api)
        self.sheets = self.api.spreadsheets.return_value
        self.values = self.sheets.values.return_value
        self.headers = [
            'Logo', 'Brand', 'Value', 'Code', 'Pin', 'Expiry',
            'Email Date', 'Message ID', 'Added By', 'Created At',
        ]
        self.rows = []
        self.config_rows = []
        self.written_cells = []
        self.fail_read = False
        self.reject_batch = False
        self.lose_response = False
        self.sheets.get.return_value.execute.return_value = {
            'sheets': [{'properties': {
                'title': 'Sheet1', 'sheetId': 7, 'gridProperties': {'rowCount': 1},
            }}, {'properties': {
                'title': '_config', 'sheetId': 8,
                'gridProperties': {'rowCount': 1000, 'columnCount': 2},
            }}, {'properties': self.service._guard_properties(9)}],
        }
        self.values.get.side_effect = self.read_request
        self.values.update.side_effect = self.header_request
        self.sheets.batchUpdate.side_effect = self.batch_request

    @staticmethod
    def voucher(code='TEST-CODE-001', **fields):
        return {
            'Logo': '=IMAGE("https://example.com/test-logo.png")',
            'Brand': 'Test Brand', 'Value': '1,000', 'Code': code, 'Pin': '0007',
            'Expiry': '31-Dec-2026', 'Email Date': '2026-10-30 12:45:05',
            'Message ID': 'test-message', 'Added By': 'automation',
            'Created At': '2026-11-02 14:06:09', **fields,
        }

    def read_request(self, **kwargs):
        def execute():
            if kwargs['range'].endswith('A1:Z1'):
                return {'values': [self.headers.copy()] if self.headers else []}
            if kwargs['range'] == '_config!A2:B':
                return {'values': copy.deepcopy(self.config_rows)}
            if self.fail_read:
                raise RuntimeError('Existing rows unavailable')
            return {'values': copy.deepcopy(self.rows)}
        return Mock(execute=execute)

    def header_request(self, **kwargs):
        def execute():
            self.headers = kwargs['body']['values'][0].copy()
            return {}
        return Mock(execute=execute)

    def batch_request(self, **kwargs):
        def execute():
            if self.reject_batch:
                raise RuntimeError('Atomic batch rejected')
            pending_rows = copy.deepcopy(self.rows)
            pending_cells = []
            pending_headers = self.headers.copy()
            pending_config = copy.deepcopy(self.config_rows)
            metadata = self.sheets.get.return_value.execute.return_value
            pending_metadata = copy.deepcopy(metadata)
            replies = []
            for request in kwargs['body']['requests']:
                replies.append({})
                if 'deleteSheet' in request:
                    sheet_id = request['deleteSheet']['sheetId']
                    if not any(sheet['properties']['sheetId'] == sheet_id for sheet in pending_metadata['sheets']):
                        raise HttpError(Response({'status': '400'}), b'{"error":{"message":"Stale revision"}}')
                    pending_metadata['sheets'] = [
                        sheet for sheet in pending_metadata['sheets']
                        if sheet['properties']['sheetId'] != sheet_id
                    ]
                elif 'addSheet' in request:
                    properties = copy.deepcopy(request['addSheet']['properties'])
                    if any(
                        sheet['properties']['sheetId'] == properties['sheetId']
                        or sheet['properties']['title'] == properties['title']
                        for sheet in pending_metadata['sheets']
                    ):
                        raise HttpError(Response({'status': '400'}), b'{"error":{"message":"Duplicate sheet"}}')
                    pending_metadata['sheets'].append({'properties': properties})
                    replies[-1] = {'addSheet': {'properties': properties}}
                elif 'updateCells' in request:
                    update = request['updateCells']
                    start = update['start']
                    properties = next(
                        sheet['properties'] for sheet in pending_metadata['sheets']
                        if sheet['properties']['sheetId'] == start['sheetId']
                    )
                    for offset, row in enumerate(update['rows']):
                        values = [
                            next(iter(cell.get('userEnteredValue', {}).values()), '')
                            for cell in row['values']
                        ]
                        if any(isinstance(value, str) and len(value) > 50000 for value in values):
                            raise ValueError('Synthetic Sheets cell exceeds 50000 characters')
                        index = start['rowIndex'] + offset
                        if properties['title'] == '_config':
                            if index:
                                while len(pending_config) < index:
                                    pending_config.append([])
                                pending_config[index - 1] = values
                        elif start['sheetId'] == 7 and index == 0:
                            column = start.get('columnIndex', 0)
                            while len(pending_headers) < column + len(values):
                                pending_headers.append('')
                            pending_headers[column:column + len(values)] = values
                        else:
                            raise AssertionError('Unexpected synthetic update')
                elif 'appendDimension' in request:
                    dimension = request['appendDimension']
                    properties = next(
                        sheet['properties'] for sheet in pending_metadata['sheets']
                        if sheet['properties']['sheetId'] == dimension['sheetId']
                    )
                    properties['gridProperties']['rowCount'] += dimension['length']
                elif 'appendCells' in request:
                    cells = request['appendCells']['rows']
                    pending_cells.extend(cells)
                    for row in cells:
                        pending_rows.append([
                            next(iter(cell.get('userEnteredValue', {}).values()), '')
                            for cell in row['values']
                        ])
                elif 'sortRange' not in request:
                    raise AssertionError(f"Unexpected batch operation: {list(request)}")
            self.rows = pending_rows
            self.headers = pending_headers
            self.config_rows = pending_config
            metadata.clear()
            metadata.update(pending_metadata)
            self.written_cells.extend(pending_cells)
            if self.lose_response:
                self.lose_response = False
                raise TimeoutError('Response lost after server committed the batch')
            return {'replies': replies}
        return Mock(execute=execute)

    def test_duplicates_in_the_same_batch_are_written_once(self):
        added = self.service._update_sheet([
            self.voucher(), self.voucher(), self.voucher('TEST-CODE-002'),
        ])

        self.assertEqual(added, 2)
        self.assertEqual(len(self.rows), 2)

    def test_existing_and_incoming_codes_use_the_same_normalization(self):
        self.rows = [['', 'Test Brand', 1000, "'TEST-CODE-001  "]]

        added = self.service._update_sheet([
            self.voucher('  TEST-CODE-001  '),
            self.voucher("'TEST-CODE-002"), self.voucher('TEST-CODE-002 '),
        ])

        self.assertEqual(added, 1)
        self.assertEqual([row[3] for row in self.rows[1:]], ['TEST-CODE-002'])

    def test_replaying_a_successful_batch_adds_zero_rows(self):
        first = self.service._update_sheet([self.voucher()])
        second = self.service._update_sheet([self.voucher()])

        self.assertEqual((first, second), (1, 0))
        self.assertEqual(len(self.rows), 1)
        self.sheets.batchUpdate.assert_called_once()

    def test_existing_row_read_failure_is_not_treated_as_an_empty_sheet(self):
        self.fail_read = True

        with self.assertRaisesRegex(RuntimeError, 'Existing rows unavailable'):
            self.service._update_sheet([self.voucher()])

        self.sheets.batchUpdate.assert_not_called()
        self.values.append.assert_not_called()

    def test_append_and_sort_are_one_atomic_batch(self):
        self.service._update_sheet([self.voucher()])

        self.sheets.batchUpdate.assert_called_once()
        kwargs = self.sheets.batchUpdate.call_args.kwargs
        self.assertEqual(kwargs['spreadsheetId'], 'test-sheet')
        requests = kwargs['body']['requests']
        self.assertEqual([list(request) for request in requests], [
            ['deleteSheet'], ['addSheet'], ['appendCells'], ['sortRange'],
        ])
        self.assertEqual(requests[0]['deleteSheet']['sheetId'], 9)
        self.assertEqual(requests[2]['appendCells']['sheetId'], 7)
        self.assertEqual(requests[3]['sortRange'], {
            'range': {'sheetId': 7, 'startRowIndex': 1},
            'sortSpecs': [{'dimensionIndex': 6, 'sortOrder': 'DESCENDING'}],
        })
        self.values.append.assert_not_called()
        self.values.update.assert_not_called()

    def test_rejected_batch_has_no_fallback_or_partial_rows(self):
        self.reject_batch = True

        with self.assertRaisesRegex(RuntimeError, 'Atomic batch rejected'):
            self.service._update_sheet([self.voucher()])

        self.assertEqual(self.rows, [])
        self.values.append.assert_not_called()
        self.values.update.assert_not_called()
        self.sheets.batchUpdate.assert_called_once()

    def test_lost_success_response_does_not_duplicate_rows_on_retry(self):
        self.lose_response = True

        with self.assertRaises(TimeoutError):
            self.service._update_sheet([self.voucher()])
        self.assertEqual(len(self.rows), 1)

        added = self.service._update_sheet([self.voucher()])

        self.assertEqual(added, 0)
        self.assertEqual(len(self.rows), 1)
        self.sheets.batchUpdate.assert_called_once()

    def test_code_and_pin_are_literal_text_without_numeric_or_formula_conversion(self):
        self.service._update_sheet([
            self.voucher('0001234567890123456789', Pin='00001234'),
            self.voucher('=TEST-CODE', Pin='=TEST-PIN'),
        ])

        self.assertEqual(len(self.written_cells), 2)
        for row, code, pin in zip(
            self.written_cells,
            ['0001234567890123456789', '=TEST-CODE'], ['00001234', '=TEST-PIN'],
        ):
            self.assertEqual(row['values'][3]['userEnteredValue'], {'stringValue': code})
            self.assertEqual(row['values'][4]['userEnteredValue'], {'stringValue': pin})

    def test_amount_logo_and_dates_keep_native_sheet_types(self):
        self.service._update_sheet([self.voucher()])

        cells = self.written_cells[0]['values']
        self.assertEqual(cells[0]['userEnteredValue'], {
            'formulaValue': '=IMAGE("https://example.com/test-logo.png")',
        })
        self.assertEqual(cells[2]['userEnteredValue'], {'numberValue': 1000})
        epoch = datetime(1899, 12, 30)
        self.assertEqual(cells[5]['userEnteredValue'], {
            'numberValue': (datetime(2026, 12, 31) - epoch).total_seconds() / 86400,
        })
        self.assertEqual(cells[6]['userEnteredValue'], {
            'numberValue': (datetime(2026, 10, 30, 12, 45, 5) - epoch).total_seconds() / 86400,
        })
        self.assertEqual(cells[5]['userEnteredFormat']['numberFormat']['type'], 'DATE')
        self.assertEqual(cells[6]['userEnteredFormat']['numberFormat']['type'], 'DATE_TIME')
        self.assertEqual(cells[9]['userEnteredValue'], {
            'numberValue': (datetime(2026, 11, 2, 14, 6, 9) - epoch).total_seconds() / 86400,
        })

    def test_supported_expiry_formats_have_the_same_native_date(self):
        serial = (datetime(2026, 12, 31) - datetime(1899, 12, 30)).days
        for value in [
            '2026-12-31', '31-Dec-2026', '31 Dec 2026', '31/12/2026',
            '31-12-2026', '31Dec2026', '31/Dec/2026', 'Dec 31, 2026', serial,
        ]:
            with self.subTest(value=value):
                cell = self.service._voucher_cell('Expiry', value)

                self.assertEqual(cell['userEnteredValue'], {'numberValue': serial})
                self.assertEqual(cell['userEnteredFormat']['numberFormat']['type'], 'DATE')

    def test_rfc_email_offsets_produce_the_same_native_ist_datetime(self):
        serial = (
            datetime(2026, 10, 30, 12, 45, 5) - datetime(1899, 12, 30)
        ).total_seconds() / 86400
        for raw in [
            'Fri, 30 Oct 2026 07:15:05 +0000',
            'Fri, 30 Oct 2026 12:45:05 +0530',
            'Fri, 30 Oct 2026 03:15:05 -0400',
        ]:
            with self.subTest(raw=raw):
                parsed = self.service._parse_email_date(raw)
                cell = self.service._voucher_cell('Email Date', parsed)

                self.assertEqual(cell['userEnteredValue'], {'numberValue': serial})
                self.assertEqual(
                    cell['userEnteredFormat']['numberFormat'],
                    {'type': 'DATE_TIME', 'pattern': 'yyyy-mm-dd hh:mm:ss'},
                )

    def test_ist_conversion_preserves_the_next_calendar_day(self):
        parsed = self.service._parse_email_date('Fri, 30 Oct 2026 20:00:00 +0000')
        expected = (
            datetime(2026, 10, 31, 1, 30) - datetime(1899, 12, 30)
        ).total_seconds() / 86400

        self.assertEqual(
            self.service._voucher_cell('Email Date', parsed)['userEnteredValue'],
            {'numberValue': expected},
        )

    def test_currency_prefixes_and_valid_grouping_produce_numeric_amounts(self):
        for value, expected in [
            ('1000', 1000), ('1,000.50', 1000.5), ('Rs. 1,000', 1000),
            ('INR 1,00,000.50', 100000.5), ('\u20b9 1,000', 1000),
        ]:
            with self.subTest(value=value):
                cell = self.service._voucher_cell('Value', value)

                self.assertEqual(cell['userEnteredValue'], {'numberValue': expected})

    def test_malformed_amount_grouping_is_not_silently_reinterpreted(self):
        with patch('builtins.print') as output:
            cell = self.service._voucher_cell('Value', '1,2,3')

        self.assertEqual(cell['userEnteredValue'], {'stringValue': '1,2,3'})
        self.assertIn('Unrecognized voucher Value', str(output.call_args_list))

    def test_unknown_date_and_amount_preserve_text_with_warnings(self):
        with patch('builtins.print') as output:
            self.service._update_sheet([
                self.voucher(Expiry='Not supplied', Value='Not supplied'),
            ])

        cells = self.written_cells[0]['values']
        self.assertEqual(cells[2]['userEnteredValue'], {'stringValue': 'Not supplied'})
        self.assertEqual(cells[5]['userEnteredValue'], {'stringValue': 'Not supplied'})
        self.assertIn('Unrecognized voucher Value', str(output.call_args_list))
        self.assertIn('Unrecognized voucher Expiry', str(output.call_args_list))

    def test_non_finite_numbers_are_rejected_before_writing(self):
        for value in ['NaN', 'Infinity', '-Infinity', float('nan'), float('inf')]:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, 'Non-finite'):
                    self.service._update_sheet([self.voucher(Value=value)])

                self.sheets.batchUpdate.assert_not_called()
                self.assertEqual(self.rows, [])

    def test_invalid_identifier_types_are_rejected(self):
        for code in [True, 1.5, {'invalid': 'code'}, ['invalid']]:
            with self.subTest(code=code):
                with self.assertRaisesRegex(ValueError, 'code'):
                    self.service._update_sheet([self.voucher(code)])

                self.sheets.batchUpdate.assert_not_called()

    def test_zero_identifiers_are_text_and_do_not_erase_leading_zeros(self):
        self.service._update_sheet([
            self.voucher(0, Pin=0), self.voucher('000', Pin='000'),
        ])

        self.assertEqual([row[3:5] for row in self.rows], [['0', '0'], ['000', '000']])

    def test_non_logo_text_is_not_interpreted_as_a_formula(self):
        self.service._update_sheet([self.voucher(Brand='=TEST-BRAND')])

        self.assertEqual(
            self.written_cells[0]['values'][1]['userEnteredValue'],
            {'stringValue': '=TEST-BRAND'},
        )

    def test_legacy_headers_preserve_code_pin_and_expiry_alignment(self):
        self.headers[3:6] = ['E-Gift Card Code', 'PIN', 'Valid Till']

        added = self.service._update_sheet([self.voucher()])

        self.assertEqual(added, 1)
        self.assertEqual(self.rows[0][3:5], ['TEST-CODE-001', '0007'])
        self.assertIsInstance(self.rows[0][5], (int, float))
        self.assertEqual(self.service._update_sheet([self.voucher()]), 0)

    def test_blank_sheet_is_initialized_and_first_batch_is_replayable(self):
        self.headers = []

        self.assertEqual(self.service._update_sheet([self.voucher()]), 1)
        self.assertEqual(self.service._update_sheet([self.voucher()]), 0)

        self.assertEqual(self.headers[3:5], ['Code', 'Pin'])
        self.assertEqual(len(self.rows), 1)

    def test_empty_input_is_a_no_op(self):
        self.assertEqual(self.service._update_sheet([]), 0)

        self.service.get_sheets_service.assert_not_called()

    def test_missing_code_is_rejected_without_writing_any_vouchers(self):
        for code in ['', ' ', None, "'"]:
            with self.subTest(code=code):
                with self.assertRaisesRegex(ValueError, 'code'):
                    self.service._update_sheet([self.voucher(), self.voucher(code)])
                self.sheets.batchUpdate.assert_not_called()

    def test_missing_code_column_does_not_disable_deduplication(self):
        self.headers.remove('Code')

        with self.assertRaisesRegex(ValueError, 'Code'):
            self.service._update_sheet([self.voucher()])

        self.sheets.batchUpdate.assert_not_called()

    def test_duplicate_logs_do_not_expose_voucher_codes(self):
        self.rows = [['', 'Test Brand', 1000, 'TEST-CODE-001']]

        with patch('builtins.print') as output:
            self.service._update_sheet([self.voucher()])

        self.assertNotIn('TEST-CODE-001', str(output.call_args_list))

    def configure_email(self):
        messages = self.repo.service.users.return_value.messages.return_value
        messages.list.return_value.execute.return_value = {'messages': [{'id': 'test-message'}]}
        messages.get.return_value.execute.return_value = {'payload': {
            'mimeType': 'text/html',
            'body': {'data': base64.urlsafe_b64encode(b'test voucher html').decode()},
            'headers': [
                {'name': 'From', 'value': 'GyFTR <gifts@gyftr.com>'},
                {'name': 'Date', 'value': 'Fri, 30 Oct 2026 12:45:05 +0530'},
                {'name': 'Subject', 'value': 'Test voucher'},
            ],
        }}

    def configure_trigger(self, html):
        self.configure_email()
        messages = self.repo.service.users.return_value.messages.return_value
        messages.get.return_value.execute.return_value['payload']['body']['data'] = (
            base64.urlsafe_b64encode(html.encode()).decode()
        )
        state = {'LAST_GMAIL_HISTORY_ID': '100'}
        self.service._get_config_value = Mock(side_effect=state.get)
        self.service._set_config_value = Mock(side_effect=state.__setitem__)
        self.service._get_message_ids_since_history = Mock(return_value=['test-message'])
        main._get_service_factory.cache_clear()
        self.addCleanup(main._get_service_factory.cache_clear)
        factory_patch = patch('main.ServiceFactory')
        factory = factory_patch.start().return_value
        self.addCleanup(factory_patch.stop)
        factory.get_gyftr_processing_service.return_value = self.service
        event = {'data': base64.b64encode(json.dumps({'historyId': '200'}).encode()).decode()}
        context = SimpleNamespace(timestamp=datetime.now(timezone.utc).isoformat())
        return event, context, state

    def test_release_pipeline_modern_parser_write_checkpoint_and_duplicate_push(self):
        event, context, state = self.configure_trigger(MODERN_HTML)

        first = main.process_pubsub_message_gen1(event, context)
        replay = main.process_pubsub_message_gen1(event, context)

        self.assertEqual(first['rows_added'], 1)
        self.assertEqual(replay['rows_added'], 0)
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(self.rows[0][3:5], ['TEST-MODERN-001', '0007'])
        self.assertEqual(state['LAST_GMAIL_HISTORY_ID'], '200')
        self.assertFalse(state['PENDING_GMAIL_HISTORY_BATCH'])
        self.assertEqual(self.written_cells[0]['values'][0]['userEnteredValue'], {
            'formulaValue': '=IMAGE("https://example.com/logo/510.png")',
        })
        self.assertIn('numberValue', self.written_cells[0]['values'][6]['userEnteredValue'])
        self.sheets.batchUpdate.assert_called_once()
        self.api.spreadsheets.assert_called_once_with()
        self.sheets.values.assert_called_once_with()

    def test_release_pipeline_legacy_parser_preserves_identifiers_and_native_expiry(self):
        event, context, state = self.configure_trigger(LEGACY_HTML)

        result = main.process_pubsub_message_gen1(event, context)

        self.assertEqual(result['rows_added'], 1)
        self.assertEqual(self.rows[0][3:5], ['0001234567890123456789', '001234'])
        self.assertEqual(self.written_cells[0]['values'][5]['userEnteredValue'], {
            'numberValue': (datetime(2019, 3, 7) - datetime(1899, 12, 30)).days,
        })
        self.assertEqual(state['LAST_GMAIL_HISTORY_ID'], '200')

    def test_release_pipeline_lost_write_response_retries_without_duplicates(self):
        event, context, state = self.configure_trigger(MODERN_HTML)
        self.lose_response = True

        with self.assertRaises(main.RetryableProcessingError):
            main.process_pubsub_message_gen1(event, context)
        self.assertEqual(state['LAST_GMAIL_HISTORY_ID'], '100')
        self.repo.mark_as_read.assert_not_called()

        result = main.process_pubsub_message_gen1(event, context)

        self.assertEqual(result['rows_added'], 0)
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(state['LAST_GMAIL_HISTORY_ID'], '200')
        self.assertFalse(state['PENDING_GMAIL_HISTORY_BATCH'])
        self.sheets.batchUpdate.assert_called_once()

    def test_release_pipeline_real_parser_failure_preserves_work_for_retry(self):
        event, context, state = self.configure_trigger(MODERN_HTML)

        with patch(
            'src.parsers.gyftr_parser.BeautifulSoup',
            side_effect=RuntimeError('Synthetic parser failure'),
        ):
            with self.assertRaises(main.RetryableProcessingError):
                main.process_pubsub_message_gen1(event, context)

        self.assertEqual(state['LAST_GMAIL_HISTORY_ID'], '100')
        self.assertTrue(state['PENDING_GMAIL_HISTORY_BATCH'])
        self.assertEqual(self.rows, [])
        self.repo.mark_as_read.assert_not_called()

        result = main.process_pubsub_message_gen1(event, context)

        self.assertEqual(result['rows_added'], 1)
        self.assertEqual(state['LAST_GMAIL_HISTORY_ID'], '200')
        self.assertEqual(len(self.rows), 1)

    def test_release_pipeline_claim_invitation_is_not_a_parser_failure(self):
        event, context, state = self.configure_trigger(
            '<p>Claim the Voucher of your choice from the below mentioned brands.</p>',
        )

        result = main.process_pubsub_message_gen1(event, context)

        self.assertEqual(result['errors'], [])
        self.assertEqual(result['rows_added'], 0)
        self.assertEqual(state['LAST_GMAIL_HISTORY_ID'], '200')
        self.sheets.batchUpdate.assert_not_called()
        self.repo.mark_as_read.assert_called_once_with('test-message')

    def test_local_processing_reports_real_parser_failure_without_marking_read(self):
        self.configure_email()
        with patch(
            'src.parsers.gyftr_parser.BeautifulSoup',
            side_effect=RuntimeError('Synthetic parser failure'),
        ):
            result = self.service.process_new_gyftr_emails()

        self.assertTrue(result['errors'])
        self.assertEqual(result['rows_added'], 0)
        self.repo.mark_as_read.assert_not_called()
        self.sheets.batchUpdate.assert_not_called()

    def test_history_retry_after_lost_sheet_response_keeps_one_row(self):
        self.configure_email()
        state = {'LAST_GMAIL_HISTORY_ID': '100'}
        self.service._get_config_value = Mock(side_effect=state.get)
        self.service._set_config_value = Mock(side_effect=state.__setitem__)
        self.service._get_message_ids_since_history = Mock(return_value=['test-message'])
        self.lose_response = True

        with patch(
            'src.services.gyftr_processing_service.extract_vouchers_from_html',
            side_effect=lambda html: [self.voucher()],
        ):
            first = self.service.process_from_gmail_history('200')
            self.assertTrue(first['errors'])
            self.assertEqual(state['LAST_GMAIL_HISTORY_ID'], '100')
            self.repo.mark_as_read.assert_not_called()

            second = self.service.process_from_gmail_history('200')

        self.assertEqual(second['errors'], [])
        self.assertEqual(second['rows_added'], 0)
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(state['LAST_GMAIL_HISTORY_ID'], '200')
        self.assertFalse(state['PENDING_GMAIL_HISTORY_BATCH'])
        self.service._get_message_ids_since_history.assert_called_once()

    def test_local_processing_reports_actual_insert_count(self):
        self.configure_email()
        self.rows = [['', 'Test Brand', 1000, 'TEST-CODE-001']]

        with patch(
            'src.services.gyftr_processing_service.extract_vouchers_from_html',
            return_value=[self.voucher(), self.voucher('TEST-CODE-002')],
        ):
            result = self.service.process_new_gyftr_emails()

        self.assertEqual(result['errors'], [])
        self.assertEqual(result['vouchers_found'], 2)
        self.assertEqual(result['rows_added'], 1)

    def test_checkpoint_failure_can_replay_without_duplicating_sheet_rows(self):
        self.configure_email()
        state = {'LAST_GMAIL_HISTORY_ID': '100'}
        self.service._get_config_value = Mock(side_effect=state.get)
        self.service._get_message_ids_since_history = Mock(return_value=['test-message'])
        fail_checkpoint = True

        def save(key, value):
            if key == 'LAST_GMAIL_HISTORY_ID' and fail_checkpoint:
                raise RuntimeError('Checkpoint write unavailable')
            state[key] = value

        self.service._set_config_value = Mock(side_effect=save)
        with patch(
            'src.services.gyftr_processing_service.extract_vouchers_from_html',
            side_effect=lambda html: [self.voucher()],
        ):
            first = self.service.process_from_gmail_history('200')
            self.assertTrue(first['errors'])
            self.assertEqual(first['rows_added'], 1)
            self.assertEqual(state['LAST_GMAIL_HISTORY_ID'], '100')
            self.repo.mark_as_read.assert_not_called()
            fail_checkpoint = False

            second = self.service.process_from_gmail_history('200')

        self.assertEqual(second['errors'], [])
        self.assertEqual(second['rows_added'], 0)
        self.assertEqual(len(self.rows), 1)
        self.assertEqual(state['LAST_GMAIL_HISTORY_ID'], '200')
        self.assertFalse(state['PENDING_GMAIL_HISTORY_BATCH'])
        self.sheets.batchUpdate.assert_called_once()

    def test_local_processing_does_not_mark_read_on_write_failure(self):
        self.configure_email()
        self.reject_batch = True

        with patch(
            'src.services.gyftr_processing_service.extract_vouchers_from_html',
            return_value=[self.voucher()],
        ):
            result = self.service.process_new_gyftr_emails()

        self.assertTrue(result['errors'])
        self.assertEqual(result['rows_added'], 0)
        self.repo.mark_as_read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
