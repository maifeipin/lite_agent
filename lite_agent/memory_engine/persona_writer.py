"""
persona_writer — 维护 data/persona.md 主档案

职责：
- 解析现有 persona.md（章节切分）
- 把每日蒸馏 LLM 产出的画像增量合并进去
- 三段写入策略：
    ## 手动校正  ← 永不动（用户审完移到这里）
    ## LLM 自动提取 ← 每次蒸馏整体替换
    ## ⏳ 待确认  ← 追加新条目（去重）

并发控制：用文件锁防止双跑（A 后台线程 + B cron 03:00）同时写。
敏感词过滤：在写入前再做一次正则过滤兜底（蒸馏阶段也过滤过一次，双保险）。
"""

import os
import re
import json
import time
import threading
from datetime import datetime
from typing import Dict, List, Optional


# ========== 常量 ==========

from lite_agent.core.constants import PROJECT_ROOT

# persona.md 文件位置：跟 store/distilled_cache 同目录，会被现有备份脚本捎上
PERSONA_PATH = os.path.join(PROJECT_ROOT, 'data', 'persona.md')

# 标准章节顺序。新生成 persona.md 时按这个顺序铺底。
SECTIONS = [
    '## 身份与角色',
    '## 工作偏好',
    '## 技术栈熟练度',
    '## 当前进行中项目',
    '## 已知决策',
    '## 个人事实',
    '## ⏳ 待确认',  # 由 LLM 自动追加，等用户审
]

# 每个一级章节内部的三个子段
SUBSECTIONS = [
    '### 手动校正',     # 用户校正过的内容，永不动
    '### LLM 自动提取', # 每次蒸馏整体替换
]

# 敏感词正则黑名单 — 命中条目直接丢弃，连占位符也不要。
# 设计原则：宁可漏报（漏写一条偏好）也不误纳（写入了密码 / API key）。
# 注意：不用 \b 词边界做尾界——中文字符与 \b 配合不可靠
# （如 "sk-xxx的余额" 中 "x" 和 "的" 中间不被识别为 \b 边界）
_SENSITIVE_PATTERNS = [
    # API keys / tokens
    re.compile(r'sk-[a-zA-Z0-9]{20,}'),                  # OpenAI / DeepSeek 系
    re.compile(r'AKID[a-zA-Z0-9]{16,}'),                  # 腾讯云
    re.compile(r'AKIA[A-Z0-9]{16}'),                       # AWS
    re.compile(r'(ghp|gho|ghu|ghs|ghr)_[a-zA-Z0-9]{36}'),  # GitHub PAT
    re.compile(r'xox[bp]-[a-zA-Z0-9-]{50,}'),              # Slack
    # 通用密码 / 凭据 字段
    re.compile(r'(password|passwd|pwd|secret|token|apikey|api_key)\s*[:=]\s*["\']?[^\s"\']{6,}',
               re.IGNORECASE),
    # 私钥
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    # 邮箱中的复杂凭据样式（如 user:pass@host）
    re.compile(r'\b[a-zA-Z0-9._-]+:[^\s@]{6,}@[a-zA-Z0-9.-]+'),
]


def _has_sensitive(text: str) -> bool:
    """文本是否包含敏感凭据。"""
    if not text:
        return False
    for p in _SENSITIVE_PATTERNS:
        if p.search(text):
            return True
    return False


# ========== 文件锁 ==========

# 进程内锁（防同进程内两个线程同时写）
_FILE_LOCK = threading.Lock()


# ========== 解析 ==========

def _parse_persona(content: str) -> Dict[str, Dict[str, List[str]]]:
    """
    把 persona.md 解析成结构化字典：
      {
        '## 身份与角色': {
          '### 手动校正': ['- 后端开发者', ...],
          '### LLM 自动提取': ['- 偏好 .NET 8', ...],
        },
        ...
        '## ⏳ 待确认': {
          '_items': ['- 偏好 deepseek 而不是 gemini', ...]
        }
      }
    """
    result: Dict[str, Dict[str, List[str]]] = {}
    if not content:
        return result

    current_section = None
    current_subsection = None
    for line in content.split('\n'):
        stripped = line.strip()
        # 一级章节（## 开头）
        if stripped.startswith('## ') and not stripped.startswith('### '):
            current_section = stripped
            result.setdefault(current_section, {})
            current_subsection = None
            # ⏳ 待确认是平铺，没有子段
            if '⏳' in current_section or '待确认' in current_section:
                result[current_section].setdefault('_items', [])
                current_subsection = '_items'
            continue
        # 二级章节（### 开头）
        if stripped.startswith('### '):
            if current_section is None:
                continue
            current_subsection = stripped
            result[current_section].setdefault(current_subsection, [])
            continue
        # 内容行（item）
        if current_section and current_subsection and stripped.startswith('-'):
            result[current_section][current_subsection].append(line.rstrip())

    return result


