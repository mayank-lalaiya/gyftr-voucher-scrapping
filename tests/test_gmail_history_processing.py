import base64
from contextlib import nullcontext
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from googleapiclient.errors import HttpError
from httplib2 import Response

from src.services.gyftr_processing_service import GyftrProcessingService


def api_error(status):
    return HttpError(
        Response({'status': str(status), 'reason': 'Test API failure'}),
        b'{"error": {"message": "Test API failure"}}',
    )


class GmailHistoryProcessingTests(unittest.TestCase):
    def setUp(self):
        self.repo = Mock()
        self.messages = self.repo.service.users.return_value.messages.return_value
        self.service = GyftrProcessingService(
            self.repo, SimpleNamespace(gyftr_spreadsheet_id='test-sheet'),
        )
        self.service._sheet_write_session = Mock(side_effect=nullcontext)
        self.state = {'LAST_GMAIL_HISTORY_ID': '100'}
        self.events = []
        self.service._get_config_value = Mock(side_effect=self.state.get)
        self.service._set_config_value = Mock(side_effect=self.save_config)
        self.service._get_message_ids_since_history = Mock(return_value=['message-1'])
        self.service._update_sheet = Mock(side_effect=self.write_vouchers)
        self.repo.mark_as_read.side_effect = lambda message_id: self.events.append('mark_read')
        self.messages.get.side_effect = self.message_request
        self.parser = patch(
            'src.services.gyftr_processing_service.extract_vouchers_from_html',
            side_effect=lambda html: [{'Code': html, 'Brand': 'Amazon'}],
        )
        self.parser.start()
        self.addCleanup(self.parser.stop)

    def save_config(self, key, value):
        self.events.append(key)
        self.state[key] = value

    def write_vouchers(self, vouchers):
        self.events.append('write')
        return len(vouchers)

    @staticmethod
    def message_request(userId, id, format):
        payload = {
            'mimeType': 'text/html',
            'headers': [
                {'name': 'From', 'value': 'GyFTR <gifts@gyftr.com>'},
                {'name': 'Date', 'value': 'Fri, 30 Oct 2026 12:45:05 +0530'},
                {'name': 'Subject', 'value': 'Test voucher'},
            ],
            'body': {'data': base64.urlsafe_b64encode(id.encode()).decode()},
        }
        return Mock(execute=Mock(return_value={'payload': payload}))

    def pending(self):
        return json.loads(self.state['PENDING_GMAIL_HISTORY_BATCH'])

    def test_checkpoint_follows_successful_sheet_write(self):
        result = self.service.process_from_gmail_history('200')

        self.assertEqual(result['errors'], [])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '200')
        self.assertLess(self.events.index('write'), self.events.index('LAST_GMAIL_HISTORY_ID'))
        self.assertLess(self.events.index('write'), self.events.index('mark_read'))
        self.assertFalse(result['has_more'])
        self.assertFalse(self.state['PENDING_GMAIL_HISTORY_BATCH'])

    def test_sheet_failure_preserves_cursor_and_pending_messages(self):
        self.service._update_sheet.side_effect = RuntimeError('Sheet unavailable')

        result = self.service.process_from_gmail_history('200')

        self.assertTrue(result['errors'])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '100')
        self.assertEqual(self.pending()['message_ids'], ['message-1'])
        self.repo.mark_as_read.assert_not_called()

    def test_message_fetch_failure_does_not_checkpoint_or_write(self):
        self.messages.get.return_value = None
        self.messages.get.side_effect = api_error(503)

        result = self.service.process_from_gmail_history('200')

        self.assertTrue(result['errors'])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '100')
        self.service._update_sheet.assert_not_called()

    def test_parser_exception_does_not_checkpoint(self):
        with patch(
            'src.services.gyftr_processing_service.extract_vouchers_from_html',
            side_effect=ValueError('Malformed voucher'),
        ):
            result = self.service.process_from_gmail_history('200')

        self.assertTrue(result['errors'])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '100')
        self.service._update_sheet.assert_not_called()

    def test_deleted_message_is_a_terminal_skip(self):
        self.messages.get.side_effect = api_error(404)

        result = self.service.process_from_gmail_history('200')

        self.assertEqual(result['errors'], [])
        self.assertEqual(result['messages_skipped_deleted'], 1)
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '200')
        self.service._update_sheet.assert_not_called()

    def test_history_transient_failure_does_not_fall_back_to_search(self):
        self.service._get_message_ids_since_history.side_effect = api_error(429)

        result = self.service.process_from_gmail_history('200')

        self.assertTrue(result['errors'])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '100')
        self.messages.list.assert_not_called()

    def test_config_read_failure_does_not_fall_back_or_checkpoint(self):
        self.service._get_config_value.side_effect = RuntimeError('Config unavailable')

        result = self.service.process_from_gmail_history('200')

        self.assertTrue(result['errors'])
        self.service._get_message_ids_since_history.assert_not_called()
        self.messages.list.assert_not_called()
        self.service._set_config_value.assert_not_called()

    def test_corrupt_pending_state_is_not_silently_discarded(self):
        self.state['PENDING_GMAIL_HISTORY_BATCH'] = '{broken json'

        result = self.service.process_from_gmail_history('200')

        self.assertTrue(result['errors'])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '100')
        self.service._get_message_ids_since_history.assert_not_called()

    def test_invalid_saved_cursor_is_not_replaced_by_a_bounded_search(self):
        self.state['LAST_GMAIL_HISTORY_ID'] = 'not-a-history-id'

        result = self.service.process_from_gmail_history('200')

        self.assertTrue(result['errors'])
        self.messages.list.assert_not_called()
        self.service._set_config_value.assert_not_called()

    def test_older_notification_cannot_move_cursor_backward(self):
        self.state['LAST_GMAIL_HISTORY_ID'] = '300'

        result = self.service.process_from_gmail_history('200')

        self.assertEqual(result['errors'], [])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '300')
        self.service._get_message_ids_since_history.assert_not_called()
        self.service._set_config_value.assert_not_called()

    def test_more_than_fifty_messages_resume_without_skipping(self):
        message_ids = [f'message-{index}' for index in range(75)]
        self.service._get_message_ids_since_history.return_value = message_ids

        first = self.service.process_from_gmail_history('200')

        self.assertEqual(first['emails_checked'], 50)
        self.assertTrue(first['has_more'])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '100')
        self.assertEqual(self.pending()['message_ids'], message_ids[50:])
        second = self.service.process_from_gmail_history('200')

        self.assertEqual(second['emails_checked'], 25)
        self.assertFalse(second['has_more'])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '200')
        self.assertEqual(
            [request.kwargs['id'] for request in self.messages.get.call_args_list],
            message_ids,
        )
        self.service._get_message_ids_since_history.assert_called_once_with('100')

    def test_pending_batch_resumes_even_on_an_older_notification(self):
        self.service._get_message_ids_since_history.return_value = ['first', 'second']
        self.service.process_from_gmail_history('200', max_messages=1)

        result = self.service.process_from_gmail_history('150', max_messages=1)

        self.assertEqual(result['errors'], [])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '200')
        self.assertFalse(result['has_more'])
        self.assertEqual(self.messages.get.call_args.kwargs['id'], 'second')

    def test_newer_notification_is_not_lost_while_finishing_pending_batch(self):
        self.service._get_message_ids_since_history.return_value = ['first', 'second']
        self.service.process_from_gmail_history('200', max_messages=1)

        result = self.service.process_from_gmail_history('300', max_messages=1)

        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '200')
        self.assertTrue(result['has_more'])

    def test_expired_history_search_paginates_and_keeps_thirty_day_limit(self):
        self.service._get_message_ids_since_history.side_effect = api_error(404)
        self.messages.list.return_value.execute.side_effect = [
            {'messages': [{'id': f'old-{index}'} for index in range(50)], 'nextPageToken': 'page-2'},
            {'messages': [{'id': 'old-50'}]},
        ]

        first = self.service.process_from_gmail_history('200')
        second = self.service.process_from_gmail_history('200')

        self.assertEqual(first['errors'], [])
        self.assertTrue(first['has_more'])
        self.assertEqual(second['emails_checked'], 1)
        self.assertFalse(second['has_more'])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '200')
        self.assertEqual(self.messages.list.call_args_list, [
            call(userId='me', q='from:gifts@gyftr.com newer_than:30d', maxResults=50, pageToken=None),
            call(userId='me', q='from:gifts@gyftr.com newer_than:30d', maxResults=50, pageToken='page-2'),
        ])

    def test_fallback_page_failure_does_not_checkpoint_partial_discovery(self):
        self.service._get_message_ids_since_history.side_effect = api_error(404)
        self.messages.list.return_value.execute.side_effect = [
            {'messages': [{'id': 'first'}], 'nextPageToken': 'page-2'},
            api_error(503),
        ]

        result = self.service.process_from_gmail_history('200')

        self.assertTrue(result['errors'])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '100')
        self.messages.get.assert_not_called()
        self.service._set_config_value.assert_not_called()

    def test_missing_cursor_uses_bounded_recovery(self):
        self.state.clear()
        self.messages.list.return_value.execute.return_value = {'messages': [{'id': 'first'}]}

        result = self.service.process_from_gmail_history('200')

        self.assertEqual(result['errors'], [])
        self.assertEqual(result['mode'], 'fallback_search')
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '200')
        self.service._get_message_ids_since_history.assert_not_called()

    def test_empty_history_can_checkpoint_without_a_sheet_write(self):
        self.service._get_message_ids_since_history.return_value = []

        result = self.service.process_from_gmail_history('200')

        self.assertEqual(result['errors'], [])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '200')
        self.service._update_sheet.assert_not_called()

    def test_history_pagination_collects_all_message_ids(self):
        service = GyftrProcessingService(
            self.repo, SimpleNamespace(gyftr_spreadsheet_id='test-sheet'),
        )
        history = self.repo.service.users.return_value.history.return_value
        history.list.return_value.execute.side_effect = [
            {'history': [{'messagesAdded': [{'message': {'id': 'first'}}]}],
             'nextPageToken': 'page-2'},
            {'history': [{'messagesAdded': [{'message': {'id': 'second'}}]}]},
        ]

        self.assertEqual(service._get_message_ids_since_history('100'), ['first', 'second'])
        self.assertEqual(history.list.call_args_list, [
            call(userId='me', startHistoryId='100', historyTypes=['messageAdded'], pageToken=None),
            call(userId='me', startHistoryId='100', historyTypes=['messageAdded'], pageToken='page-2'),
        ])

    def test_discovered_message_ids_are_deduplicated(self):
        self.service._get_message_ids_since_history.return_value = ['first', 'first', 'second']

        result = self.service.process_from_gmail_history('200')

        self.assertEqual(result['emails_checked'], 2)
        self.assertEqual(self.messages.get.call_count, 2)

    def test_failed_checkpoint_save_keeps_work_replayable(self):
        def fail_checkpoint(key, value):
            if key == 'LAST_GMAIL_HISTORY_ID':
                raise RuntimeError('Checkpoint unavailable')
            self.save_config(key, value)
        self.service._set_config_value.side_effect = fail_checkpoint

        result = self.service.process_from_gmail_history('200')

        self.assertTrue(result['errors'])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '100')
        self.assertEqual(self.pending()['message_ids'], ['message-1'])
        self.service._update_sheet.assert_called_once()

    def test_pending_cleanup_failure_does_not_reprocess_committed_work(self):
        fail_cleanup = True
        def save(key, value):
            if key == 'PENDING_GMAIL_HISTORY_BATCH' and not value and fail_cleanup:
                raise RuntimeError('Pending cleanup unavailable')
            self.save_config(key, value)
        self.service._set_config_value.side_effect = save

        first = self.service.process_from_gmail_history('200')
        self.assertTrue(first['errors'])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '200')
        fail_cleanup = False

        second = self.service.process_from_gmail_history('200')

        self.assertEqual(second['errors'], [])
        self.service._update_sheet.assert_called_once()
        self.assertFalse(self.state['PENDING_GMAIL_HISTORY_BATCH'])

    def test_zero_batch_size_is_rejected_without_state_changes(self):
        result = self.service.process_from_gmail_history('200', max_messages=0)

        self.assertTrue(result['errors'])
        self.service._set_config_value.assert_not_called()

    def test_pending_save_failure_prevents_processing(self):
        self.service._set_config_value.side_effect = RuntimeError('Config unavailable')

        result = self.service.process_from_gmail_history('200')

        self.assertTrue(result['errors'])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '100')
        self.messages.get.assert_not_called()
        self.service._update_sheet.assert_not_called()
        self.repo.mark_as_read.assert_not_called()

    def test_pending_progress_failure_preserves_entire_replayable_batch(self):
        ids = [f'message-{index}' for index in range(75)]
        self.service._get_message_ids_since_history.return_value = ids

        def save(key, value):
            if key == 'PENDING_GMAIL_HISTORY_BATCH' and key in self.state:
                raise RuntimeError('Progress unavailable')
            self.save_config(key, value)

        self.service._set_config_value.side_effect = save

        result = self.service.process_from_gmail_history('200')

        self.assertTrue(result['errors'])
        self.assertEqual(result['rows_added'], 50)
        self.assertEqual(self.pending()['message_ids'], ids)
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '100')
        self.repo.mark_as_read.assert_not_called()

    def test_pending_read_failure_does_not_discover_or_advance(self):
        def read(key):
            if key == 'PENDING_GMAIL_HISTORY_BATCH':
                raise RuntimeError('Pending state unavailable')
            return self.state.get(key)

        self.service._get_config_value.side_effect = read

        result = self.service.process_from_gmail_history('200')

        self.assertTrue(result['errors'])
        self.service._get_message_ids_since_history.assert_not_called()
        self.service._set_config_value.assert_not_called()
        self.messages.list.assert_not_called()

    def test_invalid_pending_fields_are_preserved_for_repair(self):
        for batch in [
            [],
            {},
            {'target_history_id': 'invalid', 'message_ids': [], 'mode': 'gmail_history'},
            {'target_history_id': '200', 'message_ids': 'message-1', 'mode': 'gmail_history'},
            {'target_history_id': '200', 'message_ids': [None], 'mode': 'gmail_history'},
            {'target_history_id': '200', 'message_ids': [], 'mode': 'unknown'},
        ]:
            with self.subTest(batch=batch):
                raw_pending = json.dumps(batch)
                self.state['PENDING_GMAIL_HISTORY_BATCH'] = raw_pending

                result = self.service.process_from_gmail_history('200')

                self.assertTrue(result['errors'])
                self.assertEqual(self.state['PENDING_GMAIL_HISTORY_BATCH'], raw_pending)
                self.service._set_config_value.assert_not_called()
                self.service._get_message_ids_since_history.assert_not_called()

    def test_invalid_incoming_ids_do_not_change_state(self):
        for history_id in ['', '0', '-1', 'invalid', None]:
            with self.subTest(history_id=history_id):
                result = self.service.process_from_gmail_history(history_id)

                self.assertTrue(result['errors'])
                self.service._get_config_value.assert_not_called()
                self.service._set_config_value.assert_not_called()

    def test_zero_saved_cursor_is_not_treated_as_missing(self):
        for history_id in ['0', 0, False]:
            with self.subTest(history_id=history_id):
                self.state['LAST_GMAIL_HISTORY_ID'] = history_id

                result = self.service.process_from_gmail_history('200')

                self.assertTrue(result['errors'])
                self.messages.list.assert_not_called()
                self.service._set_config_value.assert_not_called()

    def test_final_checkpoint_does_not_overwrite_a_newer_saved_cursor(self):
        def write(vouchers):
            self.state['LAST_GMAIL_HISTORY_ID'] = '300'
            return len(vouchers)

        self.service._update_sheet.side_effect = write

        result = self.service.process_from_gmail_history('200')

        self.assertEqual(result['errors'], [])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '300')
        self.assertNotIn(call('LAST_GMAIL_HISTORY_ID', '200'), self.service._set_config_value.call_args_list)
        self.assertFalse(self.state['PENDING_GMAIL_HISTORY_BATCH'])

    def test_mark_read_failure_is_logged_after_durable_success(self):
        self.repo.mark_as_read.side_effect = RuntimeError('Read labels unavailable')

        with patch('builtins.print') as output:
            result = self.service.process_from_gmail_history('200')

        self.assertEqual(result['errors'], [])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '200')
        self.assertFalse(self.state['PENDING_GMAIL_HISTORY_BATCH'])
        self.assertTrue(any('Could not mark email' in str(args) for args in output.call_args_list))

    def test_case_insensitive_headers_and_sender_are_processed(self):
        request = self.message_request('me', 'message-1', 'full')
        for header in request.execute.return_value['payload']['headers']:
            header['name'] = header['name'].lower()
            if header['name'] == 'from':
                header['value'] = 'GyFTR <GIFTS@GYFTR.COM>'
        self.messages.get.side_effect = None
        self.messages.get.return_value = request

        result = self.service.process_from_gmail_history('200')

        self.assertEqual(result['errors'], [])
        self.assertEqual(result['vouchers_found'], 1)
        self.service._update_sheet.assert_called_once()

    def test_sender_substring_is_not_accepted_as_gyftr(self):
        request = self.message_request('me', 'message-1', 'full')
        request.execute.return_value['payload']['headers'][0]['value'] = (
            'Other <not-gifts@gyftr.com>'
        )
        self.messages.get.side_effect = None
        self.messages.get.return_value = request

        result = self.service.process_from_gmail_history('200')

        self.assertEqual(result['errors'], [])
        self.assertEqual(self.state['LAST_GMAIL_HISTORY_ID'], '200')
        self.service._update_sheet.assert_not_called()
        self.repo.mark_as_read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
