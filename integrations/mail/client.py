import os
import sys
import json
import subprocess
from typing import List, Dict, Any, Optional, Tuple

from integrations.mail.process import run_process
from core import config_loader
from core.utils.masker import mask_secrets


from integrations.mail.paths import get_parser_script_dir, get_statement_db_path


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
        if script_dir:
            self.script_dir = os.path.abspath(os.path.expanduser(script_dir))
        else:
            self.script_dir = get_parser_script_dir()

        if db_path:
            self.db_path = os.path.abspath(os.path.expanduser(db_path))
        else:
            self.db_path = get_statement_db_path(script_dir=self.script_dir)

    def _build_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        target_db = os.path.abspath(self.db_path)
        env['MAIL_STATEMENT_DB_PATH'] = target_db
        env['MAIL_STATEMENT_DATA_ROOT'] = os.path.dirname(target_db)
        env.pop('MAIL_STATEMENT_REPORT_DIR', None)
        env.pop('MAIL_STATEMENT_DOWNLOAD_DIR', None)
        return env

    def _resolve_script_path(self) -> str:
        cli_py = os.path.join(self.script_dir, "mail_client.py")
        if not os.path.exists(cli_py):
            raise FileNotFoundError(f"找不到 mail_client.py: {cli_py}")
        return cli_py

    def _check_security(self) -> Optional[Dict[str, Any]]:
        """检查脚本目录、可执行文件、数据库及数据根是否在安全沙箱内。"""
        cfg = config_loader.load_config() or {}
        from security import SecurityGuard
        guard = SecurityGuard(cfg)
        if not guard.sandbox_paths:
            return None

        cli_py = os.path.join(self.script_dir, "mail_client.py")
        paths_to_check = [
            self.script_dir,
            os.path.realpath(self.script_dir),
            cli_py,
            os.path.realpath(cli_py) if os.path.lexists(cli_py) else cli_py,
            self.db_path,
            os.path.realpath(self.db_path) if os.path.lexists(self.db_path) else self.db_path,
            os.path.dirname(os.path.abspath(self.db_path)),
            os.path.realpath(os.path.dirname(os.path.abspath(self.db_path))),
        ]
        for p in paths_to_check:
            if not guard.check_path(p):
                return {
                    "schema_version": 1,
                    "command": "",
                    "status": "error",
                    "data": None,
                    "errors": [f"安全沙箱拒绝访问未授权路径: {p}"]
                }
        return None

    def run(self, command: str, args: Optional[List[str]] = None, timeout: int = 60) -> Dict[str, Any]:
        """
        执行外部命令并解析统一 envelope:
        {"schema_version": 1, "command": ..., "status": "ok"|"error", "data": ..., "errors": [...]}
        """
        sec_err = self._check_security()
        if sec_err:
            sec_err["command"] = command
            return sec_err

        cli_py = self._resolve_script_path()
        cmd_args = [sys.executable, cli_py, command] + (args or [])
        env = self._build_env()
        try:
            res = run_process(
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

        MAX_OUTPUT_BYTES = 10 * 1024 * 1024
        stdout_bytes = res.stdout.encode('utf-8') if isinstance(res.stdout, str) else b''
        if len(stdout_bytes) > MAX_OUTPUT_BYTES:
            return {
                "schema_version": 1,
                "command": command,
                "status": "error",
                "data": None,
                "errors": [f"命令输出超出上限 ({len(stdout_bytes)} > {MAX_OUTPUT_BYTES} 字节)"]
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
                or 'data' not in parsed_json
                or (parsed_json.get('status') == 'ok' and not isinstance(parsed_json.get('data'), dict))):
            return {
                "schema_version": 1,
                "command": command,
                "status": "error",
                "data": parsed_json if isinstance(parsed_json, dict) else None,
                "errors": ["输出不符合统一 Envelope 规范"]
            }

        parsed_json.setdefault("data", None)

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

    def identity_preview(
        self,
        statement_id: int,
        account: str,
        transport_uid: str,
        bank: str,
        transport_kind: str = "pop3_uidl",
        eml_path: Optional[str] = None
    ) -> Dict[str, Any]:
        """生成身份关联预览计划"""
        if not isinstance(statement_id, int) or isinstance(statement_id, bool) or statement_id <= 0:
            raise ValueError("statement_id 必须为正整数")
        args = [
            "--statement-id", str(statement_id),
            "--account", str(account),
            "--transport-kind", str(transport_kind),
            "--transport-uid", str(transport_uid),
            "--bank", str(bank),
        ]
        if eml_path:
            args.extend(["--eml", str(eml_path)])
        envelope = self.run("identity_preview", args)
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["identity_preview 执行失败"])
            raise MailCLIError(errors)
        return envelope.get("data") or {}

    def identity_confirm(
        self,
        plan_path: str,
        plan_hash: str,
        actor: str,
        reason: str
    ) -> Dict[str, Any]:
        """显式执行人工身份关联确认"""
        if not actor or not actor.strip():
            raise ValueError("actor 必须为非空字符串")
        if not reason or not reason.strip():
            raise ValueError("reason 必须为非空字符串")
        args = [
            "--plan", str(plan_path),
            "--plan-hash", str(plan_hash),
            "--actor", str(actor),
            "--reason", str(reason),
        ]
        envelope = self.run("identity_confirm", args)
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["identity_confirm 执行失败"])
            raise MailCLIError(errors)
        return envelope.get("data") or {}

    def identity_status(
        self,
        statement_id: Optional[int] = None,
        account: Optional[str] = None,
        transport_uid: Optional[str] = None
    ) -> Dict[str, Any]:
        """查询身份关联状态与审计日志"""
        args = []
        if statement_id:
            args.extend(["--statement-id", str(statement_id)])
        if account:
            args.extend(["--account", str(account)])
        if transport_uid:
            args.extend(["--transport-uid", str(transport_uid)])
        envelope = self.run("identity_status", args)
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["identity_status 执行失败"])
            raise MailCLIError(errors)
        return envelope.get("data") or {}

    def mail_stats(self, timeout: float = 30) -> Dict[str, Any]:
        """获取邮件处理统计（只读 readmodel）"""
        envelope = self.run("mail_stats", [], timeout=timeout)
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["获取邮件统计失败"])
            raise MailCLIError(errors)
        return envelope.get("data") or {}

    def mail_search(self, keyword: str, limit: int = 20, account_name: Optional[str] = None, timeout: float = 30, *, offset: int = 0) -> List[Dict[str, Any]]:
        """全文检索邮件（只读 readmodel）"""
        args = ["--keyword", str(keyword), "--limit", str(limit), "--offset", str(offset)]
        if account_name:
            args.extend(["--account", str(account_name)])
        envelope = self.run("mail_search", args, timeout=timeout)
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["搜索邮件失败"])
            raise MailCLIError(errors)
        data = envelope.get("data") or {}
        return data.get("results") or []

    def mail_summary_get(self, summary_id: int, timeout: float = 30) -> Optional[Dict[str, Any]]:
        """获取单条邮件摘要详情"""
        envelope = self.run("mail_summary_get", ["--id", str(summary_id)], timeout=timeout)
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["获取邮件摘要失败"])
            raise MailCLIError(errors)
        data = envelope.get("data") or {}
        return data.get("summary")

    def mail_mark_processed(self, summary_id: int, timeout: float = 30) -> Dict[str, Any]:
        """标记邮件为已处理状态"""
        envelope = self.run("mail_mark_processed", ["--id", str(summary_id)], timeout=timeout)
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["标记邮件处理状态失败"])
            raise MailCLIError(errors)
        return envelope.get("data") or {}

    def mail_noise_rules_list(self, timeout: float = 30) -> List[Tuple[str, str]]:
        """查询自定义降噪规则列表"""
        envelope = self.run("mail_noise_rules_list", [], timeout=timeout)
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["获取降噪规则失败"])
            raise MailCLIError(errors)
        data = envelope.get("data") or {}
        return [tuple(r) for r in data.get("rules") or []]

    def mail_noise_rule_add(self, pattern_type: str, pattern_value: str, summary_id: Optional[int] = None, timeout: float = 30) -> Dict[str, Any]:
        """添加自定义降噪规则"""
        args = ["--type", str(pattern_type), "--value", str(pattern_value)]
        if summary_id:
            args.extend(["--summary-id", str(summary_id)])
        envelope = self.run("mail_noise_rule_add", args, timeout=timeout)
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["添加降噪规则失败"])
            raise MailCLIError(errors)
        return envelope.get("data") or {}

    def mail_noise_rule_delete(self, pattern_value: str, pattern_type: Optional[str] = None, timeout: float = 30) -> Dict[str, Any]:
        """删除自定义降噪规则"""
        args = ["--value", str(pattern_value)]
        if pattern_type:
            args.extend(["--type", str(pattern_type)])
        envelope = self.run("mail_noise_rule_delete", args, timeout=timeout)
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["删除降噪规则失败"])
            raise MailCLIError(errors)
        return envelope.get("data") or {}

    def mail_headers(self, limit: int = 15, timeout: float = 30) -> List[Dict[str, Any]]:
        """获取最近邮件标题列表（Envelope 契约）"""
        envelope = self.run("mail_headers", ["--limit", str(limit)], timeout=timeout)
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["获取邮件标题列表失败"])
            raise MailCLIError(errors)
        data = envelope.get("data") or {}
        return data.get("headers") or []

    def mail_missed(self, limit: int = 15, timeout: float = 30) -> List[Dict[str, Any]]:
        """获取可能错过的非高优邮件列表（Envelope 契约）"""
        envelope = self.run("mail_missed", ["--limit", str(limit)], timeout=timeout)
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["获取可能错过的邮件失败"])
            raise MailCLIError(errors)
        data = envelope.get("data") or {}
        return data.get("missed") or []

    def view_original(self, account: str, uid: str, max_bytes: int = 50000, timeout: float = 30) -> Dict[str, Any]:
        """获取邮件原文（受控大小与只读保护）"""
        args = ["--account", str(account), "--uid", str(uid), "--max-bytes", str(max_bytes)]
        envelope = self.run("mail_view_original", args, timeout=timeout)
        if envelope.get("status") != "ok":
            errors = "; ".join(envelope.get("errors") or ["获取邮件原文失败"])
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
        sec_err = self._check_security()
        if sec_err:
            return {
                "exit_code": -1,
                "summary": None,
                "errors": sec_err["errors"],
                "masked_output": sec_err["errors"][0],
                "success": False,
            }

        cli_py = os.path.join(self.script_dir, "mail_client.py")
        cmd_args = [sys.executable, cli_py, "sync3m", str(months)]
        env = self._build_env()

        runner_fn = runner or run_process
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
