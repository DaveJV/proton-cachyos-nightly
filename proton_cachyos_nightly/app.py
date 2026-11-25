"""PySide6 front end."""

from __future__ import annotations

import html
import re
import sys
import threading
import traceback
from datetime import datetime

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QIcon, QPalette
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
        self.setWindowIcon(QIcon.fromTheme("proton-cachyos-nightly", QIcon.fromTheme("system-software-update")))
        self.resize(820, 680)

        self.cfg = core.load_config()
        self.cancel = threading.Event()
        self.state: core.Status | None = None
        self.thread: QThread | None = None
        self.worker: Worker | None = None
        self.notice = ""
        self.after_job = None  # runs once the current worker thread has finished

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # Variant picker
        top = QHBoxLayout()
        top.addWidget(QLabel("Variant:"))
        self.variant = QComboBox()
        self.variant.setMinimumWidth(180)
        # Created before the first fill, which updates its text.
        self.cpu_note = QLabel()
        # Greyed via palette, not setEnabled(False): disabled widgets show no tooltip.
        self.cpu_note.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        self.cpu_note.setCursor(Qt.CursorShape.WhatsThisCursor)
        initial_cpu_hidden = {
            v: r for v in core.KNOWN_VARIANTS if (r := core.cpu_hidden_reason(v))
        }
        self._fill_variants(
            [v for v in core.KNOWN_VARIANTS if v not in initial_cpu_hidden],
            core.RELEASE_VARIANTS,
        )
        self._show_cpu_hidden(initial_cpu_hidden)
        if not self._select(self.cfg.get("variant") or ""):
            self._select(core.default_variant())
        self.variant.currentIndexChanged.connect(self.on_variant_changed)
        top.addWidget(self.variant)
        self.cpu_hint = QLabel(f"(default: {core.default_variant()})")
        self.cpu_hint.setEnabled(False)
        top.addWidget(self.cpu_hint)
        top.addWidget(self.cpu_note)
        top.addStretch(1)
        root.addLayout(top)
        self.hidden_note = QLabel()
        self.hidden_note.setWordWrap(True)
        self.hidden_note.setTextFormat(Qt.TextFormat.RichText)
        self.hidden_note.hide()
        root.addWidget(self.hidden_note)

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
        self.l_source = lab()
        self.l_stable = lab()
        self.l_repo = lab()
        # "Worth updating?": installed line (+ Retry) above the stable line,
        # so a failed installed compare never hides the stable comparison.
        self.l_worth_inst = lab()
        self.b_retry_cmp = QPushButton(QIcon.fromTheme("view-refresh"), "Retry")
        self.b_retry_cmp.setToolTip("Check again how far your build is behind the nightly")
        self.b_retry_cmp.clicked.connect(self.on_retry_compare)
        self.b_retry_cmp.hide()
        self.l_worth_stable = lab()
        worth = QWidget()
        wv = QVBoxLayout(worth)
        wv.setContentsMargins(0, 0, 0, 0)
        wv.setSpacing(2)
        inst_row = QHBoxLayout()
        inst_row.setContentsMargins(0, 0, 0, 0)
        inst_row.addWidget(self.l_worth_inst, 1)
        inst_row.addWidget(self.b_retry_cmp, 0, Qt.AlignmentFlag.AlignTop)
        wv.addLayout(inst_row)
        wv.addWidget(self.l_worth_stable)
        form.addRow("Installed:", self.l_installed)
        form.addRow("Previous:", self.l_previous)
        form.addRow("Latest nightly:", self.l_nightly)
        form.addRow("Source:", self.l_source)
        form.addRow("Stable (GitHub):", self.l_stable)
        form.addRow("Stable (CachyOS repo):", self.l_repo)
        form.addRow("Worth updating?", worth)
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

    def cur_variant(self) -> str:
        return self.variant.currentData() or ""

    def _select(self, variant: str) -> bool:
        i = self.variant.findData(variant)
        if i < 0:
            return False
        self.variant.blockSignals(True)
        self.variant.setCurrentIndex(i)
        self.variant.blockSignals(False)
        self._update_variant_tooltip()
        return True

    def _fill_variants(self, variants, release_variants):
        cur = self.cur_variant()
        self.variant.blockSignals(True)
        self.variant.clear()
        for v in variants:
            tags = []
            note = core.experimental_note(v)
            if note:
                tags.append("experimental")
            if v not in release_variants:
                tags.append("Actions artifact")
            label = f"{v}  ({', '.join(tags)})" if tags else v
            self.variant.addItem(label, v)
            if note:
                self.variant.setItemData(self.variant.count() - 1, note, Qt.ItemDataRole.ToolTipRole)
        self.variant.blockSignals(False)
        if cur:
            self._select(cur)
        self._update_variant_tooltip()

    def _update_variant_tooltip(self):
        # Closed combo shows the selected item's tooltip too.
        self.variant.setToolTip(core.experimental_note(self.cur_variant()) or "")

    def _show_cpu_hidden(self, hidden: dict[str, str]):
        """Small 'ⓘ N hidden' label whose tooltip says why."""
        if not hidden:
            self.cpu_note.hide()
            return
        n = len(hidden)
        self.cpu_note.setText(f"ⓘ {n} variant{'s' * (n != 1)} hidden for this CPU")
        by_reason: dict[str, list[str]] = {}
        for v, r in hidden.items():
            by_reason.setdefault(r, []).append(v)
        self.cpu_note.setToolTip("\n\n".join(f"{', '.join(vs)}:\n{r}" for r, vs in by_reason.items()))
        self.cpu_note.show()

    def busy(self) -> bool:
        return self.thread is not None

    def set_busy(self, on: bool):
        for w in (self.b_install, self.b_rollback, self.b_refresh, self.variant, self.b_retry_cmp):
            w.setEnabled(not on)
        if not on:
            self.update_buttons()

    def update_buttons(self):
        v = self.cur_variant()
        inst = core.installed_build()
        s = self.state
        build = s.pick(v) if s else None
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
        self._fill_variants(s.variants, core.release_variants(s.nightly))
        want = self.cfg.get("variant") or core.default_variant()
        # A saved artifact-only choice is hidden without auth: show the CPU
        # default for now but keep the saved preference in the config.
        for target in (want, core.default_variant(), s.variants[0] if s.variants else ""):
            if self._select(target):
                break
        self._show_cpu_hidden(s.cpu_hidden)
        if s.hidden_variants:
            self.hidden_note.setText(
                "<i>Hidden: " + ", ".join(s.hidden_variants) + ". These are only built as "
                "GitHub Actions artifacts, and downloading artifacts needs GitHub auth. "
                "Run <code>gh auth login</code>, then Refresh.</i>"
            )
            self.hidden_note.show()
        else:
            self.hidden_note.hide()
        self.render_local()
        v = self.cur_variant()
        build = s.pick(v)
        rel = s.release_for(build)

        if build:
            self.l_nightly.setText(
                f'<a href="{rel.html_url}">{html.escape(build.name)}</a> · {fmt_time(rel.published_at)}'
            )
        elif s.nightly:
            self.l_nightly.setText(
                f'<a href="{s.nightly.html_url}">{html.escape(s.nightly.build_name or s.nightly.tag)}</a>'
                f" <i>({v} not published yet)</i>"
            )
        else:
            self.l_nightly.setText("<i>no nightly release right now</i>")

        self.l_source.setText(self.source_text(s, v, build))

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
        ci, cs = s.vs_installed, s.vs_stable
        self.render_worth(s)

        # Changelog tabs
        if rel:
            n = rel
            # GitHub renders single newlines in release bodies as line breaks.
            body = re.sub(r"(?<!\n)\n(?!\n)", "  \n", n.body.replace("\r\n", "\n").strip())
            body = body or "_No release notes._"
            self.t_notes.setMarkdown(f"# {n.name}\n\n{body}")
        else:
            self.t_notes.setMarkdown("_No nightly release or Snapshot artifacts found right now._")
        self.t_inst.setHtml(self.commits_html(ci, "your installed build"))
        self.t_stable.setHtml(self.commits_html(cs, f"stable ({st.tag if st else '?'})"))
        self.tabs.setTabText(1, f"New since installed ({ci.ahead_by})" if ci and not ci.error_kind else "New since installed")
        self.tabs.setTabText(2, f"Ahead of stable ({cs.ahead_by})" if cs and not cs.error_kind else "Ahead of stable")
        self.update_buttons()

    def source_text(self, s: core.Status, v: str, build: core.Build | None) -> str:
        if build and build.source == core.SRC_RELEASE:
            return "<b>Release</b>"
        if build:
            if s.nightly is None:
                why = "the nightly release doesn't exist right now"
            elif v not in core.release_variants(s.nightly):
                why = "the nightly release doesn't publish this variant"
            else:
                why = "the release assets kept 404ing"
            return f"<b>Actions artifact</b> ({html.escape(build.origin)}), because {why}"
        if not s.has_token and (s.nightly is None or s.rebuilding):
            return ("<b>None</b>. The nightly release is missing or being rebuilt, and the Actions "
                    "artifact fallback needs GitHub auth (<code>gh auth login</code>).")
        if s.snapshot_error:
            return f"<b>None</b>. Couldn't list Snapshot artifacts: {html.escape(s.snapshot_error)}"
        return "<b>None</b>. Nightly is being rebuilt and no Snapshot artifact was found."

    def render_worth(self, s: core.Status):
        """Fill the two 'Worth updating?' lines. Only friendly text here:
        error details are in the log file."""
        inst = core.installed_build()
        ci = s.vs_installed
        retry = False
        if not inst:
            t = "Not installed yet."
        elif ci is None:
            t = ("Installed build has no commit id to compare." if not core.commit_from_build(inst)
                 else "Nightly commit unknown right now.")
        elif ci.error_kind == "not_found":
            t = "Your installed commit isn't on GitHub any more, so it can't be compared."
        elif ci.error_kind:
            t = f"Couldn't check how far behind you are ({html.escape(ci.error_text)})."
            retry = True
        elif ci.ahead_by == 0:
            t = "<b>Up to date</b>"
        else:
            n = ci.ahead_by
            t = (f'<b>Nightly is <a href="{ci.html_url}">{n} commit{"s" * (n != 1)}</a> '
                 "ahead of your build.</b>")
        self.l_worth_inst.setText(t)
        self.b_retry_cmp.setVisible(retry)

        cs = s.vs_stable
        if cs is None:
            st = ""
        elif cs.error_kind:
            st = f"Couldn't compare nightly with stable ({html.escape(cs.error_text)})."
        else:
            st = f'Nightly is <a href="{cs.html_url}">{cs.ahead_by} commits</a> ahead of stable'
            if cs.behind_by:
                st += f" (stable has {cs.behind_by} not in nightly)"
            st += "."
        self.l_worth_stable.setText(st)
        self.l_worth_stable.setVisible(bool(st))

    def on_retry_compare(self):
        s = self.state
        if not s or not s.compared_sha:
            return
        gh = core.GitHub(self.cancel)
        installed, head = core.installed_build(), s.compared_sha
        self.run(lambda progress, status: core.compare_installed(gh, installed, head),
                 on_done=self.on_compare_retried, label="Checking how far behind you are…")

    @Slot(object)
    def on_compare_retried(self, c):
        self.idle_bar()
        if self.state:
            self.state.vs_installed = c
            self.render()
        failed = c is not None and c.error_kind not in (None, "not_found")
        self.msg.setText("Check failed again; details are in the log." if failed
                         else f"Last checked {datetime.now():%H:%M:%S}")

    def commits_html(self, c: core.Compare | None, what: str) -> str:
        if c is None:
            return f"<p><i>Nothing to compare against {html.escape(what)}.</i></p>"
        if c.error_kind:
            return f"<p><i>Couldn't load the commit list ({html.escape(c.error_text)}).</i></p>"
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
        job, self.after_job = self.after_job, None
        if job:
            job()

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
        v = self.cur_variant()
        self.run(lambda progress, status: core.check_status(gh, v, status),
                 on_done=self.on_status, label="Checking for updates…")

    @Slot(object)
    def on_status(self, s: core.Status):
        self.state = s
        self.idle_bar()
        self.render()
        if s.rebuilding and not s.pick(self.cur_variant()):
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
        v = self.cur_variant()
        self.run(lambda progress, status: core.install(gh, v, progress, status),
                 on_done=self.on_installed, label="Installing…")

    @Slot(object)
    def on_installed(self, build: str):
        self.idle_bar(full=True)
        self.notice = f"✔ Installed {build}. Restart Steam if it was running."
        self.msg.setText(self.notice)
        # Re-check so the compare data reflects the new installed build.
        self.after_job = self.refresh

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
        self.after_job = self.refresh

    def on_variant_changed(self, _index: int):
        v = self.cur_variant()
        self._update_variant_tooltip()
        self.cfg["variant"] = v
        try:
            core.save_config(self.cfg)
        except OSError as e:
            self.msg.setText(f"⚠ Could not save config: {e}")
        s = self.state
        if s:
            b = s.pick(v)
            if b and b.sha != s.compared_sha:
                self.refresh()  # different commit: redo the compares
            else:
                self.render()
        else:
            self.render_local()

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
