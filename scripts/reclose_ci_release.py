"""Focal SSV image-only CI release. Registry credentials never touch disk."""
import argparse
import base64
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import threading
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
CRM = ROOT.parent / 'global66-crm-b2c/global66-crm-b2c'
spec = importlib.util.spec_from_file_location('existing_release', CRM / 'scripts/ssv-postqa-v2-ci.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
op = release.op
op.REPORT = ROOT / 'docs/reclose-ci-release-20260925.json'
ALLOWED = {
    'app/services/momento_3_sync.py', 'app/services/idempotency_service.py',
    'app/services/s3_service.py', 'app/utils/pdf_generator.py',
    'tests/test_reclose_pipeline.py', 'tests/test_postqa_v2.py',
    'tests/test_momento_3.py', 'tests/test_momento_3_edge_cases.py',
    'scripts/postqa_v2_offline_tests.py', 'scripts/reclose_ci_release.py',
    'docs/reclose-pipeline.md',
}


def engine_request(docker, environment, method, path, headers=None):
    """Use Docker's local stdio transport; auth travels over stdin, not argv/logs."""
    lines = [f'{method} {path} HTTP/1.1', 'Host: localhost', 'Connection: close', 'Content-Length: 0']
    lines += [f'{k}: {v}' for k, v in (headers or {}).items()]
    request = ('\r\n'.join(lines) + '\r\n\r\n').encode()
    with subprocess.Popen(docker + ['system', 'dial-stdio'], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment) as process:
        timeout = threading.Timer(900, process.kill)
        timeout.start()
        try:
            # Closing stdin before the response also closes Docker's transport.
            process.stdin.write(request)
            process.stdin.flush()
            class Socket:
                def makefile(self, *args, **kwargs):
                    return process.stdout
            response = http.client.HTTPResponse(Socket())
            response.begin()
            assert response.status == 200, f'Docker engine HTTP {response.status}'
            return response.read()
        finally:
            timeout.cancel()
            process.stdin.close()
            process.wait(timeout=15)


def build():
    report = json.loads(op.REPORT.read_text())
    baseline = report['repos']['ssv']
    head = op.git(ROOT, 'rev-parse', 'HEAD')
    assert op.git(ROOT, 'branch', '--show-current') == baseline['branch']
    assert op.git(ROOT, 'ls-remote', 'origin', 'refs/heads/' + baseline['branch']).split()[0] == head
    changed = set(op.git(ROOT, 'diff', '--name-only', baseline['baseline_head'], head).splitlines())
    assert changed and changed <= ALLOWED, 'Commit outside focal scope'
    assert op.git(CRM, 'rev-parse', 'HEAD') == report['repos']['crm']['baseline_head']
    _, ecr = op.clients()
    endpoint = subprocess.check_output(['docker', 'context', 'inspect', '--format', '{{.Endpoints.docker.Host}}'], text=True).strip()
    assert endpoint.startswith(('npipe://', 'unix://'))
    environment = {**os.environ, 'DOCKER_HOST': endpoint}
    environment.pop('DOCKER_CONTEXT', None)
    with tempfile.TemporaryDirectory(prefix='reclose-ci-build-') as directory:
        temporary = Path(directory).resolve()
        assert temporary.is_relative_to(Path(tempfile.gettempdir()).resolve())
        config = temporary / 'empty-docker-config'
        config.mkdir()  # No config.json, login or credentials on disk.
        docker = ['docker', '--config', str(config)]
        assert engine_request(docker, environment, 'GET', '/_ping') == b'OK'
        archive = temporary / 'source.tar'
        subprocess.run(['git', 'archive', '--format=tar', '--output=' + str(archive), head], cwd=ROOT, check=True)
        source = temporary / 'source'
        source.mkdir()
        with tarfile.open(archive) as package:
            assert not any(Path(m.name).name.startswith('.env') and not m.name.endswith('.example') for m in package.getmembers())
            package.extractall(source, filter='data')
        for key in ('ssv-api', 'ssv-worker'):
            repository, stage = op.TARGETS[key][3:]
            tag = f'ci-reclose-{key}-{head[:12]}'
            image = f'{op.REGISTRY}/{repository}:{tag}'
            subprocess.run(docker + ['build', '--platform', 'linux/amd64', '-f', 'infrastructure/Dockerfile',
                           '--target', stage, '--label', 'org.opencontainers.image.revision=' + head,
                           '-t', image, '.'], cwd=source, env=environment, check=True)
            auth = ecr.get_authorization_token()['authorizationData'][0]
            assert auth['proxyEndpoint'] == 'https://' + op.REGISTRY
            username, password = base64.b64decode(auth['authorizationToken']).decode().split(':', 1)
            registry_auth = base64.b64encode(json.dumps({'username': username, 'password': password,
                                               'serveraddress': op.REGISTRY}).encode()).decode()
            pushed = engine_request(docker, environment, 'POST',
                                    '/v1.51/images/' + quote(op.REGISTRY + '/' + repository, safe='') + '/push?tag=' + tag,
                                    {'X-Registry-Auth': registry_auth})
            for line in pushed.splitlines():
                event = json.loads(line)
                assert not event.get('error'), 'Registry push failed (details withheld)'
            digest = ecr.describe_images(repositoryName=repository, imageIds=[{'imageTag': tag}])['imageDetails'][0]['imageDigest']
            report.setdefault('images', {})[key] = f'{op.REGISTRY}/{repository}@{digest}'
            op.save(report)
            print('BUILD_PUSH_PASS', key, digest, flush=True)
    report['repos']['ssv']['commit'] = head
    report['ssv_build'] = 'PASS'
    op.save(report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['baseline', 'build', 'deploy', 'health'])
    phase = parser.parse_args().phase
    if phase == 'baseline':
        op.baseline()
    elif phase == 'build':
        build()
    elif phase == 'deploy':
        release.deploy('ssv')
    else:
        op.health()
