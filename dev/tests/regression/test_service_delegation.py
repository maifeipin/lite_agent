"""Migration contracts for database selection and worker generations."""
from unittest.mock import patch


def test_sentinel_default_keeps_all_matching_historical_findings(tmp_path):
    import json
    from datetime import datetime
    from lite_agent.skills import ops_sentinel
    from lite_agent.features.sys_ops import security_service
    findings = tmp_path / 'findings.jsonl'
    records = [dict(timestamp=datetime.now().isoformat(), severity='medium',
                    module='review', message=f'finding-{i}') for i in range(21)]
    findings.write_text(''.join(json.dumps(r) + '\n' for r in records))
    with patch.object(security_service, 'FINDINGS_FILE', str(findings)):
        report = ops_sentinel.sentinel_findings()
    assert '21 条告警' in report
    assert all(f'finding-{i}\n' in report for i in range(21))

def test_socks_database_environment_contract(tmp_path,monkeypatch):
    from lite_agent.features.network_ops import socks5_service as s
    expected=str(tmp_path/'legacy.sqlite')
    (tmp_path/'legacy.sqlite').touch()
    monkeypatch.setenv('SOCKS5_DB_PATH',expected)
    with patch.object(s,'load_config',return_value={}):
        assert s.get_socks5_db_path()==expected

def test_socks_preserves_local_port_exclusions():
    from lite_agent.features.network_ops import socks5_service as s
    assert {1080,1081,1082,7890,7891,8080,8887,8888,18988} <= set(s.LOCAL_PORT_EXCLUDES)

def test_stale_shared_owner_cannot_decrement_new_generation():
    from lite_agent.features.network_ops import socks5_service as s
    s.stop_failover_worker()
    def idle(stop_event,**kwargs):
        stop_event.wait()
    with patch.object(s,'failover_worker_loop',side_effect=idle):
        try:
            old1=s.start_failover_worker()
            old2=s.start_failover_worker()
            s.stop_failover_worker()
            new1=s.start_failover_worker()
            new2=s.start_failover_worker()
            worker=s._failover_worker_thread
            old2()
            new1()
            assert worker.is_alive(), 'stale shared-owner handle consumed a new owner lease'
            new2()
        finally:
            s.stop_failover_worker()


def test_real_backup_archive_contains_data_not_only_symlinks(tmp_path):
    import shutil, subprocess, zipfile, pytest
    if not shutil.which('zip'):
        pytest.skip("zip command not available in this environment (e.g. minimal docker image)")
    from contextlib import ExitStack
    from lite_agent.features.sys_ops import backup_service as b
    root = tmp_path / 'app'; root.mkdir()
    (root / 'data').mkdir()
    (root / 'data' / 'review.txt').write_text('review-backup-content')
    billing = tmp_path / 'parser'; billing.mkdir()
    real_run = subprocess.run
    def isolated_run(args, **kwargs):
        if args[0] == 'zip':
            return real_run(args, **kwargs)
        raise RuntimeError('external command disabled for review')
    with ExitStack() as stack:
        stack.enter_context(patch.object(b, '_BASE_DIR', str(root)))
        stack.enter_context(patch.object(b, '_BILLING_DIR', str(billing)))
        stack.enter_context(patch.object(b.subprocess, 'run', side_effect=isolated_run))
        for name in ('_backup_mongodb', '_backup_vaultwarden', '_create_meili_dump', '_backup_hedgedoc', '_backup_halo'):
            stack.enter_context(patch.object(b, name, return_value=None))
        b.do_backup()
    archives = list((root / 'backup').glob('data_*.zip'))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as archive:
        assert 'data/review.txt' in archive.namelist(), archive.namelist()
        assert archive.read('data/review.txt') == b'review-backup-content'


