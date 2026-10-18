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
            from lite_agent.core.constants import PROJECT_ROOT
            db_dir = os.path.join(PROJECT_ROOT, 'data', 'memory')

        self.engine = MemoryEngine(enable_chroma=True, enable_llm_distill=False)
        self._lock = threading.Lock()

        # persona.md 缓存（避免每条消息都读盘）
        # 5 分钟过期；蒸馏只每天一次，5 分钟刷新足够及时
        self._persona_cache = ''
        self._persona_loaded_at = 0.0
        self._persona_ttl = 300  # 秒

        # 首次部署：确保 data/persona.md 存在（不覆盖已有内容）
        try:
            from . import persona_writer
            persona_writer.init_persona_template()
        except Exception as e:
            print(f'[persona] 模板初始化失败 (非致命): {e}')

    def set_llm(self, callback):
        """设置 LLM 蒸馏回调"""
        self.engine.set_llm(callback)

    def _get_persona(self) -> str:
        """读取 persona.md，带短 TTL 缓存。"""
        now = time.time()
        if self._persona_cache and (now - self._persona_loaded_at) < self._persona_ttl:
            return self._persona_cache
        try:
            from . import persona_writer
            content = persona_writer.load_persona()
            self._persona_cache = content
            self._persona_loaded_at = now
            return content
        except Exception as e:
            print(f'[persona] 加载失败: {e}')
            return ''


    # ========== Hook 1: 回复前检索 ==========

    def before_reply(self, user_id: str, text: str, user_nick: str = '') -> str:
        """
        在 AI 回复前检索相关记忆并附加 persona 主档案
        返回: 拼接到 system_prompt 的记忆上下文文本

        拼接顺序：
          1. persona.md (稳定画像，永远在最前)
          2. 从历史纠正中学到的偏好 (preference 类型，高优先级，可操作)
          3. user 长期画像（旧体系沉淀，向下兼容）
          4. 相关历史记忆 (RAG 检索)
        """
        if not text or len(text) < 2:
            return ''

        try:
            with self._lock:
                results = self.engine.recall(text, speaker_id=user_id, top_k=5)
                user_ctx = self.engine.get_user_context(user_id)

            parts = []

            # 1. persona.md 主档案 — 稳定的"我是谁"画像
            persona = self._get_persona()
            if persona and persona.strip():
                parts.append(f"\n\n{persona}")

            # 2. 从历史纠正中学到的偏好 — 按 preference 类型单独分组
            # 注意: 不同检索路径的字段名不同:
            #   语义搜索 → tags (逗号分隔字符串, 含 'preference')
            #   规则提取 → type ('preference'/'decision'/'fact'/etc)
            #   关键词搜索 → tags (JSON 数组字符串)
            prefs = []
            for r in results:
                rtype = r.get('type', '')
                rtags = r.get('tags', '')
                if rtype == 'preference' or 'preference' in str(rtags):
                    prefs.append(r)
            other_results = [r for r in results
                           if r not in prefs]
            if prefs:
                pref_lines = []
                for r in prefs:
                    pref_lines.append(f"- ⚠️ {r['content'][:300]}")
                parts.append(
                    "\n\n## 🧭 从历史纠正中学到的工具/行为偏好 "
                    "(高优先级，选择工具前必须检查)\n"
                    + '\n'.join(pref_lines)
                )

            # 3. 旧版 user_profiles 画像 (兼容)
            if user_ctx:
                parts.append(f"\n\n## 用户长期画像（旧版）\n{user_ctx}")

            # 4. RAG 检索 — 跟当前 query 相关的历史 (偏好的已单列，这里放其余)
            if other_results:
                memory_lines = []
                for r in other_results:
                    memory_lines.append(f"- {r['content'][:300]}")
                if memory_lines:
                    parts.append(
                        "\n\n## 相关历史记忆\n" + '\n'.join(memory_lines)
                    )

            return '\n'.join(parts) if parts else ''

        except Exception as e:
            print(f'[记忆检索] 降级: {e}')
            return ''

    # ========== Hook 2: 回复后存储 ==========

    def after_reply(self, user_id: str, user_nick: str,
                    user_text: str, bot_reply: str,
                    channel: str = ''):
        """
        异步存储对话 + 纠正偏好实时学习
        """
        def _store():
            try:
                with self._lock:
                    self.engine.remember(user_id, user_nick, user_text, role='user')
                    self.engine.remember(user_id, user_nick, bot_reply, role='bot')
                    # 隐式反馈: 检测纠正/追问
                    score = self.engine.feedback.detect_implicit_feedback(
                        user_id, user_text
                    )
                    # 负面反馈 → 实时提取并存储偏好 (不等日蒸馏, 闭合学习回路)
                    if score < 0:
                        pref = self._extract_correction_preference(
                            user_text, bot_reply
                        )
                        if pref:
                            self.engine.force_remember(
                                user_id, user_nick, pref,
                                memory_type='preference', importance=0.9
                            )
                            print(f'[学习] 从纠正中提取偏好: {pref[:80]}')
            except Exception as e:
                print(f'[记忆存储] 降级: {e}')

        t = threading.Thread(target=_store, daemon=True)
        t.start()

    # ========== 定时蒸馏 ==========

    def start_distill_scheduler(self, interval_hours: float = 24.0):
        """启动后台蒸馏线程

        - 启动后等 5 分钟（让服务稳定）就跑第一次（不再傻等 24h）
        - 之后每 interval_hours 跑一次
        - 优先调 daily_distill (LLM 提取画像 + 写 persona.md)；
          降级到 distill_rules（纯规则）当没 LLM callback 时
        """

        def _loop():
            initial_delay = 300  # 启动 5 分钟后跑第一次
            time.sleep(initial_delay)
            while True:
                try:
                    with self._lock:
                        # daily_distill 走 LLM 提取（如已注入 set_llm）
                        # 内部检测到 self.llm is None 时自动 fallback 到规则蒸馏
                        summary = self.engine.distiller.daily_distill(since_hours=interval_hours)
                    if summary:
                        print(f'[记忆蒸馏] 完成: {len(summary)} 字符')
                    else:
                        print('[记忆蒸馏] 跳过（消息数不足）')
                except Exception as e:
                    print(f'[记忆蒸馏] 出错: {e}')
                time.sleep(interval_hours * 3600)

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

    # ========== 纠正偏好提取 (闭合反馈-学习回路) ==========

    def _extract_correction_preference(self, user_text: str,
                                       bot_reply: str) -> Optional[str]:
        """用 LLM 从用户的纠正语中提取可操作的偏好规则。

        用户纠正 agent 后实时调用（不等日蒸馏），
        提取的偏好以高优先级存入记忆池，下次类似查询直接被 RAG 召回。
        """
        llm = getattr(self.engine, '_llm_callback', None)
        if not llm:
            return None
        prompt = (
            '用户问了问题，助手给了一个不符合预期的回复。用户纠正说：\n'
            '---\n'
            f'用户纠正: {user_text[:300]}\n'
            '---\n'
            f'助手之前的错误回复（摘要）: {bot_reply[:200]}\n'
            '---\n'
            '请提取用户纠正中隐含的**工具/行为偏好规则**，写成一句简洁可操作的指令。\n'
            '格式: "当用户查询<主题>时，应该<用哪个工具/怎么做>，不要<错误做法>"\n'
            '如果用户的纠正不涉及具体工具选择或行为偏好（只是闲聊纠正语气），返回 "NONE"。\n'
            '只返回指令本身，不要额外解释。'
        )
        try:
            result = llm(prompt)
            if result and result.strip() and result.strip().upper() != 'NONE':
                return result.strip()
        except Exception:
            pass
        return None

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

    # ========== Persona ==========

    def persona_content(self) -> str:
        """返回 data/persona.md 全文。供 /persona 命令使用。"""
        try:
            from . import persona_writer
            return persona_writer.load_persona()
        except Exception as e:
            return f'(加载 persona.md 失败: {e})'

    def persona_pending(self) -> list:
        """返回 ## ⏳ 待确认 段所有条目，按出现顺序。"""
        try:
            from . import persona_writer
            return persona_writer.list_pending()
        except Exception:
            return []

    def persona_confirm(self, index: int, target_section: str = '## 工作偏好'):
        """把"待确认"第 index (1-based) 条升格到 target_section 的 ### 手动校正。"""
        try:
            from . import persona_writer
            result = persona_writer.confirm_pending(index, target_section=target_section)
            # 升格后 persona.md 已变，清掉 before_reply 缓存让下一条消息读到新版
            self._persona_cache = ''
            self._persona_loaded_at = 0.0
            return result
        except Exception as e:
            print(f'[persona_confirm] 失败: {e}')
            return None

    def close(self):
        self.engine.store.close()
