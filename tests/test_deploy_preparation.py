import copy
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import sys
import unittest
from unittest.mock import patch
import zipfile

from scripts import prepare_main_deploy as release


ROOT = Path(__file__).resolve().parents[1]


def main_config():
    return {
        'name': f'projects/test-project/locations/us-central1/functions/{release.FUNCTION}',
        'status': 'ACTIVE', 'versionId': '5', 'runtime': 'python311',
        'entryPoint': 'process_pubsub_message_gen1', 'availableMemoryMb': 512,
        'timeout': '60s', 'maxInstances': 1, 'ingressSettings': 'ALLOW_ALL',
        'serviceAccountEmail': 'test-project@appspot.gserviceaccount.com',
        'eventTrigger': {
            'eventType': 'google.pubsub.topic.publish',
            'resource': 'projects/test-project/topics/gmail-notifications',
            'failurePolicy': {},
        },
        'environmentVariables': {'REFRESH_TOKEN': 'synthetic-secret-not-for-snapshot'},
        'buildEnvironmentVariables': {'BUILD_TOKEN': 'synthetic-build-secret'},
    }


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def make_source(self, root):
        for name in ('main.py', 'requirements.txt', 'src/factory.py', 'src/config/module.py'):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('')
        shutil.copyfile(ROOT / '.gcloudignore', root / '.gcloudignore')

    @unittest.skipUnless(shutil.which('gcloud'), 'gcloud is required for real deployment archive verification')
    def test_actual_cloud_functions_archive_contains_the_runtime_payload(self):
        self.make_source(self.directory / 'source')
        sdk = release.gcloud_text('info', '--format=value(installation.sdk_root)').strip()
        code = (
            "import sys; from pathlib import Path; "
            "sys.path[:0] = [str(Path(sys.argv[1]) / 'lib'), str(Path(sys.argv[1]) / 'lib' / 'third_party')]; "
            "from googlecloudsdk.command_lib.functions import source_util; "
            "source_util.CreateSourcesZipFile(sys.argv[2], sys.argv[3], '.gcloudignore')"
        )
        result = subprocess.run(
            [sys.executable, '-c', code, sdk, str(self.directory), str(self.directory / 'source')],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr[:1500])
        with zipfile.ZipFile(self.directory / 'fun.zip') as archive:
            files = {entry.filename for entry in archive.infolist() if not entry.is_dir()}
            self.assertEqual(files, {'main.py', 'requirements.txt', 'src/factory.py', 'src/config/module.py'})

    def make_archive(self, path, extra=None):
        with zipfile.ZipFile(path, 'w') as archive:
            for name in ('main.py', 'requirements.txt', 'src/factory.py'):
                archive.writestr(name, '')
            for name, value in (extra or {}).items():
                archive.writestr(name, value)

    @unittest.skipUnless(shutil.which('gcloud'), 'gcloud is required for actual upload-policy verification')
    def test_actual_upload_excludes_environment_credentials_tools_and_non_python_assets(self):
        self.make_source(self.directory)
        forbidden = (
            '.env', 'token.json', 'credentials.json', 'key.json',
            '.venv/lib/package.py', 'venv/lib/package.py', 'tests/test_example.py',
            'scripts/deploy.py', 'README.md', 'src/config/credentials.json',
            'src/config/.env', 'src/config/private.txt', 'src/__pycache__/factory.pyc',
        )
        for name in forbidden:
            path = self.directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic excluded content')
        output = release.gcloud_text('meta', 'list-files-for-upload', str(self.directory))
        self.assertEqual(set(output.splitlines()), {
            'main.py', 'requirements.txt', 'src/factory.py', 'src/config/module.py',
        })

    def test_snapshot_contains_keys_and_fingerprints_not_credentials(self):
        config = main_config()
        snapshot = release.function_snapshot(config)
        text = json.dumps(snapshot)
        self.assertNotIn('synthetic-secret-not-for-snapshot', text)
        self.assertNotIn('synthetic-build-secret', text)
        self.assertEqual(snapshot['environmentVariablesKeys'], ['REFRESH_TOKEN'])
        config['environmentVariables']['REFRESH_TOKEN'] = 'changed'
        self.assertNotEqual(snapshot, release.function_snapshot(config))

    def test_commands_update_only_main_without_replacing_environment(self):
        config = main_config()
        command = release.deployment_command(config, 'test-project', 'us-central1', self.directory, True)
        rollback = release.deployment_command(config, 'test-project', 'us-central1', self.directory, False)
        self.assertEqual(command[:4], ['gcloud', 'functions', 'deploy', release.FUNCTION])
        self.assertIn('--retry', command)
        self.assertIn('--no-retry', rollback)
        self.assertIn('--max-instances=1', command)
        self.assertIn('--memory=512MB', command)
        self.assertIn('--runtime=python311', command)
        self.assertIn('--trigger-topic=gmail-notifications', command)
        self.assertFalse(any('env-vars' in part or 'build-env' in part for part in command))
        self.assertNotIn('synthetic-secret-not-for-snapshot', ' '.join(command))
        self.assertIn('--format=' + release.DEPLOY_FORMAT, command)

    @unittest.skipUnless(shutil.which('gcloud'), 'gcloud is required for help-only CLI validation')
    def test_actual_deploy_and_rollback_arguments_are_accepted_without_execution(self):
        for retry in (True, False):
            with self.subTest(retry=retry):
                command = release.deployment_command(
                    main_config(), 'test-project', 'us-central1', self.directory, retry,
                )
                result = subprocess.run(
                    [*command, '--help'], capture_output=True, text=True, check=False,
                    env={**release.os.environ, 'PAGER': 'cat', 'CLOUDSDK_PAGER': 'cat'},
                )
                self.assertEqual(result.returncode, 0, result.stderr[:1000])

    def test_extracts_only_runtime_source_and_rejects_duplicate_members(self):
        archive = self.directory / 'source.zip'
        self.make_archive(archive, {
            '.env': 'synthetic secret', '.venv/lib/package.py': '',
            'src/__pycache__/factory.pyc': '',
        })
        manifest = release.extract_rollback_source(
            archive, self.directory / 'rollback', ROOT / '.gcloudignore',
        )
        self.assertEqual(set(manifest), {
            '.gcloudignore', 'main.py', 'requirements.txt', 'src/factory.py',
        })
        with zipfile.ZipFile(archive, 'a') as source:
            source.writestr('src/./factory.py', 'duplicate normalized path')
        with self.assertRaises(FileExistsError):
            release.extract_rollback_source(
                archive, self.directory / 'duplicate', ROOT / '.gcloudignore',
            )

    def test_rejects_archive_path_traversal_and_symlinks(self):
        for index, name in enumerate(('../escape.py', '/main.py', 'src/../escape.py', 'src\\file.py')):
            with self.subTest(name=name):
                archive = self.directory / f'unsafe-{index}.zip'
                self.make_archive(archive, {name: ''})
                with self.assertRaisesRegex(ValueError, 'Unsafe path'):
                    release.extract_rollback_source(
                        archive, self.directory / f'unsafe-{index}', ROOT / '.gcloudignore',
                    )
        archive = self.directory / 'symlink.zip'
        self.make_archive(archive)
        with zipfile.ZipFile(archive, 'a') as source:
            entry = zipfile.ZipInfo('src/link.py')
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            source.writestr(entry, '/outside')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            release.extract_rollback_source(
                archive, self.directory / 'symlink', ROOT / '.gcloudignore',
            )

    def test_rejects_missing_required_runtime_files(self):
        with self.assertRaisesRegex(ValueError, 'required'):
            release.require_source_files({'main.py', 'requirements.txt'})

    def test_rejects_output_inside_repository_before_any_gcloud_call(self):
        with patch.object(release, 'gcloud_json') as read:
            with self.assertRaisesRegex(ValueError, 'outside'):
                release.prepare(self.directory, self.directory / 'output', 'test-project', 'us-central1')
            read.assert_not_called()

    def test_rejects_unexpected_live_configuration(self):
        for field, value in (
            ('runtime', 'python314'), ('maxInstances', 2),
            ('availableMemoryMb', 256), ('status', 'DEPLOY_IN_PROGRESS'),
        ):
            with self.subTest(field=field):
                config = main_config()
                config[field] = value
                with self.assertRaisesRegex(ValueError, 'baseline'):
                    release.validate_main(config, 'test-project', 'us-central1')

    def test_preparation_saves_private_artifacts_but_never_executes_deploy(self):
        self.make_source(self.directory / 'repo')
        calls = []

        def read(*args):
            calls.append(args)
            if args[:2] == ('functions', 'describe'):
                return main_config()
            return {}

        with (
            patch.object(release, 'gcloud_json', side_effect=read),
            patch.object(release, 'gcloud_text', return_value='main.py\nrequirements.txt\nsrc/factory.py\n'),
            patch.object(release, 'download_source', side_effect=lambda config, path: self.make_archive(path)),
        ):
            plan = release.prepare(
                self.directory / 'repo', self.directory / 'private-release',
                'test-project', 'us-central1',
            )
        self.assertEqual(plan['expectedVersion'], '5')
        self.assertFalse(any('deploy' in call for call in calls))
        directory = self.directory / 'private-release'
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for name in ('baseline.json', 'deployment-plan.json', 'deployed-source.zip'):
            self.assertEqual(stat.S_IMODE((directory / name).stat().st_mode), 0o600)
        self.assertNotIn('synthetic-secret-not-for-snapshot', (directory / 'baseline.json').read_text())
        self.assertEqual(plan['candidateManifest'], release.source_manifest(directory / 'candidate-source'))

    def test_live_drift_prevents_issuing_deployment_plan(self):
        self.make_source(self.directory / 'repo')
        changed = copy.deepcopy(main_config())
        changed['versionId'] = '6'
        with (
            patch.object(release, 'gcloud_json', side_effect=[main_config(), main_config(), {}, {}, changed]),
            patch.object(release, 'gcloud_text', return_value='main.py\nrequirements.txt\nsrc/factory.py\n'),
            patch.object(release, 'download_source', side_effect=lambda config, path: self.make_archive(path)),
        ):
            with self.assertRaisesRegex(RuntimeError, 'changed during preparation'):
                release.prepare(
                    self.directory / 'repo', self.directory / 'drift-release',
                    'test-project', 'us-central1',
                )
        self.assertFalse((self.directory / 'drift-release' / 'deployment-plan.json').exists())

    def test_gcloud_failure_does_not_expose_stdout_or_stderr(self):
        result = subprocess.CompletedProcess([], 1, 'private stdout', 'private stderr')
        with patch.object(release.subprocess, 'run', return_value=result):
            with self.assertRaises(RuntimeError) as raised:
                release.gcloud_text('functions', 'describe', release.FUNCTION)
        self.assertNotIn('private', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
