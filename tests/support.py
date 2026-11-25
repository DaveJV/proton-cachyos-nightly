"""Shared test setup. Import this before the package: core reads these
environment variables at import time, so every test module must see the
same temp dirs."""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.TemporaryDirectory(prefix="pcn-test-")
ROOT = Path(_TMP.name)
os.environ.update(
    PCN_DATA_DIR=str(ROOT / "data"),
    PCN_COMPAT_DIR=str(ROOT / "compat"),
    XDG_CACHE_HOME=str(ROOT / "cache"),
    XDG_CONFIG_HOME=str(ROOT / "config"),
    GITHUB_TOKEN="test-token",  # keeps `gh auth token` from running
    QT_QPA_PLATFORM="offscreen",
    PCN_CPU_FLAGS="avx avx2 bmi1 bmi2 fma movbe f16c abm",
    PCN_MACHINE="x86_64",
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])
