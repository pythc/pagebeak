import subprocess
from types import SimpleNamespace

import httpx
import pytest
from akb import native, preview
from fastapi import HTTPException

PDF = b'%PDF-1.7\nfixture-read-only-preview'
BODY = {'uri': 'cloudreve://my/material.docx', 'file_id': 'owned-file'}
HEADERS = {'X-Cloudreve-Token': 'native-current-user-token', 'X-AKB-File-User': 'native-owner'}


def test_preview_uses_current_drive_identity_and_preserves_origin_guards(app, monkeypatch):
    application, client = app
    monkeypatch.setattr(native, 'verify_identity', lambda *_: {'id': 'native-owner'})
    calls = []
    monkeypatch.setattr(preview, 'render', lambda settings, token, body: calls.append((token, body.file_id)) or PDF)
    assert client.post('/v1/native/preview', json=BODY).status_code == 422
    assert client.post('/v1/native/preview', headers={**HEADERS, 'X-AKB-File-User': 'other'}, json=BODY).status_code == 409
    assert client.post('/v1/native/preview', headers={**HEADERS, 'Origin': 'https://other.test'}, json=BODY).status_code == 403
    response = client.post('/v1/native/preview', headers={**HEADERS, 'Origin': application.state.settings.native_origin}, json=BODY)
    assert response.status_code == 200 and response.content == PDF
    assert response.headers['content-type'] == 'application/pdf' and response.headers['cache-control'] == 'no-store'
    assert 'set-cookie' not in response.headers and len(calls) == 1


def fake_files(monkeypatch, *, url='http://127.0.0.1:5878/signed-original', extension='docx',
               declared_size=None, replace_on_read=0, content=b'office-original'):
    info = {'id': 'owned-file', 'type': 0, 'name': 'material.' + extension, 'primary_entity': 'version-1',
            'size': len(content) if declared_size is None else declared_size, 'updated_at': 'first', 'path': BODY['uri']}
    observations = {'info_reads': 0, 'streams': 0, 'converted': 0}

    class Client:
        def __init__(self, **kwargs):
            assert kwargs['headers']['Authorization'] == 'Bearer native-current-user-token'
            assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, method, path, **kwargs):
            if path.endswith('file/info'):
                observations['info_reads'] += 1
                data = {**info}
                if observations['info_reads'] == replace_on_read:
                    data['primary_entity'] = 'replaced-version'
            else:
                assert path.endswith('file/url') and method == 'POST'
                data = {'urls': [{'url': url}]}
            return httpx.Response(200, json={'code': 0, 'data': data}, request=httpx.Request(method, 'http://fixture' + path))

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def raise_for_status(self):
            pass

        def iter_bytes(self, _):
            yield content

    def stream(method, signed_url, **kwargs):
        assert method == 'GET' and signed_url == url
        assert 'headers' not in kwargs and kwargs['follow_redirects'] is False
        observations['streams'] += 1
        return Stream()

    def convert(directory, extension):
        assert (directory / ('input/document' + extension)).read_bytes() == content
        assert (directory / ('input/document' + extension)).stat().st_mode & 0o777 == 0o444
        observations['converted'] += 1
        return PDF

    monkeypatch.setattr(preview.httpx, 'Client', Client)
    monkeypatch.setattr(preview.httpx, 'stream', stream)
    monkeypatch.setattr(preview, 'convert_document', convert)
    return observations


def test_preview_rechecks_version_and_removes_original_after_success(app, monkeypatch):
    application, _ = app
    seen = fake_files(monkeypatch)
    assert preview.render(application.state.settings, HEADERS['X-Cloudreve-Token'], preview.PreviewInput(**BODY)) == PDF
    assert seen == {'info_reads': 3, 'streams': 1, 'converted': 1}
    assert not list((application.state.settings.object_root().parent / 'preview-tmp').glob('office-*'))


@pytest.mark.parametrize('options,version,status,converted', [
    ({'url': 'https://untrusted.test/private'}, None, 422, 0),
    ({'declared_size': preview.INPUT_LIMIT + 1}, None, 413, 0),
    ({'declared_size': 1}, None, 413, 0),
    ({'extension': 'exe'}, None, 415, 0),
    ({}, 'old-version', 409, 0),
    ({'replace_on_read': 2}, None, 409, 0),
    ({'replace_on_read': 3}, None, 409, 1),
])
def test_preview_rejects_unsafe_or_changed_originals(app, monkeypatch, options, version, status, converted):
    application, _ = app
    seen = fake_files(monkeypatch, **options)
    with pytest.raises(HTTPException) as error:
        preview.render(application.state.settings, HEADERS['X-Cloudreve-Token'], preview.PreviewInput(**BODY, version=version))
    assert error.value.status_code == status and seen['converted'] == converted
    assert not list((application.state.settings.object_root().parent / 'preview-tmp').glob('office-*'))
    if 'url' in options:
        assert seen['streams'] == 0


def test_preview_rejects_other_spaces_before_contacting_file_service(app):
    with pytest.raises(HTTPException) as error:
        preview.render(app[0].state.settings, 'no-call', preview.PreviewInput(uri='cloudreve://other/file.docx', file_id='id'))
    assert error.value.status_code == 422


def conversion_input(tmp_path):
    source = tmp_path / 'input'
    source.mkdir()
    (source / 'document.docx').write_bytes(b'not-executed')


def test_conversion_container_is_isolated_and_cleanup_always_runs(tmp_path, monkeypatch):
    conversion_input(tmp_path)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == 'run':
            assert '--network=none' in command and '--read-only' in command and '--cap-drop=ALL' in command
            assert '--security-opt=no-new-privileges' in command and '--pull=never' in command
            assert '--memory=768m' in command and '--pids-limit=128' in command
            assert next(v for v in command if v.startswith('--user=')).split('=')[1].split(':')[0] != '0'
            mounts = [command[i+1] for i, v in enumerate(command) if v == '--mount']
            assert len(mounts) == 3 and all(str(tmp_path) in v for v in mounts)
            assert any(v.endswith('/input,readonly') for v in mounts)
            assert 'MacroSecurityLevel' in (tmp_path/'profile/user/registrymodifications.xcu').read_text()
            (tmp_path / 'output/document.pdf').write_bytes(PDF)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(preview.office_conversion.subprocess, 'run', run)
    assert preview.convert_document(tmp_path, '.docx') == PDF
    assert commands[-1][1:3] == ['rm', '-f']


def test_conversion_timeout_kills_container(tmp_path, monkeypatch):
    conversion_input(tmp_path)
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == 'run':
            raise subprocess.TimeoutExpired(command, 90)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(preview.office_conversion.subprocess, 'run', run)
    with pytest.raises(HTTPException) as error:
        preview.convert_document(tmp_path, '.docx')
    assert error.value.status_code == 504 and commands[-1][1:3] == ['rm', '-f']
