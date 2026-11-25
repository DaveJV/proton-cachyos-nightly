"""Non-GUI logic: GitHub queries, download/verify, install, rollback.

Everything here is blocking and Qt-free so it can run in a worker thread
(or be driven from a script / test).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
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

# Display order. The Snapshot workflow builds all of these; the nightly
# release only publishes RELEASE_VARIANTS, the rest are Actions-artifact only.
KNOWN_VARIANTS = [
    "x86_64_v4-llvm", "x86_64_v4", "x86_64_v3-llvm", "x86_64_v3",
    "x86_64-llvm", "x86_64", "x86_64-llvm-wow64", "x86_64-wow64", "arm64",
]
RELEASE_VARIANTS = {"x86_64_v4-llvm", "x86_64_v3-llvm", "x86_64_v3", "x86_64", "arm64"}

SNAPSHOT_WORKFLOW = "snapshot.yml"
# How many times a missing/404ing release asset is retried before falling
# back to the Snapshot workflow's artifacts.
RELEASE_RETRIES = 3

SRC_RELEASE = "release"
SRC_ARTIFACT = "artifact"
SOURCE_LABEL = {SRC_RELEASE: "Release", SRC_ARTIFACT: "Actions artifact"}

# Release assets are proton-cachyos-…; older Snapshot artifacts were proton-proton-….
ASSET_RE = re.compile(
    r"^proton-(?P<version>.+?)-(?P<variant>x86_64(?:_v\d)?(?:-llvm)?(?:-wow64)?|arm64)\.tar\.xz$"
)
SHA_RE = re.compile(r"-g(?P<sha>[0-9a-f]{7,40})(?:-|$)")

ProgressCb = Callable[[int, int], None]
StatusCb = Callable[[str], None]


class UpdaterError(Exception):
    pass


class NightlyRebuilding(UpdaterError):
    """The nightly release (or the wanted asset) is temporarily missing."""

    def __init__(self, msg: str = "Nightly is being rebuilt."):
        super().__init__(msg)


class NeedsAuth(UpdaterError):
    pass


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
    zipped: bool = False  # Actions artifact: a zip holding one file named `name`


@dataclass
class Build:
    """One variant of one release: the tarball plus its checksum file."""

    name: str  # tarball name without .tar.xz, also what goes in .build
    variant: str
    tarball: Asset
    checksum: Asset | None
    source: str = SRC_RELEASE
    origin: str = ""  # e.g. "run #658"

    @property
    def sha(self) -> str | None:
        return commit_from_build(self.name)

    @property
    def source_label(self) -> str:
        label = SOURCE_LABEL[self.source]
        return f"{label} ({self.origin})" if self.origin else label


@dataclass
class Release:
    tag: str
    name: str
    body: str
    html_url: str
    published_at: str
    prerelease: bool
    builds: dict[str, Build] = field(default_factory=dict)  # variant -> Build
    source: str = SRC_RELEASE

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
    nightly: Release | None  # the rolling release, if it exists
    snapshot: Release | None  # newest Snapshot run, as a pseudo-release
    stable: Release | None
    repo_package: tuple[str, str] | None  # (package name, version)
    vs_installed: Compare | None
    vs_stable: Compare | None
    compared_sha: str | None = None  # nightly commit the compares were made against
    rebuilding: bool = False  # release asset missing after retries
    has_token: bool = False
    variants: list[str] = field(default_factory=list)  # selectable
    hidden_variants: list[str] = field(default_factory=list)  # artifact-only, no auth
    snapshot_error: str | None = None

    def pick(self, variant: str) -> Build | None:
        return pick_build(variant, self.nightly, self.snapshot, self.has_token, self.rebuilding)

    def release_for(self, build: Build | None) -> Release | None:
        if build is None:
            return self.nightly or self.snapshot
        return self.snapshot if build.source == SRC_ARTIFACT else self.nightly


def release_variants(nightly: Release | None) -> set[str]:
    return RELEASE_VARIANTS | (set(nightly.builds) if nightly else set())


def pick_build(
    variant: str,
    nightly: Release | None,
    snapshot: Release | None,
    has_token: bool,
    fallback: bool,
) -> Build | None:
    """Release asset first; the Actions artifact when the release lacks the
    variant entirely, doesn't exist, or (fallback=True) kept 404ing."""
    rb = nightly.builds.get(variant) if nightly else None
    if rb and rb.checksum:
        return rb
    use_artifact = nightly is None or fallback or variant not in release_variants(nightly)
    if use_artifact and has_token and snapshot:
        return snapshot.builds.get(variant)
    return None


