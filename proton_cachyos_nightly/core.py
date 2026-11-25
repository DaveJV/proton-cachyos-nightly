"""Non-GUI logic: GitHub queries, download/verify, install, rollback.

Everything here is blocking and Qt-free so it can run in a worker thread
(or be driven from a script / test).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import requests

REPO = "CachyOS/proton-cachyos"
API = f"https://api.github.com/repos/{REPO}"
TOOL_NAME = "proton-cachyos-nightly"

HOME = Path.home()
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
XDG_CACHE = Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache")

# Overridable for testing without touching the real Steam folder.
COMPAT_DIR = Path(
    os.environ.get("PCN_COMPAT_DIR")
    or HOME / ".local/share/Steam/compatibilitytools.d"
)
DATA_DIR = Path(os.environ.get("PCN_DATA_DIR") or HOME / ".local/share" / TOOL_NAME)
INSTALL_DIR = COMPAT_DIR / TOOL_NAME
PREVIOUS_DIR = DATA_DIR / "previous"
CONFIG_FILE = XDG_CONFIG / TOOL_NAME / "config.json"
CACHE_DIR = XDG_CACHE / TOOL_NAME

KNOWN_VARIANTS = ["x86_64_v4-llvm", "x86_64_v3-llvm", "x86_64_v3", "x86_64", "arm64"]

ASSET_RE = re.compile(
    r"^proton-cachyos-(?P<version>.+?)-(?P<variant>x86_64(?:_v\d)?(?:-llvm)?|arm64)\.tar\.xz$"
)
SHA_RE = re.compile(r"-g(?P<sha>[0-9a-f]{7,40})(?:-|$)")

ProgressCb = Callable[[int, int], None]
StatusCb = Callable[[str], None]


class UpdaterError(Exception):
    pass


class NightlyRebuilding(UpdaterError):
    """The nightly release (or the wanted asset) is temporarily missing."""

    def __init__(self, msg: str = "Nightly is being rebuilt, try again in a few minutes."):
        super().__init__(msg)


class Cancelled(UpdaterError):
    def __init__(self):
        super().__init__("Cancelled.")


class ToolInUse(UpdaterError):
    pass


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(CONFIG_FILE)


# --------------------------------------------------------------------------
# CPU detection
# --------------------------------------------------------------------------

_V3_FLAGS = {"avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "abm", "movbe", "xsave"}
_V4_FLAGS = {"avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"}


def cpu_flags() -> set[str]:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("flags"):
                return set(line.split(":", 1)[1].split())
    except OSError:
        pass
    return set()


def default_variant() -> str:
    if platform.machine() in ("aarch64", "arm64"):
        return "arm64"
    flags = cpu_flags()
    if _V3_FLAGS <= flags and _V4_FLAGS <= flags:
        return "x86_64_v4-llvm"
    if _V3_FLAGS <= flags:
        return "x86_64_v3-llvm"
    return "x86_64"


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class Asset:
    name: str
    url: str
    size: int


@dataclass
class Build:
    """One variant of one release: the tarball plus its checksum file."""

    name: str  # tarball name without .tar.xz, also what goes in .build
    variant: str
    tarball: Asset
    checksum: Asset | None

    @property
    def sha(self) -> str | None:
        return commit_from_build(self.name)


@dataclass
class Release:
    tag: str
    name: str
    body: str
    html_url: str
    published_at: str
    prerelease: bool
    builds: dict[str, Build] = field(default_factory=dict)  # variant -> Build

    @property
    def build_name(self) -> str | None:
        """Name of the build without the variant suffix."""
        for b in self.builds.values():
            return b.name[: -len(b.variant) - 1]
        return None

    @property
    def sha(self) -> str | None:
        n = self.build_name
        return commit_from_build(n) if n else None


@dataclass
class Compare:
    base: str
    head: str
    ahead_by: int
    behind_by: int
    commits: list[tuple[str, str]]  # (short sha, title), newest first
    html_url: str
    error: str | None = None


@dataclass
class Status:
    variant: str
    installed: str | None
    previous: str | None
    nightly: Release | None
    stable: Release | None
    repo_package: tuple[str, str] | None  # (package name, version)
    vs_installed: Compare | None
    vs_stable: Compare | None
    rebuilding: bool = False
    variants: list[str] = field(default_factory=list)


def commit_from_build(name: str) -> str | None:
    m = SHA_RE.search(name)
    return m.group("sha") if m else None


def variant_from_build(name: str) -> str | None:
    m = ASSET_RE.match(name + ".tar.xz")
    return m.group("variant") if m else None


# --------------------------------------------------------------------------
# GitHub client
# --------------------------------------------------------------------------

class GitHub:
    def __init__(self, cancel: threading.Event | None = None):
        self.cancel = cancel or threading.Event()
        self.s = requests.Session()
        self.s.headers["User-Agent"] = f"{TOOL_NAME}-updater"
        self.s.headers["Accept"] = "application/vnd.github+json"
        self.s.headers["X-GitHub-Api-Version"] = "2022-11-28"
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if token:
            self.s.headers["Authorization"] = f"Bearer {token}"

    def _cache_path(self, url: str) -> Path:
        return CACHE_DIR / "api" / (hashlib.sha1(url.encode()).hexdigest() + ".json")

    def get_json(self, url: str, params: dict | None = None):
        """GET with an on-disk ETag cache (304 responses don't count against
        the unauthenticated 60 requests/hour limit)."""
        full = requests.Request("GET", url, params=params).prepare().url
        cpath = self._cache_path(full)
        cached = None
        headers = {}
        try:
            cached = json.loads(cpath.read_text())
            headers["If-None-Match"] = cached["etag"]
        except (OSError, ValueError, KeyError):
            cached = None
        r = self.s.get(full, headers=headers, timeout=30)
        if r.status_code == 304 and cached:
            return cached["data"]
        if r.status_code in (403, 429) and r.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(r.headers.get("X-RateLimit-Reset", "0"))
            mins = max(1, round((reset - time.time()) / 60))
            raise UpdaterError(
                f"GitHub API rate limit reached; resets in ~{mins} min. "
                "Set GITHUB_TOKEN to raise the limit."
            )
        if r.status_code == 404:
            raise FileNotFoundError(full)
        r.raise_for_status()
        data = r.json()
        if r.headers.get("ETag"):
            try:
                cpath.parent.mkdir(parents=True, exist_ok=True)
                cpath.write_text(json.dumps({"etag": r.headers["ETag"], "data": data}))
            except OSError:
                pass
        return data

    # -- releases ---------------------------------------------------------

    def releases(self) -> list[Release]:
        raw = self.get_json(f"{API}/releases", {"per_page": 30})
        out = []
        for r in raw:
            if r.get("draft"):
                continue
            rel = Release(
                tag=r["tag_name"],
                name=r.get("name") or r["tag_name"],
                body=r.get("body") or "",
                html_url=r["html_url"],
                published_at=r.get("published_at") or "",
                prerelease=bool(r.get("prerelease")),
            )
            assets = {a["name"]: a for a in r.get("assets", []) if a.get("state", "uploaded") == "uploaded"}
            for aname, a in assets.items():
                m = ASSET_RE.match(aname)
                if not m:
                    continue
                base = aname[: -len(".tar.xz")]
                cs = assets.get(base + ".sha512sum")
                rel.builds[m.group("variant")] = Build(
                    name=base,
                    variant=m.group("variant"),
                    tarball=Asset(aname, a["browser_download_url"], a.get("size", 0)),
                    checksum=Asset(cs["name"], cs["browser_download_url"], cs.get("size", 0)) if cs else None,
                )
            out.append(rel)
        out.sort(key=lambda x: x.published_at, reverse=True)
        return out

    def latest_nightly_and_stable(self) -> tuple[Release | None, Release | None]:
        rels = self.releases()
        nightly = next((r for r in rels if r.prerelease and "nightly" in r.tag.lower()), None)
        stable = next((r for r in rels if not r.prerelease), None)
        return nightly, stable

    def compare(self, base: str, head: str) -> Compare:
        url = f"{API}/compare/{base}...{head}"
        per = 100
        try:
            d = self.get_json(url, {"per_page": per})
            raw = list(d.get("commits", []))
            # Commits come oldest first and paginated; fetch the last page(s)
            # so the list shows the newest ones.
            total = d.get("total_commits", len(raw))
            if total > per:
                last = -(-total // per)
                raw = self.get_json(url, {"per_page": per, "page": last}).get("commits", [])
                if len(raw) < per and last > 2:
                    prev = self.get_json(url, {"per_page": per, "page": last - 1}).get("commits", [])
                    raw = (prev + raw)[-per:]
        except FileNotFoundError:
            return Compare(base, head, 0, 0, [], "", error=f"{base[:12]} not found upstream")
        except (requests.RequestException, UpdaterError) as e:
            return Compare(base, head, 0, 0, [], "", error=str(e))
        commits = [
            (c["sha"][:8], (c["commit"]["message"].splitlines() or [""])[0])
            for c in raw
        ]
        commits.reverse()  # newest first
        return Compare(
            base, head,
            ahead_by=d.get("ahead_by", 0),
            behind_by=d.get("behind_by", 0),
            commits=commits,
            html_url=d.get("html_url", ""),
        )

    # -- downloads ----------------------------------------------------------

    def download(self, url: str, dest: Path, progress: ProgressCb | None = None) -> str:
        """Stream url to dest, return its sha512 hex digest."""
        h = hashlib.sha512()
        with self.s.get(url, stream=True, timeout=60, headers={"Accept": "application/octet-stream"}) as r:
            if r.status_code == 404:
                raise NightlyRebuilding()
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if self.cancel.is_set():
                        raise Cancelled()
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        return h.hexdigest()

    def fetch_text(self, url: str) -> str:
        r = self.s.get(url, timeout=30)
        if r.status_code == 404:
            raise NightlyRebuilding()
        r.raise_for_status()
        return r.text


# --------------------------------------------------------------------------
# Local state
# --------------------------------------------------------------------------

def read_build(path: Path) -> str | None:
    try:
        return (path / ".build").read_text().strip() or None
    except OSError:
        return None


def installed_build() -> str | None:
    return read_build(INSTALL_DIR)


def previous_build() -> str | None:
    if not PREVIOUS_DIR.is_dir():
        return None
    return read_build(PREVIOUS_DIR) or "(unknown build)"


def repo_package() -> tuple[str, str] | None:
    for pkg in ("proton-cachyos-slr", "proton-cachyos"):
        try:
            r = subprocess.run(["pacman", "-Q", pkg], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode == 0:
            name, ver = r.stdout.split()[:2]
            return name, ver
    return None


def repo_version_tag_date(ver: str) -> str | None:
    """'1:11.0.20261015-1' -> '20261015'."""
    m = re.search(r"(\d{8})", ver)
    return m.group(1) if m else None


def tool_in_use(path: Path = INSTALL_DIR) -> list[str]:
    """Processes whose command line mentions the install folder."""
    try:
        r = subprocess.run(["pgrep", "-af", "--", str(path)], capture_output=True, text=True)
    except OSError:
        return []
    me = str(os.getpid())
    return [l for l in r.stdout.splitlines() if l and l.split()[0] != me]


def ensure_not_in_use() -> None:
    procs = tool_in_use()
    if procs:
        lines = "\n".join(p[:160] for p in procs[:5])
        raise ToolInUse(
            "A game or tool is still running from the install folder. "
            "Close it and try again.\n\n" + lines
        )


def rewrite_vdf(tool_dir: Path, name: str = TOOL_NAME) -> None:
    """Set the internal name and display_name in compatibilitytool.vdf so
    Steam/Faugus selections keep pointing at the same tool after updates."""
    vdf = tool_dir / "compatibilitytool.vdf"
    text = vdf.read_text()
    text, n1 = re.subn(
        r'("compat_tools"\s*\{\s*)"[^"]*"',
        lambda m: f'{m.group(1)}"{name}"',
        text,
        count=1,
    )
    text, n2 = re.subn(
        r'("display_name"\s+)"[^"]*"',
        lambda m: f'{m.group(1)}"{name}"',
        text,
    )
    if not n1:
        raise UpdaterError("Unexpected compatibilitytool.vdf layout: internal name not found")
    vdf.write_text(text)


# --------------------------------------------------------------------------
# Status / install / rollback
# --------------------------------------------------------------------------

def check_status(gh: GitHub, variant: str, status: StatusCb | None = None, retries: int = 3) -> Status:
    status = status or (lambda _m: None)
    nightly = stable = None
    rebuilding = False
    for attempt in range(retries):
        status("Checking GitHub for releases…")
        nightly, stable = gh.latest_nightly_and_stable()
        if nightly and variant in nightly.builds and nightly.builds[variant].checksum:
            rebuilding = False
            break
        rebuilding = True
        if attempt < retries - 1:
            status("Nightly is being rebuilt, retrying…")
            _sleep(gh.cancel, 10)

    installed = installed_build()
    vs_installed = vs_stable = None
    if nightly and nightly.sha:
        inst_sha = commit_from_build(installed) if installed else None
        if inst_sha:
            status("Comparing installed build with nightly…")
            vs_installed = gh.compare(inst_sha, nightly.sha)
        if stable:
            status("Comparing stable with nightly…")
            vs_stable = gh.compare(stable.tag, nightly.sha)

    variants = sorted(
        set(nightly.builds if nightly else []) | set(KNOWN_VARIANTS),
        key=lambda v: (KNOWN_VARIANTS.index(v) if v in KNOWN_VARIANTS else 99, v),
    )
    return Status(
        variant=variant,
        installed=installed,
        previous=previous_build(),
        nightly=nightly,
        stable=stable,
        repo_package=repo_package(),
        vs_installed=vs_installed,
        vs_stable=vs_stable,
        rebuilding=rebuilding,
        variants=variants,
    )


def _sleep(cancel: threading.Event, seconds: float) -> None:
    if cancel.wait(seconds):
        raise Cancelled()


def _cleanup_temp() -> None:
    if DATA_DIR.is_dir():
        for p in DATA_DIR.glob("tmp-*"):
            shutil.rmtree(p, ignore_errors=True)


def install(
    gh: GitHub,
    variant: str,
    progress: ProgressCb | None = None,
    status: StatusCb | None = None,
    max_wait: float = 15 * 60,
    retry_delay: float = 30,
) -> str:
    """Download, verify, extract and swap in the newest nightly for variant.
    Returns the installed build name."""
    status = status or (lambda _m: None)
    progress = progress or (lambda _d, _t: None)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COMPAT_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_temp()

    deadline = time.monotonic() + max_wait
    while True:
        try:
            return _install_once(gh, variant, progress, status)
        except NightlyRebuilding as e:
            if time.monotonic() + retry_delay > deadline:
                raise
            status(f"Nightly is being rebuilt, retrying in {int(retry_delay)} s…")
            progress(0, 0)
            _sleep(gh.cancel, retry_delay)
        finally:
            _cleanup_temp()


def _install_once(gh: GitHub, variant: str, progress: ProgressCb, status: StatusCb) -> str:
    status("Looking up latest nightly…")
    nightly, _ = gh.latest_nightly_and_stable()
    if not nightly or variant not in nightly.builds or not nightly.builds[variant].checksum:
        raise NightlyRebuilding()
    build = nightly.builds[variant]

    if installed_build() == build.name:
        status(f"{build.name} is already installed.")
        return build.name

    # Fail early rather than after a 300 MB download.
    ensure_not_in_use()

    tmp = Path(tempfile.mkdtemp(prefix="tmp-", dir=DATA_DIR))
    status("Fetching checksum…")
    sumtext = gh.fetch_text(build.checksum.url)
    expected = None
    for line in sumtext.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == build.tarball.name:
            expected = parts[0].lower()
    if not expected or not re.fullmatch(r"[0-9a-f]{128}", expected):
        raise UpdaterError(f"Could not parse {build.checksum.name}")

    status(f"Downloading {build.tarball.name}…")
    tarball = tmp / build.tarball.name
    got = gh.download(build.tarball.url, tarball, progress)
    if got != expected:
        raise UpdaterError(
            "SHA-512 mismatch, refusing to install.\n"
            f"expected {expected[:32]}…\n     got {got[:32]}…"
        )

    status("Checksum OK. Extracting…")
    progress(0, 0)
    extract = tmp / "extract"
    extract.mkdir()
    r = subprocess.run(
        ["tar", "-xf", str(tarball), "-C", str(extract), "--no-same-owner"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise UpdaterError("tar failed:\n" + r.stderr.strip()[-2000:])
    tarball.unlink()
    if gh.cancel.is_set():
        raise Cancelled()

    vdfs = list(extract.glob("*/compatibilitytool.vdf")) or list(extract.glob("compatibilitytool.vdf"))
    if len(vdfs) != 1:
        raise UpdaterError("Archive layout unexpected: compatibilitytool.vdf not found")
    newdir = vdfs[0].parent
    rewrite_vdf(newdir)
    (newdir / ".build").write_text(build.name + "\n")

    status("Swapping in new build…")
    swap_in(newdir)
    status(f"Installed {build.name}")
    return build.name


def swap_in(newdir: Path) -> None:
    ensure_not_in_use()
    if INSTALL_DIR.exists():
        if PREVIOUS_DIR.exists():
            shutil.rmtree(PREVIOUS_DIR)
        shutil.move(str(INSTALL_DIR), str(PREVIOUS_DIR))
    try:
        shutil.move(str(newdir), str(INSTALL_DIR))
    except Exception:
        if PREVIOUS_DIR.exists() and not INSTALL_DIR.exists():
            shutil.move(str(PREVIOUS_DIR), str(INSTALL_DIR))
        raise


def rollback(status: StatusCb | None = None) -> str | None:
    """Swap the previous build back in; the current one becomes previous."""
    status = status or (lambda _m: None)
    if not PREVIOUS_DIR.is_dir():
        raise UpdaterError("No previous build to roll back to.")
    ensure_not_in_use()
    status("Rolling back…")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    hold = DATA_DIR / "rollback-hold"
    if hold.exists():
        shutil.rmtree(hold)
    had_current = INSTALL_DIR.exists()
    if had_current:
        shutil.move(str(INSTALL_DIR), str(hold))
    try:
        shutil.move(str(PREVIOUS_DIR), str(INSTALL_DIR))
    except Exception:
        if had_current:
            shutil.move(str(hold), str(INSTALL_DIR))
        raise
    if had_current:
        shutil.move(str(hold), str(PREVIOUS_DIR))
    try:
        rewrite_vdf(INSTALL_DIR)
    except (OSError, UpdaterError):
        pass
    b = installed_build()
    status(f"Rolled back to {b or 'previous build'}")
    return b
