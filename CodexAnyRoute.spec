# -*- mode: python ; coding: utf-8 -*-


# Comprehensive excludes — PyInstaller auto-discovers far more than we need.
# Stripping these saves several MB without touching functionality.
#
# Key categories:
#   • Build/packaging tooling  → never used at runtime
#   • Stdlib test/dev/convert   → never imported by this app
#   • Unused optional deps of our direct requirements
#   • PyInstaller internal hooks that we don't distribute with the app
EXCLUDES = [
    # Build tooling (only needed to build the exe, not at runtime)
    'pyinstaller',
    'pyinstaller_hooks_contrib',
    'altgraph',
    'pefile',
    'pywin32',
    'pywin32_ctypes',
    # Python packaging (never used by the running app)
    'setuptools',
    '_distutils_hack',
    'distutils',
    'pkg_resources',
    'pip',
    'wheel',
    # Stdlib test / documentation / 2to3 conversion (never imported)
    'unittest',
    'doctest',
    'pydoc',
    'pydoc_data',
    'lib2to3',
    'tkinter.test',
    'tkinter.tix',
    'email.test',
    'test',
    # Numerical / plotting stacks (never used by this app)
    'numpy',
    'pandas',
    'scipy',
    'matplotlib',
    'IPython',
    'jupyter',
    'notebook',
    'sympy',
    'pandas.errors',
    # Optional httpx backends not used in our sync test path
    'h11',
    # Optional uvicorn anyio backends (Windows uses default asyncio loop)
    'uvloop',
    'winloop',
    # Optional trio backends (never used on Windows)
    'trio',
    'outcome',
]


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['wsproto', 'wsproto.connection', 'wsproto.events', 'wsproto.handshake', 'wsproto.utilities'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=2,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CodexAnyRoute',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
