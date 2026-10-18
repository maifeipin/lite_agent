import os
import sqlite3
import pytest
from unittest.mock import MagicMock
from concurrent.futures import ThreadPoolExecutor

from lite_agent.skills.ops_todo import (
    upsert_source_todo,
    resolve_source_todo,
    todo_add,
    todo_update,
    _ensure_schema,
    _render_markdown
)
from lite_agent.integrations.mail.quality_todo import consume_quality_events


@pytest.fixture
def temp_todo_db(tmp_path):
    db_file = str(tmp_path / "test_todos.db")
    _ensure_schema(db_file)
    return db_file


def test_upsert_source_todo_idempotent_creation_and_update(temp_todo_db):
    source_key = "mail_quality:test_case_1"

    # 第一次写入: 创建
    res1 = upsert_source_todo(
        source_key=source_key,
        title="招商银行表头格式变化",
        description="发现格式变化",
        created_via="mail_quality",
        source_metadata={"event_id": "evt_1", "count": 1, "cycle_id": 1, "revision": 1},
        db_path=temp_todo_db
    )
    assert res1["created"] is True
    assert res1["status"] == "created"
    todo_id = res1["id"]

    # 查库验证
    conn = sqlite3.connect(temp_todo_db)
    cur = conn.cursor()
    cur.execute("SELECT id, title, status, source_key FROM todos WHERE source_key=?", (source_key,))
    row = cur.fetchone()
    assert row is not None
    assert row[0] == todo_id
    assert row[1] == "招商银行表头格式变化"
    assert row[2] == "pending"

    # 第二次写入: 同一 source_key，内容更新
    res2 = upsert_source_todo(
        source_key=source_key,
        title="招商银行表头格式变化 (更新)",
        description="更新后的详细描述",
        created_via="mail_quality",
        source_metadata={"event_id": "evt_1", "count": 2, "cycle_id": 1, "revision": 2},
        db_path=temp_todo_db
    )
    assert res2["created"] is False
    assert res2["status"] == "updated"
    assert res2["id"] == todo_id  # ID 稳定不变

    # 验证未生成重复记录，总数仍为 1
    cur.execute("SELECT COUNT(*) FROM todos WHERE source_key=?", (source_key,))
    assert cur.fetchone()[0] == 1

    # 验证标题与描述已更新
    cur.execute("SELECT title, description FROM todos WHERE id=?", (todo_id,))
    updated_row = cur.fetchone()
    assert updated_row[0] == "招商银行表头格式变化 (更新)"
    assert updated_row[1] == "更新后的详细描述"
    conn.close()


def test_upsert_source_todo_dropped_protection(temp_todo_db):
    source_key = "mail_quality:test_case_dropped"

    # 初始创建
    res = upsert_source_todo(
        source_key=source_key,
        title="已知噪音问题",
        description="人工确认不需要跟进",
        db_path=temp_todo_db
    )
    todo_id = res["id"]

    # 人工手动设为 dropped
    conn = sqlite3.connect(temp_todo_db)
    conn.execute("UPDATE todos SET status='dropped', dropped_reason='手动忽略' WHERE id=?", (todo_id,))
    conn.commit()

    # 再次触发同事件: 必须受保护，严禁复活为 pending
    res_again = upsert_source_todo(
        source_key=source_key,
        title="已知噪音问题 (再次扫描)",
        description="新描述",
        db_path=temp_todo_db
    )
    assert res_again["created"] is False
    assert res_again["status"] == "protected"
    assert res_again["id"] == todo_id

    # 验证数据库状态依然为 dropped
    cur = conn.cursor()
    cur.execute("SELECT status, dropped_reason FROM todos WHERE id=?", (todo_id,))
    row = cur.fetchone()
    assert row[0] == "dropped"
    assert row[1] == "手动忽略"
    conn.close()


