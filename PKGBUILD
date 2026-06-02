# Maintainer: Will Handley <wh260@cam.ac.uk>
pkgname=python-mddb
pkgver=$(grep '^version = ' pyproject.toml | head -1 | sed 's/.*= "\(.*\)"/\1/')
pkgrel=1
pkgdesc='A YAML+markdown card substrate for agentic knowledge work'
arch=('any')
url='https://github.com/handley-lab/mddb'
license=('MIT')
depends=('python' 'python-yaml' 'git')
makedepends=('python-build' 'python-installer' 'python-wheel' 'python-setuptools')

build() {
  cd "$startdir"
  python -m build --wheel --no-isolation
}

check() {
  cd "$startdir"
  python -m installer --destdir="$srcdir/test-install" dist/*.whl
  PYTHONPATH="$srcdir/test-install$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    python -m pytest tests/
}

package() {
  cd "$startdir"
  python -m installer --destdir="$pkgdir" dist/*.whl
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
