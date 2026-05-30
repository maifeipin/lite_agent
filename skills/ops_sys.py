import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from skill_engine import skill
import subprocess

@skill(
    name='ops_sys_status',
    description='获取VPS系统状态，包括主机名、运行时间、系统负载、CPU使用率、内存和磁盘信息',
    params={
        'detail': {
            'type': 'boolean',
            'description': '是否返回详细信息（包含占用内存和CPU最高的进程列表）',
            'default': False
        }
    }
)
def ops_sys_status(detail: bool = False) -> str:
    def run(cmd, timeout=5):
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
            return r.stdout.strip() or r.stderr.strip()
        except subprocess.TimeoutExpired:
            return '(获取超时)'
        except Exception as e:
            return f'(获取失败: {e})'
    
    sections = []
    sections.append(f"主机名: {run('hostname')}")
    sections.append(f"运行时间: {run('uptime -p')}")
    
    cmd_load = "cat /proc/loadavg | awk '{print $1, $2, $3}'"
    sections.append(f"系统负载: {run(cmd_load)}")
    
    cmd_cpu = "top -bn1 | grep 'Cpu(s)' | awk '{printf \"用户%.1f%%, 系统%.1f%%, 空闲%.1f%%\", $2, $4, $8}'"
    sections.append(f"CPU使用: {run(cmd_cpu)}")
    
    cmd_mem = "free -h | awk 'NR==2{printf \"已用 %s / 总计 %s\", $3, $2}'"
    sections.append(f"物理内存: {run(cmd_mem)}")
    
    cmd_disk = "df -h / | awk 'NR==2{printf \"已用 %s / 总计 %s (%s)\", $3, $2, $5}'"
    sections.append(f"根目录磁盘: {run(cmd_disk)}")
    
    if detail:
        sections.append("\n--- 占用资源最高的进程 ---")
        sections.append(run("ps aux --sort=-%mem | head -n 6"))
    
    return '\n'.join(sections)