def test_upsert_source_todo_snoozed_shelved_preservation(temp_todo_db):
    source_key = "mail_quality:test_case_snoozed"

    # 创建
    res = upsert_source_todo(
        source_key=source_key,
        title="稍后处理的异常",
        description="初始描述",
        due_at="2026-10-15T12:00:00+08:00",
        db_path=temp_todo_db
    )
    todo_id = res["id"]

    # 人工设为 snoozed
    conn = sqlite3.connect(temp_todo_db)
    conn.execute("UPDATE todos SET status='snoozed', snoozed_until='2026-10-20T09:00:00+08:00' WHERE id=?", (todo_id,))
    conn.commit()

    # 再次同步: 机器元数据更新，但不覆盖人工 snoozed 状态和原始 due_at
    res_again = upsert_source_todo(
        source_key=source_key,
        title="稍后处理的异常 (新扫描)",
        description="机器试图覆盖描述",
        due_at="2026-10-30T12:00:00+08:00",
        source_metadata={"count": 5},
        db_path=temp_todo_db
    )
    assert res_again["status"] == "updated"

    cur = conn.cursor()
    cur.execute("SELECT status, snoozed_until, due_at, description FROM todos WHERE id=?", (todo_id,))
    row = cur.fetchone()
    assert row[0] == "snoozed"
    assert row[1] == "2026-10-20T09:00:00+08:00"
    assert row[2] == "2026-10-15T12:00:00+08:00"  # 人工/初始 due_at 未被冲刷
    conn.close()


def test_upsert_source_todo_done_same_cycle_protected(temp_todo_db):
    """R5 契约：已完成的 Todo，旧事件重复投递（同周期）严禁复活。"""
    source_key = "mail_quality:test_case_done_same_cycle"

    # 创建并设为 done
    res = upsert_source_todo(
        source_key=source_key,
        title="已修复异常",
        source_metadata={"cycle_id": 1, "revision": 1},
        db_path=temp_todo_db
    )
    todo_id = res["id"]
    conn = sqlite3.connect(temp_todo_db)
    conn.execute("UPDATE todos SET status='done', completed_at='2026-10-10T10:00:00Z' WHERE id=?", (todo_id,))
    conn.commit()

    # 旧事件重投（cycle_id=1 相同周期）：必须 protected，严禁复活
    res_redelivery = upsert_source_todo(
        source_key=source_key,
        title="已修复异常 (旧事件重投)",
        description="重复扫描",
        source_metadata={"cycle_id": 1, "revision": 1},
        db_path=temp_todo_db
    )
    assert res_redelivery["status"] == "protected"
    assert res_redelivery["reason"] == "already_done_same_cycle"
    assert res_redelivery["id"] == todo_id

    cur = conn.cursor()
    cur.execute("SELECT status, completed_at FROM todos WHERE id=?", (todo_id,))
    row = cur.fetchone()
    assert row[0] == "done"
    assert row[1] == "2026-10-10T10:00:00Z"
    conn.close()


def test_upsert_source_todo_done_relapse_new_cycle_reopens(temp_todo_db):
    """R5 契约：已完成的 Todo，真正复发（新发生周期 cycle_id > cur_cycle）重新打开为 pending。"""
    source_key = "mail_quality:test_case_relapse_new_cycle"

    # 创建并设为 done
    res = upsert_source_todo(
        source_key=source_key,
        title="已修复异常",
        source_metadata={"cycle_id": 1, "revision": 1},
        db_path=temp_todo_db
    )
    todo_id = res["id"]
    conn = sqlite3.connect(temp_todo_db)
    conn.execute("UPDATE todos SET status='done', completed_at='2026-10-10T10:00:00Z' WHERE id=?", (todo_id,))
    conn.commit()

    # 真正复发：上游产生 cycle_id=2 的新周期事件
    res_relapse = upsert_source_todo(
        source_key=source_key,
        title="已修复异常 (新周期再次复发)",
        description="新周期发生",
        source_metadata={"cycle_id": 2, "revision": 2},
        db_path=temp_todo_db
    )
    assert res_relapse["status"] == "updated"
    assert res_relapse["id"] == todo_id
    assert res_relapse["reason"] == "relapse_reopened"

    cur = conn.cursor()
    cur.execute("SELECT status, completed_at, title FROM todos WHERE id=?", (todo_id,))
    row = cur.fetchone()
    assert row[0] == "pending"
    assert row[1] is None
    assert row[2] == "已修复异常 (新周期再次复发)"
    conn.close()


