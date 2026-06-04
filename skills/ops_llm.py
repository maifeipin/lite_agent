import sys, os, json
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from skill_engine import skill

def check_deepseek_balance() -> str:
    """内部函数：查询 DeepSeek 余额"""
    try:
        import config_loader
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

@skill(
    name='llm_check_balance',
    description='查询当前 DeepSeek AI 大模型账户的剩余余额。当你想知道 API 费用消耗情况时使用此技能。'
)
def llm_check_balance() -> str:
    return check_deepseek_balance()