def test_candidate_probe_preserves_original_resolver_mapping(tmp_path):
    from unittest.mock import Mock
    from lite_agent.features.network_ops import socks5_service as s
    proxy = {
        'id': 2, 'is_active': 0, 'servername': 'review', 'host': '192.0.2.20',
        'runcmd': 'naive --proxy=https://user:pass@example.invalid --host-resolver-rules="MAP example.invalid 192.0.2.20"'
    }
    calls = []
    process = Mock()
    def capture(args, **kwargs):
        calls.append(args)
        return process
    sock = Mock(); sock.connect_ex.return_value = 0
    with patch.object(s, 'get_socks5_proxy_by_id', return_value=proxy), \
         patch.object(s, 'get_current_active_proxy', return_value=None), \
         patch.object(s, '_get_free_local_port', return_value=29877), \
         patch.object(s.subprocess, 'Popen', side_effect=capture), \
         patch.object(s.time, 'sleep'), \
         patch.object(s.socket, 'socket') as socket_factory, \
         patch.object(s, 'test_socks5_outbound_http', return_value={'success': True}):
        socket_factory.return_value.__enter__.return_value = sock
        result = s.test_socks5_proxy_outbound(2)
    assert result['success']
    assert any('host-resolver-rules' in str(arg) for arg in calls[0]) or '--config' in calls[0], calls


def test_blog_export_rejects_previously_forbidden_directory_before_side_effects():
    from lite_agent.features.content_ops import content_service as s
    with patch.object(s.os, 'makedirs') as mkdir, patch.object(s, 'halo_request', return_value={'status': 200, 'data': {'content': []}}) as request:
        result = s.blog_export_articles('/etc/review-export')
    assert '拒绝' in result, result
    mkdir.assert_not_called()
    request.assert_not_called()


def test_sentinel_preserves_historical_filter_arguments():
    from lite_agent.skills import ops_sentinel
    from lite_agent.features.sys_ops import security_service
    with patch.object(security_service.os.path, 'exists', return_value=False):
        result = ops_sentinel.sentinel_findings(hours=24, min_severity='medium')
    assert isinstance(result, str)


def test_web_clip_preserves_historical_keyword_arguments():
    from lite_agent.skills import ops_web_clipper
    from lite_agent.features.content_ops import content_service
    with patch.object(content_service.urllib.request, 'urlopen', side_effect=AssertionError('network blocked')):
        result = ops_web_clipper.web_clip(urls='https://example.invalid', screenshot=True, force_hedgedoc=False)
    assert isinstance(result, str)


def test_journal_preserves_historical_service_and_keyword_arguments():
    from lite_agent.skills import ops_logs
    from lite_agent.features.sys_ops import health_service
    with patch.object(health_service.subprocess, 'run') as mock_run:
        mock_run.return_value.stdout = 'dummy log'
        mock_run.return_value.returncode = 0
        res = ops_logs.ops_read_journal(service_name='lite-agent', keyword='error', lines=20)
        assert res == 'dummy log'
        args = mock_run.call_args[0][0]
        assert '-u' in args and 'lite-agent' in args
        assert '--grep' in args and 'error' in args


def test_clipper_preserves_adapter_endpoint_and_response_contract():
    import json
    from unittest.mock import Mock
    from lite_agent.features.content_ops import content_service as c
    response = Mock()
    response.read.return_value = json.dumps({'Success': True, 'Title': 'review', 'Markdown': 'review body', 'ImageCount': 0, 'Url': 'https://example.invalid'}).encode()
    with patch.object(c.urllib.request, 'urlopen') as open_url, patch.object(c, 'load_config', return_value={'rssadapter': {'url': 'http://127.0.0.1:5216'}}):
        open_url.return_value.__enter__.return_value = response
        result = c.web_clip('https://example.invalid', auto_upload=False)
    assert open_url.call_args.args[0].full_url.endswith('/api/Url2Md')
    assert 'review body' in result, result


def test_clipper_batch_endpoint_and_smart_delivery():
    import json
    from unittest.mock import Mock
    from lite_agent.features.content_ops import content_service as c
    response = Mock()
    response.read.return_value = json.dumps([
        {'Success': True, 'Title': 'page1', 'Markdown': 'body1', 'ImageCount': 0, 'Url': 'https://example.invalid/1'},
        {'Success': True, 'Title': 'page2', 'Markdown': 'body2', 'ImageCount': 0, 'Url': 'https://example.invalid/2'},
    ]).encode()
    with patch.object(c.urllib.request, 'urlopen') as open_url, patch.object(c, 'load_config', return_value={'rssadapter': {'url': 'http://127.0.0.1:5216'}}):
        open_url.return_value.__enter__.return_value = response
        result = c.web_clip('https://example.invalid/1, https://example.invalid/2', auto_upload=False)
    assert open_url.call_args.args[0].full_url.endswith('/api/Url2Md/batch')
    assert 'page1' in result and 'page2' in result, result
