"""
Unit tests for SecurityGuard.check_path boundary enforcement.
Covers:
- Empty sandbox_paths (open semantics)
- Legitimate subpaths, root path itself, relative paths
- Prefix collision attack (e.g. allowed vs allowed-evil)
- Traversal attack (../)
- Symlink escapes (file symlink, directory symlink)
- Nonexistent target with resolvable parent
- Nonexistent target escaping via symlinked parent
- Broken symlinks
- Multiple sandbox roots
- Invalid inputs
"""

import os
import pytest
from security import SecurityGuard


def test_empty_sandbox_paths_allows_all(tmp_path):
    guard = SecurityGuard({"security": {"sandbox_paths": []}})
    assert guard.check_path("/etc/passwd") is True
    assert guard.check_path(str(tmp_path / "foo")) is True


def test_none_or_invalid_inputs(tmp_path):
    guard = SecurityGuard({"security": {"sandbox_paths": [str(tmp_path)]}})
    assert guard.check_path(None) is False
    assert guard.check_path("") is False
    assert guard.check_path(123) is False


def test_root_and_subpaths(tmp_path):
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    sub_file = sandbox_dir / "sub" / "file.txt"
    sub_file.parent.mkdir()
    sub_file.write_text("hello", encoding="utf-8")

    guard = SecurityGuard({"security": {"sandbox_paths": [str(sandbox_dir)]}})

    # Root itself
    assert guard.check_path(str(sandbox_dir)) is True
    # Sub file
    assert guard.check_path(str(sub_file)) is True
    # Nonexistent child under root
    assert guard.check_path(str(sandbox_dir / "not_created_yet.txt")) is True
    # Deep nonexistent child under root
    assert guard.check_path(str(sandbox_dir / "a" / "b" / "c.txt")) is True


def test_prefix_collision_rejected(tmp_path):
    allowed_dir = tmp_path / "allowed"
    allowed_dir.mkdir()
    evil_dir = tmp_path / "allowed-evil"
    evil_dir.mkdir()
    evil_file = evil_dir / "secret.txt"
    evil_file.write_text("evil", encoding="utf-8")

    guard = SecurityGuard({"security": {"sandbox_paths": [str(allowed_dir)]}})

    assert guard.check_path(str(allowed_dir)) is True
    # Prefix collision must be rejected
    assert guard.check_path(str(evil_dir)) is False
    assert guard.check_path(str(evil_file)) is False


def test_traversal_rejected(tmp_path):
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside", encoding="utf-8")

    guard = SecurityGuard({"security": {"sandbox_paths": [str(sandbox_dir)]}})

    # Traversal attempts
    assert guard.check_path(str(sandbox_dir / ".." / "outside.txt")) is False
    assert guard.check_path(str(sandbox_dir / "sub" / ".." / ".." / "outside.txt")) is False


def test_symlink_escape_rejected(tmp_path):
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "secret.txt"
    outside_file.write_text("secret", encoding="utf-8")

    # Symlink file pointing outside
    link_file = sandbox_dir / "link_to_outside.txt"
    os.symlink(str(outside_file), str(link_file))

    # Symlink dir pointing outside
    link_dir = sandbox_dir / "link_to_outside_dir"
    os.symlink(str(outside_dir), str(link_dir))

    guard = SecurityGuard({"security": {"sandbox_paths": [str(sandbox_dir)]}})

    # Following symlink out of sandbox must be rejected
    assert guard.check_path(str(link_file)) is False
    assert guard.check_path(str(link_dir)) is False
    assert guard.check_path(str(link_dir / "secret.txt")) is False
    # Nonexistent child in symlinked directory pointing outside
    assert guard.check_path(str(link_dir / "nonexistent.txt")) is False


def test_broken_symlink_pointing_outside_rejected(tmp_path):
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    broken_link = sandbox_dir / "broken_link"
    os.symlink(str(tmp_path / "nonexistent_outside"), str(broken_link))

    guard = SecurityGuard({"security": {"sandbox_paths": [str(sandbox_dir)]}})
    assert guard.check_path(str(broken_link)) is False


def test_internal_symlink_allowed(tmp_path):
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    real_file = sandbox_dir / "target.txt"
    real_file.write_text("ok", encoding="utf-8")
    internal_link = sandbox_dir / "link_to_target.txt"
    os.symlink(str(real_file), str(internal_link))

    guard = SecurityGuard({"security": {"sandbox_paths": [str(sandbox_dir)]}})
    assert guard.check_path(str(internal_link)) is True


def test_multiple_roots(tmp_path):
    root1 = tmp_path / "root1"
    root1.mkdir()
    root2 = tmp_path / "root2"
    root2.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    guard = SecurityGuard({"security": {"sandbox_paths": [str(root1), str(root2)]}})
    assert guard.check_path(str(root1 / "a.txt")) is True
    assert guard.check_path(str(root2 / "b.txt")) is True
    assert guard.check_path(str(outside / "c.txt")) is False
