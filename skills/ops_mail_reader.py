import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.skill_engine import skill
from core.config_loader import load_config
import subprocess

# 由 main.py 注入，用于秒级推送高优邮件到 IM 通道
_agent = None


def _run_mail_reader_cmd(cmd_args: list, timeout=180) -> str:
    cfg = load_config() or {}
    
    # 从 billing 获取 script_dir
    billing_dir = cfg.get("billing", {}).get("script_dir", "/home/liteagent/mail-statement-parser")
    mail_client_py = os.path.join(billing_dir, "mail_client.py")
    
    if not os.path.exists(mail_client_py):
        return f"❌ 找不到邮件脚本: {mail_client_py}"
        
    # 获取默认 LLM 配置，供环境变量注入
    llm_cfg = cfg.get("llm", {})
    default_model = llm_cfg.get("default", "flash")
    model_cfg = llm_cfg.get("models", {}).get(default_model, {})
    
    # 解包环境变量以允许 ${VAR} 形式占位符替换
    api_key_raw = model_cfg.get("api_key", "")
    if api_key_raw.startswith("${") and api_key_raw.endswith("}"):
        env_var_name = api_key_raw[2:-1]
        api_key = os.environ.get(env_var_name, "")
    else:
        api_key = api_key_raw
        
    base_url = model_cfg.get("base_url", "https://api.openai.com/v1")
    model = model_cfg.get("model", "")
    
    # 简单的 provider 判定
    tags = model_cfg.get("tags", [])
    provider = "gemini" if "gemini" in tags or "generativelanguage" in base_url or "googleapis" in base_url else "openai"
    
    # 构建子进程环境变量
    env = dict(os.environ)
    if api_key:
        env["LLM_API_KEY"] = api_key
    if base_url:
        env["LLM_BASE_URL"] = base_url
    if provider:
        env["LLM_PROVIDER"] = provider
    if model:
        env["LLM_MODEL"] = model
        
    cmd = [sys.executable, mail_client_py] + cmd_args
    try:
        r = subprocess.run(cmd, cwd=billing_dir, capture_output=True, text=True, encoding='utf-8', timeout=timeout, env=env)
        output = r.stdout.strip()
        if r.returncode != 0:
            return f"⚠️ 脚本执行出错 (代码 {r.returncode}):\n{r.stderr.strip()}"
        return output or "✅ 脚本执行成功，无额外输出。"
    except subprocess.TimeoutExpired:
        return f"❌ 脚本执行超时 (> {timeout}秒)"
    except Exception as e:
        return f"❌ 脚本调用失败: {e}"


def _get_db_path_and_import_db():
    cfg = load_config() or {}
    billing_dir = cfg.get("billing", {}).get("script_dir", "/home/liteagent/mail-statement-parser")
    import sys
    if billing_dir not in sys.path:
        sys.path.insert(0, billing_dir)
    import statement_db  # type: ignore
    db_path = os.path.join(billing_dir, "statements.db")
    return db_path, statement_db


def _format_headers_table(headers_list: list) -> str:
    """把邮件标题列表转化为 Markdown 格式表格"""
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


def _format_missed_list(missed_list: list) -> str:
    """把可能错过的邮件列表转化为 Markdown 格式展示"""
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


def _handle_large_content(text: str, title: str) -> str:
    if len(text) <= 2500:
        return text
    cfg = load_config() or {}
    hc = cfg.get("hedgedoc", {})
    if hc.get("enabled"):
        try:
            from core.utils.hedgedoc import upload_to_hedgedoc
            url = upload_to_hedgedoc(text, hc)
            if url:
                return f"📝 {title}超长，已为您自动上传至 HedgeDoc 保管：\n👉 [点击查看完整内容]({url})"
        except Exception as e:
            print(f"HedgeDoc upload failed: {e}")
    return text[:2400] + "\n\n...(内容超长已自动截断)"


