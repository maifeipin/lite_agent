import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from skill_engine import skill
import subprocess

@skill(
    name='ops_security_audit',
    description='安全审查：检查最近的失败登录尝试、SSH爆破情况及当前连接',
    params={
        'hours': {
            'type': 'integer',
            'description': '检查最近多少小时内的登录记录',
            'default': 24
        }
    }
)
def ops_security_audit(hours: int = 24) -> str:
    def run(cmd, timeout=10):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return r.stdout.strip() or r.stderr.strip() or '(无结果)'
        except subprocess.TimeoutExpired:
            return '(超时)'
        except Exception as e:
            return f'(错误: {e})'
    
    sections = []
    
    # 成功登录
    sections.append("=== 最近10次成功登录 ===")
    sections.append(run("last -n 10 -a"))
    
    # 失败登录尝试
    sections.append("\n=== 最近失败登录尝试 ===")
    sections.append(run("lastb -n 20 2>/dev/null || echo '(需要root权限或无失败记录)'"))
    
    # SSH 爆破统计
    sections.append("\n=== SSH 失败来源 IP TOP10 ===")
    sections.append(run(
        "grep 'Failed password' /var/log/auth.log 2>/dev/null | "
        "awk '{print $(NF-3)}' | sort | uniq -c | sort -rn | head -n 10 || "
        "echo '(无法读取 auth.log 或无失败记录)'"
    ))
    
    # 当前连接
    sections.append("\n=== 当前在线用户 ===")
    sections.append(run("who"))
    
    return '\n'.join(sections)
