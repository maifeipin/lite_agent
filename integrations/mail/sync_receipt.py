"""Pure extraction, validation, and error formatting for mail sync receipts."""
import json
import re
from typing import Optional, Dict, Any, List

from core.utils.masker import mask_secrets

_JSON_SUMMARY_RE = re.compile(
    r'--- JSON_SUMMARY_START ---\s*(.*?)\s*--- JSON_SUMMARY_END ---', re.S)


def extract_raw_json_summary(raw_output: str) -> Optional[str]:
    """提取 stdout 中的 JSON_SUMMARY 文本块（未解析字符串）。"""
    # Only the final summary is authoritative; never reuse an earlier successful
    # receipt if the final block is truncated or malformed.
    text = raw_output or ''
    start = text.rfind('--- JSON_SUMMARY_START ---')
    if start < 0:
        return None
    match = _JSON_SUMMARY_RE.match(text, start)
    return match.group(1).strip() if match else None


def parse_json_summary(raw_output: str) -> Optional[Dict[str, Any]]:
    """提取 mail_client.py sync3m 末尾的结构化回执；无回执或解析失败返回 None。"""
    raw_json = extract_raw_json_summary(raw_output)
    if not raw_json:
        return None
    try:
        summary = json.loads(raw_json)
        if (not isinstance(summary, dict) or not isinstance(summary.get('accounts'), list)
                or not summary['accounts'] or any(not isinstance(a, dict)
                or not isinstance(a.get('status'), str) for a in summary['accounts'])):
            return None
        for account in summary['accounts']:
            if not account['status'].strip():
                return None
            for name in ('errors','parse_failed','scanned','new_bills','new_summaries','inserted','skipped_dup',
                         'associated_existing','identity_conflict','insufficient_evidence'):
                value = account.get(name)
                if name in account and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                    return None
        return summary
    except (TypeError, ValueError):
        return None


def summarize_account_errors(summary: Optional[Dict[str, Any]]) -> List[str]:
    """从结构化回执中提取各账户错误/失败说明。"""
    errors = []
    for acct in (summary or {}).get('accounts') or []:
        name = acct.get('account') or '?'
        if acct.get('status') not in (None, 'ok'):
            errors.append(f"账户 {name} 同步状态 {acct['status']}：{acct.get('error', '')}")
        if acct.get('status') == 'ok' and acct.get('error'):
            errors.append(f"账户 {name} 返回错误详情：{acct['error']}")
        if acct.get('parse_failed'):
            errors.append(f"账户 {name} 解析失败 {acct['parse_failed']} 封")
        if acct.get('errors'):
            errors.append(f"账户 {name} 处理异常 {acct['errors']} 封")
    return errors


def _mask_receipt(value):
    """Sanitize values, not serialized JSON, so escaped quotes remain valid."""
    if isinstance(value, dict):
        return {key: ('***' if isinstance(child, str) and
                      re.search(r'password|token|secret|api_?key|access_key|bearer', key, re.I)
                      else _mask_receipt(child)) for key, child in value.items()}
    if isinstance(value, list):
        return [_mask_receipt(child) for child in value]
    return mask_secrets(value) if isinstance(value, str) else value


def evaluate_sync_receipt(
    returncode: int,
    stdout: str,
    stderr: str = "",
    command: str = "sync3m"
) -> Dict[str, Any]:
    """评估进程退出码、输出和结构化回执。
    无回执、坏回执、非零进程分别保留失败原因，脱敏密码/token。
    """
    raw_combined = (stdout or "") + ("\n" + stderr if stderr else "")
    def mask_block(match):
        try:
            payload = json.dumps(_mask_receipt(json.loads(match.group(1))), ensure_ascii=False)
        except (TypeError, ValueError):
            payload = '[invalid receipt omitted]'
        return '--- JSON_SUMMARY_START ---\n' + payload + '\n--- JSON_SUMMARY_END ---'

    safe_output = _JSON_SUMMARY_RE.sub(mask_block, raw_combined)
    last_start = safe_output.rfind('--- JSON_SUMMARY_START ---')
    if last_start >= 0 and '--- JSON_SUMMARY_END ---' not in safe_output[last_start:]:
        safe_output = safe_output[:last_start] + '[truncated receipt omitted]'
    masked_output = mask_secrets(safe_output)
    summary = parse_json_summary(stdout)
    errors: List[str] = []

    if returncode != 0:
        errors.append(f"{command} 返回非零退出码 {returncode}")

    if summary is None:
        reason = '无回执' if '--- JSON_SUMMARY_START ---' not in (stdout or '') else '回执损坏或结构非法'
        errors.append(f"{command} 缺少有效的账户同步回执（{reason}），无法确认同步完整性")
        # 旧版脚本或崩溃回退：从脱敏日志提取关键词兜底
        for line in masked_output.splitlines():
            line_str = line.strip()
            if any(word in line_str for word in ('无法连接', '鉴权/刷新失败', '处理失败', '解析失败')):
                if '处理失败 0' not in line_str:
                    errors.append(line_str)
    else:
        errors.extend(summarize_account_errors(summary))

    deduped_errors = list(dict.fromkeys(mask_secrets(str(e)) for e in errors))

    return {
        "summary": _mask_receipt(summary) if summary is not None else None,
        "errors": deduped_errors,
        "masked_output": masked_output,
        "has_receipt": summary is not None,
        "success": returncode == 0 and summary is not None and len(deduped_errors) == 0,
    }
