"""Exercise the migrated clipper through fake protocol ports and real SQLite."""
import json
from unittest.mock import Mock
import pytest


@pytest.fixture
def clipper(tmp_path, monkeypatch):
    from lite_agent.features.content_ops import content_service as c
    monkeypatch.setattr(c, '_CACHE_DB_PATH', str(tmp_path / 'clips.sqlite'))
    monkeypatch.setattr(c, '_cache_inited', False)
    monkeypatch.setattr(c, 'load_config', lambda: {
        'rssadapter': {'url': 'http://127.0.0.1:5216'},
        'hedgedoc': {'enabled': True}})
    upload = Mock(return_value='https://example.invalid/note')
    monkeypatch.setattr(c, 'upload_to_hedgedoc', upload)
    server = Mock(side_effect=AssertionError('unexpected HTTP request'))
    monkeypatch.setattr(c.urllib.request, 'urlopen', server)
    return c, server, upload


def response(body):
    result = Mock()
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    result.read.return_value = json.dumps(body).encode()
    return result


def test_short_uppercase_response_and_normalized_cache_reuse(clipper):
    c, server, upload = clipper
    url = 'https://example.invalid/article'
    server.side_effect = None
    server.return_value = response(dict(Success=True, Title='Review',
        Markdown='short review body', ImageCount=0, Url=url))
    first = c.web_clip(url + '?utm_source=first')
    second = c.web_clip(url + '?utm_source=second')
    assert 'short review body' in first and 'short review body' in second
    server.assert_called_once()
    req = server.call_args.args[0]
    assert req.full_url.endswith('/api/Url2Md')
    assert json.loads(req.data)['screenshot'] is False
    upload.assert_not_called()


def test_screenshot_bypasses_existing_cache_and_does_not_replace_it(clipper):
    c, server, upload = clipper
    url = 'https://example.invalid/screenshot'
    c._cache_put(url, dict(success=True, markdown='cached original', url=url))
    server.side_effect = None
    server.return_value = response(dict(Success=True, Markdown='fresh screenshot text', Url=url))
    assert 'fresh screenshot text' in c.web_clip(url, screenshot=True)
    server.assert_called_once()
    assert json.loads(server.call_args.args[0].data)['screenshot'] is True
    assert c._cache_get(url)['markdown'] == 'cached original'
    upload.assert_not_called()


@pytest.mark.parametrize('success,lifetime', [(True, 7 * 24 * 3600), (False, 300)])
def test_success_and_failure_cache_expire_at_legacy_lifetimes(clipper, monkeypatch, success, lifetime):
    c, _, _ = clipper
    now = [1000000.0]
    monkeypatch.setattr(c.time, 'time', lambda: now[0])
    url = 'https://example.invalid/ttl'
    c._cache_put(url, dict(success=success, markdown='body', error='review error'))
    now[0] += lifetime - 1
    assert c._cache_get(url)['success'] is success
    now[0] += 2
    assert c._cache_get(url) is None


def test_long_article_reuses_uploaded_link_without_refetch_or_reupload(clipper):
    c, server, upload = clipper
    url = 'https://example.invalid/long'
    server.side_effect = None
    server.return_value = response(dict(Success=True, Markdown='x' * 2601, Url=url))
    first = c.web_clip(url)
    second = c.web_clip(url)
    assert 'https://example.invalid/note' in first and 'https://example.invalid/note' in second
    server.assert_called_once()
    upload.assert_called_once()


@pytest.mark.parametrize('images,force', [(1, False), (0, True)])
def test_image_or_explicit_force_uploads_short_content(clipper, images, force):
    c, server, upload = clipper
    server.side_effect = None
    server.return_value = response(dict(success=True, markdown='short',
        imageCount=images, url='https://example.invalid/forced'))
    assert 'https://example.invalid/note' in c.web_clip(
        'https://example.invalid/forced', force_hedgedoc=force)
    upload.assert_called_once()


def test_upload_failure_returns_text_without_claiming_a_note(clipper):
    c, server, upload = clipper
    server.side_effect = None
    server.return_value = response(dict(success=True, markdown='x' * 2601,
        url='https://example.invalid/failure'))
    upload.side_effect = RuntimeError('fake upload failed')
    result = c.web_clip('https://example.invalid/failure')
    assert '上传失败已截断' in result
    assert '点击查看完整图文' not in result


def test_batch_uses_legacy_endpoint_and_delivers_each_result(clipper):
    c, server, upload = clipper
    server.side_effect = None
    server.return_value = response([
        dict(Success=True, Title='One', Markdown='first body'),
        dict(success=True, title='Two', markdown='second body')])
    result = c.web_clip('https://example.invalid/one,https://example.invalid/two')
    server.assert_called_once()
    req = server.call_args.args[0]
    assert req.full_url.endswith('/api/Url2Md/batch')
    assert json.loads(req.data) == [
        {'url': 'https://example.invalid/one', 'screenshot': False},
        {'url': 'https://example.invalid/two', 'screenshot': False}]
    assert 'first body' in result and 'second body' in result
    upload.assert_not_called()
