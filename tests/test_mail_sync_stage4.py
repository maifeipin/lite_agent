"""Stage 4 test suite: mail sync receipt parsing, bounded quality event draining, and unified fetch service."""
import json
import os
import sqlite3
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from core.billing_workflow import BillingWorkflow
from integrations.mail.client import MailCLI, MailCLIError
from integrations.mail.sync_receipt import (
    extract_raw_json_summary,
    parse_json_summary,
    summarize_account_errors,
    evaluate_sync_receipt,
)
from integrations.mail.sync_service import drain_quality_events, sync_and_consume
from skills import ops_billing, ops_todo
from tests.test_mail_quality_closeout import parser_repo


@pytest.fixture(autouse=True)
def isolate_default_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(ops_todo, 'DB_PATH', str(tmp_path / 'default-todos.db'))
    monkeypatch.setattr(ops_todo, 'MD_PATH', str(tmp_path / 'personal.md'))
    monkeypatch.setattr('integrations.mail.sync_service.config_loader.load_config', lambda: {})


# ============================================================================
# 1. sync_receipt.py 纯函数与边界测试
# ============================================================================

def test_extract_raw_json_summary_extracts_tagged_block():
    stdout = "prefix logs\n--- JSON_SUMMARY_START ---\n{\"accounts\": []}\n--- JSON_SUMMARY_END ---\nsuffix logs"
    assert extract_raw_json_summary(stdout) == '{"accounts": []}'
    assert extract_raw_json_summary("no summary here") is None
    assert extract_raw_json_summary("") is None


@pytest.mark.parametrize("payload,expected_valid", [
    ('{"accounts": [{"account": "cmb", "status": "ok"}]}', True),
    ('{"accounts": []}', False),
    ('{"accounts": ["bad"]}', False),
    ('{"accounts": [{}]}', False),
    ('[]', False),
    ('null', False),
    ('{}', False),
    ('{broken json', False),
])
def test_parse_json_summary_boundary_validation(payload, expected_valid):
    tagged = f"--- JSON_SUMMARY_START ---\n{payload}\n--- JSON_SUMMARY_END ---"
    res = parse_json_summary(tagged)
    if expected_valid:
        assert isinstance(res, dict)
        assert len(res["accounts"]) == 1
    else:
        assert res is None


def test_summarize_account_errors():
    summary = {
        "accounts": [
            {"account": "acc1", "status": "ok"},
            {"account": "acc2", "status": "failed", "error": "auth failed"},
            {"account": "acc3", "status": "ok", "parse_failed": 2},
            {"account": "acc4", "status": "ok", "errors": 3},
        ]
    }
    errors = summarize_account_errors(summary)
    assert len(errors) == 3
    assert any("acc2 同步状态 failed" in e for e in errors)
    assert any("acc3 解析失败 2 封" in e for e in errors)
    assert any("acc4 处理异常 3 封" in e for e in errors)


def test_evaluate_sync_receipt_masks_sensitive_info_and_keeps_reasons():
    raw_stdout = (
        "connecting to user@example.com with password: secret_token_123456\n"
        "--- JSON_SUMMARY_START ---\n"
        "{\"accounts\": [{\"account\": \"test\", \"status\": \"failed\", \"error\": \"bad auth with password: secret_token_123456\"}]}\n"
        "--- JSON_SUMMARY_END ---"
    )
    res = evaluate_sync_receipt(returncode=1, stdout=raw_stdout, stderr="stderr password: secret_token_123456")
    assert res["success"] is False
    assert res["has_receipt"] is True
    # 敏感信息脱敏验证
    for err in res["errors"]:
        assert "secret_token_123456" not in err
    assert "secret_token_123456" not in res["masked_output"]
    assert any("非零退出码 1" in e for e in res["errors"])
    assert any("test 同步状态 failed" in e for e in res["errors"])


def test_evaluate_sync_receipt_missing_receipt_fallback_to_masked_logs():
    raw_stdout = "无法连接 POP3 服务器 pop.test.invalid\n处理失败 1 封"
    res = evaluate_sync_receipt(returncode=0, stdout=raw_stdout)
    assert res["has_receipt"] is False
    assert res["success"] is False
    assert any("缺少有效的账户同步回执" in e for e in res["errors"])
    assert any("无法连接" in e for e in res["errors"])


