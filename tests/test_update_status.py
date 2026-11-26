"""'Update status' panel: compare retries, friendly errors, skip on same sha,
plus the short build names and the grouped 'What's new' list.

Runs fully offline: requests.Session.get is replaced by a fake, and every
path the app touches points into a temp dir.

    python -m unittest discover -s tests -v
"""

import json
import unittest
from unittest import mock

import support  # noqa: F401  (sets env before the package is imported)

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


class UpdateStatusCore(unittest.TestCase):
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


class UpdateStatusUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qapp()

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

        head, sub = w.l_update.text(), w.l_update_sub.text()
        self.assertEqual(head, "Unable to check")
        self.assertEqual(sub, "Update check failed (GitHub error)")
        self.assertTrue(w.b_retry_cmp.isVisibleTo(w), "Retry button shown")
        for text in (head, sub, w.t_new.toHtml()):
            for raw in ("api.github.com", "Server Error", "500", "Error:", "Traceback"):
                self.assertNotIn(raw, text)
        self.assertIn("Couldn't load the list of changes (GitHub error)", w.t_new.toPlainText())
        # The stable comparison still works.
        self.assertEqual(w.tabs.tabText(1), "Ahead of stable (5)")
        self.assertIn("5 changes in the nightly", w.t_stable.toPlainText())
        self.assertIn("Stable also has 2 changes", w.t_stable.toPlainText())

    def test_up_to_date(self):
        _install(NIGHTLY_BUILD)
        w = self._window(_check_status(FakeGitHubHTTP("500")))
        self.assertEqual(w.l_update.text(), "Up to date")
        self.assertFalse(w.b_retry_cmp.isVisibleTo(w))
        self.assertEqual(w.tabs.tabText(0), "What's new")
        self.assertIn("Nothing new since your build", w.t_new.toPlainText())

    def test_update_available_shows_grouped_changes(self):
        _install(OLD_BUILD)
        s = _check_status(FakeGitHubHTTP("500"))
        s.vs_installed = core.Compare(OLD_SHA, NIGHTLY_SHA, 3, 0, [
            ("33333333", "wine: update submodule"),
            ("22222222", "wine: update submodule"),
            ("11111111", "proton: add PROTON_FOO"),
        ], "https://github.com/x/compare")
        w = self._window(s)
        self.assertEqual(w.l_update.text(), "Update available")
        self.assertIn("3 new commits", w.l_update_sub.text())
        self.assertEqual(w.tabs.tabText(0), "What's new (3)")
        text = w.t_new.toPlainText()
        self.assertIn("3 changes since your build (11.0 · 2026-11-20)", text)
        self.assertIn("Wine", text)
        self.assertIn("Update submodule ×2", text)
        self.assertIn("Add PROTON_FOO", text)
        self.assertIn("Compare on GitHub", text)

    def test_headline_names_are_short_with_full_name_in_tooltip(self):
        _install(OLD_BUILD)
        w = self._window(_check_status(FakeGitHubHTTP("500")))
        self.assertEqual(w.l_installed.text(), "11.0 · 2026-11-20")
        self.assertEqual(w.l_installed.toolTip(), OLD_BUILD)
        self.assertEqual(w.l_installed_sub.text(), VARIANT)
        self.assertEqual(w.l_nightly.text(), "11.0 · 2026-11-25")
        self.assertTrue(w.l_nightly.toolTip().startswith(NIGHTLY_BUILD))
        self.assertIn(VARIANT, w.l_nightly_sub.text())


class DisplayHelpers(unittest.TestCase):
    def test_short_build_name(self):
        self.assertEqual(core.short_build_name(NIGHTLY_BUILD), "11.0 · 2026-11-25")
        self.assertEqual(core.short_version("cachyos-11.0-20261015-slr"), "11.0 · 2026-10-15")
        self.assertEqual(core.short_version("1:11.0.20261015-1"), "11.0 · 2026-10-15")

    def test_short_build_name_fallback_is_elided(self):
        name = "proton-cachyos-some-very-long-unexpected-build-name-x86_64_v3"
        short = core.short_build_name(name)
        self.assertLessEqual(len(short), core.SHORT_NAME_MAX)
        self.assertIn("…", short)
        self.assertNotIn("x86_64_v3", short)
        self.assertEqual(core.short_build_name("proton-cachyos-odd-x86_64"), "odd")

    def test_group_commits(self):
        groups = core.group_commits([
            ("a", "build: tweak flags"),
            ("b", "wine: update submodule"),
            ("c", 'Revert "nvidia-libs: drop thing"'),
            ("d", "wine: update submodule"),
            ("e", "Added a patch without an area"),
            ("f", "protonfixes: update submodule"),
        ])
        names = [g for g, _ in groups]
        self.assertEqual(names[0], "Wine")
        self.assertEqual(names[-2:], ["Other", "Build and CI"])
        wine = dict(groups)["Wine"]
        self.assertEqual((wine[0].text, wine[0].count), ("Update submodule", 2))
        self.assertEqual(dict(groups)["NVIDIA"][0].text, "Revert: Drop thing")
        self.assertEqual(dict(groups)["Game fixes"][0].text, "Update submodule")

    def test_full_changelog_url(self):
        rel = core.Release("t", "t", "Version: x\n**Full Changelog**: https://github.com/a/b/compare/x...y",
                           "https://github.com/a/b/releases/tag/t", "", True)
        self.assertEqual(core.full_changelog_url(rel), "https://github.com/a/b/compare/x...y")
        rel.body = "no link"
        self.assertIsNone(core.full_changelog_url(rel))

if __name__ == "__main__":
    unittest.main()
