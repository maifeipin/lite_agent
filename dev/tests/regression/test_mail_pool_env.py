"""Offline regressions for the mail LLM endpoint pool handed to the parser subprocess.

Two facts drive these cases:
1. Gemini is not OpenAI-compatible: the pool must not fabricate the
   .../v1beta/openai compat base_url for a gemini_native driver.
2. The parser's egress must go through the configured proxy; dropping it makes
   the gemini endpoint unreachable (vps1 cannot reach Google directly).
"""
import json

from lite_agent.features.mail_ops.reader_service import build_llm_pool_env


def _cfg():
    return {
        'llm': {
            'mail_pool': ['flash', 'gemini-flash'],
            'models': {
                'flash': {
                    'driver': 'deepseek',
                    'base_url': 'https://api.deepseek.com/v1',
                    'api_key': 'sk-abc',
                    'model': 'deepseek-v4-flash',
                },
                'gemini-flash': {
                    'driver': 'gemini_native',
                    'api_key': 'gk-abc',
                    'model': 'gemini-2.5-flash',
                    'proxy': 'socks5://127.0.0.1:18988',
                },
            },
        }
    }


def test_proxy_is_propagated_to_every_endpoint():
    pool = json.loads(build_llm_pool_env(_cfg())['LLM_POOL'])
    gemini = [e for e in pool if e['provider'] == 'gemini']
    assert gemini and gemini[0]['proxy'] == 'socks5://127.0.0.1:18988'


def test_gemini_base_url_is_not_faked_as_openai_compat():
    pool = json.loads(build_llm_pool_env(_cfg())['LLM_POOL'])
    gemini = [e for e in pool if e['provider'] == 'gemini'][0]
    assert gemini['base_url'] == ''
    assert 'openai' not in gemini['base_url']


def test_openai_endpoint_keeps_its_base_url_and_provider():
    pool = json.loads(build_llm_pool_env(_cfg())['LLM_POOL'])
    flash = [e for e in pool if e['provider'] == 'openai'][0]
    assert flash['base_url'] == 'https://api.deepseek.com/v1'
    assert flash['model'] == 'deepseek-v4-flash'
    assert flash['proxy'] == ''


def test_no_mail_pool_yields_no_env():
    cfg = _cfg()
    cfg['llm'].pop('mail_pool')
    assert build_llm_pool_env(cfg) == {}
