#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lite Agent 模型安全切换工具 (CLI 交互版)
- 列出 conf.d/llm.json 中所有已配置的模型
- 标注当前在用的模型与各个配置文件中的引用分布
- 支持键盘方向键 (↑/↓) / j/k 移动光标选择，回车确认
- 一键原子更新 conf.d/llm.json, task_routing.json, task_specs.json
- 自动备份原配置并重启 lite-agent.service
"""

import os
import sys
import json
import shutil
import subprocess
import argparse
import copy
import tempfile
import time
import stat
import fcntl
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF_D = os.path.join(PROJECT_ROOT, "conf.d")

LLM_JSON = os.path.join(CONF_D, "llm.json")
ROUTING_JSON = os.path.join(CONF_D, "task_routing.json")
SPECS_JSON = os.path.join(CONF_D, "task_specs.json")
COMMITTEE_JSON = os.path.join(CONF_D, "committee.json")


def load_json(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    bak = path + ".bak"
    if os.path.exists(path):
        shutil.copy2(path, bak)
    atomic_write(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())


def atomic_write(path, content):
    mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o600
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def get_key_unix():
    import termios
    import tty
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch1 = sys.stdin.read(1)
        if ch1 == '\x1b':
            import select
            if not select.select([sys.stdin], [], [], 0.1)[0]:
                return 'ESC'
            ch2 = sys.stdin.read(1)
            if ch2 == '[':
                ch3 = sys.stdin.read(1)
                if ch3 == 'A':
                    return 'UP'
                elif ch3 == 'B':
                    return 'DOWN'
                elif ch3 == 'C':
                    return 'RIGHT'
                elif ch3 == 'D':
                    return 'LEFT'
            return 'ESC'
        elif ch1 in ('\r', '\n'):
            return 'ENTER'
        elif ch1 in ('q', 'Q', '\x03'):
            return 'QUIT'
        elif ch1 in ('k', 'K'):
            return 'UP'
        elif ch1 in ('j', 'J'):
            return 'DOWN'
        return ch1
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def main():
    parser = argparse.ArgumentParser(description="切换主模型及路由模型；失败恢复原配置")
    parser.add_argument("target", nargs="?")
    parser.add_argument("--yes", action="store_true", help="无需交互确认")
    parser.add_argument("--no-restart", action="store_true", help="仅更新配置，不重启")
    options = parser.parse_args()
    def switch(target):
        if not apply_switch(target, llm_cfg, routing_cfg, specs_cfg, committee_cfg,
                            yes=options.yes, restart=not options.no_restart):
            raise SystemExit(1)
    if not os.path.exists(LLM_JSON):
        print(f"❌ 找不到配置文件: {LLM_JSON}")
        sys.exit(1)

    llm_cfg = load_json(LLM_JSON)
    routing_cfg = load_json(ROUTING_JSON)
    specs_cfg = load_json(SPECS_JSON)
    committee_cfg = load_json(COMMITTEE_JSON)

    current_default = llm_cfg.get("default", "")
    models_dict = llm_cfg.get("models", {})
    model_keys = list(models_dict.keys())

    if not model_keys:
        print("❌ 未在 llm.json 中找到任何模型配置！")
        sys.exit(1)

    # 引用分析
    def get_references(name):
        refs = []
        if current_default == name:
            refs.append("主默认(default)")
        if routing_cfg.get("planner_model") == name:
            refs.append("规划器(planner)")
        if routing_cfg.get("classifier_model") == name:
            refs.append("分类器(classifier)")
        routes_matched = []
        for r in routing_cfg.get("route_rules", []):
            if r.get("model") == name:
                routes_matched.append(r.get("type", ""))
        if routes_matched:
            refs.append(f"路由规则[{','.join(routes_matched)}]")
        if specs_cfg.get("author_model") == name:
            refs.append("TaskSpec起草")
        if specs_cfg.get("validator_model") == name:
            refs.append("TaskSpec校验")
        if name in committee_cfg.get("models", []):
            refs.append("会审评委")
        return refs

    # 若指定了命令行参数，支持直接非交互执行: ./switch_model.py flash
    if options.target:
        target = options.target.strip()
        if target not in model_keys:
            print(f"❌ 模型 '{target}' 不在可用模型列表中！")
            print(f"可用模型: {', '.join(model_keys)}")
            sys.exit(1)
        switch(target)
        return

    # 交互式选择菜单
    selected_idx = 0
    if current_default in model_keys:
        selected_idx = model_keys.index(current_default)

    is_tty = sys.stdin.isatty()

    if not is_tty:
        print("当前非交互式 TTY 终端，使用序号选择模式：")
        for i, k in enumerate(model_keys, 1):
            cur = "[当前生效]" if k == current_default else ""
            m_info = models_dict[k].get("model", k)
            print(f"{i}. {k} ({m_info}) {cur}")
        try:
            choice = input(f"请输入要切换的模型编号 (1-{len(model_keys)}): ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(model_keys):
                switch(model_keys[idx])
            else:
                print("无效输入")
        except Exception as e:
            print(f"操作已取消: {e}")
        return

    # ANSI 终端光标切换界面
    while True:
        sys.stdout.write("\033[2J\033[H")
        print("============================================================")
        print("            Lite Agent 模型安全切换工具 (CLI)               ")
        print("============================================================")
        print(f"当前全局生效模型: \033[1;32m{current_default}\033[0m")
        cur_refs = get_references(current_default)
        if cur_refs:
            print(f"当前引用分布: {', '.join(cur_refs)}")
        print("------------------------------------------------------------")
        print("操作说明: 使用 \033[1;36m↑/↓\033[0m 或 \033[1;36mj/k\033[0m 移动光标，\033[1;32mEnter\033[0m 确认切换，\033[1;31mQ\033[0m 退出:\n")

        for i, k in enumerate(model_keys):
            m_cfg = models_dict[k]
            real_model = m_cfg.get("model", k)
            driver = m_cfg.get("driver", "openai")
            is_cur = (k == current_default)
            check = "\033[1;32m[✓]\033[0m" if is_cur else "[ ]"

            note = ""
            if "deepseek" in k or "flash" in k:
                note = " \033[1;33m(推荐: DeepSeek V4 Flash 现成密钥)\033[0m"
            elif "gemini" in k:
                note = " \033[90m(需 SOCKS5 代理)\033[0m"

            if i == selected_idx:
                sys.stdout.write(f" \033[1;36m➜ {check} {k:<15} ({real_model}, {driver}){note}\033[0m\n")
            else:
                sys.stdout.write(f"   {check} {k:<15} ({real_model}, {driver}){note}\n")

        print("------------------------------------------------------------")
        chosen_k = model_keys[selected_idx]
        chosen_refs = get_references(chosen_k)
        print(f"光标选定模型: \033[1m{chosen_k}\033[0m")
        if chosen_refs:
            print(f"现有引用分布: {', '.join(chosen_refs)}")
        else:
            print("现有引用分布: 暂无主流程引用（切换后将升级为全局主模型）")

        sys.stdout.flush()

        key = get_key_unix()
        if key == 'UP':
            selected_idx = (selected_idx - 1) % len(model_keys)
        elif key == 'DOWN':
            selected_idx = (selected_idx + 1) % len(model_keys)
        elif key == 'ENTER':
            target = model_keys[selected_idx]
            switch(target)
            break
        elif key in ('QUIT', 'ESC'):
            print("\n已取消切换。")
            break


def restart_service():
    command = ["systemctl"] if os.geteuid() == 0 else ["sudo", "-n", "systemctl"]
    subprocess.run(command + ["restart", "lite-agent.service"], check=True,
                   capture_output=True, text=True, timeout=45)
    # A restart return code alone does not establish that the process stayed up.
    for _ in range(5):
        time.sleep(1)
        subprocess.run(command + ["is-active", "--quiet", "lite-agent.service"],
                       check=True, capture_output=True, timeout=10)


def apply_switch(target_model, llm_cfg, routing_cfg, specs_cfg, committee_cfg,
                 *, yes=False, restart=True):
    if target_model not in llm_cfg.get("models", {}):
        raise ValueError(f"未知模型: {target_model}")
    print(f"准备切换主模型及路由模型为 {target_model}；保留会审评委配置。")
    if not yes and input("确认应用？[Y/n]: ").strip().lower() not in ("", "y", "yes"):
        return False
    do_restart = restart and (yes or input("立即重启服务？[Y/n]: ").strip().lower() in ("", "y", "yes"))
    updates = {LLM_JSON: copy.deepcopy(llm_cfg)}
    updates[LLM_JSON]["default"] = target_model
    if routing_cfg:
        routing = copy.deepcopy(routing_cfg)
        routing["planner_model"] = routing["classifier_model"] = target_model
        for rule in routing.get("route_rules", []):
            rule["model"] = target_model
            allowed = rule.setdefault("allowed_models", [])
            if target_model not in allowed:
                allowed.append(target_model)
        updates[ROUTING_JSON] = routing
    if specs_cfg:
        specs = copy.deepcopy(specs_cfg)
        specs["author_model"] = specs["validator_model"] = target_model
        updates[SPECS_JSON] = specs
    with open(os.path.join(CONF_D, ".switch-model.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        originals = {path: Path(path).read_bytes() if os.path.exists(path) else None for path in updates}
        # Abort if another writer changed config since the menu was loaded.
        expected = {LLM_JSON: llm_cfg, ROUTING_JSON: routing_cfg, SPECS_JSON: specs_cfg}
        if any(load_json(path) != expected[path] for path in updates):
            raise RuntimeError("配置已变化，请重新运行切换脚本")
        try:
            for path, data in updates.items():
                save_json(path, data)
            if do_restart:
                restart_service()
        except (Exception, KeyboardInterrupt) as exc:
            failures = []
            for path, content in originals.items():
                try:
                    if content is None:
                        if os.path.exists(path):
                            os.unlink(path)
                    else:
                        atomic_write(path, content)
                except Exception as rollback_error:
                    failures.append(str(rollback_error))
            if do_restart:
                try:
                    restart_service()
                except Exception as rollback_error:
                    failures.append(str(rollback_error))
            print(f"切换失败：{type(exc).__name__}；已尝试恢复原配置。")
            if failures:
                print("恢复未完全成功，请检查配置与服务状态。")
            return False
    if do_restart:
        print(f"已更新为 {target_model}；服务连续 5 次状态检查通过。模型实际可调用性需发消息验证。")
    else:
        print(f"已保存 {target_model} 配置；需手动重启服务生效。")
    return True


if __name__ == "__main__":
    main()
