import os
import sys
import json
import subprocess
from typing import List, Dict, Any, Optional

from core import config_loader
from core.utils.masker import mask_secrets


class MailCLIError(RuntimeError):
    """Mail CLI 执行异常"""
    pass


class MailCLI:
    """
    对外部 mail-statement-parser CLI 的统一结构化封装。
    负责执行子进程、超时控制与版本化 JSON Envelope 校验，
    不读取任何邮箱凭据。
    """

    def __init__(self, script_dir: Optional[str] = None, db_path: Optional[str] = None):
        self.db_path = db_path
        if script_dir:
            self.script_dir = script_dir
        else:
            # 环境变量优先，方便本地单测覆盖
            env_dir = os.environ.get("MAIL_STATEMENT_PARSER_DIR")
            if env_dir and os.path.isdir(env_dir):
                self.script_dir = env_dir
            else:
                config = config_loader.load_config() or {}
                self.script_dir = (
                    config.get("billing", {}).get("script_dir")
                    or "/home/liteagent/mail-statement-parser"
                )

    def _resolve_script_path(self) -> str:
        cli_py = os.path.join(self.script_dir, "mail_client.py")
        if not os.path.exists(cli_py):
            raise FileNotFoundError(f"找不到 mail_client.py: {cli_py}")
        return cli_py

    def run(self, command: str, args: Optional[List[str]] = None, timeout: int = 60) -> Dict[str, Any]:
        """
        执行外部命令并解析统一 envelope:
        {"schema_version": 1, "command": ..., "status": "ok"|"error", "data": ..., "errors": [...]}
        """
        cli_py = self._resolve_script_path()
        cmd_args = [sys.executable, cli_py, command] + (args or [])
        env = os.environ.copy()
        if self.db_path is not None:
            env['MAIL_STATEMENT_DB_PATH'] = os.path.abspath(self.db_path)
        try:
            res = subprocess.run(
                cmd_args,
                cwd=self.script_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "schema_version": 1,
                "command": command,
                "status": "error",
                "data": None,
                "errors": [f"命令执行超时 ({timeout}s): {exc}"]
            }
        except Exception as exc:
            return {
                "schema_version": 1,
                "command": command,
                "status": "error",
                "data": None,
                "errors": [f"子进程启动失败: {exc}"]
            }

        stdout = res.stdout.strip()
        if not stdout:
            err = mask_secrets(res.stderr.strip()) or f"进程退出码 {res.returncode}，无标准输出"
            return {
                "schema_version": 1,
                "command": command,
                "status": "error",
                "data": None,
                "errors": [err]
            }

        # 从输出尾部寻找合法的 JSON 行（防止上游打印了调试前缀）
        parsed_json = None
        lines = stdout.splitlines()
        for line in reversed(lines):
            line_str = line.strip()
            if line_str.startswith("{") and line_str.endswith("}"):
                try:
                    parsed_json = json.loads(line_str)
                    break
                except Exception:
                    pass

        if parsed_json is None:
            try:
                parsed_json = json.loads(stdout)
            except Exception as exc:
                return {
                    "schema_version": 1,
                    "command": command,
                    "status": "error",
                    "data": None,
                    "errors": [f"无法解析 JSON 回执: {exc}", f"原始输出: {mask_secrets(stdout[:300])}"]
                }

        # 确保满足 envelope 契约
        if (not isinstance(parsed_json, dict) or parsed_json.get('schema_version') != 1
                or parsed_json.get('command') != command
                or parsed_json.get('status') not in ('ok', 'error')
                or not isinstance(parsed_json.get('errors'), list)
                or any(not isinstance(err, str) for err in parsed_json.get('errors', []))
                or (parsed_json.get('status') == 'ok' and not isinstance(parsed_json.get('data'), dict))
                or 'data' not in parsed_json):
            return {
                "schema_version": 1,
                "command": command,
                "status": "error",
                "data": parsed_json,
                "errors": ["输出不符合统一 Envelope 规范"]
            }

        if res.returncode != 0 and parsed_json.get("status") == "ok":
            parsed_json["status"] = "error"
            parsed_json.setdefault("errors", []).append(f"进程返回非零退出码: {res.returncode}")

        return parsed_json

    def quality_events(self, limit: int = 100, timeout: float = 60) -> List[Dict[str, Any]]:
        """只读获取未确认的质量事件"""
        envelope = self.run("quality_events", ["--limit", str(limit), "--kind", "quality"], timeout=timeout)
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["获取质量事件失败"])
            raise MailCLIError(errors)
        data = envelope.get("data") or {}
        if not isinstance(data, dict) or not isinstance(data.get('events'), list):
            raise MailCLIError('Invalid quality events payload')
        return data['events']

    def quality_ack(self, event_id: str, todo_id: str, revision: Optional[int] = None, timeout: float = 60) -> Dict[str, Any]:
        """幂等确认已落库/处理的质量事件（支持单调 revision 条件确认）"""
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise ValueError('quality_ack requires a positive revision')
        args = ["--event-id", str(event_id), "--todo-id", str(todo_id)]
        if revision is not None:
            args.extend(["--revision", str(revision)])
        envelope = self.run("quality_ack", args, timeout=timeout)
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["确认质量事件失败"])
            raise MailCLIError(errors)
        return envelope.get("data") or {}

    def billing_snapshot(self, months: int = 1, detail_limit: int = 20) -> Dict[str, Any]:
        """获取版本化账单快照（只读 readmodel）"""
        envelope = self.run("billing_snapshot", ["--months", str(months), "--detail-limit", str(detail_limit)])
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["获取账单快照失败"])
            raise MailCLIError(errors)
        return envelope.get("data") or {}

    def billing_trends(self, months: int = 3, merchant: Optional[str] = None) -> Dict[str, Any]:
        """获取商户/币种统计趋势（只读 readmodel）"""
        args = ["--months", str(months)]
        if merchant:
            args.extend(["--merchant", str(merchant)])
        envelope = self.run("billing_trends", args)
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["获取账单趋势统计失败"])
            raise MailCLIError(errors)
        return envelope.get("data") or {}

    def mark_paid(
        self,
        bank_code: Optional[str] = None,
        statement_month: Optional[str] = None,
        statement_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """精确标记账单已还"""
        args = []
        if (isinstance(statement_id, int) and not isinstance(statement_id, bool)
                and statement_id > 0):
            args.extend(["--statement-id", str(statement_id)])
        else:
            raise ValueError("结构化适配器仅支持正整数 statement_id 还款标记")

        envelope = self.run("mark_paid", args)
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["标记还款状态失败"])
            raise MailCLIError(errors)
        return envelope.get("data") or {}

    def sync_fetch(
        self,
        months: int = 1,
        timeout: int = 300,
        runner: Optional[Any] = None
    ) -> Dict[str, Any]:
        """
        执行外部 sync3m 批量拉取命令并解析 tagged summary。
        与普通 JSON Envelope 协议分离，支持自定义 runner 与超时控制。
        """
        cli_py = os.path.join(self.script_dir, "mail_client.py")
        cmd_args = [sys.executable, cli_py, "sync3m", str(months)]
        env = os.environ.copy()
        if self.db_path is not None:
            env['MAIL_STATEMENT_DB_PATH'] = os.path.abspath(self.db_path)

        runner_fn = runner or subprocess.run
        try:
            res = runner_fn(
                cmd_args, cwd=self.script_dir, capture_output=True, text=True,
                timeout=timeout, env=env
            )
            from integrations.mail.sync_receipt import evaluate_sync_receipt
            eval_res = evaluate_sync_receipt(res.returncode, res.stdout, res.stderr, command="sync3m")
            return {
                "exit_code": res.returncode,
                "summary": eval_res["summary"],
                "errors": eval_res["errors"],
                "masked_output": eval_res["masked_output"],
                "success": eval_res["success"],
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "exit_code": -1,
                "summary": None,
                "errors": [f"sync3m: TimeoutExpired ({timeout}s)"],
                "masked_output": mask_secrets(f"[sync3m] TimeoutExpired: {exc}"),
                "success": False,
            }
        except Exception as exc:
            err_name = type(exc).__name__
            return {
                "exit_code": -1,
                "summary": None,
                "errors": [f"sync3m: {err_name}"],
                "masked_output": mask_secrets(f"[sync3m] {err_name}: {exc}"),
                "success": False,
            }
