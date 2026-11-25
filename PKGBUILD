# Maintainer: DaveJV
pkgname=proton-cachyos-nightly-git
_name=proton-cachyos-nightly
pkgver=0.1.0.r0.g0000000
pkgrel=1
pkgdesc="PySide6 app to install and update proton-cachyos nightly builds for Steam and Faugus"
arch=('any')
url="https://github.com/DaveJV/proton-cachyos-nightly"
license=('MIT')
depends=('python' 'pyside6' 'python-requests' 'tar' 'xz' 'procps-ng')
makedepends=('git')
optdepends=('steam: to use the installed compatibility tool'
            'faugus-launcher: to use the installed compatibility tool')
provides=("$_name")
conflicts=("$_name")
source=("$_name::git+$url.git")
sha256sums=('SKIP')

pkgver() {
  cd "$_name"
  local ver
  ver=$(python -c 'import re;print(re.search(r"\"(.+?)\"", open("proton_cachyos_nightly/__init__.py").read())[1])')
  printf "%s.r%s.g%s" "$ver" "$(git rev-list --count HEAD)" "$(git rev-parse --short=7 HEAD)"
}

build() {
  cd "$_name"
  python -m compileall -q proton_cachyos_nightly
}

package() {
  cd "$_name"
  local site
  site=$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
  install -d "$pkgdir$site"
  cp -r --no-preserve=ownership proton_cachyos_nightly "$pkgdir$site/"
  install -Dm755 bin/$_name "$pkgdir/usr/bin/$_name"
  install -Dm644 packaging/$_name.desktop "$pkgdir/usr/share/applications/$_name.desktop"
  install -Dm644 packaging/$_name.svg "$pkgdir/usr/share/icons/hicolor/scalable/apps/$_name.svg"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
  install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
}
