"""
蒸馏引擎 v2 — LLM 复盘 + 四分类 + 两段式写入

策略:
  Level 0: 原始消息（不蒸馏，永久保留）
  Level 1: LLM 复盘蒸馏 — 凌晨 03:00 触发，通读 24h 对话
           提取 concept/event/preference/troubleshooting 四种维度的 JSON
           → 写入 distilled_cache (防丢失)
           → 调用 Embedding → ChromaDB
  Level 2: 周聚合 — 7 天 Level 1 摘要合并
  Level 3: 规则提取 — 关键词 + LLM 混合提取偏好/决策/事实

双触发器:
  1. 固定: 凌晨 03:00
  2. 动态: 未蒸馏消息 > 100 条
"""

import json
import time
import re
from typing import List, Dict, Optional, Callable


# ========== LLM 蒸馏 Prompt ==========
#
# 蒸馏目标重定义：
#   旧版 prompt 做"对话复盘"——把对话分成 concept/event/preference/troubleshooting
#   四个桶。规则化但缺乏"这个人是怎样的"画像沉淀，蒸馏 24 条 cache 后用户画像仍是空。
#
#   新版 prompt 做"画像增量提取"——从这 24h 对话中提取**关于主用户本人**的稳定特征，
#   合并进 data/persona.md 主档案。结果是一份跨工具可读的个人简历式 markdown，
#   "给任何智能应用都可以快速了解我"的入口。
#
# 输出 JSON 与 persona.md 章节的映射在 persona_writer.update_persona() 里定义：
#   identity / preferences / skills / current_projects / decisions / facts → 各章节
#   pending → ## ⏳ 待确认 段（累积去重，等用户审）

DISTILL_PROMPT = """你是个人助理的"画像提炼器"。请阅读下面这位用户最近 24 小时的对话，从中提取**关于这个人本身的稳定特征**——不是聊了什么话题，而是 ta 的偏好、习惯、决定、技能、所做的事。

严格要求：
1. 只输出 JSON，不要 markdown 代码块包裹，不要任何其他文字
2. 只关注**用户本人**的特征，不要把 LLM 助手的回答当成用户的偏好
3. 过滤掉寒暄、单字回复、表情等无意义内容
4. **绝对不要**写入任何疑似密码 / API key / 私钥 / token / 凭据的内容（即使是占位符也不要）；如果对话里出现，整条丢弃
5. 每条记录写成短语形式（一句话总结），不要原样复制对话
6. **客观中性描述，不评价、不阿谀、不夸张**：
   - ✗ "沟通风格直接、指令清晰，常使用'请直接按指令输出'" (评价 + 例证)
   - ✓ "沟通风格直接，倾向给出明确指令而非引导式提问" (描述事实)
   - ✗ "具备深度代码审计与架构师能力，能输出万字级技术报告" (赞美)
   - ✓ "曾要求对工作区源码做完整审计，关注 SQL 并发、模块覆盖" (描述行为)
7. 如果某个分类没有内容，给空数组 []

JSON 格式（七个固定字段）：
{{
  "identity":         ["如：'后端开发者，专注 .NET + Python 全栈'"],
  "preferences":      ["如：'倾向用 LaunchAgent 不用 LaunchDaemon'", "'PR 协作要求一个开发一个审'"],
  "skills":           ["如：'熟练 Playwright headed 浏览器调试'"],
  "current_projects": ["如：'RssAdapter / lite_agent / rssnextui 三仓协作'"],
  "decisions":        ["如：'最终采用 LaunchAgent KeepAlive 守护 dotnet'"],
  "facts":            ["如：'Mac 端跑 Playwright，VPS 端跑 lite-agent'（不要写 IP / 域名 / 端口等敏感信息）"],
  "pending":          ["不太确定但值得记下的推断，如：'可能偏好 deepseek 而不是 gemini（基于调用次数）'"]
}}

对话内容:
{conversations}
"""


