"""
记忆蒸馏技能 — 手动/定时触发

挂载方式:
  1. 手动: /distill (通过 agent 内置指令)
  2. 定时: crontab / systemd timer 每天凌晨 03:00 调用
  3. 动态: 未蒸馏消息 > 100 条自动触发

LLM 蒸馏:
  需要设置 LLM_API_KEY 和 LLM_BASE_URL 环境变量
  或通过 --no-llm 标志使用纯规则蒸馏
"""

import os
import sys
import json
import argparse
import urllib.request

# 自动注入项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_engine.engine import MemoryEngine


def get_llm_callback():
    """创建一个使用 DeepSeek / OpenAI API 的 LLM 回调"""
    api_key = os.environ.get('LLM_API_KEY', '')
    base_url = os.environ.get('LLM_BASE_URL', 'https://api.deepseek.com/v1')

    if not api_key:
        return None

    def call_llm(prompt: str) -> str:
        req_data = json.dumps({
            'model': 'deepseek-chat',
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': 1024,
            'temperature': 0.3,
        }).encode('utf-8')

        req = urllib.request.Request(
            f'{base_url}/chat/completions',
            data=req_data,
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {api_key}',
            }
        )

        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
            return result['choices'][0]['message']['content']

    return call_llm


def main():
    parser = argparse.ArgumentParser(description='记忆蒸馏器')
    parser.add_argument('--mode', choices=['daily', 'weekly', 'retry'], default='daily')
    parser.add_argument('--no-llm', action='store_true', help='使用纯规则蒸馏 (不调用 LLM)')
    parser.add_argument('--dry-run', action='store_true', help='仅显示待蒸馏数量，不执行')
    args = parser.parse_args()

    engine = MemoryEngine()

    if args.dry_run:
        count = engine.store.count_unprocessed()
        pending = len(engine.store.get_pending_cache())
        print(f'待蒸馏消息: {count}')
        print(f'待向量化缓存: {pending}')
        return

    if args.mode == 'retry':
        count = engine.retry_failed_cache()
        print(f'重试缓存: {count} 条')
        return

    if args.mode == 'weekly':
        result = engine.distiller.weekly_merge()
        if result:
            print(f'周聚合完成: {len(result)} 字符')
        else:
            print('周聚合: 数据不足')
        return

    # daily mode
    llm_cb = None if args.no_llm else get_llm_callback()
    if llm_cb:
        print('[蒸馏] 使用 LLM 复盘模式')
        engine.set_llm(llm_cb)

    result = engine.distill_with_llm(llm_cb)
    if result:
        print(f'日蒸馏完成')
        engine.retry_failed_cache()
    else:
        print('日蒸馏: 数据不足（需至少 5 条未处理消息）')

    engine.store.close()


if __name__ == '__main__':
    main()
