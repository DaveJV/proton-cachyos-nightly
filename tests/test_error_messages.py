"""Failure dialog and other error text: friendly messages only, details in the log.

    python -m unittest discover -s tests -v
"""

import errno
import unittest
from unittest import mock

import support  # noqa: F401  (sets env before the package is imported)

import requests  # noqa: E402

from proton_cachyos_nightly import core  # noqa: E402

SECRET_URL = "https://api.github.com/repos/CachyOS/proton-cachyos/releases?per_page=30"
RAW_MARKERS = ("http", "api.github.com", "Traceback", "ConnectionError", "HTTPError",
               "Errno", "per_page", "/secret/")


def _http_error(status: int) -> requests.HTTPError:
    r = requests.Response()
    r.status_code = status
    r.url = SECRET_URL
    return requests.HTTPError(f"{status} Server Error for url: {SECRET_URL}", response=r)


def _log_text() -> str:
    return core.LOG_FILE.read_text() if core.LOG_FILE.exists() else ""


class ReportError(unittest.TestCase):
    def test_log_path_uses_tilde_under_home(self):
        from pathlib import Path

        with mock.patch.object(core, "LOG_FILE", Path.home() / ".local/share/x/x.log"):
            self.assertEqual(core.log_path_display(), "~/.local/share/x/x.log")

    def assertFriendly(self, text: str):
        for raw in RAW_MARKERS:
            self.assertNotIn(raw, text.replace(core.log_path_display(), "<log>"))

    def test_network_error(self):
        msg = core.report_error(requests.ConnectionError(f"Max retries exceeded: {SECRET_URL}"), "Installing")
        self.assertIn("Installing failed.", msg)
        self.assertIn("network error", msg)
        self.assertIn(core.log_path_display(), msg)
        self.assertFriendly(msg)
        self.assertIn(SECRET_URL, _log_text())
        self.assertIn("Traceback", _log_text())

    def test_http_5xx(self):
        msg = core.report_error(_http_error(502), "Checking for updates")
        self.assertIn("GitHub returned an error", msg)
        self.assertFriendly(msg)

    def test_disk_full(self):
        exc = OSError(errno.ENOSPC, "No space left on device", "/secret/path/file.tar.xz")
        msg = core.report_error(exc, "Installing")
        self.assertIn("There isn't enough disk space.", msg)
        self.assertFriendly(msg)

    def test_unexpected_exception(self):
        msg = core.report_error(KeyError("assets"), "Checking for updates")
        self.assertIn("Something went wrong.", msg)
        self.assertNotIn("assets", msg)

    def test_updater_error_detail_goes_to_log_only(self):
        exc = core.UpdaterError("Extracting the archive failed, so nothing was installed.",
                                detail="tar: /secret/tmp/x.tar.xz: Unexpected EOF")
        msg = core.report_error(exc, "Installing")
        self.assertTrue(msg.startswith("Extracting the archive failed"))
        self.assertFriendly(msg)
        self.assertIn("Unexpected EOF", _log_text())

    def test_tool_in_use_names_processes_but_not_command_lines(self):
        procs = ["4242 /usr/lib/wine/wineserver -p /secret/prefix",
                 "4243 /home/u/.local/share/Steam/compatibilitytools.d/x/files/bin/wine64 game.exe"]
        with mock.patch.object(core, "tool_in_use", return_value=procs):
            with self.assertRaises(core.ToolInUse) as cm:
                core.ensure_not_in_use()
        msg = core.report_error(cm.exception, "Installing")
        self.assertIn("wineserver", msg)
        self.assertIn("wine64", msg)
        self.assertFriendly(msg)
        self.assertIn("/secret/prefix", _log_text())

    def test_github_404_has_no_url(self):
        r = requests.Response()
        r.status_code = 404
        r.url = SECRET_URL
        r._content = b"{}"
        with mock.patch.object(requests.Session, "get", lambda self, url, **kw: r):
            with self.assertRaises(core.GitHubNotFound) as cm:
                core.GitHub().get_json(SECRET_URL)
        self.assertFriendly(str(cm.exception))


class FailureDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = support.qapp()

    def _window(self):
        from proton_cachyos_nightly import app

        with mock.patch.object(app.MainWindow, "refresh", lambda self: None):
            w = app.MainWindow()
        self.addCleanup(w.deleteLater)
        return w

    def _fail_through_worker(self, w, exc, context):
        """Drive a real Worker (synchronously) into the window's failure slot
        and return the dialog text."""
        from proton_cachyos_nightly import app

        def boom(progress, status):
            raise exc

        worker = app.Worker(boom, context=context)
        worker.failed.connect(w.on_failed)
        with mock.patch.object(app.QMessageBox, "warning") as warn:
            worker.run()
        self.assertEqual(warn.call_count, 1)
        return warn.call_args.args[2]

    def test_dialog_and_status_line_are_friendly(self):
        w = self._window()
        text = self._fail_through_worker(
            w, requests.ConnectionError(f"HTTPSConnectionPool: {SECRET_URL}"), "Checking for updates")
        self.assertIn("Checking for updates failed.", text)
        self.assertIn("Couldn't reach GitHub (network error)", text)
        for raw in RAW_MARKERS:
            self.assertNotIn(raw, text.replace(core.log_path_display(), "<log>"))
            self.assertNotIn(raw, w.msg.text())
        self.assertTrue(w.msg.text().endswith("Details are in the log."))
        self.assertIn(SECRET_URL, _log_text())

    def test_worker_context_comes_from_run_label(self):
        from proton_cachyos_nightly import app

        w = self._window()
        with mock.patch.object(app.QThread, "start"):
            w.run(lambda progress, status: None, on_done=lambda r: None, label="Installing…")
        self.assertEqual(w.worker.context, "Installing")

    def test_source_row_snapshot_error_is_short(self):
        w = self._window()
        s = core.Status(
            variant="x86_64_v3", installed=None, previous=None, nightly=None, snapshot=None,
            stable=None, repo_package=None, vs_installed=None, vs_stable=None,
            rebuilding=True, has_token=True,
            snapshot_error=core.short_error(requests.ConnectionError(SECRET_URL)),
        )
        text = w.source_text(s, "x86_64_v3", None)
        self.assertIn("Couldn't list Snapshot artifacts (network error).", text)
        self.assertNotIn("http", text)


if __name__ == "__main__":
    unittest.main()
