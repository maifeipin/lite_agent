import sqlite3

import core.config_loader as config_loader


def test_rollback_setting_restores_write_by_audit_id(tmp_path, monkeypatch):
    monkeypatch.setattr(config_loader, "PROJECT_ROOT", str(tmp_path))
    config_loader._db_initialized = False
    config_loader._base_cache = None
    config_loader._sqlite_cache = {}
    config_loader._sqlite_ttl = 0
    config_loader._merged_cache = None
    config_loader._merged_ttl = 0

    try:
        config_loader.write_setting(
            "task_specs.author_max_tokens", 8192, operator="test"
        )
        db_path = tmp_path / "data" / "settings.db"
        with sqlite3.connect(str(db_path)) as conn:
            audit_id = conn.execute(
                "SELECT id FROM audit_log ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]

        assert config_loader.rollback_setting(audit_id, operator="test") is True

        with sqlite3.connect(str(db_path)) as conn:
            setting = conn.execute(
                "SELECT value FROM settings WHERE key=?",
                ("task_specs.author_max_tokens",),
            ).fetchone()
            rollback = conn.execute(
                "SELECT action FROM audit_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert setting is None
        assert rollback == ("ROLLBACK_SQLITE",)
    finally:
        config_loader._db_initialized = False
        config_loader._base_cache = None
        config_loader._sqlite_cache = {}
        config_loader._sqlite_ttl = 0
        config_loader._merged_cache = None
        config_loader._merged_ttl = 0