# ========== 敏感词正则 (与 persona_writer 同步) ==========
# 在蒸馏阶段就过滤一遍，避免敏感凭据被发送给 LLM 留在调用日志里。
# persona_writer 写入时还会再过滤一次，双保险。
#
# 注意：不用 \b 词边界做尾界——中文字符与 \b 配合不可靠
# (如 "sk-xxx的余额" 中 "x" 和 "的" 中间没有 \b)。改用 (?=...) 前瞻或不收尾。
_SENSITIVE_LINE_PATTERNS = [
    re.compile(r'sk-[a-zA-Z0-9]{20,}'),
    re.compile(r'AKID[a-zA-Z0-9]{16,}'),
    re.compile(r'AKIA[A-Z0-9]{16}'),
    re.compile(r'(ghp|gho|ghu|ghs|ghr)_[a-zA-Z0-9]{36}'),
    re.compile(r'(password|passwd|pwd|secret|token|apikey|api_key)\s*[:=]\s*["\']?[^\s"\']{6,}',
               re.IGNORECASE),
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
]


def _strip_sensitive_lines(text: str) -> str:
    """整行删除含敏感凭据的对话行，避免发给 LLM。"""
    out = []
    for line in text.split('\n'):
        if any(p.search(line) for p in _SENSITIVE_LINE_PATTERNS):
            continue
        out.append(line)
    return '\n'.join(out)