def sort_variants(vs) -> list[str]:
    return sorted(set(vs), key=lambda v: (KNOWN_VARIANTS.index(v) if v in KNOWN_VARIANTS else 99, v))


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
        self.token = github_token()
        if self.token:
            self.s.headers["Authorization"] = f"Bearer {self.token}"

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
        if r.status_code == 401 and self.token:
            raise NeedsAuth("GitHub rejected the gh token. Run `gh auth login` again.")
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

    # -- snapshot workflow artifacts ---------------------------------------

    def latest_snapshot(self, max_runs: int = 5) -> Release | None:
        """Newest successful push run of the Snapshot workflow on a */dev
        branch that still has unexpired artifacts, as a pseudo-release."""
        d = self.get_json(
            f"{API}/actions/workflows/{SNAPSHOT_WORKFLOW}/runs",
            {"event": "push", "status": "success", "per_page": 30},
        )
        runs = [
            r for r in d.get("workflow_runs", [])
            if (r.get("head_branch") or "").endswith("/dev") and r.get("conclusion") == "success"
        ]
        runs.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        for run in runs[:max_runs]:
            arts = self.get_json(f"{API}/actions/runs/{run['id']}/artifacts", {"per_page": 100})
            live = {a["name"]: a for a in arts.get("artifacts", []) if not a.get("expired")}
            origin = f"run #{run.get('run_number', run['id'])}"
            rel = _snapshot_release(run)
            for aname, a in live.items():
                m = ASSET_RE.match(aname)
                if not m:
                    continue
                base = aname[: -len(".tar.xz")]
                cs = live.get(base + ".sha512sum")
                if not cs:
                    continue
                rel.builds[m.group("variant")] = Build(
                    name=base,
                    variant=m.group("variant"),
                    tarball=Asset(aname, a["archive_download_url"], a.get("size_in_bytes", 0), zipped=True),
                    checksum=Asset(cs["name"], cs["archive_download_url"], cs.get("size_in_bytes", 0), zipped=True),
                    source=SRC_ARTIFACT,
                    origin=origin,
                )
            if rel.builds:
                return rel
        return None

    # -- downloads ----------------------------------------------------------

    def _open(self, asset: Asset, timeout: float = 60):
        if asset.zipped and not self.token:
            raise NeedsAuth(
                "Actions artifacts can only be downloaded with GitHub auth. Run `gh auth login`."
            )
        # requests drops the Authorization header on the cross-host redirect
        # to the blob store, as required.
        # The artifact zip endpoint answers 415 to Accept: octet-stream.
        headers = {} if asset.zipped else {"Accept": "application/octet-stream"}
        r = self.s.get(asset.url, stream=True, timeout=timeout, headers=headers)
        if r.status_code == 404 and not asset.zipped:
            r.close()
            raise NightlyRebuilding()
        if r.status_code in (404, 410) and asset.zipped:
            r.close()
            raise UpdaterError(f"Artifact {asset.name} has expired or was removed. Refresh and try again.")
        if r.status_code in (401, 403) and asset.zipped:
            r.close()
            raise NeedsAuth("GitHub refused the artifact download. Check `gh auth status`.")
        r.raise_for_status()
        return r

    def _stream(self, asset: Asset, dest: Path, progress: ProgressCb | None) -> str:
        h = hashlib.sha512()
        with self._open(asset) as r:
            total = int(r.headers.get("Content-Length") or 0) or asset.size
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

    def download(self, asset: Asset, dest: Path, progress: ProgressCb | None = None,
                 status: StatusCb | None = None) -> str:
        """Fetch asset to dest (unzipping artifacts), return the sha512 of dest."""
        if not asset.zipped:
            return self._stream(asset, dest, progress)
        zpath = dest.with_name(dest.name + ".zip")
        self._stream(asset, zpath, progress)
        if status:
            status(f"Unzipping {asset.name}…")
        if progress:
            progress(0, 0)
        try:
            digest = unzip_member(zpath, asset.name, dest, self.cancel)
        finally:
            zpath.unlink(missing_ok=True)
        return digest

    def fetch_text(self, asset: Asset) -> str:
        if not asset.zipped:
            with self._open(asset, timeout=30) as r:
                return r.text
        with self._open(asset, timeout=30) as r:
            data = r.content
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            member = _zip_member(zf, asset.name)
            return zf.read(member).decode()


