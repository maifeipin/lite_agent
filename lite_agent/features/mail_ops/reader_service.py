"""
Mail Ops 邮件读取、智能摘要与高优推送服务 (features.mail_ops.reader_service)。
从 skills/ops_mail_reader.py 迁入业务主体。
"""
import os
import sys
import json
import re
import subprocess
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from lite_agent.core.config_loader import load_config
from lite_agent.core.model_config import is_gemini_driver
from lite_agent.integrations.mail.process import run_process


def build_llm_pool_env(cfg: dict) -> dict:
    """构建 LLM 端点池环境变量字典"""
    models = cfg.get("llm", {}).get("models", {})
    pool_keys = cfg.get("llm", {}).get("mail_pool")
    if not pool_keys:
        return {}
    endpoints = []
    for k in pool_keys:
        m = models.get(k)
        if not m:
            continue
        driver = m.get("driver", "")
        provider = "gemini" if is_gemini_driver(driver) else "openai"
        # gemini_native 走 Gemini 原生 generateContent，不是 OpenAI 兼容格式；
        # 这里不再伪造 .../v1beta/openai，避免下游把原生端点当成兼容层处理。
        burl = m.get("base_url") or ("" if provider == "gemini" else "https://api.openai.com/v1")
        k_raw = m.get("api_key", "")
        if k_raw.startswith("${") and k_raw.endswith("}"):
            api_key = os.environ.get(k_raw[2:-1], "")
        else:
            api_key = k_raw
        if not api_key:
            continue
        # proxy 必须随端点下发：Gemini 等海外端点的出网要走代理，
        # 丢掉 proxy 会让子进程直连不可达地址，池里的 gemini 端点形同虚设。
        endpoints.append({'api_key': api_key, 'base_url': burl, 'provider': provider,
                          'model': m.get('model', ''), 'proxy': m.get('proxy', '')})
    if not endpoints:
        return {}
    return {'LLM_POOL': json.dumps(endpoints, ensure_ascii=False)}


def run_mail_reader_cmd(cmd_args: list, timeout=None) -> str:
    cfg = load_config() or {}
    if timeout is None:
        timeout = cfg.get('mail', {}).get('subprocess_timeout', 300)

    from lite_agent.integrations.mail.paths import get_parser_script_dir
    from lite_agent.security import SecurityGuard
    billing_dir = get_parser_script_dir()
    guard = SecurityGuard(cfg)
    if not guard.check_path(billing_dir):
        return f"❌ 拒绝访问未授权目录: {billing_dir}"

    from lite_agent.integrations.mail.client import MailCLI
    cli = MailCLI(script_dir=billing_dir)
    sec_error = cli._check_security()
    if sec_error:
        return "❌ " + "; ".join(sec_error['errors'])
    mail_client_py = os.path.join(billing_dir, "mail_client.py")
    if not os.path.exists(mail_client_py):
        return f"❌ 找不到邮件脚本: {mail_client_py}"

    llm_cfg = cfg.get("llm", {})
    default_model = llm_cfg.get("default", "flash")
    model_cfg = llm_cfg.get("models", {}).get(default_model, {})

    api_key_raw = model_cfg.get("api_key", "")
    if api_key_raw.startswith("${") and api_key_raw.endswith("}"):
        env_var_name = api_key_raw[2:-1]
        api_key = os.environ.get(env_var_name, "")
    else:
        api_key = api_key_raw

    base_url = model_cfg.get("base_url", "https://api.openai.com/v1")
    model = model_cfg.get("model", "")

    driver = model_cfg.get("driver", "")
    provider = "gemini" if is_gemini_driver(driver) else "openai"

    env = cli._build_env()
    if api_key:
        env["LLM_API_KEY"] = api_key
    if base_url:
        env["LLM_BASE_URL"] = base_url
    if provider:
        env["LLM_PROVIDER"] = provider
    if model:
        env["LLM_MODEL"] = model

    env.update(build_llm_pool_env(cfg))

    cmd = [sys.executable, mail_client_py] + cmd_args
    try:
        r = run_process(cmd, cwd=billing_dir, capture_output=True, text=True, encoding='utf-8', timeout=timeout, env=env)
        output = r.stdout.strip()
        if r.returncode != 0:
            return f"⚠️ 脚本执行出错 (代码 {r.returncode}):\n{r.stderr.strip()}"
        return output or "✅ 脚本执行成功，无额外输出。"
    except subprocess.TimeoutExpired:
        return f"❌ 脚本执行超时 (> {timeout}秒)"
    except Exception as e:
        return f"❌ 脚本调用失败: {e}"


