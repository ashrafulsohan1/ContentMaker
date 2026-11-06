# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the one-file ContentMaker.exe (v1.0.1 packaging).

Driven by scripts/build_exe.py — invoke it through that script (or
``python -m PyInstaller ContentMaker.spec``); do NOT pass --onefile style
flags that would regenerate this spec.

Datas (checked-in content bundled into the exe):
- app/web/templates + app/web/static (vendored htmx) — the UI;
- app/data — editable platform/preset/price tables;
- build_staging/ffmpeg_bin — included ONLY when scripts/build_exe.py
  --bundle-ffmpeg staged it (the dir check below), which is runtime resolver
  tier 2 (sys._MEIPASS/ffmpeg_bin).

Entry point: app/launcher.py (argparse + uvicorn + browser, clean Ctrl+C).
console=True on purpose: the console shows uvicorn logs and makes Ctrl+C the
documented stop. Writable content (projects/, state/) is created next to the
exe via app.config.app_home() — never inside the _MEIPASS extraction dir.
"""
import os

DATAS = [
    ("app/web/templates", "app/web/templates"),
    ("app/web/static", "app/web/static"),
    ("app/data", "app/data"),
]
if os.path.isdir(os.path.join(SPECPATH, "build_staging", "ffmpeg_bin")):
    DATAS.append(("build_staging/ffmpeg_bin", "ffmpeg_bin"))

a = Analysis(
    ["app/launcher.py"],
    pathex=[SPECPATH],
    binaries=[],
    datas=DATAS,
    # uvicorn resolves its loop/http/websockets implementations dynamically
    # inside functions, so static analysis misses the concrete modules. Same
    # for the key probes: the catalog dispatches them via importlib string
    # names (app.providers.catalog._PROBES → test_provider_key), so each probe
    # module must be pinned here or Settings' "Test key" endpoint 500s in the
    # exe (pkg-f1). tests/test_build_exe.py enforces this pin.
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.httptools_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.protocols.websockets.wsproto_impl",
        "uvicorn.lifespan.off",
        "uvicorn.lifespan.on",
        "app.providers.gemini.keytest",
    ],
    hookspath=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ContentMaker",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
