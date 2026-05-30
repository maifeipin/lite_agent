import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from skill_engine import skill
import subprocess
import re

@skill(
    name='ops_list_crontab',
    description='查看当前用户的 crontab 定时任务列表'
)
def ops_list_crontab() -> str:
    try:
        r = subprocess.run('crontab -l 2>/dev/null', shell=True, capture_output=True, text=True, timeout=5)
        output = r.stdout.strip()
        if not output:
            return '当前用户没有配置 crontab 定时任务'
        return output
    except Exception as e:
        return f'❌ 获取失败: {e}'


@skill(
    name='ops_run_script',
    description='执行 VPS 上的指定脚本文件（如备份脚本、证书检查脚本等），返回执行输出。仅限 .sh 和 .py 脚本',
    params={
        'script_path': {
            'type': 'string',
            'description': '脚本的绝对路径，如 /root/script/backup.sh'
        },
        'args': {
            'type': 'string',
            'description': '传递给脚本的参数（可选）',
            'default': ''
        }
    }
)
def ops_run_script(script_path: str, args: str = '') -> str:
    if not os.path.isabs(script_path):
        return '❌ 拒绝访问: 请提供绝对路径'
    if not os.path.exists(script_path):
        return f'❌ 脚本不存在: {script_path}'
    
    ext = os.path.splitext(script_path)[1].lower()
    if ext == '.sh':
        cmd = f"bash '{script_path}' {args}".strip()
    elif ext == '.py':
        cmd = f"python3 '{script_path}' {args}".strip()
    else:
        return f'❌ 安全拦截: 不支持的脚本类型 {ext}，仅支持执行 .sh 和 .py 脚本'
    
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        output = r.stdout.strip()
        stderr = r.stderr.strip()
        
        # 过滤掉 curl 的进度条噪音
        if stderr:
            filtered = []
            for line in stderr.splitlines():
                if re.match(r'^[\s\d:%\-a-zA-Z]+$', line) and ('Dload' in line or '% Total' in line):
                    continue
                filtered.append(line)
            
            if filtered:
                output += f"\n\n[标准错误输出 stderr]:\n" + '\n'.join(filtered)
                
        return output or f"✅ 脚本执行完成，无输出内容 (退出码: {r.returncode})"
    
    except subprocess.TimeoutExpired:
        return '❌ 脚本执行超时 (强制中断，超时时间 60s)'
    except Exception as e:
        return f'❌ 脚本执行失败: {e}'