class Distiller:
    """多级蒸馏器 v2"""

    def __init__(self, store, llm_callback: Callable = None):
        self.store = store
        self.llm = llm_callback  # (prompt: str) -> str

    # ========== 消息价值分类 (规则部分) ==========

    NOISE_PATTERNS = [
        r'^[好的嗯哦啊哈]{1,3}$',
        r'^(收到|明白|了解|OK|ok|知道了)$',
        r'^(好|行|可以|对|是的|没错|对的对的)$',
        r'^[👍👌🙏❤️]+\s*$',
        r'^\s*$',
    ]

    HIGH_VALUE_PATTERNS = [
        (r'(决定|定下来|就用|选择|确认|采用)了?.*', 'event'),
        (r'(我喜欢|我习惯|我偏好|我用|我选择).*', 'preference'),
        (r'(我不喜欢|我讨厌|我不习惯|别用|不要用).*', 'preference'),
        (r'.*?(报错|错误|失败|不行|出错|bug|Bug|BUG|exception|Exception|Error).*', 'troubleshooting'),
        (r'.*?(修复|解决|搞定|Fix|fix|workaround|绕过了).*', 'troubleshooting'),
        (r'.*.(github\.com|项目|开源|repo|框架|架构).*', 'concept'),
        (r'^(帮我|请|把|让我|搭建|部署|配置|安装|启动).*', 'event'),
    ]

    MEMORY_TYPE_MAP = {
        'decision': 'event',
        'preference': 'preference',
        'fact': 'event',
        'question': 'event',
        'command': 'event',
        'noise': None,
        'conversation': 'event',
    }

    def classify_value(self, content: str) -> tuple:
        """
        (is_noise, memory_type, importance_boost)
        """
        text = content.strip()

        for pattern in self.NOISE_PATTERNS:
            if re.match(pattern, text):
                return True, None, -0.3

        for pattern, mem_type in self.HIGH_VALUE_PATTERNS:
            if re.match(pattern, text):
                return False, mem_type, 0.2

        return False, 'event', 0.0

    # ========== Level 1: LLM 复盘蒸馏 ==========

    def daily_distill(self, since_hours: float = 24.0,
                      min_count: int = 5) -> Optional[str]:
        """
        LLM 驱动的日蒸馏
        流程: 拉取消息 → 主用户识别 → 敏感词过滤 → LLM 画像提取
              → 写入 cache → 向量化 → 更新 persona.md
        """
        msgs = self.store.get_unprocessed_messages(
            since_days=since_hours / 24.0, min_count=min_count
        )
        if not msgs:
            return None

        source_ids = [m['id'] for m in msgs]
        source_count = len(msgs)
        date_key = time.strftime('daily_%Y-%m-%d')

        # 主用户识别：本批消息里 user 角色出现最多的 speaker_id
        # （蒸馏的画像只针对主用户，避免不同用户互相污染画像）
        main_speaker = self._identify_main_speaker(msgs)

        # 1. 构建对话文本 — 只取主用户相关对话，并过滤敏感凭据行
        relevant_msgs = [m for m in msgs if m.get('speaker_id') == main_speaker] \
            if main_speaker else msgs
        conv_text = self._format_conversations(relevant_msgs)
        conv_text = _strip_sensitive_lines(conv_text)

        # 2. 调用 LLM 提取画像
        if self.llm:
            try:
                raw_json = self.llm(DISTILL_PROMPT.format(conversations=conv_text))
                # 清理可能的 markdown 包裹
                raw_json = raw_json.strip()
                if raw_json.startswith('```'):
                    raw_json = re.sub(r'^```\w*\n?', '', raw_json)
                    raw_json = re.sub(r'\n?```$', '', raw_json)
                distill_data = json.loads(raw_json)
            except Exception as e:
                print(f'[蒸馏] LLM 画像提取失败: {e}，回退规则蒸馏')
                distill_data = self._rule_distill(msgs)
        else:
            distill_data = self._rule_distill(msgs)

        # 3. 写入缓存（防丢失）
        cache_id = self.store.save_distill_cache(
            date_key, json.dumps(distill_data, ensure_ascii=False), source_count
        )

        # 4. 向量化 + 写入 ChromaDB（仍兼容旧 4 字段格式以避免回归）
        try:
            self._vectorize_and_store(distill_data, source_ids, cache_id)
            self.store.mark_cache_vectorized(cache_id)
        except Exception as e:
            print(f'[蒸馏] 向量化失败: {e}，已缓存等待重试')
            self.store.mark_cache_failed(cache_id)

        # 5. 标记原始消息已蒸馏
        self.store.mark_distilled(source_ids)

        # 6. 更新 persona.md 主档案（新版 prompt 字段才会有意义）
        try:
            from . import persona_writer
            persona_writer.update_persona(distill_data, main_speaker=main_speaker or '')
        except Exception as e:
            print(f'[蒸馏] persona.md 更新失败: {e}')

        return json.dumps(distill_data, ensure_ascii=False, indent=2)

    def _identify_main_speaker(self, msgs: List[Dict]) -> str:
        """
        从这批消息里识别主用户：user 角色消息数最多的 speaker_id。
        若所有消息都没 speaker_id 或都是 bot 回复，返回空字符串。
        """
        from collections import Counter
        speakers = Counter()
        for m in msgs:
            sid = m.get('speaker_id')
            if not sid or m.get('role') != 'user':
                continue
            speakers[sid] += 1
        if not speakers:
            return ''
        return speakers.most_common(1)[0][0]

    def _format_conversations(self, msgs: List[Dict]) -> str:
        """格式化对话为 LLM 可读文本"""
        lines = []
        for m in msgs:
            role = '用户' if m['role'] == 'user' else '助手'
            nick = m.get('speaker_nick', '')
            tag = f'[{role}{":" + nick if nick else ""}]'
            lines.append(f'{tag} {m["content"][:300]}')
        return '\n'.join(lines)

    def _rule_distill(self, msgs: List[Dict]) -> Dict:
        """降级方案：纯规则蒸馏（当 LLM 不可用时）"""
        result = {'concept': [], 'event': [], 'preference': [], 'troubleshooting': []}

        for m in msgs:
            content = m['content']
            mem_type = m.get('memory_type', 'event')

            if mem_type == 'concept':
                result['concept'].append({'content': content, 'keywords': []})
            elif mem_type == 'preference':
                # 提取偏好关键词
                kw = re.findall(r'(喜欢|习惯|偏好|选择|用|部署|安装)([\u4e00-\u9fa5a-zA-Z0-9.+-/]+)', content)
                result['preference'].append({
                    'content': content,
                    'keywords': [f'{k[0]}{k[1]}' for k in kw[:3]],
                })
            elif mem_type == 'troubleshooting':
                cause_match = re.search(r'(报错|错误|失败|原因)[:：]?\s*([\u4e00-\u9fa5a-zA-Z.]+)', content)
                fix_match = re.search(r'(解决|修复|搞定|workaround)[:：]?\s*([\u4e00-\u9fa5a-zA-Z.]+)', content)
                result['troubleshooting'].append({
                    'content': content,
                    'cause': cause_match.group(2) if cause_match else '',
                    'solution': fix_match.group(2) if fix_match else '',
                })
            else:
                result['event'].append({'content': content, 'time': ''})

        return result

    def _vectorize_and_store(self, distill_data: Dict,
                             source_ids: List[int], cache_id: int):
        """将蒸馏产物分别向量化存入"""

        importance_map = {
            'troubleshooting': 0.9,
            'preference': 0.85,
            'concept': 0.8,
            'event': 0.65,
        }

        for mem_type in ['troubleshooting', 'preference', 'concept', 'event']:
            items = distill_data.get(mem_type, [])
            for item in items:
                content = item.get('content', '')
                if not content or len(content) < 3:
                    continue
                importance = importance_map.get(mem_type, 0.6)

                self.store.save_message(
                    speaker_id='system',
                    speaker_nick='system',
                    content=content,
                    role='system',
                    msg_type='distillate',
                    importance=importance,
                    memory_type=mem_type,
                    topic_tags=item.get('keywords', []),
                )

    # ========== 缓存重试 ==========

    def retry_pending_cache(self) -> int:
        """重试失败的缓存"""
        pending = self.store.get_pending_cache()
        count = 0
        for cache in pending:
            try:
                data = json.loads(cache['raw_json'])
                self._vectorize_and_store(data, [], cache['id'])
                self.store.mark_cache_vectorized(cache['id'])
                count += 1
            except Exception as e:
                print(f'[重试] 缓存 {cache["cache_key"]}: {e}')
        return count

    # ========== Level 2: 周聚合 ==========

    def weekly_merge(self) -> Optional[str]:
        """把 7 天蒸馏产物合并为周摘要"""
        rows = self.store.conn.execute(
            """SELECT content FROM conversations
               WHERE is_distillate = 1
               ORDER BY created_at DESC LIMIT 7"""
        ).fetchall()

        if len(rows) < 3:
            return None

        merged = f"[周摘要 {time.strftime('%Y-W%W')}] 本周关键信息：\n"
        merged += "\n".join(f"- {r[0][:200]}" for r in rows)

        self.store.conn.execute(
            """INSERT INTO conversations
               (speaker_id, speaker_nick, role, content, created_at,
                importance, topic_tags, is_distillate, memory_type)
               VALUES ('system','system','system',?,?,0.85,'["weekly_summary"]',1,'event')""",
            (merged, time.time())
        )
        self.store.conn.commit()
        return merged

    # ========== Level 3: 规则提取 ==========

    def extract_rules(self, speaker_id: str) -> List[Dict]:
        """从历史记录中提取个人规则/偏好"""
        rows = self.store.conn.execute(
            """SELECT content, importance, memory_type
               FROM conversations
               WHERE speaker_id = ?
                 AND role = 'user'
                 AND is_distillate = 0
                 AND importance >= 0.5
               ORDER BY created_at DESC
               LIMIT 200""",
            (speaker_id,)
        ).fetchall()

        rules = []
        for content, importance, mem_type in rows:
            if mem_type == 'preference':
                rules.append({'type': 'preference', 'content': content, 'importance': importance})
            elif mem_type == 'troubleshooting':
                rules.append({'type': 'troubleshooting', 'content': content, 'importance': importance})
            elif mem_type == 'concept':
                rules.append({'type': 'concept', 'content': content, 'importance': importance})
            elif any(kw in content for kw in ['决定', '就用', '选择', '确认']):
                rules.append({'type': 'decision', 'content': content, 'importance': importance})
            elif any(kw in content for kw in ['我的', '我们的', '公司', '项目']):
                rules.append({'type': 'fact', 'content': content, 'importance': importance})

        rules.sort(key=lambda x: x['importance'], reverse=True)
        return rules[:20]

    # ========== 画像层自动更新 ==========

    def update_persona(self, speaker_id: str):
        """基于蒸馏产物更新用户画像"""
        # 收集最近的 preference 和概念
        prefs = self.store.conn.execute(
            """SELECT content FROM conversations
               WHERE speaker_id = ? AND memory_type = 'preference'
               ORDER BY created_at DESC LIMIT 10""",
            (speaker_id,)
        ).fetchall()

        concepts = self.store.conn.execute(
            """SELECT content, topic_tags FROM conversations
               WHERE speaker_id = ? AND memory_type = 'concept'
               ORDER BY created_at DESC LIMIT 10""",
            (speaker_id,)
        ).fetchall()

        # 提取偏好话题
        all_tags = []
        for _, tags_json in concepts:
            try:
                all_tags.extend(json.loads(tags_json) if isinstance(tags_json, str) else tags_json)
            except Exception:
                pass

        preferred = list(set(all_tags))[:10]

        profile = {
            'preference_count': len(prefs),
            'concept_count': len(concepts),
            'preferred_topics': preferred,
            'last_distilled': time.time(),
        }

        self.store.conn.execute(
            """UPDATE user_profiles SET profile_json = ?, preferred_topics = ?
               WHERE speaker_id = ?""",
            (json.dumps(profile, ensure_ascii=False),
             json.dumps(preferred, ensure_ascii=False),
             speaker_id)
        )
        self.store.conn.commit()


