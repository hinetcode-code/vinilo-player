#!/bin/sh
# Construye el paquete .deb a partir del árbol de package/
# Uso: ./build-deb.sh   (requiere dpkg-deb)
set -e
cd "$(dirname "$0")"
VER=$(sed -n 's/^Version: //p' package/DEBIAN/control)
OUT="vinilo_$(echo "$VER" | tr . _)_all.deb"
dpkg-deb --root-owner-group --build package "$OUT"
echo "Generado: $OUT"
