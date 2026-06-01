import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from skill_engine import skill
import subprocess

@skill(
    name='ops_read_logs',
    description='读取 VPS 上指定的日志文件。可按关键字过滤',
    params={
        'log_path': {
            'type': 'string',
            'description': '日志文件的绝对路径，如 /var/log/syslog'
        },
        'keyword': {
            'type': 'string',
            'description': '可选的过滤关键字，只返回包含该关键字的行',
            'default': ''
        },
        'lines': {
            'type': 'integer',
            'description': '返回最近多少行日志',
            'default': 50
        }
    }
)
def ops_read_logs(log_path: str, keyword: str = '', lines: int = 50) -> str:
    # 路径安全检查
    if not os.path.isabs(log_path):
        return f'❌ 拒绝访问: 请提供绝对路径 ({log_path})'
    if not os.path.exists(log_path):
        return f'❌ 文件不存在: {log_path}'
    if not os.path.isfile(log_path):
        return f'❌ 不是一个有效的文件: {log_path}'
    
    try:
        if keyword:
            # 过滤关键字并取最后 N 行
            cmd = f"grep -i '{keyword}' '{log_path}' | tail -n {lines}"
        else:
            # 直接取最后 N 行
            cmd = f"tail -n {lines} '{log_path}'"
            
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        output = r.stdout.strip()
        
        if not output:
            if keyword:
                return f"日志中没有找到包含关键字 '{keyword}' 的行"
            else:
                return f"日志文件为空: {log_path}"
                
        return output
    except subprocess.TimeoutExpired:
        return '❌ 读取超时 (>10秒)'
    except Exception as e:
        return f'❌ 读取失败: {e}'

@skill(
    name='ops_read_journal',
    description='读取 VPS 上的 Systemd 服务日志 (journalctl)。当用户要求查看某个后台服务(如 feishu-bot, nginx)的日志时使用此技能。可按关键字过滤',
    params={
        'service_name': {
            'type': 'string',
            'description': '系统服务的名称，例如 feishu-bot, nginx, sshd 等'
        },
        'keyword': {
            'type': 'string',
            'description': '可选的过滤关键字，只返回包含该关键字的日志行',
            'default': ''
        },
        'lines': {
            'type': 'integer',
            'description': '返回最近多少行日志',
            'default': 50
        }
    }
)
def ops_read_journal(service_name: str, keyword: str = '', lines: int = 50) -> str:
    # 防止命令注入
    if not service_name.replace('-', '').replace('_', '').isalnum():
        return f'❌ 拒绝访问: 非法的服务名称 ({service_name})'
        
    try:
        if keyword:
            # 过滤关键字并取最后 N 行
            cmd = f"journalctl -u {service_name} --no-pager | grep -i '{keyword}' | tail -n {lines}"
        else:
            # 直接取最后 N 行
            cmd = f"journalctl -u {service_name} --no-pager | tail -n {lines}"
            
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        output = r.stdout.strip()
        
        if not output:
            if keyword:
                return f"服务 {service_name} 的日志中没有找到包含关键字 '{keyword}' 的行"
            else:
                return f"服务 {service_name} 目前没有可用的系统日志"
                
        return output
    except subprocess.TimeoutExpired:
        return '❌ 读取超时 (>10秒)'
    except Exception as e:
        return f'❌ 读取失败: {e}'