# ========== 双触发器 ==========

class DistillTrigger:
    """蒸馏触发器 — 固定时间 + 动态阈值"""

    def __init__(self, distiller: Distiller,
                 cron_hour: int = 3,
                 threshold: int = 100):
        self.distiller = distiller
        self.cron_hour = cron_hour
        self.threshold = threshold
        self._last_cron_run = 0
        self._last_threshold_run = 0

    def should_run(self, now: float = None) -> str:
        """
        检查是否应该触发蒸馏
        返回: 'cron' | 'threshold' | None
        """
        if now is None:
            now = time.time()

        # 1. 固定时间触发（凌晨 cron_hour:00）
        current_hour = time.localtime(now).tm_hour
        today_start = now - now % 86400
        cron_time = today_start + self.cron_hour * 3600

        if self._last_cron_run < cron_time <= now:
            self._last_cron_run = now
            return 'cron'

        # 2. 动态阈值触发（堆积 > N 条）
        unprocessed = self.distiller.store.count_unprocessed()
        if unprocessed > self.threshold:
            self._last_threshold_run = now
            return 'threshold'

        return None

    def run_if_needed(self, llm_callback: Callable = None):
        """检查并执行蒸馏"""
        trigger = self.should_run()
        if not trigger:
            return

        if llm_callback:
            self.distiller.llm = llm_callback

        print(f'[蒸馏触发器] {trigger} 触发')
        try:
            result = self.distiller.daily_distill()
            if result:
                print(f'[蒸馏] 完成: {trigger}')
            else:
                print(f'[蒸馏] 跳过: {trigger} (数据不足)')
        except Exception as e:
            print(f'[蒸馏] 失败: {trigger}: {e}')
