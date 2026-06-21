#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Edge Sentinel 阶段二: 白名单全等 + 参数模板校验 (中枢/边缘共用)。

设计见 implementation_plan_phase2.md §3。纯 stdlib (仅 shlex),中枢 skill
预校验 + 边缘最终校验共用同一份逻辑,确保"所见即所签,所签即所执行"。

校验规则:
1. 拒绝一切 shell 元字符 (管道 | 重定向 > < ; & 反引号 $ () 换行) —— 热私钥
   绝不允许管道/拼接,需管道的走根私钥。
2. shlex.split 拆 cmd → [prog, *args]。
3. prog 全等匹配白名单某条目的 cmd。
4. 每个 token 落入允许集之一:
   - 独立 flag (-d/-h) ∈ allow_args
   - 带 value 的 flag (-i eth0): flag ∈ allow_args 且 value ∈ allow_if
   - cat 等带文件位置参: 位置参 ∈ allow_files
   - journalctl --since '1 hour ago': '1 hour ago' 作为一个 token,需 ∈ allow_args
5. 任一 token 不在允许集 → 拒。
"""
import shlex

# 严禁出现的 shell 元字符 —— 命中即拒,无论是否在白名单
_FORBIDDEN_CHARS = set('|;&`$()<>{}\n\r')


def _has_forbidden(cmd: str) -> bool:
    return any(ch in _FORBIDDEN_CHARS for ch in cmd)


def validate_cmd(cmd: str, whitelist: list) -> tuple:
    """校验一条命令是否被白名单允许。

    :param cmd: 原始命令字符串,如 "vnstat -d -i eth0"
    :param whitelist: 白名单列表,每项形如
        {"cmd": "vnstat", "allow_args": ["-d","-h"], "allow_if": ["eth0","eth1"]}
        可选键: allow_files (cat 类文件位置参白名单)
    :return: (ok: bool, reason: str)  ok=True 时 reason="ok"
    """
    if not cmd or not cmd.strip():
        return False, "空命令"
    if _has_forbidden(cmd):
        return False, "命令含禁用 shell 元字符 (热私钥不允许管道/重定向/拼接)"

    try:
        tokens = shlex.split(cmd)
    except ValueError as e:
        return False, f"命令解析失败: {e}"
    if not tokens:
        return False, "空命令"

    prog = tokens[0]
    entry = next((e for e in whitelist if e.get("cmd") == prog), None)
    if entry is None:
        return False, f"程序 '{prog}' 不在白名单"

    allow_args = set(entry.get("allow_args", []))
    allow_if = set(entry.get("allow_if", []))
    allow_files = set(entry.get("allow_files", []))

    # 带 value 的 flag 集合: 凡出现在 allow_args 里、且程序语义需要接 value 的 flag。
    value_flags = set(entry.get("value_flags", []))

    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in value_flags:
            # 下一个 token 是它的 value
            if i + 1 >= len(tokens):
                return False, f"flag '{tok}' 缺少参数"
            value = tokens[i + 1]
            
            # 特例: -n 允许纯数字
            if tok == "-n" and value.isdigit():
                i += 2
                continue
                
            # value 可能是网卡(-i eth0 → allow_if) 也可能是时间字面量(--since '1 hour ago' → allow_args)
            if value not in allow_if and value not in allow_args:
                return False, f"flag '{tok}' 的参数 '{value}' 不在 allow_if/allow_args 允许集"
            i += 2
            continue
            
        if tok in allow_args:
            i += 1
            continue
            
        # 位置参 (非 flag 开头) 逻辑
        if not tok.startswith("-"):
            if allow_if and tok in allow_if:
                i += 1
                continue
            if allow_files and tok in allow_files:
                i += 1
                continue
            if allow_files:
                return False, f"文件/位置参数 '{tok}' 不在 allow_files/allow_if 白名单 (防越权)"
                
        return False, f"参数 '{tok}' 不在允许集 (allow_args/allow_if/allow_files)"

    return True, "ok"


DEFAULT_WHITELIST = [
    {"cmd": "df", "allow_args": ["-h", "-i"]},
    {"cmd": "free", "allow_args": ["-m", "-h", "-g"]},
    {"cmd": "w", "allow_args": []},
    {"cmd": "uptime", "allow_args": []},
    {
        "cmd": "vnstat",
        "allow_args": ["-d", "-h", "-m", "-5", "--oneline"],
        "allow_if": ["eth0", "eth1", "ens3", "enp3s0"],
        "value_flags": ["-i", "--interface"],
    },
    {
        "cmd": "cat",
        "allow_args": [],
        "allow_files": [
            "/var/log/auth.log",
            "/var/log/secure",
            "/etc/ssh/sshd_config",
        ],
    },
    {
        "cmd": "journalctl",
        "allow_args": ["--no-pager", "-n", "today", "yesterday", "1 hour ago", "2 hours ago"],
        "allow_if": ["ssh", "sshd"],
        "value_flags": ["-u", "--since", "--unit", "-n"],
    },
    {"cmd": "ss", "allow_args": ["-tlnp", "-tunlp"]},
    {"cmd": "systemctl", "allow_args": ["status"], "allow_if": ["ssh", "sshd", "fail2ban"], "value_flags": []},
    {"cmd": "crontab", "allow_args": ["-l"]},
    {"cmd": "sha256sum", "allow_args": [], "allow_files": ["/etc/passwd", "/etc/ssh/sshd_config", "/root/.ssh/authorized_keys", "/opt/edge_sentinel/whitelist.json"]},
    {"cmd": "md5sum", "allow_args": [], "allow_files": ["/etc/passwd", "/etc/ssh/sshd_config", "/root/.ssh/authorized_keys", "/opt/edge_sentinel/whitelist.json"]},
    {"cmd": "tail", "allow_args": ["-n", "10", "20", "50", "100", "-f"], "allow_files": ["/var/log/auth.log", "/var/log/secure", "/var/log/syslog"]},
    {"cmd": "ls", "allow_args": ["-l", "-la", "-al", "-h", "-t", "-r"], "allow_files": ["/opt/edge_sentinel", "/var/log", "/etc/ssh"]}
]
