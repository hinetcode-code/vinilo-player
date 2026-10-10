#!/bin/sh
# Instala/actualiza yt-dlp (nightly), ytmusicapi y Deno en /opt/vinilo. Se puede relanzar con sudo.
APP=/opt/vinilo
mkdir -p "$APP/bin"
RC=0

echo "[vinilo] Preparando entorno Python…"
if [ ! -x "$APP/venv/bin/python" ]; then
    python3 -m venv --system-site-packages "$APP/venv" || { echo "[vinilo] ERROR creando el entorno Python" >&2; exit 1; }
fi
if ! "$APP/venv/bin/python" -m pip install -q --no-cache-dir -U --pre "yt-dlp[default]" ytmusicapi; then
    echo "[vinilo] AVISO: no se pudieron instalar yt-dlp/ytmusicapi (¿sin conexión?)." >&2
    RC=1
fi

if [ ! -x "$APP/bin/deno" ]; then
    echo "[vinilo] Descargando Deno (lo necesita yt-dlp para YouTube)…"
    case "$(uname -m)" in
        x86_64)  T=x86_64-unknown-linux-gnu ;;
        aarch64) T=aarch64-unknown-linux-gnu ;;
        *) T="" ;;
    esac
    if [ -n "$T" ]; then
        TMP="$(mktemp -d)"
        if curl -fsSL -o "$TMP/deno.zip" "https://github.com/denoland/deno/releases/latest/download/deno-$T.zip" \
           && unzip -q -o "$TMP/deno.zip" -d "$APP/bin"; then
            chmod 755 "$APP/bin/deno"
        else
            echo "[vinilo] AVISO: no se pudo instalar Deno." >&2
            RC=1
        fi
        rm -rf "$TMP"
    else
        echo "[vinilo] AVISO: arquitectura no soportada para Deno: $(uname -m)" >&2
        RC=1
    fi
fi

if [ "$RC" -ne 0 ]; then
    echo "[vinilo] Algo falló. Cuando tengas conexión, ejecuta:  sudo /usr/lib/vinilo/setup-deps.sh" >&2
fi
exit 0
