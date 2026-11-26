"""PySide6 front end."""

from __future__ import annotations

import html
import sys
import threading
from datetime import datetime

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QFont, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStyle,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from . import __version__, core

REBUILD_RETRY_MS = 2 * 60 * 1000


class Worker(QObject):
    progress = Signal(float, float)
    status = Signal(str)
    done = Signal(object)
    failed = Signal(str, bool)  # message, is_rebuilding

    def __init__(self, fn, *args, context: str = "The operation"):
        super().__init__()
        self.fn = fn
        self.args = args
        self.context = context  # e.g. "Installing", used in error text

    @Slot()
    def run(self):
        try:
            res = self.fn(*self.args, progress=self.progress.emit, status=self.status.emit)
        except core.NightlyRebuilding as e:
            self.failed.emit(str(e), True)
        except core.Cancelled as e:
            self.failed.emit(str(e), False)
        except Exception as e:  # noqa: BLE001 - anything else becomes friendly text
            # Full details (URLs, tracebacks, tool output) go to the log only.
            self.failed.emit(core.report_error(e, self.context), False)
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

        # Status: three headline columns, then a smaller grid of details.
        # Colours come from the palette only (PlaceholderText for muted text,
        # Link for links), so the panel follows the Qt/KDE colour scheme.
        box = QGroupBox("Status")
        sv = QVBoxLayout(box)
        heads = QHBoxLayout()
        heads.setSpacing(16)

        self.l_installed, self.l_installed_sub = self._column(heads, "Installed")
        heads.addWidget(self._line(QFrame.Shape.VLine))
        self.l_nightly, self.l_nightly_sub = self._column(heads, "Latest nightly")
        heads.addWidget(self._line(QFrame.Shape.VLine))

        # Update status: theme icon + headline, detail line + Retry.
        upd = QVBoxLayout()
        upd.setSpacing(2)
        upd.addWidget(self._caption("Update status"))
        row = QHBoxLayout()
        row.setSpacing(6)
        self.i_update = QLabel()
        row.addWidget(self.i_update)
        self.l_update = self._value_label(headline=True)
        row.addWidget(self.l_update, 1)
        upd.addLayout(row)
        row = QHBoxLayout()
        self.l_update_sub = self._value_label(muted=True)
        row.addWidget(self.l_update_sub, 1)
        self.b_retry_cmp = QPushButton(QIcon.fromTheme("view-refresh"), "Retry")
        self.b_retry_cmp.setToolTip("Check again whether an update is available")
        self.b_retry_cmp.clicked.connect(self.on_retry_compare)
        self.b_retry_cmp.hide()
        row.addWidget(self.b_retry_cmp, 0, Qt.AlignmentFlag.AlignTop)
        upd.addLayout(row)
        upd.addStretch(1)
        heads.addLayout(upd, 1)
        sv.addLayout(heads)

        sv.addWidget(self._line(QFrame.Shape.HLine))
        details = QGridLayout()
        details.setHorizontalSpacing(8)
        details.setVerticalSpacing(2)
        self.l_previous = self._detail(details, 0, 0, "Previous")
        self.l_source = self._detail(details, 0, 2, "Source")
        self.l_stable = self._detail(details, 1, 0, "Stable (GitHub)")
        self.l_repo = self._detail(details, 1, 2, "Stable (CachyOS repo)")
        details.setColumnStretch(1, 1)
        details.setColumnStretch(3, 1)
        sv.addLayout(details)
        root.addWidget(box)

        # Changes: built from the compare data (the nightly release body is
        # only a version line and a link).
        self.tabs = QTabWidget()
        self.t_new = self._browser()
        self.t_stable = self._browser()
        self.tabs.addTab(self.t_new, "What's new")
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

    def _scaled_font(self, factor: float, bold: bool = False) -> QFont:
        f = QFont(self.font())
        f.setPointSizeF(f.pointSizeF() * factor)
        f.setBold(bold)
        return f

    def _caption(self, text: str) -> QLabel:
        l = QLabel(text)
        l.setFont(self._scaled_font(0.9))
        l.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        return l

    def _value_label(self, headline: bool = False, muted: bool = False) -> QLabel:
        l = QLabel("…" if headline else "")
        l.setTextFormat(Qt.TextFormat.RichText)
        l.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        l.linkActivated.connect(self.on_link)
        l.setWordWrap(True)
        if headline:
            l.setFont(self._scaled_font(1.25, bold=True))
        if muted:
            l.setForegroundRole(QPalette.ColorRole.PlaceholderText)
        return l

    def _column(self, layout: QHBoxLayout, caption: str) -> tuple[QLabel, QLabel]:
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(self._caption(caption))
        value = self._value_label(headline=True)
        sub = self._value_label(muted=True)
        col.addWidget(value)
        col.addWidget(sub)
        col.addStretch(1)
        layout.addLayout(col, 1)
        return value, sub

    def _detail(self, grid: QGridLayout, row: int, col: int, caption: str) -> QLabel:
        cap = self._caption(caption + ":")
        cap.setFont(self.font())
        grid.addWidget(cap, row, col, Qt.AlignmentFlag.AlignTop)
        value = self._value_label()
        grid.addWidget(value, row, col + 1, Qt.AlignmentFlag.AlignTop)
        return value

    @staticmethod
    def _line(shape: QFrame.Shape) -> QFrame:
        f = QFrame()
        f.setFrameShape(shape)
        f.setFrameShadow(QFrame.Shadow.Sunken)
        return f

    def _set_icon(self, *names: str, fallback: QStyle.StandardPixmap):
        icon = next((i for n in names if not (i := QIcon.fromTheme(n)).isNull()),
                    self.style().standardIcon(fallback))
        size = self.l_update.fontMetrics().height()
        self.i_update.setPixmap(icon.pixmap(size, size))

    def _muted_css(self) -> str:
        return self.palette().color(QPalette.ColorRole.PlaceholderText).name()

    @Slot(str)
    def on_link(self, href: str):
        if href == "#whats-new":
            self.tabs.setCurrentWidget(self.t_new)
        else:
            QDesktopServices.openUrl(QUrl(href))

    @staticmethod
    def build_html(name: str) -> str:
        return html.escape(core.short_build_name(name))

    @staticmethod
    def build_tip(name: str, extra: str = "") -> str:
        """Full build name (plus optional extra line) for a tooltip."""
        return name + (f"\n{extra}" if extra else "")

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
        if inst:
            self.l_installed.setText(self.build_html(inst))
            self.l_installed_sub.setText(html.escape(core.variant_from_build(inst) or ""))
        elif core.INSTALL_DIR.is_dir():
            self.l_installed.setText("Unknown build")
            self.l_installed_sub.setText("No build information found")
        else:
            self.l_installed.setText("Not installed")
            self.l_installed_sub.setText("")
        self.l_installed.setToolTip(inst or "")

        prev = core.previous_build()
        if prev and core.variant_from_build(prev):
            self.l_previous.setText(f"{self.build_html(prev)} ({html.escape(core.variant_from_build(prev))})")
        else:
            self.l_previous.setText(html.escape(prev or "None"))
        self.l_previous.setToolTip(prev or "")
        if self.state is None:
            self.l_update.setText("Checking…")
            self.i_update.clear()
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
            self.l_nightly.setText(self.build_html(build.name))
            self.l_nightly_sub.setText(
                f'{html.escape(build.variant)} · <a href="{html.escape(rel.html_url)}">View on GitHub</a>')
            self.l_nightly.setToolTip(self.build_tip(build.name, f"Published {fmt_time(rel.published_at)}"))
        elif s.nightly:
            name = s.nightly.build_name or s.nightly.tag
            self.l_nightly.setText(self.build_html(name))
            self.l_nightly_sub.setText(f"{html.escape(v)} not published yet")
            self.l_nightly.setToolTip(name)
        else:
            self.l_nightly.setText("Unavailable")
            self.l_nightly_sub.setText("No nightly release right now")
            self.l_nightly.setToolTip("")

        self.l_source.setText(self.source_text(s, v, build))

        st, cs = s.stable, s.vs_stable
        if st:
            short = core.short_version(st.tag) or st.tag
            self.l_stable.setText(f'<a href="{html.escape(st.html_url)}">{html.escape(short)}</a>')
            self.l_stable.setToolTip(self.build_tip(st.tag, f"Published {fmt_time(st.published_at)}"))
        else:
            self.l_stable.setText("Unknown")
            self.l_stable.setToolTip("")
        if s.repo_package:
            pkg, ver = s.repo_package
            note = ""
            rd = core.repo_version_tag_date(ver)
            gd = core.repo_version_tag_date(st.tag) if st else None
            if rd and gd:
                note = " (newer than GitHub)" if rd > gd else " (older than GitHub)" if rd < gd else " (same as GitHub)"
            self.l_repo.setText(html.escape(core.short_version(ver) or ver) + note)
            self.l_repo.setToolTip(f"{pkg} {ver}")
        else:
            self.l_repo.setText("Not installed")
            self.l_repo.setToolTip("proton-cachyos-slr isn't installed from the CachyOS repo")

        self.render_update_status(s)

        ci = s.vs_installed
        self.t_new.setHtml(self.whats_new_html(s, rel))
        self.t_stable.setHtml(self.ahead_of_stable_html(cs, st))
        self.tabs.setTabText(0, f"What's new ({ci.ahead_by})" if ci and not ci.error_kind and ci.ahead_by
                             else "What's new")
        self.tabs.setTabText(1, f"Ahead of stable ({cs.ahead_by})" if cs and not cs.error_kind else "Ahead of stable")
        self.update_buttons()

    def source_text(self, s: core.Status, v: str, build: core.Build | None) -> str:
        if build and build.source == core.SRC_RELEASE:
            return "Nightly release"
        if build:
            if s.nightly is None:
                why = "the nightly release doesn't exist right now"
            elif v not in core.release_variants(s.nightly):
                why = "the nightly release doesn't include this variant"
            else:
                why = "the release download kept failing"
            return f"Actions artifact ({html.escape(build.origin)}), because {why}"
        if not s.has_token and (s.nightly is None or s.rebuilding):
            return ("None. The nightly release is missing or being rebuilt, and the backup "
                    "source needs GitHub sign-in (<code>gh auth login</code>).")
        if s.snapshot_error:
            return f"None. Couldn't list Snapshot artifacts ({html.escape(s.snapshot_error)})."
        return "None. The nightly is being rebuilt and no Snapshot artifact was found."

    def render_update_status(self, s: core.Status):
        """Fill the 'Update status' column. Only friendly text here: error
        details are in the log file."""
        inst = core.installed_build()
        ci = s.vs_installed
        retry = False
        SP = QStyle.StandardPixmap
        if not inst:
            head, sub = (("Ready to install", "The latest nightly can be installed") if s.pick(self.cur_variant())
                         else ("Nothing to install", "No nightly is available right now"))
            self._set_icon("dialog-information", fallback=SP.SP_MessageBoxInformation)
        elif ci is None:
            head = "Unable to compare"
            sub = ("The installed build has no commit ID" if not core.commit_from_build(inst)
                   else "The latest nightly's commit is unavailable right now")
            self._set_icon("dialog-information", fallback=SP.SP_MessageBoxInformation)
        elif ci.error_kind == "not_found":
            head, sub = "Unable to compare", "The installed build's commit is no longer on GitHub"
            self._set_icon("dialog-warning", fallback=SP.SP_MessageBoxWarning)
        elif ci.error_kind:
            head, sub = "Unable to check", f"Update check failed ({html.escape(ci.error_text)})"
            retry = True
            self._set_icon("dialog-warning", fallback=SP.SP_MessageBoxWarning)
        elif ci.ahead_by == 0:
            head, sub = "Up to date", "You have the latest nightly"
            self._set_icon("checkmark", "emblem-ok-symbolic", "dialog-ok", fallback=SP.SP_DialogApplyButton)
        else:
            n = ci.ahead_by
            head = "Update available"
            sub = f'<a href="#whats-new">{n} new commit{"s" * (n != 1)}</a> since your build'
            self._set_icon("update-medium", "software-update-available", "system-software-update",
                           fallback=SP.SP_ArrowUp)
        self.l_update.setText(head)
        self.l_update_sub.setText(sub)
        self.b_retry_cmp.setVisible(retry)

    def _changes_list(self, commits: list[tuple[str, str]]) -> str:
        muted = self._muted_css()
        out = []
        for area, items in core.group_commits(commits):
            out.append(f"<h4>{html.escape(area)}</h4><ul>")
            for it in items:
                count = f' <span style="color:{muted}">×{it.count}</span>' if it.count > 1 else ""
                out.append(f"<li>{html.escape(it.text)}{count}</li>")
            out.append("</ul>")
        return "".join(out)

    @staticmethod
    def _links(*links: tuple[str | None, str]) -> str:
        parts = [f'<a href="{html.escape(u)}">{html.escape(t)}</a>' for u, t in links if u]
        return f"<p>{' · '.join(parts)}</p>" if parts else ""

    def whats_new_html(self, s: core.Status, rel: core.Release | None) -> str:
        """Readable list of what changed between the installed build and the
        latest nightly, plus links to the full changelog."""
        inst = core.installed_build()
        ci = s.vs_installed
        full = core.full_changelog_url(rel)
        if full:
            full_link = (full, "Full changelog on GitHub")
        elif rel and rel.source == core.SRC_ARTIFACT:
            full_link = (rel.html_url, "Workflow run on GitHub")
        else:
            full_link = (rel.html_url if rel else None, "Release on GitHub")

        if not inst:
            intro = ("<p>Nothing installed yet. Once you install a nightly, this tab lists what each "
                     "new nightly changes. <i>Ahead of stable</i> shows what the nightly adds over "
                     "the stable release.</p>")
            return intro + self._links(full_link)
        if ci is None or ci.error_kind == "not_found":
            return ("<p>The list of changes since your build isn't available.</p>"
                    + self._links(full_link))
        if ci.error_kind:
            return (f"<p>Couldn't load the list of changes ({html.escape(ci.error_text)}).</p>"
                    + self._links(full_link))
        if not ci.ahead_by:
            return "<p>You have the latest nightly. Nothing new since your build.</p>" + self._links(full_link)
        n = ci.ahead_by
        out = [f"<p><b>{n} change{'s' * (n != 1)}</b> since your build "
               f"({html.escape(core.short_build_name(inst))}).</p>",
               self._links((ci.html_url, "Compare on GitHub"), full_link),
               self._changes_list(ci.commits)]
        if len(ci.commits) < n:
            out.append(f"<p><i>Showing the newest {len(ci.commits)}. The rest are on GitHub.</i></p>")
        return "".join(out)

    def ahead_of_stable_html(self, c: core.Compare | None, st: core.Release | None) -> str:
        what = html.escape(core.short_version(st.tag) or st.tag) if st else "stable"
        if c is None:
            return "<p><i>No stable release to compare with.</i></p>"
        if c.error_kind:
            return f"<p><i>Couldn't load the list of changes ({html.escape(c.error_text)}).</i></p>"
        if not c.ahead_by:
            return f"<p>The nightly has nothing beyond stable {what}.</p>"
        n = c.ahead_by
        intro = f"<b>{n} change{'s' * (n != 1)}</b> in the nightly that stable {what} doesn't have."
        if c.behind_by:
            intro += f" Stable also has {c.behind_by} change{'s' * (c.behind_by != 1)} not in the nightly."
        out = [f"<p>{intro}</p>", self._links((c.html_url, "Compare on GitHub")),
               self._changes_list(c.commits)]
        if len(c.commits) < n:
            out.append(f"<p><i>Showing the newest {len(c.commits)}. The rest are on GitHub.</i></p>")
        return "".join(out)

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
        self.worker = Worker(fn, *args, context=label.rstrip("…. ") or "The operation")
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
            first = msg.splitlines()[0]
            if first.endswith(" Details are in the log:"):
                first = first[: -len(":")] + "."
            self.msg.setText(f"⚠ {first}")
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
            core.report_error(e, "Saving the config")
            self.msg.setText("⚠ Couldn't save your variant choice. Details are in the log.")
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