# ============================================================================
# 2. drain_quality_events 批次、积压与防忙循环测试
# ============================================================================

def test_drain_quality_events_stops_on_empty_immediately():
    client = Mock()
    client.quality_events.return_value = []
    stats = drain_quality_events(client, event_limit=100, max_batches=3)
    assert stats["batches"] == 1
    assert stats["total"] == 0
    assert stats["acked"] == 0
    assert stats["errors"] == []
    assert not stats["backlog_hit"]


def test_drain_quality_events_stops_when_no_ack_progress_to_prevent_busy_loop(tmp_path):
    # 当队列中只有被跳过的事件（如还款事件 skipped=1, acked=0）时，必须单批中断
    client = Mock()
    client.quality_events.return_value = [
        {"event_id": "b1", "case_key": "k1", "event_type": "statement_recorded", "revision": 1, "cycle_id": 1, "diagnostics": []}
    ]
    todo_db = str(tmp_path / "todo.db")
    stats = drain_quality_events(client, todo_db_path=todo_db, event_limit=10, max_batches=3)
    # 只执行了 1 批就中断，没有执行满 3 批
    assert stats["batches"] == 1
    assert stats["total"] == 1
    assert stats["skipped"] == 1
    assert stats["acked"] == 0
    # 没有重复调用
    assert client.quality_events.call_count == 1


def test_drain_quality_events_detects_backlog_when_max_batches_hit():
    client = Mock()
    # 模拟每次返回满 batch (10 条)，且还有剩余
    client.quality_events.return_value = [
        {"event_id": f"e{i}", "case_key": f"k{i}", "event_type": "quality_resolved", "revision": 1, "cycle_id": 1, "diagnostics": []}
        for i in range(10)
    ]
    client.quality_ack.return_value = {"status": "ok", "acked": True}

    stats = drain_quality_events(client, event_limit=10, max_batches=2)
    assert stats["batches"] == 2
    assert stats["backlog_hit"] is True
    assert any("已达最大消费批次上限" in e for e in stats["errors"])



def test_drain_quality_events_aborts_on_write_failure(tmp_path):
    client = Mock()
    client.quality_events.return_value = [
        {"event_id": "e1", "case_key": "k1", "event_type": "quality_detected", "revision": 1, "cycle_id": 1, "diagnostics": []}
    ]
    with patch("integrations.mail.quality_todo.upsert_source_todo", side_effect=OSError("disk read-only")):
        stats = drain_quality_events(client, todo_db_path=str(tmp_path / "todo.db"), max_batches=3)
    assert stats["batches"] == 1
    assert stats["failed"] == 1
    assert stats["acked"] == 0
    client.quality_ack.assert_not_called()


# ============================================================================
# 3. sync_and_consume 与真实 CLI 联动（临时 parser DB + Todo DB）
# ============================================================================