# ========== 渲染 ==========

def _render_persona(parsed: Dict[str, Dict[str, List[str]]],
                    main_speaker: str = '',
                    last_updated: float = None) -> str:
    """
    把结构化字典还原成 markdown 文本。
    """
    if last_updated is None:
        last_updated = time.time()
    ts = datetime.fromtimestamp(last_updated).strftime('%Y-%m-%d %H:%M:%S')

    lines = [
        '# 个人画像 / Persona',
        '',
        f'> 自动维护 + 手动校正  |  最后更新: {ts}',
        f'> 主用户: `{main_speaker or "未识别"}`',
        '> 任何 LLM 看到这份文档应当：(1) 优先尊重 ### 手动校正 段；'
        '(2) ### LLM 自动提取 段视为高置信，但允许新蒸馏覆盖；'
        '(3) ## ⏳ 待确认 段视为低优先级建议',
        '',
    ]

    for section in SECTIONS:
        lines.append(section)
        sec_data = parsed.get(section, {})

        if '⏳' in section or '待确认' in section:
            items = sec_data.get('_items', [])
            if items:
                lines.append('')
                lines.extend(items)
            else:
                lines.append('')
                lines.append('*（暂无待确认条目）*')
        else:
            for sub in SUBSECTIONS:
                lines.append('')
                lines.append(sub)
                items = sec_data.get(sub, [])
                if items:
                    lines.extend(items)
                else:
                    lines.append('*（暂无）*')

        lines.append('')

    return '\n'.join(lines).rstrip() + '\n'


# ========== 合并 ==========

def _normalize_item(text: str) -> str:
    """item 归一化用于去重：去前缀、去空白、小写化（中文不变）"""
    text = text.lstrip('-* \t').strip()
    text = re.sub(r'\s+', ' ', text)
    return text.lower()


def _merge_section(
    existing: Dict[str, List[str]],
    new_items_by_section: Dict[str, List[str]],
    section_name: str,
) -> Dict[str, List[str]]:
    """
    合并单个一级章节的内容：
    - ### 手动校正 段保持不变（永不动）
    - ### LLM 自动提取 段整体替换为本次蒸馏结果
    - 返回新的 sec_data
    """
    result = dict(existing)
    new = new_items_by_section.get(section_name, [])
    # 整体替换 LLM 自动提取段
    result['### LLM 自动提取'] = new if new else result.get('### LLM 自动提取', [])
    # 手动校正段不变
    result.setdefault('### 手动校正', existing.get('### 手动校正', []))
    return result


def _merge_pending(
    existing_pending: List[str],
    new_pending: List[str],
    max_items: int = 50,
) -> List[str]:
    """
    待确认段：追加去重，超过 max_items 时丢弃最旧。
    """
    seen = {_normalize_item(x): x for x in existing_pending}
    for item in new_pending:
        norm = _normalize_item(item)
        if norm and norm not in seen:
            seen[norm] = item
    items = list(seen.values())
    if len(items) > max_items:
        items = items[-max_items:]
    return items


def _filter_sensitive(items: List[str]) -> List[str]:
    """剥掉命中敏感词的 item。"""
    return [x for x in items if not _has_sensitive(x)]


# ========== 公共 API ==========