def test_upsert_source_todo_human_description_due_protected(temp_todo_db):
    """R5 契约：人工编辑过的描述和截止日期，机器后续事件更新严禁覆盖。"""
    source_key = "mail_quality:test_case_human_protection"

    res = upsert_source_todo(
        source_key=source_key,
        title="待排查问题",
        description="机器初始描述",
        due_at="2026-10-15T18:00:00+08:00",
        source_metadata={"cycle_id": 1, "revision": 1},
        db_path=temp_todo_db
    )
    todo_id = res["id"]

    # 人工编辑描述与日期，并标记 user_edited
    conn = sqlite3.connect(temp_todo_db)
    conn.execute("""
        UPDATE todos SET
            description = '人工重要批注：这是特例，周五前查明',
            due_at = '2026-10-18T18:00:00+08:00',
            source_metadata_json = json_set(source_metadata_json, '$.user_edited', 1)
        WHERE id = ?
    """, (todo_id,))
    conn.commit()

    # 机器新扫描推送新描述与新日期
    res_update = upsert_source_todo(
        source_key=source_key,
        title="待排查问题 (机器重检)",
        description="机器重检生成的新描述，不应冲刷人工批注",
        due_at="2026-10-25T18:00:00+08:00",
        source_metadata={"cycle_id": 1, "revision": 2, "occurrence_count": 2},
        db_path=temp_todo_db
    )
    assert res_update["status"] == "updated"

    cur = conn.cursor()
    cur.execute("SELECT description, due_at FROM todos WHERE id=?", (todo_id,))
    row = cur.fetchone()
    assert row[0] == "人工重要批注：这是特例，周五前查明"
    assert row[1] == "2026-10-18T18:00:00+08:00"
    conn.close()


def test_consume_quality_events_event_type_dispatching(temp_todo_db):
    """R4 契约：事件类型严格分流
    - statement_recorded: 跳过，不建维护 Todo，不被 quality_ack；
    - quality_resolved: 关闭已有 Todo，不建异常任务，带 revision ack；
    - quality_detected: 正常建/更 Todo，带 revision ack。
    """
    mock_client = MagicMock()

    # 先通过 upsert_source_todo 预建一条待解决的 Todo
    case_key_resolve = "cmb_resolved_case"
    init_res = upsert_source_todo(
        source_key=f"mail_quality:{case_key_resolve}",
        title="招行历史解析异常",
        description="等待解决",
        source_metadata={"cycle_id": 1, "revision": 1},
        db_path=temp_todo_db
    )
    todo_id_resolve = init_res["id"]

    mock_client.quality_events.return_value = [
        # 1. 出账事件：专属还款适配器，应跳过
        {
            "event_id": "evt_statement_1",
            "case_key": "stmt_rec_key",
            "event_type": "statement_recorded",
            "bank_code": "CMB",
            "account_name": "acc1"
        },
        # 2. 质量解决事件：应闭环关闭 Todo，带 revision ack
        {
            "event_id": "evt_resolved_1",
            "case_key": case_key_resolve,
            "event_type": "quality_resolved",
            "revision": 2,
            "cycle_id": 1,
            "bank_code": "CMB"
        },
        # 3. 正常质量异常诊断事件
        {
            "event_id": "evt_detected_1",
            "case_key": "cmb_format_defect",
            "event_type": "quality_detected",
            "revision": 1,
            "cycle_id": 1,
            "bank_code": "CMB",
            "occurrence_count": 2,
            "evidence_refs": [{"uid": "U1"}, {"uid": "U2"}],
            "diagnostics": [{"code": "FORMAT_CHANGED", "severity": "error", "field": "header"}]
        }
    ]
    mock_client.quality_ack.return_value = {"acked": True}

    stats = consume_quality_events(client=mock_client, db_path=temp_todo_db)

    assert stats["total"] == 3
    assert stats["skipped"] == 1       # statement_recorded 被跳过
    assert stats["resolved"] == 1      # quality_resolved 被成功关闭
    assert stats["created"] == 1       # quality_detected 正常建任务
    assert stats["acked"] == 2         # resolved 和 detected 各 ack 1 次，statement_recorded 不 ack!
    assert stats["failed"] == 0

    # 查库验证：todo_id_resolve 状态变为 done
    conn = sqlite3.connect(temp_todo_db)
    cur = conn.cursor()
    cur.execute("SELECT status FROM todos WHERE id=?", (todo_id_resolve,))
    assert cur.fetchone()[0] == "done"

    # 验证 quality_ack 调用参数（必须携带 revision）
    ack_calls = mock_client.quality_ack.call_args_list
    assert len(ack_calls) == 2
    # 第 1 次是 resolved
    assert ack_calls[0].kwargs.get("event_id") == "evt_resolved_1"
    assert ack_calls[0].kwargs.get("revision") == 2
    # 第 2 次是 detected
    assert ack_calls[1].kwargs.get("event_id") == "evt_detected_1"
    assert ack_calls[1].kwargs.get("revision") == 1
    conn.close()


