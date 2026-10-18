"""
正反馈闭环

核心机制:
  1. 隐式反馈 — 用户追问/纠正/重新提问 → 原回复质量分下降
  2. 显式反馈 — 用户 👍/👎 → 直接加减分
  3. 引用加分 — 被检索到的记忆每次被使用 → 质量分 +0.05
  4. 衰减机制 — 长期不被检索的记忆 → 质量分缓慢衰减
"""

import time
import json
from collections import defaultdict
from typing import List, Dict, Optional


class FeedbackLoop:
    """反馈闭环"""

    def __init__(self, store, decay_days: float = 30.0, boost_use: float = 0.05):
        self.store = store
        self.decay_days = decay_days          # 多少天不用开始衰减
        self.boost_use = boost_use            # 每次引用加分
        # 会话内缓存：用户上一次消息（用于检测追问/纠正）
        self._last_user_msg: Dict[str, Dict] = {}

    # ========== 隐式反馈检测 ==========

    def detect_implicit_feedback(self, speaker_id: str,
                                  current_msg: str,
                                  bot_reply_id: Optional[int] = None) -> float:
        """
        检测隐性反馈，返回反馈分:
          +0.1 = 正面（用户接着深入问）
          -0.1 = 负面（用户纠正/重新问）
           0.0 = 无反馈
        """
        last = self._last_user_msg.get(speaker_id)
        if not last:
            self._last_user_msg[speaker_id] = {
                'content': current_msg, 'time': time.time()
            }
            return 0.0

        prev = last['content']
        # 是否在短时间内的追问/深入（正面信号）
        time_gap = time.time() - last['time']
        if time_gap < 120:
            # 用户追问细节
            if any(kw in current_msg for kw in ['那', '还有', '然后', '具体', '比如']):
                return 0.1

        # 用户纠正/重新说明（负面信号）
        if any(kw in current_msg for kw in ['不对', '不是', '错了', '重新', '再说一遍']):
            return -0.1

        # 用户直接重新问同一个问题（负面信号 — 说明上次回复没解决）
        # 简单相似度：字符重合度
        overlap = len(set(prev) & set(current_msg)) / max(len(set(prev)), 1)
        if overlap > 0.6 and time_gap < 300:
            return -0.1

        # 更新缓存
        self._last_user_msg[speaker_id] = {
            'content': current_msg, 'time': time.time()
        }
        return 0.0

    # ========== 显式反馈 ==========

    def handle_explicit_feedback(self, message_id: int, feedback_value: float):
        """处理显式 👍(+1) / 👎(-1)"""
        self.store.update_feedback(message_id, feedback_value)
        self.store.update_importance(message_id, feedback_value * 0.15)

    # ========== 引用加分 ==========

    def boost_retrieved(self, result_ids: List[str]):
        """被检索并被使用的记忆 → 质量分提高"""
        for rid in result_ids:
            # rid 格式: 'msg_123'
            if rid.startswith('msg_'):
                msg_id = int(rid.split('_')[1])
                self.store.update_importance(msg_id, self.boost_use)

    # ========== 衰减 ==========

    def apply_decay(self):
        """对长期未被检索的记忆进行质量衰减"""
        cutoff = time.time() - self.decay_days * 86400
        self.store.conn.execute(
            """UPDATE conversations
               SET importance = MAX(0.1, importance - 0.1)
               WHERE is_distillate = 0
                 AND importance > 0.2
                 AND created_at < ?""",
            (cutoff,)
        )
        self.store.conn.commit()

    # ========== 统计 ==========

    def get_stats(self) -> Dict:
        """获取记忆池状态"""
        total = self.store.conn.execute(
            "SELECT COUNT(*) FROM conversations"
        ).fetchone()[0]
        distilled = self.store.conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE is_distillate = 1"
        ).fetchone()[0]
        avg_imp = self.store.conn.execute(
            "SELECT AVG(importance) FROM conversations"
        ).fetchone()[0]

        users = self.store.conn.execute(
            "SELECT COUNT(*) FROM user_profiles"
        ).fetchone()[0]

        # 四维分布
        type_rows = self.store.conn.execute(
            """SELECT memory_type, COUNT(*) FROM conversations
               WHERE memory_type IS NOT NULL
               GROUP BY memory_type"""
        ).fetchall()
        by_type = {r[0]: r[1] for r in type_rows}

        return {
            'total_messages': total,
            'distilled': distilled,
            'avg_importance': round(avg_imp or 0, 3),
            'users': users,
            'by_type': by_type,
        }