def format_headers_table(headers_list: list) -> str:
    if not headers_list:
        return "📭 最近无任何邮件记录。"
    lines = [
        "| ID | 账户 | 发件人 | 主题 | 日期 | 状态 |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |"
    ]
    for h in headers_list:
        status_emoji = "✅" if h.get("status") == "processed" else ("🔕" if h.get("status") == "noise" else "⏳")
        lines.append(
            f"| {h.get('id')} | {h.get('account_name')} | {h.get('sender')} | {h.get('subject')} | {(h.get('email_date') or '')[:19]} | {status_emoji} {h.get('status')} |"
        )
    return "\n".join(lines)


def format_missed_list(missed_list: list) -> str:
    if not missed_list:
        return "✅ 没发现可能错过的非高优邮件。"
    lines = []
    for m in missed_list:
        lines.append(
            f"✉️ **[ID: {m.get('id')}] {m.get('subject')}**\n"
            f"👤 **发件人**: {m.get('sender')}\n"
            f"📅 **分类/日期**: {m.get('category')} / {m.get('email_date')}\n"
            f"📝 **摘要**: {m.get('summary')}\n"
        )
    return "\n---\n\n".join(lines)


def handle_large_content(text: str, title: str) -> str:
    if len(text) <= 2500:
        return text
    cfg = load_config() or {}
    hc = cfg.get("hedgedoc", {})
    if hc.get("enabled"):
        try:
            from lite_agent.core.utils.hedgedoc import upload_to_hedgedoc
            url = upload_to_hedgedoc(text, hc)
            if url:
                return f"📝 {title}超长，已为您自动上传至 HedgeDoc 保管：\n👉 [点击查看完整内容]({url})"
        except Exception as e:
            print(f"HedgeDoc upload failed: {e}")
    return text[:2400] + "\n\n...(内容超长已自动截断)"