def test_sync_and_consume_with_real_cli_and_quality_event_lifecycle(tmp_path, parser_repo):
    root, sd, models = parser_repo
    statement_db = str(tmp_path / "statements.db")
    todo_db = str(tmp_path / "todos.db")
    sd.init_db(statement_db)

    # 1. 模拟插入一个质量异常事件
    sd.upsert_quality_event(
        statement_db,
        models.QualityEventRecord(
            case_key="case-401",
            event_type="quality_detected",
            revision=1,
            cycle_id=1,
            bank_code="CMB",
            account_name="test-box",
            template_version="v1",
            diagnostics=[models.Diagnostic(code="CURRENCY_UNKNOWN", reason="missing currency")],
            evidence_refs=[{"account": "test-box", "uid": "u1", "message_id": "<u1@bank>"}]
        )
    )

    # 模拟外部 sync3m 成功输出
    valid_receipt = {
        "accounts": [
            {"account": "test-box", "status": "ok", "inserted": 1, "parse_failed": 0, "errors": 0}
        ]
    }
    stdout_payload = f"sync log\n--- JSON_SUMMARY_START ---\n{json.dumps(valid_receipt)}\n--- JSON_SUMMARY_END ---"
    mock_runner = Mock(return_value=SimpleNamespace(stdout=stdout_payload, stderr="", returncode=0))

    cli = MailCLI(str(root), db_path=statement_db)

    # 首次执行：抓取成功 + 质量事件入库并 ack
    res1 = sync_and_consume(
        script_dir=str(root),
        statement_db_path=statement_db,
        todo_db_path=todo_db,
        client=cli,
        runner=mock_runner
    )

    assert mock_runner.call_count == 1
    assert res1["fetch_errors"] == []
    q1 = res1["quality_stats"]
    assert q1["created"] == 1
    assert q1["acked"] == 1
    assert q1["failed"] == 0
    assert not res1["partial"]

    # 验证 Todo 数据库存在该待办
    with sqlite3.connect(todo_db) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM todos WHERE source_key='mail_quality:case-401'").fetchone()
        assert row is not None
        assert row["status"] == "pending"

    # 验证事件已被 ack（parser DB 中无未确认事件）
    assert sd.get_unacked_quality_events(statement_db) == []

    # 2. 幂等再次执行：无重复创建
    res2 = sync_and_consume(
        script_dir=str(root),
        statement_db_path=statement_db,
        todo_db_path=todo_db,
        client=cli,
        runner=mock_runner
    )
    assert res2["quality_stats"]["total"] == 0
    assert res2["quality_stats"]["created"] == 0

    # 3. 产生 quality_resolved 解决事件：再次消费后自动完成/关闭 Todo
    sd.upsert_quality_event(
        statement_db,
        models.QualityEventRecord(
            case_key="case-401",
            event_type="quality_resolved",
            revision=1,
            cycle_id=1,
            bank_code="CMB",
            account_name="test-box",
            diagnostics=[]
        )
    )

    res3 = sync_and_consume(
        script_dir=str(root),
        statement_db_path=statement_db,
        todo_db_path=todo_db,
        client=cli,
        runner=mock_runner
    )
    assert res3["quality_stats"]["resolved"] == 1
    assert res3["quality_stats"]["acked"] == 1

    with sqlite3.connect(todo_db) as conn:
        row = conn.execute("SELECT status FROM todos WHERE source_key='mail_quality:case-401'").fetchone()
        assert row[0] == "done"




def test_sync_and_consume_partial_failure_still_consumes_quality_events(tmp_path, parser_repo):
    root, sd, models = parser_repo
    statement_db = str(tmp_path / "statements.db")
    todo_db = str(tmp_path / "todos.db")
    sd.init_db(statement_db)

    # 先在 DB 中产生一个质量事件
    sd.upsert_quality_event(
        statement_db,
        models.QualityEventRecord(
            case_key="partial-case",
            event_type="quality_detected",
            revision=1,
            cycle_id=1,
            bank_code="ICBC",
            account_name="acc1",
            diagnostics=[models.Diagnostic(code="DATE_INVALID", reason="parse error")]
        )
    )

    # 模拟 fetch 过程中某个邮箱超时或返回 partial error
    partial_receipt = {
        "accounts": [
            {"account": "acc1", "status": "ok", "inserted": 1},
            {"account": "acc2", "status": "timeout", "error": "login timed out"}
        ]
    }
    stdout_payload = f"--- JSON_SUMMARY_START ---\n{json.dumps(partial_receipt)}\n--- JSON_SUMMARY_END ---"
    mock_runner = Mock(return_value=SimpleNamespace(stdout=stdout_payload, stderr="", returncode=0))
    cli = MailCLI(str(root), db_path=statement_db)

    res = sync_and_consume(
        script_dir=str(root),
        statement_db_path=statement_db,
        todo_db_path=todo_db,
        client=cli,
        runner=mock_runner
    )

    # 验证 fetch 报错被记录
    assert len(res["fetch_errors"]) > 0
    assert any("acc2 同步状态 timeout" in e for e in res["fetch_errors"])
    # 验证质量事件依然被独立消费成功
    assert res["quality_stats"]["created"] == 1
    assert res["quality_stats"]["acked"] == 1
    # 验证部分成功状态 partial 为 True
    assert res["partial"] is True