def test_consume_quality_events_stale_ack_not_counted(temp_todo_db):
    """R4 契约：quality_ack 返回值未确认（如 stale_revision）时，不得虚报 acked 计数。"""
    mock_client = MagicMock()
    mock_client.quality_events.return_value = [
        {
            "event_id": "evt_stale",
            "case_key": "stale_case",
            "event_type": "quality_detected",
            "cycle_id": 1,
            "bank_code": "TEST",
            "revision": 1,
            "diagnostics": [{"code": "ERR", "severity": "warning"}]
        }
    ]
    # 模拟上游返回 stale_revision 拒绝确认
    mock_client.quality_ack.return_value = {
        "acked": False,
        "event_id": "evt_stale",
        "reason": "stale_revision",
        "current_revision": 2
    }

    stats = consume_quality_events(client=mock_client, db_path=temp_todo_db)
    assert stats["total"] == 1
    assert stats["created"] == 1
    assert stats["acked"] == 0         # 严禁虚报 acked 计数
    assert stats["failed_acks"] == 1   # 必须在 failed_acks 中可见
    assert len(stats["errors"]) > 0


def test_concurrent_markdown_rendering_safe(temp_todo_db):
    """R5 契约：消除固定 .tmp 路径并发竞争，多线程渲染零崩溃。"""
    def _worker(idx):
        return upsert_source_todo(
            source_key=f"mail_quality:concurrent_{idx}",
            title=f"并发测试任务 {idx}",
            description=f"并发描述 {idx}",
            db_path=temp_todo_db
        )

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(_worker, i) for i in range(10)]
        results = [f.result() for f in futures]

    assert len(results) == 10
    assert all(r["created"] is True for r in results)


def test_manual_todo_add_backward_compatible(temp_todo_db, monkeypatch):
    import lite_agent.skills.ops_todo as ops_todo_mod
    monkeypatch.setattr(ops_todo_mod, "DB_PATH", temp_todo_db)
    monkeypatch.setattr(ops_todo_mod, "MD_PATH", temp_todo_db + ".md")

    msg = todo_add(title="手动买咖啡", kind="misc", project="life")
    assert "成功添加待办事项" in msg

    conn = sqlite3.connect(temp_todo_db)
    cur = conn.cursor()
    cur.execute("SELECT title, kind, source_key FROM todos WHERE title='手动买咖啡'")
    row = cur.fetchone()
    assert row is not None
    assert row[0] == "手动买咖啡"
    assert row[1] == "misc"
    assert row[2] is None
    conn.close()
