import base64
import contextlib
import copy
import http.client
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import httplib2
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest

from src.services.gyftr_processing_service import GyftrProcessingService
import test_sheet_writes as sheet_tests


class SheetCoordinationTests(unittest.TestCase):
    def setUp(self):
        self.backend = sheet_tests.SheetWriteTests('runTest')
        self.backend.setUp()
        self.addCleanup(self.backend.doCleanups)
        self.service = self.backend.service
        self.api = self.backend.sheets
        self.values = self.backend.values

    def second_service(self):
        service = GyftrProcessingService(
            self.backend.repo, SimpleNamespace(gyftr_spreadsheet_id='test-sheet'),
        )
        service.get_sheets_service = Mock(return_value=self.backend.api)
        return service

    def test_native_transport_replay_commits_once_even_with_default_request_retries(self):
        transport = httplib2.Http()
        connection = Mock()
        connection.sock = object()
        socket = Mock()
        error_body = b'{"error":{"message":"Stale revision"}}'
        socket.makefile.return_value = io.BytesIO(
            b'HTTP/1.1 400 Bad Request\r\nContent-Length: '
            + str(len(error_body)).encode() + b'\r\n\r\n' + error_body,
        )
        response = http.client.HTTPResponse(socket)
        response.begin()
        connection.getresponse.side_effect = [
            http.client.RemoteDisconnected('Synthetic lost committed response'), response,
        ]
        server_rejections = []

        def request(**kwargs):
            server_commit = self.backend.batch_request(**kwargs)

            def commit(*args):
                try:
                    server_commit.execute()
                except HttpError as error:
                    self.assertEqual(error.resp.status, 400)
                    server_rejections.append(error.resp.status)

            connection.request.side_effect = commit
            return HttpRequest(
                http=transport, uri='https://example.invalid/batchUpdate', method='POST',
                body=json.dumps(kwargs['body']), headers={'content-type': 'application/json'},
                postproc=lambda response, content: json.loads(content),
            )

        transport.request = lambda uri, method, body=None, headers=None, **kwargs: transport._conn_request(
            connection, '/batchUpdate', method, body, headers or {},
        )
        self.api.batchUpdate.side_effect = request
        with self.assertRaises(HttpError):
            self.service._update_sheet([self.backend.voucher()])
        self.assertEqual(connection.request.call_count, 2)
        self.assertEqual(server_rejections, [400])
        self.assertEqual(len(self.backend.rows), 1)
        self.assertEqual(self.service._update_sheet([self.backend.voucher()]), 0)
        self.assertEqual(len(self.backend.rows), 1)

    def test_overlapping_writers_cannot_commit_the_same_snapshot(self):
        second = self.second_service()
        base_read = self.backend.read_request
        interleaved = False

        def read(**kwargs):
            request = base_read(**kwargs)
            if not kwargs['range'].endswith('A2:Z'):
                return request

            def execute():
                nonlocal interleaved
                snapshot = request.execute()
                if not interleaved:
                    interleaved = True
                    second._update_sheet([self.backend.voucher()])
                return snapshot
            return Mock(execute=execute)

        self.values.get.side_effect = read
        with self.assertRaises(HttpError):
            self.service._update_sheet([self.backend.voucher()])
        self.assertEqual(len(self.backend.rows), 1)
        self.assertEqual(self.service._update_sheet([self.backend.voucher()]), 0)

    def test_stale_writer_cannot_regress_checkpoint_or_reuse_old_revision(self):
        self.service._set_config_value('LAST_GMAIL_HISTORY_ID', '100')
        second = self.second_service()
        with self.service._sheet_write_session():
            stale_id = self.service._revision_sheet_id
            second._set_config_value('LAST_GMAIL_HISTORY_ID', '200')
            second._set_config_value('LAST_GMAIL_HISTORY_ID', '300')
            self.assertNotEqual(second._revision_sheet_id, stale_id)
            with self.assertRaises(HttpError):
                self.service._set_config_value('LAST_GMAIL_HISTORY_ID', '150')
            with self.assertRaisesRegex(RuntimeError, 'invalid'):
                self.service._set_config_value('LAST_GMAIL_HISTORY_ID', '160')
        self.assertEqual(self.service._get_config_value('LAST_GMAIL_HISTORY_ID'), '300')

    def test_guard_bootstrap_is_unique_and_sessions_are_not_cached(self):
        metadata = self.api.get.return_value.execute.return_value
        metadata['sheets'] = [
            sheet for sheet in metadata['sheets']
            if sheet['properties']['title'] != self.service.WRITE_GUARD_TITLE
        ]
        self.assertIsNone(self.service._get_config_value('missing'))
        self.assertEqual(sum(
            sheet['properties']['title'] == self.service.WRITE_GUARD_TITLE
            for sheet in metadata['sheets']
        ), 1)
        self.service._set_config_value('test', 'first')
        self.second_service()._set_config_value('test', 'second')
        self.assertEqual(self.service._get_config_value('test'), 'second')
        self.assertEqual(self.service._write_session_depth, 0)
        self.assertIsNone(self.service._revision_sheet_id)

    def test_reserved_guard_tab_is_not_silently_repurposed(self):
        self.api.get.return_value.execute.return_value['sheets'][-1]['properties']['hidden'] = False
        with self.assertRaisesRegex(ValueError, 'Reserved'):
            self.service._set_config_value('test', 'value')
        self.api.batchUpdate.assert_not_called()

    def test_large_config_round_trips_at_and_above_chunk_boundary(self):
        for size in (40000, 40001, 57066, 120001):
            with self.subTest(size=size):
                text = 'x' * size
                self.service._set_config_value('PENDING_GMAIL_HISTORY_BATCH', text)
                self.assertEqual(self.service._get_config_value('PENDING_GMAIL_HISTORY_BATCH'), text)
                self.assertTrue(all(
                    len(cell) <= 40000
                    for row in self.backend.config_rows for cell in row if isinstance(cell, str)
                ))

    def test_shrinking_and_clearing_pending_state_removes_stale_chunks(self):
        key = 'PENDING_GMAIL_HISTORY_BATCH'
        self.service._set_config_value('OTHER_SETTING', '=literal')
        self.service._set_config_value(key, 'x' * 100001)
        self.service._set_config_value(key, 'small legacy-compatible value')
        self.assertEqual(self.service._get_config_value(key), 'small legacy-compatible value')
        self.assertFalse(any(row and str(row[0]).startswith(key + '__chunk_') for row in self.backend.config_rows))
        self.service._set_config_value(key, 'x' * 50001)
        self.service._set_config_value(key, '')
        self.assertEqual(self.service._get_config_value(key), '')
        self.assertEqual(self.service._get_config_value('OTHER_SETTING'), '=literal')

    def test_api_trimming_empty_config_values_still_reads_as_empty(self):
        self.backend.config_rows = [['PENDING_GMAIL_HISTORY_BATCH']]
        self.assertEqual(self.service._get_config_value('PENDING_GMAIL_HISTORY_BATCH'), '')

    def test_guard_bootstrap_lost_response_is_recoverable_without_duplicate_tabs(self):
        metadata = self.api.get.return_value.execute.return_value
        metadata['sheets'] = [
            sheet for sheet in metadata['sheets']
            if sheet['properties']['title'] != self.service.WRITE_GUARD_TITLE
        ]
        self.backend.lose_response = True
        with self.assertRaises(TimeoutError):
            self.service._set_config_value('test', 'value')
        self.service._set_config_value('test', 'value')
        self.assertEqual(self.service._get_config_value('test'), 'value')
        self.assertEqual(sum(
            sheet['properties']['title'] == self.service.WRITE_GUARD_TITLE
            for sheet in metadata['sheets']
        ), 1)

    def test_internal_tabs_are_never_selected_as_the_voucher_sheet(self):
        metadata = self.api.get.return_value.execute.return_value
        metadata['sheets'] = list(reversed(metadata['sheets']))
        self.assertEqual(self.service._update_sheet([self.backend.voucher()]), 1)
        self.assertEqual(len(self.backend.rows), 1)

    def test_empty_local_scan_does_not_initialize_sheets_or_create_a_guard(self):
        messages = self.backend.repo.service.users.return_value.messages.return_value
        messages.list.return_value.execute.return_value = {}
        self.assertEqual(self.service.process_new_gyftr_emails()['errors'], [])
        self.service.get_sheets_service.assert_not_called()
        self.api.batchUpdate.assert_not_called()

    def test_corrupt_missing_and_duplicate_chunks_fail_closed(self):
        key = 'PENDING_GMAIL_HISTORY_BATCH'
        self.service._set_config_value(key, 'x' * 50001)
        original = copy.deepcopy(self.backend.config_rows)
        for defect in ('missing', 'changed', 'duplicate', 'manifest'):
            with self.subTest(defect=defect):
                self.backend.config_rows = copy.deepcopy(original)
                if defect == 'missing':
                    self.backend.config_rows.pop()
                elif defect == 'changed':
                    self.backend.config_rows[-1][1] = 'corrupt'
                elif defect == 'duplicate':
                    self.backend.config_rows.append(self.backend.config_rows[-1].copy())
                else:
                    self.backend.config_rows[0][1] = self.service.CONFIG_CHUNK_MARKER + '{"count":true}'
                with self.assertRaises(ValueError):
                    self.service._get_config_value(key)

    def test_failed_or_ambiguous_chunk_writes_preserve_replayable_complete_state(self):
        key = 'PENDING_GMAIL_HISTORY_BATCH'
        self.service._set_config_value(key, 'old')
        self.backend.reject_batch = True
        with self.assertRaises(RuntimeError):
            self.service._set_config_value(key, 'x' * 50001)
        self.backend.reject_batch = False
        self.assertEqual(self.service._get_config_value(key), 'old')
        self.backend.lose_response = True
        with self.assertRaises(TimeoutError):
            self.service._set_config_value(key, 'x' * 50001)
        self.assertEqual(self.service._get_config_value(key), 'x' * 50001)
        self.service._set_config_value(key, '')
        self.assertEqual(self.service._get_config_value(key), '')

    def test_config_grid_expands_atomically_for_new_chunk_rows(self):
        config = self.api.get.return_value.execute.return_value['sheets'][1]['properties']
        config['gridProperties']['rowCount'] = 2
        self.service._set_config_value('pending', 'x' * 120001)
        metadata = self.api.get.return_value.execute.return_value
        updated = next(sheet['properties'] for sheet in metadata['sheets'] if sheet['properties']['title'] == '_config')
        self.assertGreaterEqual(updated['gridProperties']['rowCount'], 6)
        self.assertEqual(self.service._get_config_value('pending'), 'x' * 120001)

    def test_three_thousand_message_queue_is_saved_and_resumed_without_cell_overflow(self):
        ids = [f'{index:016x}' for index in range(3000)]
        self.backend.config_rows = [['LAST_GMAIL_HISTORY_ID', '100']]
        self.backend.configure_email()
        messages = self.backend.repo.service.users.return_value.messages.return_value
        messages.get.return_value.execute.return_value['payload']['body']['data'] = base64.urlsafe_b64encode(
            sheet_tests.MODERN_HTML.encode(),
        ).decode()
        self.service._get_message_ids_since_history = Mock(return_value=ids)
        with contextlib.redirect_stdout(io.StringIO()):
            first = self.service.process_from_gmail_history('200', max_messages=1)
            second = self.service.process_from_gmail_history('200', max_messages=1)
        self.assertEqual(first['errors'], [])
        self.assertEqual(second['errors'], [])
        self.assertTrue(second['has_more'])
        pending = json.loads(self.service._get_config_value('PENDING_GMAIL_HISTORY_BATCH'))
        self.assertEqual(pending['message_ids'], ids[2:])
        self.assertEqual(self.service._get_config_value('LAST_GMAIL_HISTORY_ID'), '100')
        self.assertEqual(len(self.backend.rows), 1)
        self.service._get_message_ids_since_history.assert_called_once()

    def test_overlapping_history_discovery_cannot_overwrite_newer_pending_work(self):
        ids = [f'{index:016x}' for index in range(76)]
        self.backend.config_rows = [['LAST_GMAIL_HISTORY_ID', '100']]
        self.backend.configure_email()
        messages = self.backend.repo.service.users.return_value.messages.return_value
        messages.get.return_value.execute.return_value['payload']['body']['data'] = base64.urlsafe_b64encode(
            sheet_tests.MODERN_HTML.encode(),
        ).decode()
        second = self.second_service()
        second._get_message_ids_since_history = Mock(return_value=ids)
        concurrent_results = []

        def discovery(start):
            concurrent_results.append(second.process_from_gmail_history('300'))
            return ids[:-1]

        self.service._get_message_ids_since_history = Mock(side_effect=discovery)
        with contextlib.redirect_stdout(io.StringIO()):
            stale = self.service.process_from_gmail_history('200')
            self.assertTrue(stale['errors'])
            self.assertEqual(concurrent_results[0]['errors'], [])
            pending = json.loads(self.service._get_config_value('PENDING_GMAIL_HISTORY_BATCH'))
            self.assertEqual(pending['target_history_id'], '300')
            self.assertEqual(pending['message_ids'], ids[50:])
            resumed = self.service.process_from_gmail_history('200')
        self.assertEqual(resumed['errors'], [])
        self.assertFalse(resumed['has_more'])
        self.assertEqual(self.service._get_config_value('LAST_GMAIL_HISTORY_ID'), '300')
        self.assertEqual(self.service._get_config_value('PENDING_GMAIL_HISTORY_BATCH'), '')
        self.assertEqual({call.kwargs['id'] for call in messages.get.call_args_list}, set(ids))
        self.assertEqual(messages.get.call_count, len(ids))


if __name__ == '__main__':
    unittest.main()