def test_sync_and_consume_fetch_timeout_still_drains_existing_events(tmp_path, parser_repo):
    root, sd, models = parser_repo
    statement_db = str(tmp_path / "statements.db")
    todo_db = str(tmp_path / "todos.db")
    sd.init_db(statement_db)

    sd.upsert_quality_event(
        statement_db,
        models.QualityEventRecord(
            case_key="timeout-case",
            event_type="quality_detected",
            revision=1,
            cycle_id=1,
            bank_code="HX",
            account_name="acc",
            diagnostics=[models.Diagnostic(code="CURRENCY_UNKNOWN", reason="unknown")]
        )
    )

    # 模拟 subprocess.TimeoutExpired
    mock_runner = Mock(side_effect=subprocess.TimeoutExpired(cmd="sync3m", timeout=300))
    cli = MailCLI(str(root), db_path=statement_db)

    res = sync_and_consume(
        script_dir=str(root),
        statement_db_path=statement_db,
        todo_db_path=todo_db,
        client=cli,
        runner=mock_runner
    )

    assert any("TimeoutExpired" in e for e in res["fetch_errors"])
    # 仍应消费已落库事件
    assert res["quality_stats"]["created"] == 1
    assert res["quality_stats"]["acked"] == 1


# ============================================================================
# 4. BillingWorkflow 与 ops_billing.billing_fetch 集成验证
# ============================================================================

def test_billing_workflow_fetch_delegates_to_sync_service(tmp_path):
    receipt = {
        "accounts": [
            {"account": "a", "status": "ok", "inserted": 1}
        ]
    }
    raw = f"--- JSON_SUMMARY_START ---\n{json.dumps(receipt)}\n--- JSON_SUMMARY_END ---"
    workflow = BillingWorkflow({}, Mock())
    with patch("core.billing_workflow.subprocess.run", return_value=SimpleNamespace(
            stdout=raw, stderr="", returncode=0)) as run_mock:
        with patch.object(MailCLI, "quality_events", return_value=[]):
            log, errors = workflow.fetch(str(tmp_path), 1)

    assert run_mock.call_count == 1
    assert errors == []
    assert "[sync3m]" in log


def test_billing_workflow_fetch_surfaces_quality_errors_without_masking_accounts(tmp_path):
    receipt = {
        "accounts": [
            {"account": "a", "status": "ok", "inserted": 1}
        ]
    }
    raw = f"--- JSON_SUMMARY_START ---\n{json.dumps(receipt)}\n--- JSON_SUMMARY_END ---"
    workflow = BillingWorkflow({}, Mock())
    with patch("core.billing_workflow.subprocess.run", return_value=SimpleNamespace(
            stdout=raw, stderr="", returncode=0)):
        with patch.object(MailCLI, "quality_events", side_effect=MailCLIError("quality read error")):
            log, errors = workflow.fetch(str(tmp_path), 1)

    assert any("quality read error" in e for e in errors)
    assert "[sync3m]" in log



def test_ops_billing_fetch_output_formatting(tmp_path, monkeypatch):
    monkeypatch.setattr(ops_billing, "BILLING_SCRIPT_DIR", str(tmp_path))
    # 模拟同步服务返回
    mock_res = {
        "fetch_log": "log",
        "fetch_errors": [],
        "summary": {
            "accounts": [
                {"account": "cmb", "status": "ok", "inserted": 2}
            ]
        },
        "quality_stats": {
            "total": 3,
            "created": 1,
            "updated": 0,
            "resolved": 1,
            "skipped": 1,
            "acked": 2,
            "failed": 0,
            "failed_acks": 0,
            "backlog_hit": True,
            "errors": []
        },
        "partial": False
    }

    with patch("integrations.mail.sync_service.sync_and_consume", return_value=mock_res):
        text = ops_billing.billing_fetch(months=1)

    assert "✅ 账单邮箱同步完成 (1 个账户)" in text
    assert "账户 cmb: 状态 ok, 新增 2 封" in text
    assert "共扫描 3 个事件" in text
    assert "新增待办 1" in text
    assert "解决 1" in text
    assert "跳过出账 1" in text
    assert "成功确认 2" in text
    assert "⚠️ 提示: 达到批次消费上限" in text


