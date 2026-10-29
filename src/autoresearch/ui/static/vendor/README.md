# Vendored libraries

Shipped as package data so `ar ui` works offline with no CDN and no build
step. Each file is copied unmodified from the npm tarball, checked against the
registry's `dist.integrity` before copying.

| File | Package | Licence | Tarball integrity |
|---|---|---|---|
| `cytoscape.min.js` | cytoscape 3.30.4 (`dist/cytoscape.min.js`) | MIT | `sha512-OxtlZwQl1WbwMmLiyPSEBuzeTIQnwZhJYYWFzZ2PhEHVFwpeaqNIkUzSiso00D98qk60l8Gwon2RP304d3BJ1A==` |
| `dagre.min.js` | dagre 0.8.5 (`dist/dagre.min.js`, bundles graphlib and lodash) | MIT | `sha512-/aTqmnRta7x7MCCpExk7HQL2O4owCT2h8NT//9I1OQ9vt29Pa0BzSAkR5lwFUcQ7491yVi/3CXU9jQ5o0Mn2Sw==` |
| `cytoscape-dagre.js` | cytoscape-dagre 2.5.0 | MIT | `sha512-VG2Knemmshop4kh5fpLO27rYcyUaaDkRw+6PiX4bstpB+QFt0p2oauMrsjVbUamGWQ6YNavh7x2em2uZlzV44g==` |

Licences are beside them as `LICENSE-<package>.txt`. To upgrade, fetch the new
tarball, check its integrity, replace the file and this table in one commit.