def _parse_high_importance(res: str) -> str:
    """从 mail_client.py fetch_summaries 输出中解析高优邮件并格式化推送卡片。"""
    import re, json
    match = re.search(r'--- JSON_PUSH_START ---\n(.*?)\n--- JSON_PUSH_END ---', res, re.DOTALL)
    if not match:
        return ""
    try:
        summaries = json.loads(match.group(1))
    except Exception:
        return ""
    if not summaries:
        return ""

    card_lines = []
    for s in summaries:
        card_text = (
            f"✉️ **[ID: {s.get('id')}] [账户: {s.get('account_name', 'default')}] 邮件提炼：{s.get('subject', '无主题')}**\n"
            f"👤 **发件人**：{s.get('sender')}\n"
            f"📅 **分类/级别**：{s.get('category')} / `{s.get('importance')}`\n"
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


@skill(
    name='mail_fetch_summaries',
    description='批量抓取最近几月的邮件，识别账单自动入库，并将通用邮件利用大模型生成分类和智能摘要。比较耗时，请耐心等待。',
    params={
        'months': {
            'type': 'integer',
            'description': '回溯抓取的月数，默认 1',
            'default': 1
        }
    }
)
def mail_fetch_summaries(months: int = 1) -> str:
    res = _run_mail_reader_cmd(["fetch_summaries", str(months)])
    high = _parse_high_importance(res)
    if high:
        return high
    # 无高优邮件时返回摘要行（去掉 JSON_PUSH 标记块）
    import re
    res = re.sub(r'\n--- JSON_PUSH_START ---\n.*?\n--- JSON_PUSH_END ---', '', res, flags=re.DOTALL)
    return res or "✅ 邮件同步完成，无高优先邮件。"


def mail_fetch_cron() -> str:
    """拉取+推送合体: POP3扫描→LLM处理→秒级推送。consecutive_skip保证无新邮件时5秒结束。"""
    res = mail_fetch_summaries(months=1)
    if res and res.startswith('✉️') and _agent:
        try:
            from core.alerts import push_alert
            import time
            push_alert(_agent, res, title='🔥 高优邮件推送', color='red',
                       dedup_key=f"high_prio:{int(time.time()//300)}")
        except Exception:
            pass
    return res


# push_unpushed_high 已废弃——拉取+推送应在同一周期, 见 mail_fetch_cron


def mail_feedback_ok(summary_id: int) -> str:
    db_path, sdb = _get_db_path_and_import_db()
    row = sdb.get_email_summary_by_id(db_path, summary_id)
    if not row:
        return f"❌ 未找到 ID={summary_id} 的邮件摘要记录。"
    
    success = sdb.update_summary_status(db_path, summary_id, 'processed')
    if success:
        return f"✅ 已成功标记邮件 ID={summary_id} 为已处理状态 (processed)。"
    return f"❌ 标记邮件 ID={summary_id} 失败。"


def mail_feedback_noise(summary_id: int) -> str:
    db_path, sdb = _get_db_path_and_import_db()
    row = sdb.get_email_summary_by_id(db_path, summary_id)
    if not row:
        return f"❌ 未找到 ID={summary_id} 的邮件摘要记录。"
    
    sender = row["sender"] or ""
    from email.utils import parseaddr
    _, addr = parseaddr(sender)
    addr_l = addr.lower()
    local, domain = addr_l.split('@') if '@' in addr_l else (addr_l, '')
    
    # 载入默认的 protected domains
    try:
        import mail_client  # type: ignore
        rules = mail_client.load_static_noise_rules()
    except Exception:
        rules = {"protected_domains": ["qq.com", "gmail.com", "163.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com"]}
        
    protected_domains = rules.get("protected_domains", [])
    
    pattern_type = "sender_email" if domain in protected_domains or not domain else "sender_domain"
    pattern_value = addr_l if pattern_type == "sender_email" else domain
    
    sdb.add_noise_rule(db_path, pattern_type, pattern_value)
    sdb.update_summary_status(db_path, summary_id, 'noise')
    
    return f"✅ 已学习降噪规则：类型={pattern_type}，规则值={pattern_value}。该邮件 ID={summary_id} 已标为 noise。"


def mail_delete_noise_rule(arg_val: str) -> str:
    db_path, sdb = _get_db_path_and_import_db()
    
    pattern_value = None
    if arg_val.isdigit():
        summary_id = int(arg_val)
        row = sdb.get_email_summary_by_id(db_path, summary_id)
        if row:
            sender = row["sender"] or ""
            from email.utils import parseaddr
            _, addr = parseaddr(sender)
            addr_l = addr.lower()
            local, domain = addr_l.split('@') if '@' in addr_l else (addr_l, '')
            
            try:
                import mail_client  # type: ignore
                rules = mail_client.load_static_noise_rules()
            except Exception:
                rules = {"protected_domains": ["qq.com", "gmail.com", "163.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com"]}
            protected_domains = rules.get("protected_domains", [])
            
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
        
    deleted1 = sdb.delete_noise_rule(db_path, "sender_domain", pattern_value)
    deleted2 = sdb.delete_noise_rule(db_path, "sender_email", pattern_value)
    
    if deleted1 or deleted2:
        return f"✅ 已成功删除降噪过滤规则：{pattern_value}。"
    return f"❌ 未在数据库中找到匹配的过滤规则值：{pattern_value}。"


def mail_list_noise_rules() -> str:
    db_path, sdb = _get_db_path_and_import_db()
    rules = sdb.load_noise_rules(db_path)
    if not rules:
        return "🔕 目前没有配置任何自定义的降噪规则。"
        
    lines = ["📋 **当前自定义降噪规则列表:**"]
    for idx, (p_type, p_val) in enumerate(rules, 1):
        lines.append(f"{idx}. [{p_type}] `{p_val}`")
    return "\n".join(lines)


@skill(
    name='mail_show_headers',
    description='查询最近的邮件标题列表',
    params={
        'limit': {
            'type': 'integer',
            'description': '显示最近的邮件封数，默认 15',
            'default': 15
        }
    }
)
def mail_show_headers(limit: int = 15) -> str:
    output = _run_mail_reader_cmd(["show_headers", str(limit)])
    try:
        import json
        headers_list = json.loads(output)
        text = _format_headers_table(headers_list)
        return _handle_large_content(text, "邮件标题列表")
    except Exception as e:
        return f"❌ 解析标题列表失败: {e}\n{output}"


@skill(
    name='mail_show_missed',
    description='查询最近 7 天可能错过的非高优邮件列表',
    params={
        'limit': {
            'type': 'integer',
            'description': '最大显示封数，默认 15',
            'default': 15
        }
    }
)
def mail_show_missed(limit: int = 15) -> str:
    output = _run_mail_reader_cmd(["show_missed", str(limit)])
    try:
        import json
        missed_list = json.loads(output)
        text = _format_missed_list(missed_list)
        return _handle_large_content(text, "可能错过的邮件")
    except Exception as e:
        return f"❌ 解析错过邮件失败: {e}\n{output}"
