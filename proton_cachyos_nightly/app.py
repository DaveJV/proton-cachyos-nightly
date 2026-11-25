"""PySide6 front end."""

from __future__ import annotations

import html
import re
import sys
import threading
import traceback
from datetime import datetime

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from . import __version__, core

REBUILD_RETRY_MS = 2 * 60 * 1000
COMMIT_URL = f"https://github.com/{core.REPO}/commit/"


class Worker(QObject):
    progress = Signal(float, float)
    status = Signal(str)
    done = Signal(object)
    failed = Signal(str, bool)  # message, is_rebuilding

    def __init__(self, fn, *args):
        super().__init__()
        self.fn = fn
        self.args = args

    @Slot()
    def run(self):
        try:
            res = self.fn(*self.args, progress=self.progress.emit, status=self.status.emit)
        except core.NightlyRebuilding as e:
            self.failed.emit(str(e), True)
        except core.UpdaterError as e:
            self.failed.emit(str(e), False)
        except Exception as e:  # network errors etc.
            traceback.print_exc()
            self.failed.emit(f"{type(e).__name__}: {e}", False)
        else:
            self.done.emit(res)


def fmt_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def mb(n: float) -> str:
    return f"{n / 1048576:.0f} MB"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Proton-CachyOS Nightly")
        self.setWindowIcon(QIcon.fromTheme("steam", QIcon.fromTheme("system-software-update")))
        self.resize(820, 680)

        self.cfg = core.load_config()
        self.cancel = threading.Event()
        self.state: core.Status | None = None
        self.thread: QThread | None = None
        self.worker: Worker | None = None
        self.notice = ""

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Variant picker
        top = QHBoxLayout()
        top.addWidget(QLabel("Variant:"))
        self.variant = QComboBox()
        self.variant.setMinimumWidth(180)
        self._fill_variants(core.KNOWN_VARIANTS)
        want = self.cfg.get("variant") or core.default_variant()
        idx = self.variant.findText(want)
        self.variant.setCurrentIndex(max(idx, 0))
        self.variant.currentTextChanged.connect(self.on_variant_changed)
        top.addWidget(self.variant)
        self.cpu_hint = QLabel(f"(CPU default: {core.default_variant()})")
        self.cpu_hint.setEnabled(False)
        top.addWidget(self.cpu_hint)
        top.addStretch(1)
        root.addLayout(top)

        # Status
        box = QGroupBox("Status")
        form = QFormLayout(box)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        def lab():
            l = QLabel("…")
            l.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            l.setOpenExternalLinks(True)
            l.setWordWrap(True)
            return l

        self.l_installed = lab()
        self.l_previous = lab()
        self.l_nightly = lab()
        self.l_stable = lab()
        self.l_repo = lab()
        self.l_worth = lab()
        form.addRow("Installed:", self.l_installed)
        form.addRow("Previous:", self.l_previous)
        form.addRow("Latest nightly:", self.l_nightly)
        form.addRow("Stable (GitHub):", self.l_stable)
        form.addRow("Stable (CachyOS repo):", self.l_repo)
        form.addRow("Worth updating?", self.l_worth)
        root.addWidget(box)

        # Changelog
        self.tabs = QTabWidget()
        self.t_notes = self._browser()
        self.t_inst = self._browser()
        self.t_stable = self._browser()
        self.tabs.addTab(self.t_notes, "Release notes")
        self.tabs.addTab(self.t_inst, "New since installed")
        self.tabs.addTab(self.t_stable, "Ahead of stable")
        root.addWidget(self.tabs, 1)

        # Progress
        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        root.addWidget(self.msg)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setTextVisible(True)
        self.bar.setFormat("")
        root.addWidget(self.bar)

        # Buttons
        btns = QHBoxLayout()
        self.b_install = QPushButton(QIcon.fromTheme("download"), "Install")
        self.b_rollback = QPushButton(QIcon.fromTheme("edit-undo"), "Rollback")
        self.b_refresh = QPushButton(QIcon.fromTheme("view-refresh"), "Refresh")
        self.b_install.clicked.connect(self.on_install)
        self.b_rollback.clicked.connect(self.on_rollback)
        self.b_refresh.clicked.connect(self.refresh)
        btns.addWidget(self.b_install)
        btns.addWidget(self.b_rollback)
        btns.addStretch(1)
        btns.addWidget(self.b_refresh)
        root.addLayout(btns)

        self.retry_timer = QTimer(self)
        self.retry_timer.setSingleShot(True)
        self.retry_timer.timeout.connect(self.refresh)

        self.render_local()
        QTimer.singleShot(0, self.refresh)

    # ------------------------------------------------------------------ ui

    def _browser(self) -> QTextBrowser:
        b = QTextBrowser()
        b.setOpenExternalLinks(True)
        return b

    def _fill_variants(self, variants):
        cur = self.variant.currentText()
        self.variant.blockSignals(True)
        self.variant.clear()
        self.variant.addItems(variants)
        if cur:
            i = self.variant.findText(cur)
            if i >= 0:
                self.variant.setCurrentIndex(i)
        self.variant.blockSignals(False)

    def busy(self) -> bool:
        return self.thread is not None

    def set_busy(self, on: bool):
        for w in (self.b_install, self.b_rollback, self.b_refresh, self.variant):
            w.setEnabled(not on)
        if not on:
            self.update_buttons()

    def update_buttons(self):
        v = self.variant.currentText()
        inst = core.installed_build()
        s = self.state
        build = s.nightly.builds.get(v) if s and s.nightly else None
        self.b_rollback.setEnabled(core.PREVIOUS_DIR.is_dir())
        if not build:
            self.b_install.setText("Install")
            self.b_install.setEnabled(s is not None)  # retries while rebuilding
            return
        if inst == build.name:
            self.b_install.setText("Up to date")
            self.b_install.setEnabled(False)
        elif not inst:
            self.b_install.setText(f"Install ({mb(build.tarball.size)})")
            self.b_install.setEnabled(True)
        elif core.variant_from_build(inst) not in (None, v):
            self.b_install.setText(f"Switch to {v} ({mb(build.tarball.size)})")
            self.b_install.setEnabled(True)
        else:
            self.b_install.setText(f"Update ({mb(build.tarball.size)})")
            self.b_install.setEnabled(True)

    def render_local(self):
        inst = core.installed_build()
        self.l_installed.setText(
            f"<b>{html.escape(inst)}</b>" if inst
            else ("installed, unknown build (no .build file)" if core.INSTALL_DIR.is_dir() else "not installed")
        )
        self.l_previous.setText(html.escape(core.previous_build() or "none"))
        self.update_buttons()

    def render(self):
        s = self.state
        if not s:
            return
        self._fill_variants(s.variants)
        self.render_local()
        v = self.variant.currentText()

        n = s.nightly
        if n:
            b = n.builds.get(v)
            name = b.name if b else (n.build_name or n.tag)
            extra = "" if b else f" <i>({v} not published{' yet' if s.rebuilding else ''})</i>"
            self.l_nightly.setText(
                f'<a href="{n.html_url}">{html.escape(name)}</a> · {fmt_time(n.published_at)}{extra}'
            )
        else:
            self.l_nightly.setText("<i>nightly is being rebuilt</i>")
        if s.rebuilding:
            self.l_nightly.setText(self.l_nightly.text() + " <b>— nightly is being rebuilt</b>")

        st = s.stable
        self.l_stable.setText(
            f'<a href="{st.html_url}">{html.escape(st.tag)}</a> · {fmt_time(st.published_at)}' if st else "unknown"
        )
        if s.repo_package:
            pkg, ver = s.repo_package
            note = ""
            rd = core.repo_version_tag_date(ver)
            gd = core.repo_version_tag_date(st.tag) if st else None
            if rd and gd:
                if rd > gd:
                    note = " <b>(newer than GitHub stable)</b>"
                elif rd < gd:
                    note = " (older than GitHub stable)"
                else:
                    note = " (same as GitHub stable)"
            self.l_repo.setText(f"{pkg} {html.escape(ver)}{note}")
        else:
            self.l_repo.setText("proton-cachyos-slr not installed")

        # Worth updating?
        parts = []
        inst = core.installed_build()
        ci = s.vs_installed
        if not inst:
            parts.append("Not installed yet.")
        elif ci and ci.error:
            parts.append(f"Couldn't compare installed build: {html.escape(ci.error)}")
        elif ci:
            if ci.ahead_by == 0:
                parts.append("<b>You're on the latest nightly commit.</b>")
            else:
                parts.append(
                    f'<b>Nightly is <a href="{ci.html_url}">{ci.ahead_by} commit{"s" * (ci.ahead_by != 1)}</a> '
                    "ahead of your build.</b>"
                )
        elif inst and not core.commit_from_build(inst):
            parts.append("Installed build has no commit id to compare.")
        cs = s.vs_stable
        if cs and not cs.error:
            t = f'Nightly is <a href="{cs.html_url}">{cs.ahead_by} commits</a> ahead of stable'
            if cs.behind_by:
                t += f" (stable has {cs.behind_by} not in nightly)"
            parts.append(t + ".")
        self.l_worth.setText("<br>".join(parts) or "—")

        # Changelog tabs
        if n:
            # GitHub renders single newlines in release bodies as line breaks.
            body = re.sub(r"(?<!\n)\n(?!\n)", "  \n", n.body.replace("\r\n", "\n").strip())
            body = body or "_No release notes._"
            self.t_notes.setMarkdown(f"# {n.name}\n\n{body}")
        else:
            self.t_notes.setMarkdown("_Nightly is being rebuilt; release notes will appear once it is published._")
        self.t_inst.setHtml(self.commits_html(ci, "your installed build"))
        self.t_stable.setHtml(self.commits_html(cs, f"stable ({st.tag if st else '?'})"))
        self.tabs.setTabText(1, f"New since installed ({ci.ahead_by})" if ci and not ci.error else "New since installed")
        self.tabs.setTabText(2, f"Ahead of stable ({cs.ahead_by})" if cs and not cs.error else "Ahead of stable")
        self.update_buttons()

    def commits_html(self, c: core.Compare | None, what: str) -> str:
        if c is None:
            return f"<p><i>Nothing to compare against {html.escape(what)}.</i></p>"
        if c.error:
            return f"<p><i>Compare failed: {html.escape(c.error)}</i></p>"
        if not c.ahead_by:
            return f"<p>Nightly has no commits beyond {html.escape(what)}.</p>"
        out = [
            f'<p><b>{c.ahead_by}</b> commit{"s" * (c.ahead_by != 1)} ahead of {html.escape(what)} '
            f'— <a href="{c.html_url}">view on GitHub</a></p><ul>'
        ]
        for sha, title in c.commits:
            out.append(
                f'<li><a href="{COMMIT_URL}{sha}"><code>{sha}</code></a> {html.escape(title)}</li>'
            )
        out.append("</ul>")
        if len(c.commits) < c.ahead_by:
            out.append(f"<p><i>Showing the newest {len(c.commits)}.</i></p>")
        return "".join(out)

    # ------------------------------------------------------------- actions

    def run(self, fn, *args, on_done, label: str):
        if self.busy():
            return
        self.retry_timer.stop()
        self.cancel.clear()
        self.set_busy(True)
        self.msg.setText(label)
        self.bar.setRange(0, 0)
        self.bar.setFormat("")

        self.thread = QThread(self)
        self.worker = Worker(fn, *args)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.status.connect(self.msg.setText)
        self.worker.done.connect(on_done)
        self.worker.failed.connect(self.on_failed)
        self.worker.done.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self.on_thread_finished)
        self.thread.start()

    @Slot()
    def on_thread_finished(self):
        self.worker.deleteLater()
        self.thread.deleteLater()
        self.worker = None
        self.thread = None
        self.set_busy(False)

    @Slot(float, float)
    def on_progress(self, done: float, total: float):
        if total <= 0:
            self.bar.setRange(0, 0)
            self.bar.setFormat("")
            return
        self.bar.setRange(0, 1000)
        self.bar.setValue(int(done * 1000 / total))
        self.bar.setFormat(f"{mb(done)} / {mb(total)}  (%p%)")

    def idle_bar(self, full: bool = False):
        self.bar.setRange(0, 1000)
        self.bar.setValue(1000 if full else 0)
        self.bar.setFormat("")

    @Slot(str, bool)
    def on_failed(self, msg: str, rebuilding: bool):
        self.idle_bar()
        if rebuilding:
            self.msg.setText(f"⚠ {msg} Retrying automatically in 2 minutes.")
            self.retry_timer.start(REBUILD_RETRY_MS)
        elif msg != "Cancelled.":
            self.msg.setText(f"⚠ {msg.splitlines()[0]}")
            QMessageBox.warning(self, "Proton-CachyOS Nightly", msg)

    def refresh(self):
        gh = core.GitHub(self.cancel)
        v = self.variant.currentText()
        self.run(lambda progress, status: core.check_status(gh, v, status),
                 on_done=self.on_status, label="Checking for updates…")

    @Slot(object)
    def on_status(self, s: core.Status):
        self.state = s
        self.idle_bar()
        self.render()
        if s.rebuilding:
            self.msg.setText("⚠ Nightly is being rebuilt. Checking again in 2 minutes.")
            self.retry_timer.start(REBUILD_RETRY_MS)
        else:
            note, self.notice = self.notice, ""
            self.msg.setText((note + "  ·  " if note else "") + f"Last checked {datetime.now():%H:%M:%S}")

    def on_install(self):
        procs = core.tool_in_use()
        if procs:
            QMessageBox.warning(self, "In use", "Close the game or tool running from\n"
                                f"{core.INSTALL_DIR}\nbefore updating.")
            return
        gh = core.GitHub(self.cancel)
        v = self.variant.currentText()
        self.run(lambda progress, status: core.install(gh, v, progress, status),
                 on_done=self.on_installed, label="Installing…")

    @Slot(object)
    def on_installed(self, build: str):
        self.idle_bar(full=True)
        self.notice = f"✔ Installed {build}. Restart Steam if it was running."
        self.msg.setText(self.notice)
        # Re-check so the compare data reflects the new installed build.
        QTimer.singleShot(0, self.refresh)

    def on_rollback(self):
        prev = core.previous_build()
        if QMessageBox.question(
            self, "Rollback",
            f"Swap back to the previous build?\n\n{prev}\n\nThe current build becomes the new 'previous'.",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.run(lambda progress, status: core.rollback(status),
                 on_done=self.on_rolled_back, label="Rolling back…")

    @Slot(object)
    def on_rolled_back(self, build):
        self.idle_bar(full=True)
        self.notice = f"✔ Rolled back to {build or 'previous build'}."
        self.msg.setText(self.notice)
        QTimer.singleShot(0, self.refresh)

    def on_variant_changed(self, v: str):
        self.cfg["variant"] = v
        try:
            core.save_config(self.cfg)
        except OSError as e:
            self.msg.setText(f"⚠ Could not save config: {e}")
        self.render() if self.state else self.render_local()

    def closeEvent(self, ev):
        if self.busy():
            if QMessageBox.question(self, "Quit", "An operation is running. Cancel it and quit?") \
                    != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
            self.cancel.set()
            self.msg.setText("Cancelling…")
            self.thread.quit()
            self.thread.wait(60000)
        ev.accept()


def main() -> int:
    if "--version" in sys.argv:
        print(__version__)
        return 0
    app = QApplication(sys.argv)
    app.setApplicationName("proton-cachyos-nightly")
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("proton-cachyos-nightly")
    w = MainWindow()
    w.show()
    return app.exec()