def load_persona() -> str:
    """读取 persona.md 全文。文件不存在返回空字符串。"""
    if not os.path.exists(PERSONA_PATH):
        return ''
    try:
        with open(PERSONA_PATH, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        print(f'[persona_writer] 读取失败: {e}')
        return ''


def update_persona(distill_json: Dict, main_speaker: str = '') -> bool:
    """
    把单次蒸馏的 JSON 输出合并进 persona.md。

    distill_json 期望格式（由新版 DISTILL_PROMPT 产出）:
      {
        "identity":     ["- 后端工程师，主要项目..."],
        "preferences":  ["- 倾向 LaunchAgent..."],
        "skills":       ["- 熟练 .NET 8 + Playwright"],
        "current_projects": ["- RssAdapter / lite_agent"],
        "decisions":    ["- 选择 LaunchAgent..."],
        "facts":        ["- 部署在 Mac + VPS 双端..."],
        "pending":      ["- 偏好 deepseek 而不是 gemini  (推断)"],
      }

    返回 True 表示成功更新。
    """
    if not isinstance(distill_json, dict):
        print(f'[persona_writer] 蒸馏输出不是 dict: {type(distill_json)}')
        return False

    # 把 LLM JSON 各字段映射到 persona.md 的章节
    field_to_section = {
        'identity':         '## 身份与角色',
        'preferences':      '## 工作偏好',
        'skills':           '## 技术栈熟练度',
        'current_projects': '## 当前进行中项目',
        'decisions':        '## 已知决策',
        'facts':            '## 个人事实',
    }

    # 敏感词过滤（双保险，蒸馏阶段已过一遍）
    new_by_section: Dict[str, List[str]] = {}
    for field, section in field_to_section.items():
        items = distill_json.get(field, [])
        if not isinstance(items, list):
            continue
        # 标准化为 markdown bullet 形式
        normalized = []
        for it in items:
            if isinstance(it, dict):
                it = it.get('content', '') or it.get('text', '')
            it = str(it).strip()
            if not it:
                continue
            if not it.startswith('-'):
                it = '- ' + it
            normalized.append(it)
        new_by_section[section] = _filter_sensitive(normalized)

    pending_raw = distill_json.get('pending', [])
    if not isinstance(pending_raw, list):
        pending_raw = []
    new_pending = []
    for it in pending_raw:
        if isinstance(it, dict):
            it = it.get('content', '') or it.get('text', '')
        it = str(it).strip()
        if not it:
            continue
        if not it.startswith('-'):
            it = '- ' + it
        new_pending.append(it)
    new_pending = _filter_sensitive(new_pending)

    with _FILE_LOCK:
        existing_content = load_persona()
        parsed = _parse_persona(existing_content)

        # 合并各章节
        for section in SECTIONS:
            if '⏳' in section or '待确认' in section:
                existing_pending = parsed.get(section, {}).get('_items', [])
                merged_pending = _merge_pending(existing_pending, new_pending)
                parsed[section] = {'_items': merged_pending}
            else:
                parsed[section] = _merge_section(
                    parsed.get(section, {}),
                    new_by_section,
                    section,
                )

        rendered = _render_persona(parsed, main_speaker=main_speaker)

        # 原子写入：先写 .tmp 再 rename
        os.makedirs(os.path.dirname(PERSONA_PATH), exist_ok=True)
        tmp = PERSONA_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(rendered)
        os.replace(tmp, PERSONA_PATH)

    return True


def init_persona_template() -> bool:
    """
    创建空的 persona.md 模板文件。如果已存在则不覆盖。
    用于首次部署后让 LLM 看到一份合法骨架。
    """
    if os.path.exists(PERSONA_PATH):
        return False
    empty_parsed: Dict[str, Dict[str, List[str]]] = {}
    rendered = _render_persona(empty_parsed)
    os.makedirs(os.path.dirname(PERSONA_PATH), exist_ok=True)
    with open(PERSONA_PATH, 'w', encoding='utf-8') as f:
        f.write(rendered)
    return True


# ========== /persona IM 命令支持 ==========

def list_pending() -> List[str]:
    """
    返回当前 ## ⏳ 待确认 段的所有条目（按出现顺序）。
    供 /persona 命令展示带编号的列表。
    """
    parsed = _parse_persona(load_persona())
    pending_section = next(
        (k for k in parsed if '⏳' in k or '待确认' in k),
        None
    )
    if not pending_section:
        return []
    return parsed[pending_section].get('_items', [])


def confirm_pending(index: int, target_section: str = '## 工作偏好') -> Optional[str]:
    """
    一键升格：把 ## ⏳ 待确认 第 index (1-based) 条移入 target_section 的
    ### 手动校正 子段。

    target_section 必须是 SECTIONS 之一且非待确认段。
    若 index 越界或 target_section 非法，返回 None。
    成功返回被移动的条目内容。
    """
    if target_section not in SECTIONS:
        return None
    if '⏳' in target_section or '待确认' in target_section:
        return None

    with _FILE_LOCK:
        content = load_persona()
        parsed = _parse_persona(content)

        # 找到待确认段
        pending_section = next(
            (k for k in parsed if '⏳' in k or '待确认' in k),
            None
        )
        if not pending_section:
            return None
        pending_items = parsed[pending_section].get('_items', [])

        # 校验编号
        if index < 1 or index > len(pending_items):
            return None
        # 取出该条目（1-based）
        item = pending_items.pop(index - 1)
        parsed[pending_section]['_items'] = pending_items

        # 追加到目标段的 ### 手动校正
        target_data = parsed.setdefault(target_section, {})
        manual_items = target_data.setdefault('### 手动校正', [])
        # 避免重复
        norm_item = _normalize_item(item)
        if not any(_normalize_item(x) == norm_item for x in manual_items):
            manual_items.append(item)

        # 渲染 + 原子写
        main_speaker = _extract_main_speaker(content)
        rendered = _render_persona(parsed, main_speaker=main_speaker)
        tmp = PERSONA_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(rendered)
        os.replace(tmp, PERSONA_PATH)

    return item


def _extract_main_speaker(content: str) -> str:
    """从 persona.md 文档头部解析出"主用户: `xxx`" 字段（保留续写）"""
    if not content:
        return ''
    m = re.search(r'主用户:\s*`([^`]+)`', content)
    if m and m.group(1) != '未识别':
        return m.group(1)
    return ''

