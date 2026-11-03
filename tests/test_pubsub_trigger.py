import base64
from contextlib import nullcontext
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main
from src.services.gyftr_processing_service import GyftrProcessingService


class PubsubTriggerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 2, 9, tzinfo=timezone.utc)
        self.context = SimpleNamespace(timestamp=self.now.isoformat(), event_id='test-event')
        self.event = self.notification({'historyId': '200', 'emailAddress': 'test@example.com'})
        main._get_service_factory.cache_clear()
        self.addCleanup(main._get_service_factory.cache_clear)
        factory_patch = patch('main.ServiceFactory')
        self.factory_class = factory_patch.start()
        self.addCleanup(factory_patch.stop)
        self.factory = self.factory_class.return_value
        self.service = self.factory.get_gyftr_processing_service.return_value
        self.success = {
            'emails_checked': 1, 'vouchers_found': 1, 'rows_added': 1,
            'errors': [], 'mode': 'gmail_history', 'has_more': False,
            'messages_skipped_deleted': 0,
        }
        self.service.process_from_gmail_history.return_value = self.success
        clock_patch = patch('main.datetime', wraps=datetime)
        self.clock = clock_patch.start()
        self.clock.now.return_value = self.now
        self.addCleanup(clock_patch.stop)

    @staticmethod
    def notification(payload):
        return {'data': base64.b64encode(json.dumps(payload).encode()).decode()}

    def test_completed_history_batch_returns_success(self):
        result = main.process_pubsub_message_gen1(self.event, self.context)

        self.assertIs(result, self.success)
        self.factory.validate_configuration.assert_called_once()
        self.service.process_from_gmail_history.assert_called_once_with(
            current_history_id='200', source='cloud_function_automation',
        )
        self.service.process_new_gyftr_emails.assert_not_called()

    def test_warm_invocations_reuse_factory_and_revalidate_configuration(self):
        main.process_pubsub_message_gen1(self.event, self.context)
        main.process_pubsub_message_gen1(self.event, self.context)

        self.factory_class.assert_called_once_with()
        self.assertEqual(self.factory.validate_configuration.call_count, 2)
        self.assertEqual(self.service.process_from_gmail_history.call_count, 2)

    def test_failed_factory_creation_can_be_retried(self):
        self.factory_class.side_effect = [RuntimeError('Factory unavailable'), self.factory]

        with self.assertRaisesRegex(RuntimeError, 'Factory unavailable'):
            main.process_pubsub_message_gen1(self.event, self.context)
        result = main.process_pubsub_message_gen1(self.event, self.context)

        self.assertIs(result, self.success)
        self.assertEqual(self.factory_class.call_count, 2)

    def test_application_errors_fail_the_invocation_for_retry(self):
        self.service.process_from_gmail_history.return_value = {
            **self.success, 'errors': ['Sheets temporarily unavailable'],
        }

        with patch('builtins.print') as output:
            with self.assertRaisesRegex(RuntimeError, '[Ee]rror|[Ff]ail'):
                main.process_pubsub_message_gen1(self.event, self.context)

        self.assertNotIn('Processing complete', str(output.call_args_list))

    def test_pending_batch_fails_the_invocation_for_continuation(self):
        self.service.process_from_gmail_history.return_value = {
            **self.success, 'rows_added': 50, 'has_more': True,
        }

        with patch('builtins.print') as output:
            with self.assertRaisesRegex(RuntimeError, 'pending|remaining|incomplete'):
                main.process_pubsub_message_gen1(self.event, self.context)

        self.service.process_from_gmail_history.assert_called_once()
        self.assertNotIn('Processing complete', str(output.call_args_list))

    def test_service_exception_is_propagated(self):
        self.service.process_from_gmail_history.side_effect = TimeoutError('API timed out')

        with self.assertRaisesRegex(TimeoutError, 'API timed out'):
            main.process_pubsub_message_gen1(self.event, self.context)

    def test_configuration_failure_is_propagated_within_retry_window(self):
        self.factory.validate_configuration.side_effect = ValueError('Missing test config')

        with self.assertRaisesRegex(ValueError, 'Missing test config'):
            main.process_pubsub_message_gen1(self.event, self.context)

        self.service.process_from_gmail_history.assert_not_called()

    def test_same_notification_can_finish_after_a_transient_failure(self):
        self.service.process_from_gmail_history.side_effect = [
            {**self.success, 'errors': ['Temporary API failure']}, self.success,
        ]

        with self.assertRaises(RuntimeError):
            main.process_pubsub_message_gen1(self.event, self.context)
        result = main.process_pubsub_message_gen1(self.event, self.context)

        self.assertIs(result, self.success)
        self.assertEqual(self.service.process_from_gmail_history.call_count, 2)

    def test_deleted_message_skip_is_success_not_a_retry(self):
        self.service.process_from_gmail_history.return_value = {
            **self.success, 'rows_added': 0, 'messages_skipped_deleted': 1,
        }

        result = main.process_pubsub_message_gen1(self.event, self.context)

        self.assertEqual(result['errors'], [])
        self.assertFalse(result['has_more'])

    def test_expired_history_recovery_can_complete_normally(self):
        self.service.process_from_gmail_history.return_value = {
            **self.success, 'mode': 'fallback_search',
        }

        result = main.process_pubsub_message_gen1(self.event, self.context)

        self.assertEqual(result['mode'], 'fallback_search')
        self.service.process_new_gyftr_emails.assert_not_called()

    def test_invalid_processing_results_fail_closed(self):
        for result in [
            None, {}, {'errors': []}, {'has_more': False},
            {'errors': 'failed', 'has_more': False},
            {'errors': [], 'has_more': 'false'},
        ]:
            with self.subTest(result=result):
                self.service.process_from_gmail_history.return_value = result

                with self.assertRaises(RuntimeError):
                    main.process_pubsub_message_gen1(self.event, self.context)

    def test_missing_data_is_rejected_without_scanning_gmail(self):
        for event in [{}, {'data': ''}, None, []]:
            with self.subTest(event=event):
                with patch('builtins.print') as output:
                    result = main.process_pubsub_message_gen1(event, self.context)

                self.assertEqual(result['status'], 'rejected')
                self.assertIn('invalid', str(output.call_args_list).lower())
                self.factory_class.assert_not_called()

    def test_malformed_data_is_rejected_without_scanning_gmail(self):
        for data in [
            'not base64!', base64.b64encode(b'not JSON').decode(),
            base64.b64encode(b'\xff').decode(),
        ]:
            with self.subTest(data=data):
                result = main.process_pubsub_message_gen1({'data': data}, self.context)

                self.assertEqual(result['status'], 'rejected')
                self.factory_class.assert_not_called()

    def test_non_object_json_is_rejected_without_scanning_gmail(self):
        for payload in [None, [], 'invalid', 123]:
            with self.subTest(payload=payload):
                result = main.process_pubsub_message_gen1(self.notification(payload), self.context)

                self.assertEqual(result['status'], 'rejected')
                self.factory_class.assert_not_called()

    def test_missing_or_invalid_history_id_is_rejected(self):
        for history_id in [None, '', '0', '-1', 'invalid', True, 1.5, [], {}, '\u0661']:
            with self.subTest(history_id=history_id):
                event = self.notification({'historyId': history_id})

                result = main.process_pubsub_message_gen1(event, self.context)

                self.assertEqual(result['status'], 'rejected')
                self.factory_class.assert_not_called()
        result = main.process_pubsub_message_gen1(self.notification({}), self.context)
        self.assertEqual(result['status'], 'rejected')

    def test_integer_history_id_is_passed_without_precision_loss(self):
        event = self.notification({'historyId': 12345678901234567890})

        main.process_pubsub_message_gen1(event, self.context)

        self.service.process_from_gmail_history.assert_called_once_with(
            current_history_id='12345678901234567890', source='cloud_function_automation',
        )

    def test_base64_bytes_are_accepted(self):
        event = {'data': self.event['data'].encode()}

        result = main.process_pubsub_message_gen1(event, self.context)

        self.assertIs(result, self.success)

    def test_twenty_four_hour_boundary_expires_before_factory_creation(self):
        for age in [timedelta(hours=24), timedelta(hours=25), timedelta(days=7)]:
            with self.subTest(age=age):
                context = SimpleNamespace(timestamp=(self.now - age).isoformat())
                with patch('builtins.print') as output:
                    result = main.process_pubsub_message_gen1(self.event, context)

                self.assertEqual(result['status'], 'expired')
                self.assertIn('pending', str(output.call_args_list).lower())
                self.factory_class.assert_not_called()

    def test_one_microsecond_before_retry_deadline_can_continue(self):
        context = SimpleNamespace(
            timestamp=(self.now - timedelta(hours=24) + timedelta(microseconds=1)).isoformat(),
        )
        self.service.process_from_gmail_history.return_value = {**self.success, 'has_more': True}

        with self.assertRaises(RuntimeError):
            main.process_pubsub_message_gen1(self.event, context)

        self.service.process_from_gmail_history.assert_called_once()

    def test_redelivery_does_not_restart_the_twenty_four_hour_budget(self):
        self.factory.validate_configuration.side_effect = ValueError('Missing test config')

        with self.assertRaises(ValueError):
            main.process_pubsub_message_gen1(self.event, self.context)
        self.clock.now.return_value = self.now + timedelta(hours=24)

        result = main.process_pubsub_message_gen1(self.event, self.context)

        self.assertEqual(result['status'], 'expired')
        self.factory.validate_configuration.assert_called_once()

    def test_utc_z_and_offset_timestamps_are_supported(self):
        for timestamp in ['2026-11-02T09:00:00Z', '2026-11-02T14:30:00+05:30']:
            with self.subTest(timestamp=timestamp):
                result = main.process_pubsub_message_gen1(
                    self.event, SimpleNamespace(timestamp=timestamp),
                )

                self.assertIs(result, self.success)

    def test_invalid_event_timestamps_are_rejected_without_api_calls(self):
        for context in [
            None, SimpleNamespace(), SimpleNamespace(timestamp='invalid'),
            SimpleNamespace(timestamp='2026-11-02T09:00:00'),
        ]:
            with self.subTest(context=context):
                result = main.process_pubsub_message_gen1(self.event, context)

                self.assertEqual(result['status'], 'rejected')
                self.factory_class.assert_not_called()

    def test_notification_logs_do_not_include_email_or_extra_payload_fields(self):
        event = self.notification({
            'historyId': '200', 'emailAddress': 'private-test@example.com',
            'extra': 'TEST-PRIVATE-CONTENT',
        })

        with patch('builtins.print') as output:
            main.process_pubsub_message_gen1(event, self.context)

        self.assertNotIn('private-test@example.com', str(output.call_args_list))
        self.assertNotIn('TEST-PRIVATE-CONTENT', str(output.call_args_list))

    def test_seventy_five_messages_finish_by_redelivering_the_same_notification(self):
        repo = Mock()
        service = GyftrProcessingService(repo, SimpleNamespace(gyftr_spreadsheet_id='test-sheet'))
        service._sheet_write_session = Mock(side_effect=nullcontext)
        self.factory.get_gyftr_processing_service.return_value = service
        state = {'LAST_GMAIL_HISTORY_ID': '100'}
        ids = [f'test-message-{index}' for index in range(75)]
        service._get_config_value = Mock(side_effect=state.get)
        service._set_config_value = Mock(side_effect=state.__setitem__)
        service._get_message_ids_since_history = Mock(return_value=ids)
        written = set()

        def write(vouchers):
            before = len(written)
            written.update(voucher['Code'] for voucher in vouchers)
            return len(written) - before

        def message_request(userId, id, format):
            return Mock(execute=Mock(return_value={'payload': {
                'mimeType': 'text/html',
                'headers': [
                    {'name': 'From', 'value': 'gifts@gyftr.com'},
                    {'name': 'Date', 'value': 'Mon, 02 Nov 2026 14:30:00 +0530'},
                ],
                'body': {'data': base64.urlsafe_b64encode(id.encode()).decode()},
            }}))

        service._update_sheet = Mock(side_effect=write)
        repo.service.users.return_value.messages.return_value.get.side_effect = message_request
        with patch(
            'src.services.gyftr_processing_service.extract_vouchers_from_html',
            side_effect=lambda html: [{'Code': html, 'Brand': 'Test Brand'}],
        ):
            with self.assertRaises(RuntimeError):
                main.process_pubsub_message_gen1(self.event, self.context)
            self.assertEqual(state['LAST_GMAIL_HISTORY_ID'], '100')
            self.assertEqual(len(json.loads(state['PENDING_GMAIL_HISTORY_BATCH'])['message_ids']), 25)

            result = main.process_pubsub_message_gen1(self.event, self.context)
            replay = main.process_pubsub_message_gen1(self.event, self.context)

        self.assertEqual(result['emails_checked'], 25)
        self.assertEqual(result['rows_added'], 25)
        self.assertEqual(replay['emails_checked'], 0)
        self.assertEqual(replay['rows_added'], 0)
        self.assertEqual(written, set(ids))
        self.assertEqual(state['LAST_GMAIL_HISTORY_ID'], '200')
        self.assertFalse(state['PENDING_GMAIL_HISTORY_BATCH'])
        service._get_message_ids_since_history.assert_called_once()
        self.assertEqual(service._update_sheet.call_count, 2)


if __name__ == '__main__':
    unittest.main()
