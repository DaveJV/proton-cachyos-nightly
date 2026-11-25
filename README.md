# proton-cachyos-nightly

A small PySide6 desktop app that installs and updates the
[proton-cachyos](https://github.com/CachyOS/proton-cachyos) **nightly** builds
for Steam and [Faugus Launcher](https://github.com/Faugus/faugus-launcher).

- Picks the newest pre-release whose tag contains `nightly`, so the
  `11.0` part is never hardcoded.
- Lets you choose a variant: `x86_64_v4-llvm`, `x86_64_v3-llvm`, `x86_64_v3`,
  `x86_64` or `arm64`. The default is `x86_64_v4-llvm` when the CPU supports
  x86-64-v4 (AVX-512), otherwise `x86_64_v3-llvm`.
- Shows the installed build, the latest nightly, the latest stable GitHub
  release and the CachyOS repo package version (`pacman -Q proton-cachyos-slr`).
  The repo package can be newer than GitHub's stable releases.
- **Worth updating?** It takes the commit sha from the build name
  (`…-g2ca6427f-…`) and uses the GitHub compare API to show how many commits
  the nightly is ahead of your installed build and of stable, with commit titles.
- Shows the release notes as markdown.
- Verifies the SHA-512 before extracting, and keeps one previous build for
  rollback.
- Handles the nightly being rebuilt. The upstream Snapshot workflow deletes
  and recreates the release on every build, so assets can be missing for a few
  minutes. The app retries and shows "nightly is being rebuilt".

Network and disk work runs in a worker thread, so the window never freezes.

## Install (CachyOS / Arch)

```sh
git clone https://github.com/DaveJV/proton-cachyos-nightly.git
cd proton-cachyos-nightly
makepkg -si
```

This installs the `proton-cachyos-nightly-git` package system-wide via pacman:
`/usr/bin/proton-cachyos-nightly`, a menu entry and an icon. Dependencies:
`python`, `pyside6`, `python-requests`, `tar`, `xz`, `procps-ng`.

To run from a checkout without installing:

```sh
python -m proton_cachyos_nightly
```

## Where things go

| Path | What |
|---|---|
| `~/.local/share/Steam/compatibilitytools.d/proton-cachyos-nightly/` | The active build. The folder name never changes. |
| `…/proton-cachyos-nightly/.build` | Build name, e.g. `proton-cachyos-11.0-20261125-base-240-g2ca6427f-x86_64_v4-llvm` |
| `~/.local/share/proton-cachyos-nightly/previous/` | The previous build, used by Rollback |
| `~/.local/share/proton-cachyos-nightly/tmp-*` | Temporary download and extract dir, removed afterwards |
| `~/.config/proton-cachyos-nightly/config.json` | Chosen variant |
| `~/.cache/proton-cachyos-nightly/api/` | ETag cache for GitHub API responses |

The internal name and `display_name` in `compatibilitytool.vdf` are rewritten
to `proton-cachyos-nightly`. Steam and Faugus therefore keep the same tool
selected across updates. Restart Steam after the first install so it picks up
the tool.

### Install / Update

1. Find the newest nightly and the asset for the chosen variant.
2. Check that nothing is running from the install folder
   (`pgrep -f <install path>`). If something is, refuse.
3. Download the `.sha512sum`, then stream the `.tar.xz` while hashing it.
4. Abort on checksum mismatch.
5. Extract with `tar` into a temp dir under `~/.local/share/proton-cachyos-nightly/`.
6. Rewrite the `.vdf` and write `.build`.
7. Move the current build to `previous/`, replacing the old one, and move the
   new build into place.

### Rollback

Swaps `previous/` and the installed folder, so you can roll forward again.
It refuses while a process is running from the install folder.

## GitHub rate limit

Unauthenticated GitHub API calls are limited to 60 per hour. A refresh uses
about 3 to 5 calls. Responses are cached with ETags, and `304 Not Modified`
replies don't count against the limit. If you hit the limit, export
`GITHUB_TOKEN` (or `GH_TOKEN`) and the app will use it.

## Testing without touching Steam

`PCN_COMPAT_DIR` and `PCN_DATA_DIR` override the compatibilitytools.d folder
and the data folder:

```sh
PCN_COMPAT_DIR=/tmp/pcn/compat PCN_DATA_DIR=/tmp/pcn/data python -m proton_cachyos_nightly
```

## License

MIT