def _zip_member(zf: zipfile.ZipFile, name: str) -> zipfile.ZipInfo:
    infos = [i for i in zf.infolist() if not i.is_dir()]
    for i in infos:
        if i.filename == name:
            return i
    if len(infos) == 1:
        return infos[0]
    raise UpdaterError(f"Artifact zip doesn't contain {name}")


def unzip_member(zpath: Path, name: str, dest: Path, cancel: threading.Event) -> str:
    """Extract the single file of an artifact zip to dest (never trusting the
    member's path), returning its sha512."""
    h = hashlib.sha512()
    with zipfile.ZipFile(zpath) as zf:
        member = _zip_member(zf, name)
        with zf.open(member) as src, open(dest, "wb") as out:
            while chunk := src.read(1 << 20):
                if cancel.is_set():
                    raise Cancelled()
                out.write(chunk)
                h.update(chunk)
    return h.hexdigest()


def _snapshot_release(run: dict) -> Release:
    sha = run.get("head_sha", "")
    msg = ((run.get("head_commit") or {}).get("message") or "").splitlines()
    branch = run.get("head_branch", "")
    num = run.get("run_number", run["id"])
    body = (
        f"Using artifacts from Snapshot workflow run [#{num}]({run.get('html_url', '')}) "
        f"on `{branch}`, commit [`{sha[:8]}`](https://github.com/{REPO}/commit/{sha}).\n\n"
        "The rolling nightly release is missing or doesn't publish this variant, so "
        "the build comes from the workflow's artifacts instead.\n\n"
    )
    if msg:
        body += f"**Head commit:** {msg[0]}\n"
    return Release(
        tag=f"snapshot-run-{num}",
        name=f"Snapshot run #{num} ({branch})",
        body=body,
        html_url=run.get("html_url", ""),
        published_at=run.get("updated_at") or run.get("created_at") or "",
        prerelease=True,
        source=SRC_ARTIFACT,
    )


def github_token() -> str | None:
    """$GITHUB_TOKEN / $GH_TOKEN, else `gh auth token`. Only needed for
    Actions artifacts (and it raises the API rate limit)."""
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok.strip()
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    tok = r.stdout.strip()
    return tok if r.returncode == 0 and tok else None


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

