"""
Unit tests for P0.2 fixes:
- ops_mail_search: operator precedence fix (WHERE (...) AND account_name=?), limit validation, readonly connection, missing DB handling
- ops_mail_stats: readonly connection, missing DB handling
- MailCLI: absolute path resolution and environment passing
"""

import os
import sqlite3
import pytest
from lite_agent.skills.ops_mail_search import _do_search
from lite_agent.skills.ops_mail_stats import _compute_stats
from lite_agent.integrations.mail.client import MailCLI


@pytest.fixture
def test_db(tmp_path):
    db_file = tmp_path / "test_statements.db"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE email_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT,
            sender TEXT,
            email_date TEXT,
            category TEXT,
            importance TEXT,
            status TEXT,
            account_name TEXT,
            uid TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE email_bodies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT,
            uid TEXT,
            plain_text TEXT,
            raw_html TEXT
        )
    """)
    # Account 1 (wanted)
    cur.execute(
        "INSERT INTO email_summaries (subject, sender, email_date, category, importance, status, account_name, uid) "
        "VALUES ('Invoice from Apple', 'billing@apple.com', '2026-10-01', 'bill', 'high', 'processed', 'wanted_account', '101')"
    )
    cur.execute(
        "INSERT INTO email_bodies (account_name, uid, plain_text) "
        "VALUES ('wanted_account', '101', 'Thank you for your payment to Apple.')"
    )

    # Account 2 (other): Has 'Apple' in plain_text and subject
    cur.execute(
        "INSERT INTO email_summaries (subject, sender, email_date, category, importance, status, account_name, uid) "
        "VALUES ('Apple News Weekly', 'newsletter@apple.com', '2026-10-02', 'news', 'low', 'processed', 'other_account', '102')"
    )
    cur.execute(
        "INSERT INTO email_bodies (account_name, uid, plain_text) "
        "VALUES ('other_account', '102', 'Latest Apple updates and rumors.')"
    )

    conn.commit()
    conn.close()
    return str(db_file)


def test_mail_search_operator_precedence_account_filter(test_db):
    # Searching for 'Apple' without account_name returns both accounts
    rows_all = _do_search("Apple", limit=20, account_name=None, db_path=test_db)
    assert len(rows_all) == 2

    # Searching with account_name='wanted_account' MUST only return wanted_account
    # Before the fix, the OR condition allowed other_account if body matched!
    rows_filtered = _do_search("Apple", limit=20, account_name="wanted_account", db_path=test_db)
    assert len(rows_filtered) == 1
    assert rows_filtered[0]["account_name"] == "wanted_account"
    assert rows_filtered[0]["uid"] == "101"

    # Searching for sender keyword with account filter
    rows_sender = _do_search("billing", limit=20, account_name="wanted_account", db_path=test_db)
    assert len(rows_sender) == 1
    assert rows_sender[0]["account_name"] == "wanted_account"

    # Other account filter
    rows_other = _do_search("Apple", limit=20, account_name="other_account", db_path=test_db)
    assert len(rows_other) == 1
    assert rows_other[0]["account_name"] == "other_account"


def test_mail_search_limit_boundaries(test_db):
    # Valid limit
    rows = _do_search("Apple", limit=1, db_path=test_db)
    assert len(rows) == 1

    # Invalid limits fallback gracefully
    rows_neg = _do_search("Apple", limit=-5, db_path=test_db)
    assert len(rows_neg) == 2

    rows_str = _do_search("Apple", limit="invalid", db_path=test_db)
    assert len(rows_str) == 2

    # Upper bound cap at 100
    rows_big = _do_search("Apple", limit=500, db_path=test_db)
    assert len(rows_big) <= 100


def test_mail_search_missing_db_does_not_create_file(tmp_path):
    nonexistent = str(tmp_path / "nonexistent" / "statements.db")
    rows = _do_search("test", db_path=nonexistent)
    assert rows == []
    assert not os.path.exists(nonexistent)


def test_mail_stats_computation_and_missing_db(test_db, tmp_path):
    stats = _compute_stats(db_path=test_db)
    assert "共 2 封" in stats
    assert "wanted_account" in stats
    assert "other_account" in stats

    missing = str(tmp_path / "nonexistent.db")
    res = _compute_stats(db_path=missing)
    assert "邮件数据库不存在" in res
    assert not os.path.exists(missing)


def test_mail_cli_absolute_paths_and_env(tmp_path, monkeypatch):
    script_dir = tmp_path / "parser_script"
    script_dir.mkdir()
    (script_dir / "mail_client.py").write_text("# dummy", encoding="utf-8")
    db_file = tmp_path / "data" / "statements.db"
    db_file.parent.mkdir()
    db_file.touch()

    cli = MailCLI(script_dir=str(script_dir), db_path=str(db_file))
    assert os.path.isabs(cli.script_dir)
    assert os.path.isabs(cli.db_path)
    assert cli.db_path == str(db_file.resolve())
