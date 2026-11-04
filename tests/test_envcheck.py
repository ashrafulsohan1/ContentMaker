"""Seam: app.envcheck — pure logic of the environment check (Phase 0).

Catches: wrong version comparison, report that hides failures or omits the
setup hint. Does not cover the subprocess probes in main() (thin shell).
"""
from app.envcheck import Check, exit_code, format_report, min_version_ok


def test_min_version_ok_accepts_newer_and_equal():
    assert min_version_ok("3.14.0", (3, 11)) is True
    assert min_version_ok("3.11", (3, 11)) is True
    assert min_version_ok("3.10.4", (3, 11)) is False
    assert min_version_ok("4.0", (3, 11)) is True


def test_format_report_all_pass_has_no_hint():
    checks = [Check("python", True, "3.14.0"), Check("ffmpeg", True, "8.0.1")]
    report = format_report(checks)
    assert "[ok] python" in report
    assert "[ok] ffmpeg" in report
    assert "MISSING" not in report
    assert "winget" not in report


def test_format_report_missing_tool_shows_setup_hint():
    checks = [Check("python", True, "3.14.0"), Check("ffmpeg", False, "not found on PATH")]
    report = format_report(checks)
    assert "[MISSING] ffmpeg" in report
    assert "winget install Gyan.FFmpeg" in report  # the setup hint


def test_exit_code_zero_only_when_all_pass():
    assert exit_code([Check("a", True, "x"), Check("b", True, "y")]) == 0
    assert exit_code([Check("a", True, "x"), Check("b", False, "y")]) == 1
