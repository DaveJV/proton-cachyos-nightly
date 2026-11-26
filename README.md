# proton-cachyos-nightly

A desktop app that installs and updates the nightly builds of
[Proton-CachyOS](https://github.com/CachyOS/proton-cachyos) for Steam and
[Faugus Launcher](https://github.com/Faugus/faugus-launcher). Nightlies get
game fixes and new features before they reach a stable release. This app
installs them in one click and lets you go back if one breaks a game.

## Features

- Installs or updates to the latest nightly in one click.
- Tells you when an update is available and lists what changed since your build.
- Shows what the nightly adds over the current stable release.
- Picks the right build for your CPU and hides builds your CPU can't run.
- Rolls back to the previous build in one click.
- Checks every download for corruption before installing it.
- Keeps the same tool name across updates, so Steam and Faugus don't need to be set up again.

## Before you install

- **Nightly builds are experimental.** Any of them can be unstable, including
  the default build the app picks for you. Builds labelled *experimental* in
  the app are the least tested. Keep a stable Proton installed for games you
  can't afford to break.
- **GitHub sign-in is optional.** You only need it for the extra builds that
  aren't part of the nightly release, and for installing while the nightly
  release is missing or being rebuilt. To sign in, install `github-cli` and
  run `gh auth login`.

## Install (CachyOS / Arch)

```sh
git clone https://github.com/DaveJV/proton-cachyos-nightly.git
cd proton-cachyos-nightly
makepkg -si
```

Open **Proton-CachyOS Nightly** from your app menu and click **Install**.
Restart Steam once, then choose `proton-cachyos-nightly` as the compatibility
tool for a game in Steam or Faugus.

## Known issues

- Any nightly can be unstable, x86_64_v3 included. The v4 and LLVM builds are
  the least tested: `x86_64_v4-llvm` crashed wineserver on one machine (Zen 4,
  Mesa 26.2.x). If a build breaks a game, click **Rollback**, or try a
  different variant.
- Some Intel 12th gen and newer CPUs report AVX-512 support they can't use,
  so the app may offer v4 builds on a CPU that can't run them.

## Technical details

**Where builds come from.** The app uses the newest pre-release in
CachyOS/proton-cachyos whose tag contains `nightly`. If that release is
missing, its files for your variant keep failing to download, or you pick a
variant it doesn't publish (`x86_64_v4`, `x86_64-llvm`, `x86_64-wow64`,
`x86_64-llvm-wow64`), it uses the artifacts of the newest successful
`snapshot.yml` workflow run on a `*/dev` branch instead. Downloading artifacts
needs a GitHub token: `$GITHUB_TOKEN`, `$GH_TOKEN` or `gh auth token`.

**Default variant.** Detected from `/proc/cpuinfo`: `x86_64_v3` if the CPU
supports x86-64-v3, otherwise `x86_64`, and `arm64` on non-x86 machines.
v4 builds are only offered when all the AVX-512 flags they need are present.

**Install and rollback.** The app refuses to update while anything is running
from the install folder. It verifies the SHA-512 checksum, extracts into a
temporary folder, sets the tool name in `compatibilitytool.vdf` to
`proton-cachyos-nightly`, then moves the current build to `previous/` and the
new one into place. Rollback swaps the two folders.

| Path | What |
|---|---|
| `~/.local/share/Steam/compatibilitytools.d/proton-cachyos-nightly/` | The installed build. `.build` holds its full name. |
| `~/.local/share/proton-cachyos-nightly/previous/` | The previous build, used by Rollback |
| `~/.local/share/proton-cachyos-nightly/proton-cachyos-nightly.log` | Error details (the UI shows a short message only) |
| `~/.config/proton-cachyos-nightly/config.json` | Chosen variant |
| `~/.cache/proton-cachyos-nightly/api/` | Cached GitHub API responses |

**Development.** Run from a checkout with `python -m proton_cachyos_nightly`.
Run the offline tests with `python -m unittest discover -s tests -v`.
`PCN_COMPAT_DIR` and `PCN_DATA_DIR` redirect the install and data folders;
`PCN_CPU_FLAGS` and `PCN_MACHINE` override CPU detection.

## License

MIT
