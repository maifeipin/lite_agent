import sys, os, json
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.skill_engine import skill
from core.command_registry import slash_command

def check_deepseek_balance() -> str:
    """内部函数：查询 DeepSeek 余额"""
    try:
        from core import config_loader
        config = config_loader.load_config()
        api_key = config.get("llm", {}).get("api_key", "")
    except Exception as e:
        return f"❌ 无法读取配置文件: {e}"
        
    if not api_key:
        return "❌ 配置文件中缺少 llm.api_key"
        
    url = "https://api.deepseek.com/user/balance"
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}"
    })
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            if data.get("is_available"):
                balance_info = data.get("balance_infos", [])
                lines = ["✅ **DeepSeek 账户余额**"]
                total = 0.0
                for info in balance_info:
                    currency = info.get("currency", "")
                    amount = float(info.get("total_balance", "0"))
                    total += amount
                    lines.append(f"- {currency}: **{amount}**")
                if total <= 0:
                    lines.append("\n⚠️ **警告: 您的余额可能已耗尽，请及时充值！**")
                return "\n".join(lines)
            else:
                return f"⚠️ 获取到的数据异常: {json.dumps(data)}"
    except urllib.error.URLError as e:
        if hasattr(e, 'read'):
            err_data = e.read().decode('utf-8')
            return f"❌ 请求失败 (HTTP {e.code}): {err_data}"
        return f"❌ 请求 DeepSeek API 失败: {e}"
    except Exception as e:
        return f"❌ 解析余额数据失败: {e}"

slash_command('/balance', category='系统',
              description='查询 DeepSeek 账户余额',
              show_in_dashboard=False, guest_ok=False)(
    lambda agent, msg, args: check_deepseek_balance())

@skill(
    name='llm_check_balance',
    description='查询当前 DeepSeek AI 大模型账户的剩余余额。当你想知道 API 费用消耗情况时使用此技能。'
)
def llm_check_balance() -> str:
    return check_deepseek_balance()

@skill(
    name='llm_usage_report',
    description='统计并生成最近几天内的大模型 API 消耗量报表（区分不同的模型）。',
    params={
        'days': {
            'type': 'integer',
            'description': '回溯查询的天数，默认为 7 天。',
            'default': 7
        }
    }
)
def llm_usage_report(days: int = 7) -> str:
    import sqlite3
    import time
    
    db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sessions.db")
    if not os.path.exists(db_path):
        return "❌ 找不到数据库文件 data/sessions.db"
        
    start_time = time.time() - (days * 24 * 3600)
    
    try:
        with sqlite3.connect(db_path) as conn:
            table_check = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='api_usage_log'").fetchone()
            if not table_check:
                return "ℹ️ 数据库中尚未存在 API 使用量日志表 (可能是功能刚上线，还没有产生新调用)。"
                
            rows = conn.execute(
                "SELECT model, SUM(prompt_tokens), SUM(completion_tokens), SUM(total_tokens) "
                "FROM api_usage_log WHERE created_at >= ? GROUP BY model ORDER BY SUM(total_tokens) DESC",
                (start_time,)
            ).fetchall()
            
            if not rows:
                return f"ℹ️ 最近 {days} 天内没有任何 API 调用记录。"
                
            lines = [f"📊 **最近 {days} 天大模型 API 消耗统计**", "| 模型名称 | 提示词 Token | 生成 Token | 总 Token |", "|:---|---:|---:|---:|"]
            total_all = 0
            for row in rows:
                model = row[0] or "unknown"
                prompt = row[1] or 0
                comp = row[2] or 0
                total = row[3] or 0
                total_all += total
                lines.append(f"| `{model}` | {prompt:,} | {comp:,} | **{total:,}** |")
                
            lines.append(f"\n💡 **期间累计总消耗**: **{total_all:,}** Tokens")
            return "\n".join(lines)
    except Exception as e:
        return f"❌ 统计 API 消耗量失败: {e}"
