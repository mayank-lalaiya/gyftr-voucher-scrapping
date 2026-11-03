"""Prepare a private main-function release and rebuild-based rollback; never deploy."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import stat
import subprocess
import urllib.error
import urllib.request
import zipfile


FUNCTION = 'gyftr-automation-v1'
WATCH_FUNCTION = 'gyftr-renew-watch'
SCHEDULER_JOB = 'gyftr-renew-gmail-watch'
PUBLIC_FUNCTION_FIELDS = (
    'name', 'status', 'versionId', 'updateTime', 'runtime', 'entryPoint',
    'availableMemoryMb', 'timeout', 'maxInstances', 'minInstances',
    'serviceAccountEmail', 'eventTrigger', 'httpsTrigger', 'ingressSettings',
    'vpcConnector', 'vpcConnectorEgressSettings', 'network', 'kmsKeyName',
    'buildWorkerPool', 'buildServiceAccount',
)
DEPLOY_FORMAT = 'json(name,status,versionId,updateTime,runtime,availableMemoryMb,maxInstances,eventTrigger)'


def gcloud_text(*args: str) -> str:
    result = subprocess.run(['gcloud', *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"gcloud read failed ({' '.join(args[:2])}, exit {result.returncode})")
    return result.stdout


def gcloud_json(*args: str) -> dict:
    result = json.loads(gcloud_text(*args, '--format=json'))
    if not isinstance(result, dict):
        raise ValueError('Expected a JSON object from gcloud')
    return result


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def function_snapshot(config: dict) -> dict:
    snapshot = {key: config[key] for key in PUBLIC_FUNCTION_FIELDS if key in config}
    for key in ('environmentVariables', 'buildEnvironmentVariables'):
        values = config.get(key, {})
        snapshot[f'{key}Keys'] = sorted(values)
        snapshot[f'{key}Sha256'] = digest(values)
    return snapshot


def private_json(path: Path, value: object) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write('\n')


def runtime_file(name: str) -> bool:
    path = PurePosixPath(name)
    return name in {'main.py', 'requirements.txt'} or (
        name.startswith('src/') and path.suffix == '.py' and '__pycache__' not in path.parts
    )


def require_source_files(names: set[str]) -> None:
    if not {'main.py', 'requirements.txt', 'src/factory.py'}.issubset(names):
        raise ValueError('Source is missing required runtime files')


def stage_source(root: Path, destination: Path) -> dict[str, str]:
    names = gcloud_text('meta', 'list-files-for-upload', str(root)).splitlines()
    require_source_files(set(names))
    if any(not runtime_file(name) for name in names):
        raise ValueError('Upload preview contains files outside the runtime allowlist')
    destination.mkdir()
    for name in names:
        source = root / name
        if source.is_symlink() or not source.resolve().is_relative_to(root.resolve()):
            raise ValueError('Runtime source must not contain external paths or symlinks')
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    shutil.copyfile(root / '.gcloudignore', destination / '.gcloudignore')
    return source_manifest(destination)


def source_manifest(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob('*')) if path.is_file()
    }


def download_source(config: dict, destination: Path) -> None:
    token = gcloud_text('auth', 'print-access-token').strip()
    request = urllib.request.Request(
        f"https://cloudfunctions.googleapis.com/v1/{config['name']}:generateDownloadUrl",
        data=json.dumps({'versionId': config['versionId']}).encode(),
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            url = json.load(response)['downloadUrl']
        with urllib.request.urlopen(url, timeout=120) as response:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                shutil.copyfileobj(response, stream)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'Deployed source download failed (HTTP {error.code})') from None
    except urllib.error.URLError:
        raise RuntimeError('Deployed source download failed; check connectivity and authentication') from None


def extract_rollback_source(archive_path: Path, destination: Path, ignore_file: Path) -> dict[str, str]:
    destination.mkdir()
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise ValueError('Deployed source archive failed its integrity check')
        names = set()
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in entry.filename:
                raise ValueError('Unsafe path in deployed source archive')
            if entry.is_dir() or not runtime_file(entry.filename):
                continue
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError('Runtime source archive contains a symlink')
            target = destination / entry.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output)
            names.add(entry.filename)
        require_source_files(names)
    shutil.copyfile(ignore_file, destination / '.gcloudignore')
    return source_manifest(destination)


def validate_main(config: dict, project: str, region: str) -> None:
    expected = {
        'name': f'projects/{project}/locations/{region}/functions/{FUNCTION}',
        'status': 'ACTIVE', 'runtime': 'python311',
        'entryPoint': 'process_pubsub_message_gen1', 'availableMemoryMb': 512,
        'timeout': '60s', 'maxInstances': 1,
    }
    if any(config.get(key) != value for key, value in expected.items()):
        raise ValueError('Main function differs from the expected release baseline; inspect before proceeding')
    trigger = config.get('eventTrigger', {})
    if (
        trigger.get('eventType') != 'google.pubsub.topic.publish'
        or not trigger.get('resource', '').startswith(f'projects/{project}/topics/')
        or not str(config.get('versionId', '')).isdecimal()
    ):
        raise ValueError('Main function has an unexpected trigger or version')


def deployment_command(config: dict, project: str, region: str, source: Path, retry: bool) -> list[str]:
    command = [
        'gcloud', 'functions', 'deploy', FUNCTION, '--no-gen2',
        f'--project={project}', f'--region={region}', f'--source={source}',
        '--ignore-file=.gcloudignore', f"--runtime={config['runtime']}",
        f"--entry-point={config['entryPoint']}",
        f"--trigger-topic={config['eventTrigger']['resource'].rsplit('/', 1)[-1]}",
        f"--memory={config['availableMemoryMb']}MB", f"--timeout={config['timeout']}",
        f"--max-instances={config['maxInstances']}",
        '--retry' if retry else '--no-retry', '--quiet', f'--format={DEPLOY_FORMAT}',
    ]
    if config.get('serviceAccountEmail'):
        command.append(f"--service-account={config['serviceAccountEmail']}")
    if config.get('ingressSettings'):
        ingress = {
            'ALLOW_ALL': 'all', 'ALLOW_INTERNAL_ONLY': 'internal-only',
            'ALLOW_INTERNAL_AND_GCLB': 'internal-and-gclb',
        }[config['ingressSettings']]
        command.append(f'--ingress-settings={ingress}')
    return command


def prepare(root: Path, output: Path, project: str, region: str) -> dict:
    root, output = root.resolve(), output.resolve()
    if output.is_relative_to(root):
        raise ValueError('Release and rollback output must be outside the repository')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    scope = [f'--project={project}', f'--region={region}']
    config = gcloud_json('functions', 'describe', FUNCTION, *scope)
    validate_main(config, project, region)
    before = function_snapshot(config)
    watch = gcloud_json('functions', 'describe', WATCH_FUNCTION, *scope)
    job = gcloud_json(
        'scheduler', 'jobs', 'describe', SCHEDULER_JOB,
        f'--project={project}', f'--location={region}',
    )
    repository = gcloud_json(
        'artifacts', 'repositories', 'describe', 'gcf-artifacts',
        f'--project={project}', f'--location={region}',
    )
    private_json(output / 'baseline.json', {
        'main': before, 'watch': function_snapshot(watch),
        'scheduler': {key: job[key] for key in ('name', 'state', 'schedule', 'timeZone', 'retryConfig', 'attemptDeadline') if key in job},
        'schedulerTarget': {key: job.get('httpTarget', {}).get(key) for key in ('uri', 'httpMethod', 'oidcToken')},
        'artifactCleanup': {
            'name': repository.get('name'), 'policies': repository.get('cleanupPolicies'),
            'dryRun': repository.get('cleanupPolicyDryRun', False),
        },
    })
    archive_path = output / 'deployed-source.zip'
    download_source(config, archive_path)
    archive_path.chmod(0o600)
    rollback_manifest = extract_rollback_source(archive_path, output / 'rollback-source', root / '.gcloudignore')
    candidate_manifest = stage_source(root, output / 'candidate-source')
    after = function_snapshot(gcloud_json('functions', 'describe', FUNCTION, *scope))
    if before != after:
        raise RuntimeError('Main function changed during preparation; no deployment plan was issued')
    plan = {
        'expectedVersion': config['versionId'],
        'sourceArchiveSha256': hashlib.sha256(archive_path.read_bytes()).hexdigest(),
        'candidateManifest': candidate_manifest, 'rollbackManifest': rollback_manifest,
        'deployCommand': deployment_command(config, project, region, output / 'candidate-source', True),
        'rollbackCommand': deployment_command(
            config, project, region, output / 'rollback-source',
            'retry' in config['eventTrigger'].get('failurePolicy', {}),
        ),
        'notes': [
            'No deployment was executed. Recheck the live version and source manifests before deploying.',
            'Commands omit environment-variable flags to retain existing credentials and configuration.',
            'Rollback rebuilds source and may resolve newer dependencies; it is not an exact image restore.',
            'Rollback does not restore sheet rows or checkpoints. Do not clear pending work automatically.',
            'Watch renewal, Scheduler and artifact cleanup must remain unchanged.',
            'First upgrade must quiesce/drain legacy writers before enabling the guarded version, then reconcile Gmail history.',
            'Legacy rollback cannot read chunked pending queues; finish or explicitly migrate pending work before rollback.',
        ],
    }
    private_json(output / 'deployment-plan.json', plan)
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--region', default='us-central1')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        plan = prepare(Path(__file__).resolve().parents[1], args.output, args.project, args.region)
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as error:
        parser.exit(1, f'Preparation failed: {error}\n')
    print(f"Prepared private release and rollback for main version {plan['expectedVersion']}.")
    print(f"Candidate upload: {len(plan['candidateManifest']) - 1} runtime files.")
    print('Not executed:', shlex.join(plan['deployCommand']))


if __name__ == '__main__':
    main()
