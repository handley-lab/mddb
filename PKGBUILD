# Maintainer: Will Handley <wh260@cam.ac.uk>
pkgname=python-mddb
pkgver=$(grep '^version = ' pyproject.toml | head -1 | sed 's/.*= "\(.*\)"/\1/')
pkgrel=1
pkgdesc='A YAML+markdown card substrate for agentic knowledge work'
arch=('any')
url='https://github.com/handley-lab/mddb'
license=('MIT')
depends=('python' 'python-yaml' 'python-slugify' 'git')
optdepends=('python-mcp: MCP server (mcp-mddb) for cross-process agents')
install=python-mddb.install

package() {
  cd "$startdir"
  local purelib
  purelib=$(env -u VIRTUAL_ENV PATH=/usr/bin:/bin \
    python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
  install -Dm644 src/mddb/__init__.py "$pkgdir/$purelib/mddb/__init__.py"
  install -Dm644 src/mddb/_core.py    "$pkgdir/$purelib/mddb/_core.py"
  install -Dm644 src/mddb/_index.py   "$pkgdir/$purelib/mddb/_index.py"
  install -Dm644 src/mddb/card.py     "$pkgdir/$purelib/mddb/card.py"
  install -Dm644 src/mddb/_mcp.py     "$pkgdir/$purelib/mddb/_mcp.py"
  install -Dm644 src/mddb/_merge.py   "$pkgdir/$purelib/mddb/_merge.py"
  install -Dm644 src/mddb/schema.sql  "$pkgdir/$purelib/mddb/schema.sql"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
  install -dm755 "$pkgdir/usr/bin"
  printf '#!/usr/bin/env python\nfrom mddb._mcp import mcp\nmcp.run()\n' > "$pkgdir/usr/bin/mcp-mddb"
  chmod 755 "$pkgdir/usr/bin/mcp-mddb"
  printf '#!/usr/bin/env python\nfrom mddb._merge import main\nmain()\n' > "$pkgdir/usr/bin/mddb-merge"
  chmod 755 "$pkgdir/usr/bin/mddb-merge"
}
