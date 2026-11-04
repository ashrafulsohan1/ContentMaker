"""Environment pre-flight checks: python, ffmpeg, ffprobe.

Pure logic (version comparison, report formatting, exit code) is tested;
the subprocess probes in main() are a thin shell around it.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

MIN_PYTHON = (3, 11)
FFMPEG_SETUP_HINT = (
    "winget install Gyan.FFmpeg  (or download from https://www.gyan.dev/ffmpeg/builds/)"
)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def min_version_ok(version: str, minimum: tuple[int, ...]) -> bool:
    parts = tuple(int(p) for p in version.split(".") if p.isdigit())
    return parts >= minimum


def format_report(checks: list[Check]) -> str:
    lines: list[str] = []
    missing = False
    for c in checks:
        tag = "[ok]" if c.ok else "[MISSING]"
        lines.append(f"{tag} {c.name} — {c.detail}")
        missing = missing or not c.ok
    if missing:
        lines.append("")
        lines.append(f"Setup hint: {FFMPEG_SETUP_HINT}")
    return "\n".join(lines)


def exit_code(checks: list[Check]) -> int:
    return 0 if all(c.ok for c in checks) else 1


def _tool_version(tool: str) -> Check:
    if shutil.which(tool) is None:
        return Check(tool, False, "not found on PATH")
    try:
        out = subprocess.run([tool, "-version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return Check(tool, False, "found but failed to run")
    first = ((out.stdout or out.stderr).splitlines() or [""])[0].strip()
    return Check(tool, True, first)


def main() -> int:
    py = ".".join(map(str, sys.version_info[:3]))
    checks = [
        Check(
            "python",
            min_version_ok(py, MIN_PYTHON),
            f"{py} (min {'.'.join(map(str, MIN_PYTHON))})",
        ),
        _tool_version("ffmpeg"),
        _tool_version("ffprobe"),
    ]
    print(format_report(checks))
    return exit_code(checks)