def parse_email_datetime(date_str: str):
    if not date_str:
        return None
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(date_str)
        if dt:
            return dt.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        clean_str = date_str.split('(')[0].strip()
        dt = datetime.fromisoformat(clean_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str[:19], fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def parse_high_importance(res: str, max_days: int = 3) -> str:
    match = re.search(r'--- JSON_PUSH_START ---\n(.*?)\n--- JSON_PUSH_END ---', res, re.DOTALL)
    if not match:
        return ""
    try:
        summaries = json.loads(match.group(1))
    except Exception:
        return ""
    if not summaries:
        return ""

    now_utc = datetime.now(timezone.utc)
    cutoff_dt = now_utc - timedelta(days=max_days)

    card_lines = []
    for s in summaries:
        ed_str = s.get('email_date') or ""
        dt = parse_email_datetime(ed_str)
        if dt and dt < cutoff_dt:
            continue

        card_text = (
            f"✉️ **[ID: {s.get('id')}] [账户: {s.get('account_name', 'default')}] 邮件提炼：{s.get('subject', '无主题')}**\n"
            f"👤 **发件人**：{s.get('sender')}\n"
            f"📅 **时间/分类**：{s.get('email_date')} / {s.get('category')} (级别: `{s.get('importance')}`)\n"
            f"📝 **摘要**：{s.get('summary')}\n"
        )
        if s.get('actions'):
            card_text += "🔔 **待办行动**：\n"
            for idx, act in enumerate(s.get('actions'), 1):
                card_text += f"  {idx}. {act}\n"
        if s.get('deadline'):
            card_text += f"⏰ **截止时间**：{s.get('deadline')} (原文: {s.get('deadline_raw')})\n"
        card_text += f"💡 快捷操作: 回复 `/ok {s.get('id')}` 确认；回复 `/noise {s.get('id')}` 降噪此来源\n"
        card_lines.append(card_text)
    return "\n---\n\n".join(card_lines)


def mail_fetch_summaries(months: int = 1) -> str:
    res = run_mail_reader_cmd(["fetch_summaries", str(months)])
    high = parse_high_importance(res)
    if high:
        return high
    res = re.sub(r'\n--- JSON_PUSH_START ---\n.*?\n--- JSON_PUSH_END ---', '', res, flags=re.DOTALL)
    return res or "✅ 邮件同步完成，无高优先邮件。"


def mail_fetch_cron(agent=None, months: int = 1) -> str:
    res = mail_fetch_summaries(months=months)
    if res and res.startswith('✉️') and agent:
        try:
            from lite_agent.core.alerts import push_alert
            import time
            push_alert(agent, res, title='🔥 高优邮件推送', color='red',
                       dedup_key=f"high_prio:{int(time.time()//300)}")
        except Exception:
            pass
    return res


def mail_fetch_only(months: int = 1) -> str:
    res = run_mail_reader_cmd(["fetch_only", str(months)])
    return res or "✅ 拉取完成，无新增。"


def mail_llm_enrich(limit: int = 30, agent=None) -> str:
    res = run_mail_reader_cmd(["enrich_pending", str(limit)])
    high = parse_high_importance(res)
    if high and agent:
        try:
            from lite_agent.core.alerts import push_alert
            import time
            push_alert(agent, high, title='🔥 高优邮件推送', color='red',
                       dedup_key=f"high_prio:{int(time.time()//300)}")
        except Exception:
            pass
    return high or res


def mail_feedback_ok(summary_id: int) -> str:
    from lite_agent.integrations.mail.client import MailCLI
    cli = MailCLI()
    try:
        row = cli.mail_summary_get(summary_id)
        if not row:
            return f"❌ 未找到 ID={summary_id} 的邮件摘要记录。"
        cli.mail_mark_processed(summary_id)
        return f"✅ 已成功标记邮件 ID={summary_id} 为已处理状态 (processed)。"
    except Exception as exc:
        return f"❌ 标记邮件 ID={summary_id} 失败: {exc}"


def mail_feedback_noise(summary_id: int) -> str:
    from lite_agent.integrations.mail.client import MailCLI
    cli = MailCLI()
    try:
        row = cli.mail_summary_get(summary_id)
        if not row:
            return f"❌ 未找到 ID={summary_id} 的邮件摘要记录。"
        sender = row.get("sender") or ""
        from email.utils import parseaddr
        _, addr = parseaddr(sender)
        addr_l = addr.lower()
        local, domain = addr_l.split('@') if '@' in addr_l else (addr_l, '')
        protected_domains = ["qq.com", "gmail.com", "163.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com"]
        pattern_type = "sender_email" if domain in protected_domains or not domain else "sender_domain"
        pattern_value = addr_l if pattern_type == "sender_email" else domain
        cli.mail_noise_rule_add(pattern_type, pattern_value, summary_id=summary_id)
        return f"✅ 已学习降噪规则：类型={pattern_type}，规则值={pattern_value}。该邮件 ID={summary_id} 已标为 noise。"
    except Exception as exc:
        return f"❌ 学习降噪规则失败: {exc}"


def mail_delete_noise_rule(arg_val: str) -> str:
    from lite_agent.integrations.mail.client import MailCLI
    cli = MailCLI()
    try:
        pattern_value = None
        if arg_val.isdigit():
            summary_id = int(arg_val)
            row = cli.mail_summary_get(summary_id)
            if row:
                sender = row.get("sender") or ""
                from email.utils import parseaddr
                _, addr = parseaddr(sender)
                addr_l = addr.lower()
                local, domain = addr_l.split('@') if '@' in addr_l else (addr_l, '')
                protected_domains = ["qq.com", "gmail.com", "163.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com"]
                if domain in protected_domains or not domain:
                    pattern_value = addr_l
                else:
                    pattern_value = domain
            else:
                return f"❌ 未找到 ID={summary_id} 的邮件摘要记录，因此无法反查规则。"
        else:
            pattern_value = arg_val.strip().lower()

        if not pattern_value:
            return "❌ 无法解析待删除的降噪规则值。"

        data = cli.mail_noise_rule_delete(pattern_value)
        deleted = data.get("deleted", 0)
        if deleted:
            return f"✅ 已成功删除降噪过滤规则：{pattern_value}。"
        return f"❌ 未在数据库中找到匹配的过滤规则值：{pattern_value}。"
    except Exception as exc:
        return f"❌ 删除降噪规则失败: {exc}"


def mail_list_noise_rules() -> str:
    from lite_agent.integrations.mail.client import MailCLI
    cli = MailCLI()
    try:
        rules = cli.mail_noise_rules_list()
        if not rules:
            return "🔕 目前没有配置任何自定义的降噪规则。"
        lines = ["📋 **当前自定义降噪规则列表:**"]
        for idx, (p_type, p_val) in enumerate(rules, 1):
            lines.append(f"{idx}. [{p_type}] `{p_val}`")
        return "\n".join(lines)
    except Exception as exc:
        return f"❌ 获取降噪规则失败: {exc}"


def mail_headers(limit: int = 15) -> str:
    from lite_agent.integrations.mail.client import MailCLI
    cli = MailCLI()
    try:
        headers_list = cli.mail_headers(limit=limit)
        text = format_headers_table(headers_list)
        return handle_large_content(text, "邮件标题列表")
    except Exception as e:
        return f"❌ 查询标题列表失败: {e}"


def mail_missed(limit: int = 15) -> str:
    from lite_agent.integrations.mail.client import MailCLI
    cli = MailCLI()
    try:
        missed_list = cli.mail_missed(limit=limit)
        text = format_missed_list(missed_list)
        return handle_large_content(text, "可能错过的邮件")
    except Exception as e:
        return f"❌ 查询错过邮件失败: {e}"


def mail_view_original(account: str, uid: str) -> str:
    from lite_agent.integrations.mail.client import MailCLI
    cli = MailCLI()
    try:
        data = cli.view_original(account=account, uid=uid)
        if not data:
            return f"❌ 找不到该邮件原文 (Account: {account}, UID: {uid})"

        subject = data.get("subject", "无主题")
        sender = data.get("sender", "未知发件人")
        date = data.get("email_date", "")
        content = data.get("content") or "无邮件正文内容"

        md_text = f"# 邮件原文: {subject}\n\n**发件人**: {sender}\n**时间**: {date}\n\n---\n\n```html\n{content}\n```"

        cfg = load_config() or {}
        hc = cfg.get("hedgedoc", {})
        if hc.get("enabled"):
            try:
                from lite_agent.core.utils.hedgedoc import upload_to_hedgedoc
                url = upload_to_hedgedoc(md_text, hc)
                if url:
                    return f"✅ 邮件原文已为您生成 HedgeDoc 链接：\n👉 [点击查看完整原文]({url})"
            except Exception as e:
                print(f"HedgeDoc upload failed: {e}")

        return handle_large_content(md_text, "邮件原文")
    except Exception as e:
        return f"❌ 查看邮件原文失败: {e}"


def backfill_bodies() -> str:
    from lite_agent.integrations.mail.paths import get_parser_script_dir, get_statement_db_path
    from lite_agent.security import SecurityGuard
    billing_dir = get_parser_script_dir()
    db_path = get_statement_db_path(script_dir=billing_dir)
    cfg = load_config() or {}
    guard = SecurityGuard(cfg)

    script_path = os.path.join(billing_dir, "scripts", "backfill_email_bodies.py")
    paths_to_check = [
        billing_dir,
        os.path.realpath(billing_dir),
        script_path,
        os.path.realpath(script_path) if os.path.lexists(script_path) else script_path,
        db_path,
        os.path.realpath(db_path) if os.path.lexists(db_path) else db_path,
        os.path.dirname(os.path.abspath(db_path)),
        os.path.realpath(os.path.dirname(os.path.abspath(db_path))),
    ]
    for p in paths_to_check:
        if not guard.check_path(p):
            return f"❌ 拒绝访问未授权目录: {p}"

    if not os.path.exists(script_path):
        return f"❌ 找不到回填脚本: {script_path}"

    cmd = [sys.executable, script_path]
    from lite_agent.integrations.mail.client import MailCLI
    env = MailCLI(script_dir=billing_dir, db_path=db_path)._build_env()
    try:
        r = run_process(cmd, cwd=billing_dir, capture_output=True, text=True, encoding='utf-8', timeout=600, env=env)
        output = r.stdout.strip()
        if r.returncode != 0:
            return f"⚠️ 回填脚本执行出错 (代码 {r.returncode}):\n{r.stderr.strip()}"
        return output or "✅ 历史邮件正文回填完成！"
    except subprocess.TimeoutExpired:
        return "❌ 回填脚本执行超时 (> 600秒)，请在服务器上手动运行。"
    except Exception as e:
        return f"❌ 脚本调用失败: {e}"