def test_different_accounts_with_same_uid_remain_isolated(tmp_path, parser_repo):
    root, sd, models = parser_repo
    statement_db = str(tmp_path / "statements.db")
    todo_db = str(tmp_path / "todos.db")
    sd.init_db(statement_db)

    # 两个账户有相同的 uid="999"，但 case_key 按账户隔离
    sd.upsert_quality_event(
        statement_db,
        models.QualityEventRecord(
            case_key="acc-A:case",
            event_type="quality_detected",
            revision=1,
            cycle_id=1,
            bank_code="CMB",
            account_name="account-A",
            diagnostics=[models.Diagnostic(code="DATE_INVALID", reason="A error")],
            evidence_refs=[{"account": "account-A", "uid": "999", "message_id": "<999@a>"}]
        )
    )
    sd.upsert_quality_event(
        statement_db,
        models.QualityEventRecord(
            case_key="acc-B:case",
            event_type="quality_detected",
            revision=1,
            cycle_id=1,
            bank_code="CMB",
            account_name="account-B",
            diagnostics=[models.Diagnostic(code="DATE_INVALID", reason="B error")],
            evidence_refs=[{"account": "account-B", "uid": "999", "message_id": "<999@b>"}]
        )
    )

    mock_runner = Mock(return_value=SimpleNamespace(
        stdout='--- JSON_SUMMARY_START ---\n{"accounts":[{"account":"account-A","status":"ok"},{"account":"account-B","status":"ok"}]}\n--- JSON_SUMMARY_END ---',
        stderr="", returncode=0
    ))
    cli = MailCLI(str(root), db_path=statement_db)

    res = sync_and_consume(
        script_dir=str(root),
        statement_db_path=statement_db,
        todo_db_path=todo_db,
        client=cli,
        runner=mock_runner
    )

    assert res["quality_stats"]["created"] == 2
    assert res["quality_stats"]["acked"] == 2

    with sqlite3.connect(todo_db) as conn:
        rows = conn.execute("SELECT source_key, title FROM todos ORDER BY source_key").fetchall()
        assert len(rows) == 2
        assert rows[0][0] == "mail_quality:acc-A:case"
        assert rows[1][0] == "mail_quality:acc-B:case"


def test_stale_ack_rejection_halts_batch_drain(tmp_path):
    client = Mock()
    client.quality_events.return_value = [
        {"event_id": "e1", "case_key": "k1", "event_type": "quality_resolved", "revision": 1, "cycle_id": 1, "diagnostics": []}
    ]
    # 模拟 CLI 报告 ack 失败（如由于 revision 冲突被拒绝）
    client.quality_ack.side_effect = MailCLIError("stale revision ack rejected")

    stats = drain_quality_events(client, todo_db_path=str(tmp_path / "todo.db"), max_batches=3)
    assert stats["batches"] == 1
    assert stats["failed_acks"] == 1
    assert stats["acked"] == 0
    # 无 ack 进展，立即终止，不进行第 2 批
    assert client.quality_events.call_count == 1


def test_write_failure_does_not_retry_fetch(tmp_path, parser_repo):
    root, sd, models = parser_repo
    statement_db = str(tmp_path / "statements.db")
    todo_db = str(tmp_path / "todos.db")
    sd.init_db(statement_db)

    sd.upsert_quality_event(
        statement_db,
        models.QualityEventRecord(
            case_key="k",
            event_type="quality_detected",
            revision=1,
            cycle_id=1,
            diagnostics=[models.Diagnostic(code="UNKNOWN", reason="fail")]
        )
    )

    mock_runner = Mock(return_value=SimpleNamespace(
        stdout='--- JSON_SUMMARY_START ---\n{"accounts":[{"account":"acc","status":"ok"}]}\n--- JSON_SUMMARY_END ---',
        stderr="", returncode=0
    ))
    cli = MailCLI(str(root), db_path=statement_db)

    # 模拟 Todo 数据库写入出错
    with patch("integrations.mail.quality_todo.upsert_source_todo", side_effect=OSError("database locked")):
        res = sync_and_consume(
            script_dir=str(root),
            statement_db_path=statement_db,
            todo_db_path=todo_db,
            client=cli,
            runner=mock_runner
        )

    # 抓取只执行了 1 次，绝不因为 Todo 写入失败而反复 fetch
    assert mock_runner.call_count == 1
    assert res["quality_stats"]["failed"] == 1
    assert res["quality_stats"]["acked"] == 0