def check_status(
    gh: GitHub,
    variant: str,
    status: StatusCb | None = None,
    retries: int = RELEASE_RETRIES,
    retry_delay: float = 10,
) -> Status:
    status = status or (lambda _m: None)
    has_token = gh.token is not None

    status("Checking Snapshot workflow artifacts…")
    snapshot = snapshot_error = None
    try:
        snapshot = gh.latest_snapshot()
    except (requests.RequestException, UpdaterError, ValueError) as e:
        snapshot_error = str(e)

    nightly = stable = None
    rebuilding = False
    for attempt in range(retries):
        status("Checking GitHub for releases…")
        nightly, stable = gh.latest_nightly_and_stable()
        if nightly is None:
            rebuilding = True  # release vanished: fall back right away
            break
        if variant not in release_variants(nightly):
            rebuilding = False  # artifact-only variant, nothing to wait for
            break
        b = nightly.builds.get(variant)
        if b and b.checksum:
            rebuilding = False
            break
        rebuilding = True
        if attempt < retries - 1:
            status("Nightly is being rebuilt, retrying…")
            _sleep(gh.cancel, retry_delay)

    rel_vs = release_variants(nightly)
    art_vs = set(snapshot.builds) if snapshot else set()
    artifact_only = (art_vs | (set(KNOWN_VARIANTS) - RELEASE_VARIANTS)) - rel_vs
    if has_token:
        variants, hidden = sort_variants(rel_vs | art_vs), []
    else:
        variants, hidden = sort_variants(rel_vs), sort_variants(artifact_only)

    build = pick_build(variant, nightly, snapshot, has_token, rebuilding)
    head = build.sha if build else (nightly.sha if nightly else None)

    installed = installed_build()
    vs_installed = vs_stable = None
    if head:
        inst_sha = commit_from_build(installed) if installed else None
        if inst_sha:
            status("Comparing installed build with nightly…")
            vs_installed = gh.compare(inst_sha, head)
        if stable:
            status("Comparing stable with nightly…")
            vs_stable = gh.compare(stable.tag, head)

    return Status(
        variant=variant,
        installed=installed,
        previous=previous_build(),
        nightly=nightly,
        snapshot=snapshot,
        stable=stable,
        repo_package=repo_package(),
        vs_installed=vs_installed,
        vs_stable=vs_stable,
        compared_sha=head,
        rebuilding=rebuilding,
        has_token=has_token,
        variants=variants,
        hidden_variants=hidden,
        snapshot_error=snapshot_error,
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
    release_retries: int = RELEASE_RETRIES,
) -> str:
    """Download, verify, extract and swap in the newest nightly for variant.
    Returns the installed build name."""
    status = status or (lambda _m: None)
    progress = progress or (lambda _d, _t: None)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COMPAT_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_temp()

    deadline = time.monotonic() + max_wait
    failures = 0
    while True:
        try:
            return _install_once(gh, variant, progress, status, fallback=failures >= release_retries)
        except NightlyRebuilding as e:
            failures += 1
            if time.monotonic() + retry_delay > deadline:
                raise
            progress(0, 0)
            if failures == release_retries and gh.token:
                status("Release assets still missing, falling back to Actions artifacts…")
                continue
            status(f"{e} Retrying in {int(retry_delay)} s…")
            _sleep(gh.cancel, retry_delay)
        finally:
            _cleanup_temp()


def _resolve_build(gh: GitHub, variant: str, status: StatusCb, fallback: bool) -> Build:
    status("Looking up latest nightly…")
    nightly, _ = gh.latest_nightly_and_stable()
    rb = nightly.builds.get(variant) if nightly else None
    # After repeated 404s, a listed release asset isn't trusted any more.
    if rb and rb.checksum and not (fallback and gh.token):
        return rb
    artifact_only = variant not in release_variants(nightly)
    if not (nightly is None or fallback or artifact_only):
        raise NightlyRebuilding()
    if not gh.token:
        if artifact_only:
            raise NeedsAuth(
                f"{variant} is only built as an Actions artifact, and downloading "
                "artifacts needs GitHub auth. Run `gh auth login`."
            )
        raise NightlyRebuilding(
            "The nightly release is missing or being rebuilt, and the Actions "
            "artifact fallback needs GitHub auth (`gh auth login`)."
        )
    status("Looking up Snapshot workflow artifacts…")
    snap = gh.latest_snapshot()
    ab = snap.builds.get(variant) if snap else None
    if not ab:
        raise UpdaterError(f"No unexpired Snapshot artifact found for {variant}.")
    return ab


def _install_once(gh: GitHub, variant: str, progress: ProgressCb, status: StatusCb,
                  fallback: bool = False) -> str:
    build = _resolve_build(gh, variant, status, fallback)

    if installed_build() == build.name:
        status(f"{build.name} is already installed.")
        return build.name

    # Fail early rather than after a 300 MB download.
    ensure_not_in_use()

    tmp = Path(tempfile.mkdtemp(prefix="tmp-", dir=DATA_DIR))
    status(f"Source: {build.source_label}. Fetching checksum…")
    sumtext = gh.fetch_text(build.checksum)
    expected = None
    for line in sumtext.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == build.tarball.name:
            expected = parts[0].lower()
    if not expected or not re.fullmatch(r"[0-9a-f]{128}", expected):
        raise UpdaterError(f"Could not parse {build.checksum.name}")

    status(f"Downloading {build.tarball.name} ({build.source_label})…")
    tarball = tmp / build.tarball.name
    got = gh.download(build.tarball, tarball, progress, status)
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
