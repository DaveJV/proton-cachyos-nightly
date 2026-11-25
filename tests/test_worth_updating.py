"""'Worth updating?' panel: compare retries, friendly errors, skip on same sha.

Runs fully offline: requests.Session.get is replaced by a fake, and every
path the app touches points into a temp dir.

    python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Must happen before the package is imported: core reads these at import.
_TMP = tempfile.TemporaryDirectory(prefix="pcn-test-")
_ROOT = Path(_TMP.name)
os.environ.update(
    PCN_DATA_DIR=str(_ROOT / "data"),
    PCN_COMPAT_DIR=str(_ROOT / "compat"),
    XDG_CACHE_HOME=str(_ROOT / "cache"),
    XDG_CONFIG_HOME=str(_ROOT / "config"),
    GITHUB_TOKEN="test-token",  # keeps `gh auth token` from running
    QT_QPA_PLATFORM="offscreen",
    PCN_CPU_FLAGS="avx avx2 bmi1 bmi2 fma movbe f16c abm",
    PCN_MACHINE="x86_64",
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from proton_cachyos_nightly import core  # noqa: E402

NIGHTLY_SHA = "2ca6427f"
OLD_SHA = "aaaaaaa1"
STABLE_TAG = "cachyos-11.0-20261015-slr"
VARIANT = "x86_64_v3"
NIGHTLY_BUILD = f"proton-cachyos-11.0-20261125-base-240-g{NIGHTLY_SHA}-{VARIANT}"
OLD_BUILD = f"proton-cachyos-11.0-20261120-base-200-g{OLD_SHA}-{VARIANT}"

STABLE_COMPARE = {
    "status": "diverged",
    "ahead_by": 5,
    "behind_by": 2,
    "total_commits": 2,
    "html_url": f"https://github.com/{core.REPO}/compare/{STABLE_TAG}...{NIGHTLY_SHA}",
    "commits": [
        {"sha": "1111111111", "commit": {"message": "first change"}},
        {"sha": "2222222222", "commit": {"message": "second change\n\nbody"}},
    ],
}


def _response(url: str, status: int, payload) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r.url = url
    r.reason = "OK" if status < 400 else "Internal Server Error"
    r.headers["Content-Type"] = "application/json"
    r._content = json.dumps(payload).encode()
    return r


class FakeGitHubHTTP:
    """Stands in for requests.Session.get. The installed-build compare
    behaves per `installed_mode`; the stable compare always succeeds."""

    def __init__(self, installed_mode: str = "500"):
        self.installed_mode = installed_mode
        self.calls: list[str] = []

    def installed_calls(self, base: str) -> int:
        return sum(f"/compare/{base}..." in u for u in self.calls)

    def __call__(self, url, **kwargs):  # patched onto the class unbound: no session arg
        self.calls.append(url)
        if f"/compare/{STABLE_TAG}...{NIGHTLY_SHA}" in url:
            return _response(url, 200, STABLE_COMPARE)
        if "/compare/" in url:
            if self.installed_mode == "network":
                raise requests.ConnectionError(f"connection refused for {url}")
            return _response(url, 500, {"message": "Server Error"})
        raise AssertionError(f"unexpected request in test: {url}")


def _release(tag: str, prerelease: bool, build_name: str | None) -> core.Release:
    rel = core.Release(
        tag=tag, name=tag, body="notes", html_url=f"https://github.com/{core.REPO}/releases/tag/{tag}",
        published_at="2026-11-25T12:00:00Z", prerelease=prerelease,
    )
    if build_name:
        rel.builds[VARIANT] = core.Build(
            name=build_name,
            variant=VARIANT,
            tarball=core.Asset(build_name + ".tar.xz", "https://example.invalid/t", 1),
            checksum=core.Asset(build_name + ".sha512sum", "https://example.invalid/s", 1),
        )
    return rel


def _install(build_name: str) -> None:
    core.INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    (core.INSTALL_DIR / ".build").write_text(build_name + "\n")


def _check_status(fake: FakeGitHubHTTP) -> core.Status:
    nightly = _release("cachyos-11.0-nightly", True, NIGHTLY_BUILD)
    stable = _release(STABLE_TAG, False, None)
    with mock.patch.object(requests.Session, "get", fake), \
         mock.patch.object(core.GitHub, "latest_nightly_and_stable", lambda self: (nightly, stable)), \
         mock.patch.object(core.GitHub, "latest_snapshot", lambda self, max_runs=5: None), \
         mock.patch.object(core, "repo_package", lambda: None):
        return core.check_status(core.GitHub(), VARIANT, retries=1)


class WorthUpdatingCore(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(core.GitHub, "RETRY_DELAYS", (0, 0, 0))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_500_is_retried_then_reported_without_breaking_stable(self):
        _install(OLD_BUILD)
        fake = FakeGitHubHTTP("500")
        s = _check_status(fake)

        self.assertEqual(fake.installed_calls(OLD_SHA), 4, "1 try + 3 retries")
        self.assertEqual(s.vs_installed.error_kind, "github")
        self.assertEqual(s.vs_stable.error_kind, None)
        self.assertEqual(s.vs_stable.ahead_by, 5)

        logged = core.LOG_FILE.read_text()
        self.assertIn(f"/compare/{OLD_SHA}...{NIGHTLY_SHA}", logged)
        self.assertIn("500", logged)

    def test_network_error_is_retried_and_classified(self):
        _install(OLD_BUILD)
        fake = FakeGitHubHTTP("network")
        s = _check_status(fake)
        self.assertEqual(fake.installed_calls(OLD_SHA), 4)
        self.assertEqual(s.vs_installed.error_kind, "network")
        self.assertEqual(s.vs_stable.ahead_by, 5)

    def test_same_sha_skips_compare_call(self):
        _install(NIGHTLY_BUILD)
        fake = FakeGitHubHTTP("500")
        s = _check_status(fake)
        self.assertTrue(s.vs_installed.skipped)
        self.assertEqual(s.vs_installed.ahead_by, 0)
        self.assertEqual(fake.installed_calls(NIGHTLY_SHA), 0)
        self.assertEqual(s.vs_stable.ahead_by, 5)


class WorthUpdatingUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        patcher = mock.patch.object(core.GitHub, "RETRY_DELAYS", (0, 0, 0))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _window(self, status: core.Status):
        from proton_cachyos_nightly import app

        with mock.patch.object(app.MainWindow, "refresh", lambda self: None):
            w = app.MainWindow()
        self.addCleanup(w.deleteLater)
        w.state = status
        w.render()
        return w

    def test_friendly_message_and_stable_comparison_after_500(self):
        _install(OLD_BUILD)
        w = self._window(_check_status(FakeGitHubHTTP("500")))

        inst = w.l_worth_inst.text()
        stable = w.l_worth_stable.text()
        self.assertIn("Couldn't check how far behind you are (GitHub error)", inst)
        self.assertTrue(w.b_retry_cmp.isVisibleTo(w), "Retry button shown")
        for text in (inst, stable):
            for raw in ("api.github.com", "Server Error", "500", "Error:", "Traceback"):
                self.assertNotIn(raw, text)
        self.assertNotIn("http", inst)
        self.assertIn("5 commits", stable)
        self.assertIn("ahead of stable", stable)
        self.assertNotIn("api.github.com", w.t_inst.toHtml())

    def test_up_to_date(self):
        _install(NIGHTLY_BUILD)
        w = self._window(_check_status(FakeGitHubHTTP("500")))
        self.assertIn("Up to date", w.l_worth_inst.text())
        self.assertFalse(w.b_retry_cmp.isVisibleTo(w))
        self.assertIn("5 commits", w.l_worth_stable.text())


if __name__ == "__main__":
    unittest.main()
