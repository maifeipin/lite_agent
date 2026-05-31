"""
记忆引擎 × lite_agent 集成层

设计原则:
  - 零侵入: 不修改 agent.py 核心逻辑，仅 hook 三个位置
  - 异步写入: 记忆存储不阻塞消息回复
  - 降级安全: ChromaDB 不可用时退回关键词搜索
"""

import threading
import time
from typing import Optional

from .engine import MemoryEngine


class AgentMemory:
    """
    lite_agent 的记忆增强层
    三个 hook 点:
      1. before_reply(user_id, text) → memory_context: str
      2. after_reply(user_id, user_text, bot_reply)
      3. periodic_distill() 定时蒸馏
    """

    def __init__(self, db_dir: str = None):
        # 与 session.py 共用 data 目录
        if db_dir is None:
            import os
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_dir = os.path.join(base, 'data', 'memory')

        self.engine = MemoryEngine(enable_chroma=True, enable_llm_distill=False)
        self._lock = threading.Lock()

    def set_llm(self, callback):
        """设置 LLM 蒸馏回调"""
        self.engine.set_llm(callback)

    # ========== Hook 1: 回复前检索 ==========

    def before_reply(self, user_id: str, text: str, user_nick: str = '') -> str:
        """
        在 AI 回复前检索相关记忆
        返回: 拼接到 system_prompt 的记忆上下文文本
        """
        if not text or len(text) < 2:
            return ''

        try:
            with self._lock:
                results = self.engine.recall(text, speaker_id=user_id, top_k=3)
                user_ctx = self.engine.get_user_context(user_id)

            parts = []
            if user_ctx:
                parts.append(f"\n\n## 用户长期画像\n{user_ctx}")

            if results:
                memory_lines = []
                for r in results:
                    memory_lines.append(f"- {r['content'][:150]}")
                if memory_lines:
                    parts.append(f"\n\n## 相关历史记忆\n" + '\n'.join(memory_lines))

            return '\n'.join(parts) if parts else ''

        except Exception as e:
            print(f'[记忆检索] 降级: {e}')
            return ''

    # ========== Hook 2: 回复后存储 ==========

    def after_reply(self, user_id: str, user_nick: str,
                    user_text: str, bot_reply: str,
                    channel: str = ''):
        """
        异步存储对话
        """
        def _store():
            try:
                with self._lock:
                    self.engine.remember(user_id, user_nick, user_text, role='user')
                    self.engine.remember(user_id, user_nick, bot_reply, role='bot')
                    # 隐式反馈
                    self.engine.feedback(user_id, user_text)
            except Exception as e:
                print(f'[记忆存储] 降级: {e}')

        t = threading.Thread(target=_store, daemon=True)
        t.start()

    # ========== 定时蒸馏 ==========

    def start_distill_scheduler(self, interval_hours: float = 24.0):
        """启动后台蒸馏线程"""

        def _loop():
            while True:
                time.sleep(interval_hours * 3600)
                try:
                    with self._lock:
                        summary = self.engine.daily_distill(since_hours=interval_hours)
                    if summary:
                        print(f'[记忆蒸馏] 完成: {len(summary)} 字符')
                except Exception as e:
                    print(f'[记忆蒸馏] 出错: {e}')

        t = threading.Thread(target=_loop, daemon=True, name='MemoryDistillThread')
        t.start()

    # ========== 统计 ==========

    def force_remember(self, user_id: str, user_nick: str,
                       content: str, memory_type: str = 'concept') -> int:
        """强制记忆 — /remember 指令"""
        try:
            with self._lock:
                return self.engine.force_remember(
                    user_id, user_nick or '', content, memory_type
                )
        except Exception as e:
            print(f'[强制记忆] 失败: {e}')
            return 0

    # ========== 蒸馏 ==========

    def distill(self):
        """手动触发蒸馏"""
        try:
            with self._lock:
                return self.engine.distill_rules()
        except Exception as e:
            print(f'[蒸馏] 失败: {e}')
            return None

    def stats(self) -> dict:
        try:
            return self.engine.stats()
        except Exception:
            return {'error': '记忆引擎不可用'}

    def close(self):
        self.engine.store.close()
