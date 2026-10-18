"""Acceptance gates must fail when a required test is skipped."""


def pytest_sessionfinish(session, exitstatus):
    reporter = session.config.pluginmanager.getplugin("terminalreporter")
    skipped = reporter.stats.get("skipped", []) if reporter is not None else []
    if skipped:
        session.exitstatus = 1
        reporter.write_sep("!", f"Acceptance gate rejected {len(skipped)} skipped test(s)")
