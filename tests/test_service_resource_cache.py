import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.services.gyftr_processing_service import GyftrProcessingService


class ServiceResourceCacheTests(unittest.TestCase):
    def setUp(self):
        self.repo = Mock()
        self.service = GyftrProcessingService(
            self.repo, SimpleNamespace(gyftr_spreadsheet_id='test-sheet'),
        )
        self.api = Mock()
        self.sheets = self.api.spreadsheets.return_value
        self.values = self.sheets.values.return_value
        builder_patch = patch(
            'src.services.gyftr_processing_service.build', return_value=self.api,
        )
        self.builder = builder_patch.start()
        self.addCleanup(builder_patch.stop)
        self.metadata = {'sheets': [
            {'properties': {'title': 'Sheet1', 'sheetId': 7}},
            {'properties': {'title': '_config', 'sheetId': 8, 'gridProperties': {'rowCount': 1000, 'columnCount': 2}}},
            {'properties': self.service._guard_properties(9)},
        ]}
        self.sheets.get.return_value.execute.return_value = self.metadata
        self.headers = [
            'Logo', 'Brand', 'Value', 'Code', 'Pin', 'Expiry',
            'Email Date', 'Message ID', 'Added By', 'Created At',
        ]
        self.config = {'LAST_GMAIL_HISTORY_ID': '100'}
        self.rows = []
        self.values.get.side_effect = self.read_request
        self.values.update.side_effect = self.update_request
        self.sheets.batchUpdate.side_effect = self.batch_request

    def batch_request(self, **kwargs):
        def execute():
            metadata = self.sheets.get.return_value.execute.return_value
            for request in kwargs['body']['requests']:
                if 'deleteSheet' in request:
                    metadata['sheets'] = [
                        sheet for sheet in metadata['sheets']
                        if sheet['properties']['sheetId'] != request['deleteSheet']['sheetId']
                    ]
                elif 'addSheet' in request:
                    metadata['sheets'].append({'properties': request['addSheet']['properties']})
                elif 'updateCells' in request:
                    update = request['updateCells']
                    if update['start']['rowIndex']:
                        key, value = [
                            cell['userEnteredValue']['stringValue']
                            for cell in update['rows'][0]['values']
                        ]
                        self.config[key] = value
            return {}
        return Mock(execute=execute)

    def read_request(self, **kwargs):
        def execute():
            if kwargs['range'] == '_config!A2:B':
                return {'values': [list(item) for item in self.config.items()]}
            if kwargs['range'].endswith('A1:Z1'):
                return {'values': [self.headers.copy()]}
            if kwargs['range'].endswith('A2:Z'):
                return {'values': [row.copy() for row in self.rows]}
            raise AssertionError(f"Unexpected read range: {kwargs['range']}")
        return Mock(execute=execute)

    def update_request(self, **kwargs):
        def execute():
            if kwargs['range'].startswith('_config!A') and kwargs['range'] != '_config!A1:B1':
                key, value = kwargs['body']['values'][0]
                self.config[key] = value
            return {}
        return Mock(execute=execute)

    def assert_one_resource_tree(self):
        self.builder.assert_called_once_with(
            'sheets', 'v4', credentials=self.repo.get_credentials.return_value,
        )
        self.repo.get_credentials.assert_called_once_with()
        self.api.spreadsheets.assert_called_once_with()
        self.sheets.values.assert_called_once_with()

    def test_api_initialization_is_lazy(self):
        self.builder.assert_not_called()
        self.repo.get_credentials.assert_not_called()

    def test_missing_spreadsheet_id_fails_before_client_initialization(self):
        self.service.settings.gyftr_spreadsheet_id = None
        operations = [
            lambda: self.service._get_config_value('LAST_GMAIL_HISTORY_ID'),
            lambda: self.service._set_config_value('LAST_GMAIL_HISTORY_ID', '200'),
            lambda: self.service._update_sheet([{'Code': 'TEST-CODE-001'}]),
        ]
        for operation in operations:
            with self.assertRaisesRegex(ValueError, 'Missing GYFTR_SPREADSHEET_ID'):
                operation()
            self.builder.assert_not_called()

    def test_config_reads_and_writes_reuse_resources_but_not_config_values(self):
        self.assertEqual(self.service._get_config_value('LAST_GMAIL_HISTORY_ID'), '100')
        self.service._set_config_value('LAST_GMAIL_HISTORY_ID', '200')
        self.assertEqual(self.service._get_config_value('LAST_GMAIL_HISTORY_ID'), '200')

        self.assert_one_resource_tree()
        self.assertEqual(self.sheets.get.call_count, 6)
        self.assertEqual(self.values.get.call_count, 3)

    def test_config_and_voucher_writes_share_the_resource_tree(self):
        self.service._get_config_value('LAST_GMAIL_HISTORY_ID')
        self.service._set_config_value('LAST_GMAIL_HISTORY_ID', '200')

        added = self.service._update_sheet([{
            'Code': 'TEST-CODE-001', 'Brand': 'Test Brand',
            'Email Date': '2026-11-02 14:30:00',
        }])

        self.assertEqual(added, 1)
        self.assert_one_resource_tree()

    def test_missing_config_values_are_not_cached(self):
        self.config.clear()
        self.assertIsNone(self.service._get_config_value('LAST_GMAIL_HISTORY_ID'))
        self.config['LAST_GMAIL_HISTORY_ID'] = '200'

        self.assertEqual(self.service._get_config_value('LAST_GMAIL_HISTORY_ID'), '200')
        self.assert_one_resource_tree()

    def test_existing_voucher_rows_are_re_read_on_each_attempt(self):
        self.rows = [['', 'Test Brand', 1000, 'TEST-CODE-001']]
        self.assertEqual(self.service._update_sheet([{'Code': 'TEST-CODE-001'}]), 0)
        self.rows.append(['', 'Test Brand', 1000, 'TEST-CODE-002'])

        self.assertEqual(self.service._update_sheet([{'Code': 'TEST-CODE-002'}]), 0)

        self.sheets.batchUpdate.assert_not_called()
        self.assert_one_resource_tree()

    def test_deleted_config_sheet_is_detected_and_recreated(self):
        self.service._get_config_value('LAST_GMAIL_HISTORY_ID')
        self.sheets.get.return_value.execute.return_value = {
            'sheets': [sheet for sheet in self.metadata['sheets'] if sheet['properties']['title'] != '_config'],
        }
        self.config.clear()

        self.assertIsNone(self.service._get_config_value('LAST_GMAIL_HISTORY_ID'))

        self.sheets.batchUpdate.assert_called_once()
        requests = self.sheets.batchUpdate.call_args.kwargs['body']['requests']
        self.assertEqual(requests[0]['deleteSheet']['sheetId'], 9)
        self.assertEqual(requests[2]['addSheet']['properties']['title'], '_config')
        self.assertEqual(requests[3]['updateCells']['start']['rowIndex'], 0)
        self.assert_one_resource_tree()

    def test_failed_service_creation_is_retried_without_caching_failure(self):
        self.builder.side_effect = [RuntimeError('Discovery unavailable'), self.api]

        with self.assertRaisesRegex(RuntimeError, 'Discovery unavailable'):
            self.service.get_sheets_service()
        self.assertIs(self.service.get_sheets_service(), self.api)
        self.assertIs(self.service.get_sheets_service(), self.api)
        self.assertEqual(self.builder.call_count, 2)

    def test_failed_spreadsheets_resource_creation_is_retried(self):
        self.api.spreadsheets.side_effect = [RuntimeError('Resource unavailable'), self.sheets]

        with self.assertRaisesRegex(RuntimeError, 'Resource unavailable'):
            self.service._get_config_value('LAST_GMAIL_HISTORY_ID')
        self.assertEqual(self.service._get_config_value('LAST_GMAIL_HISTORY_ID'), '100')

        self.builder.assert_called_once()
        self.assertEqual(self.api.spreadsheets.call_count, 2)
        self.sheets.values.assert_called_once_with()

    def test_failed_values_resource_creation_is_retried(self):
        self.sheets.values.side_effect = [RuntimeError('Values unavailable'), self.values]

        with self.assertRaisesRegex(RuntimeError, 'Values unavailable'):
            self.service._get_config_value('LAST_GMAIL_HISTORY_ID')
        self.assertEqual(self.service._get_config_value('LAST_GMAIL_HISTORY_ID'), '100')

        self.builder.assert_called_once()
        self.api.spreadsheets.assert_called_once_with()
        self.assertEqual(self.sheets.values.call_count, 2)

    def test_api_request_failure_does_not_rebuild_resources(self):
        self.sheets.get.return_value.execute.side_effect = [
            TimeoutError('Metadata timed out'), self.metadata, self.metadata,
        ]
        with self.assertRaisesRegex(TimeoutError, 'Metadata timed out'):
            self.service._get_config_value('LAST_GMAIL_HISTORY_ID')

        self.assertEqual(self.service._get_config_value('LAST_GMAIL_HISTORY_ID'), '100')
        self.assert_one_resource_tree()

    def test_separate_service_instances_do_not_share_clients(self):
        second_api = Mock()
        second_api.spreadsheets.return_value.get.return_value.execute.return_value = self.metadata
        second_api.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            'values': [['LAST_GMAIL_HISTORY_ID', '900']],
        }
        self.builder.side_effect = [self.api, second_api]
        second = GyftrProcessingService(
            Mock(), SimpleNamespace(gyftr_spreadsheet_id='other-test-sheet'),
        )

        self.assertEqual(self.service._get_config_value('LAST_GMAIL_HISTORY_ID'), '100')
        self.assertEqual(second._get_config_value('LAST_GMAIL_HISTORY_ID'), '900')
        self.assertIsNot(self.service.get_sheets_service(), second.get_sheets_service())
        self.assertEqual(self.builder.call_count, 2)


if __name__ == '__main__':
    unittest.main()
