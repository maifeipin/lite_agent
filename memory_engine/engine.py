"""
记忆引擎 — 统一接口 v2

Usage:
  engine = MemoryEngine(enable_llm_distill=bool)
  
  # 收到用户消息时
  engine.remember(speaker_id, nick, content, role='user')
  
  # 强制记忆
  engine.force_remember(speaker_id, nick, content, memory_type='concept')
  
  # 回复前检索
  context = engine.recall(query, speaker_id, top_k=5)
  
  # LLM 蒸馏 (需要 llm_callback)
  engine.distill_with_llm(llm_callback)
"""

import time
from typing import List, Dict, Optional, Callable

from .store import MemoryStore
from .pipeline import Distiller, DistillTrigger
from .feedback import FeedbackLoop


class MemoryEngine:
    """对外统一接口"""

    def __init__(self, enable_chroma: bool = True,
                 enable_llm_distill: bool = False):
        self.store = MemoryStore()
        self.distiller = Distiller(self.store)
        self.trigger = DistillTrigger(
            self.distiller, cron_hour=3, threshold=100
        )
        self.feedback = FeedbackLoop(self.store)
        self._enable_chroma = enable_chroma
        self._llm_callback: Optional[Callable] = None

    # ========== 设置 LLM 回调 ==========

    def set_llm(self, callback: Callable):
        """设置 LLM 调用函数，用于蒸馏
        callback(prompt: str) -> str
        """
        self._llm_callback = callback
        self.distiller.llm = callback

    # ========== 写入 ==========

    def remember(self, speaker_id: str, speaker_nick: str,
                 content: str, role: str = 'user',
                 msg_type: str = 'text',
                 importance: float = 0.5) -> int:
        """
        记住一条消息（自动分类）
        返回 msg_id
        """
        # 规则分类
        is_noise, memory_type, boost = self.distiller.classify_value(content)
        if is_noise:
            return 0  # 噪音不存
        if memory_type is None:
            memory_type = 'event'
        importance = min(1.0, importance + boost)

        tags = self._auto_tag(content, memory_type)

        msg_id = self.store.save_message(
            speaker_id=speaker_id,
            speaker_nick=speaker_nick,
            content=content,
            role=role,
            msg_type=msg_type,
            importance=importance,
            memory_type=memory_type,
            topic_tags=tags,
        )

        self.store.touch_user(speaker_id, speaker_nick)
        return msg_id

    def force_remember(self, speaker_id: str, speaker_nick: str,
                       content: str, memory_type: str = 'concept',
                       importance: float = 0.9) -> int:
        """
        强制记忆 — /remember 指令使用
        跳过噪音过滤，直接以高优先级存入
        """
        tags = self._auto_tag(content, memory_type)
        return self.store.save_message(
            speaker_id=speaker_id,
            speaker_nick=speaker_nick,
            content=content,
            role='user',
            msg_type='manual_memory',
            importance=importance,
            memory_type=memory_type,
            topic_tags=tags,
        )

    # ========== 检索 ==========

    def recall(self, query: str, speaker_id: str = None,
               top_k: int = 5, temporal_weight: float = 0.3) -> List[Dict]:
        """
        多维度检索 — 融合语义、时间、用户偏好
        返回给 LLM 的 context 片段列表
        """
        results = []

        # 1. 语义检索
        semantic = self.store.semantic_search(
            query, top_k=top_k, speaker_id=speaker_id, min_importance=0.2
        )
        results = self._merge_results(results, semantic, weight=1.0 - temporal_weight)

        # 2. 时间行为模式（同时间段历史）
        if speaker_id:
            temporal = self.store.temporal_pattern(speaker_id)
            results = self._merge_results(results, temporal, weight=temporal_weight)

        # 3. 用户偏好规则
        if speaker_id:
            rules = self.distiller.extract_rules(speaker_id)
            for r in rules:
                results.append({
                    'content': r['content'],
                    'importance': r['importance'],
                    'type': r['type'],
                    'distance': 0.2,
                })

        # 按 importance 排序去重
        results.sort(key=lambda x: x.get('importance', 0), reverse=True)
        seen = set()
        unique = []
        for r in results:
            c = r['content']
            if c not in seen:
                seen.add(c)
                unique.append(r)
            if len(unique) >= top_k:
                break

        # 记录检索日志
        result_ids = [r.get('id', '') for r in unique]
        retrieval_id = self.store.log_retrieval(query, result_ids, used=True)

        # 引用加分
        self.feedback.boost_retrieved(result_ids)

        return unique

    def _merge_results(self, base: List[Dict], new: List[Dict],
                       weight: float) -> List[Dict]:
        """合并检索结果，调整权重"""
        for item in new:
            item['_weight'] = weight
        return base + new

    # ========== 反馈 ==========

    def feedback(self, speaker_id: str, current_msg: str,
                 bot_reply_id: Optional[int] = None):
        """处理隐式反馈"""
        score = self.feedback.detect_implicit_feedback(
            speaker_id, current_msg, bot_reply_id
        )
        if score != 0:
            direction = '正面' if score > 0 else '负面'
            # 找到最近的 bot 回复给予反馈
            if bot_reply_id:
                self.store.update_importance(bot_reply_id, score)
            print(f'[反馈] {speaker_id}: {direction} ({score})')

    def explicit_feedback(self, message_id: int, value: float):
        """显式反馈 👍/👎"""
        self.feedback.handle_explicit_feedback(message_id, value)

    # ========== 蒸馏 ==========

    def distill_with_llm(self, llm_callback: Callable = None) -> Optional[str]:
        """LLM 驱动的蒸馏（推荐）"""
        cb = llm_callback or self._llm_callback
        if not cb:
            print('[蒸馏] 无 LLM 回调，使用规则蒸馏')
        self.distiller.llm = cb
        return self.distiller.daily_distill(since_hours=24)

    def distill_rules(self) -> Optional[str]:
        """纯规则蒸馏（不依赖 LLM）"""
        return self.distiller.daily_distill(since_hours=24)

    def retry_failed_cache(self) -> int:
        """重试失败的蒸馏缓存"""
        return self.distiller.retry_pending_cache()

    def check_trigger(self, llm_callback: Callable = None):
        """检查双触发器（应在定时任务中调用）"""
        if llm_callback:
            self.distiller.llm = llm_callback
        self.trigger.run_if_needed(llm_callback)

    def update_persona(self, speaker_id: str):
        """更新用户画像"""
        self.distiller.update_persona(speaker_id)

    # ========== 统计 ==========

    def stats(self) -> Dict:
        return self.feedback.get_stats()

    def get_user_context(self, speaker_id: str) -> str:
        """
        构建给 LLM 的用户上下文 prompt 片段
        """
        profile = self.store.get_user_profile(speaker_id)
        rules = self.distiller.extract_rules(speaker_id)

        parts = []
        if profile:
            parts.append(f"用户 {profile['nick'] or speaker_id}，已交互 {profile['interactions']} 次")
            if profile['preferred_topics']:
                topics = json.loads(profile['preferred_topics']) if isinstance(profile['preferred_topics'], str) else profile['preferred_topics']
                if topics:
                    parts.append(f"偏好话题: {', '.join(topics[:5])}")

        if rules:
            parts.append("已知偏好和决策:")
            for r in rules[:5]:
                parts.append(f"- [{r['type']}] {r['content'][:100]}")

        return '\n'.join(parts) if parts else ''

    # ========== 工具方法 ==========

    def _auto_tag(self, content: str, memory_type: str) -> List[str]:
        """自动打标签 — 四分类体系"""
        tags = [memory_type]

        tag_map = {
            'concept': ['技术', '架构', '框架', '开源', 'GitHub', '设计', '方案'],
            'event': ['操作', '部署', '配置', '安装', '运行', '测试'],
            'preference': ['偏好', '习惯', '风格', '选择', '喜欢', '工具'],
            'troubleshooting': ['报错', '调试', '修复', 'Bug', '问题', '排查'],
        }

        check_tags = tag_map.get(memory_type, [])
        for kw in check_tags:
            if kw in content:
                tags.append(kw)

        # 跨类型补充: 技术关键词
        tech_kw = ['Python', 'JS', 'SQL', 'API', '数据库', '服务器', 'Docker', 'Linux']
        for kw in tech_kw:
            if kw.lower() in content.lower():
                tags.append(kw)

        return tags[:6]


# 便捷函数
import json  # noqa: E402 (already imported in _auto_tag via json.dumps)
