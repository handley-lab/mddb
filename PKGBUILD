# Maintainer: Will Handley <wh260@cam.ac.uk>
pkgname=python-mddb
pkgver=$(grep '^version = ' pyproject.toml | head -1 | sed 's/.*= "\(.*\)"/\1/')
pkgrel=1
pkgdesc='A YAML+markdown card substrate for agentic knowledge work'
arch=('any')
url='https://github.com/handley-lab/mddb'
license=('MIT')
depends=('python' 'python-yaml' 'python-slugify' 'git')

package() {
  cd "$startdir"
  local purelib
  purelib=$(env -u VIRTUAL_ENV PATH=/usr/bin:/bin \
    python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
  install -Dm644 src/mddb/__init__.py "$pkgdir/$purelib/mddb/__init__.py"
  install -Dm644 src/mddb/_core.py    "$pkgdir/$purelib/mddb/_core.py"
  install -Dm644 src/mddb/_index.py   "$pkgdir/$purelib/mddb/_index.py"
  install -Dm644 src/mddb/card.py     "$pkgdir/$purelib/mddb/card.py"
  install -Dm644 src/mddb/schema.sql  "$pkgdir/$purelib/mddb/schema.sql"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
