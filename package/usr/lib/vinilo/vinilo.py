#!/usr/bin/env python3
"""
Vinilo 1.10.2 — descarga álbumes completos de YouTube Music.
Búsqueda por artista y/o álbum, interfaz visual GTK3, cola de descargas,
etiquetas ID3/MP4/Vorbis y carátula incrustada.
"""
import base64
import hashlib
import json
import math
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

import cairo  # noqa: E402

APP_ID = "es.vinilo.app"
APP_NAME = "Vinilo"

# El flujo de audio aparece en los applets de sonido como «Vinilo» (la pista llega por la etiqueta de título)
os.environ.setdefault("PULSE_PROP_application.name", APP_NAME)
os.environ.setdefault("PULSE_PROP_application.icon_name", "vinilo")
os.environ.setdefault("PULSE_PROP_application.id", APP_ID)
os.environ.setdefault("PULSE_PROP_media.role", "music")

GLib.set_application_name(APP_NAME)
GLib.set_prgname("vinilo")

USER_LIBS = Path(GLib.get_user_data_dir()) / "vinilo" / "pylibs"
if USER_LIBS.is_dir():
    sys.path.insert(0, str(USER_LIBS))

CONFIG_DIR = Path(GLib.get_user_config_dir()) / "vinilo"
CACHE_DIR = Path(GLib.get_user_cache_dir()) / "vinilo" / "img"
CONFIG_FILE = CONFIG_DIR / "config.json"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# clave: (etiqueta, selector de formato yt-dlp, códec destino, calidad)
FORMATS = {
    "m4a": ("M4A · AAC (sin recodificar)", "bestaudio[ext=m4a]/bestaudio/best", "m4a", "0"),
    "opus": ("Opus (sin recodificar)", "bestaudio[acodec=opus]/bestaudio/best", "opus", "0"),
    "mp3": ("MP3 320 kbps", "bestaudio/best", "mp3", "320"),
    "flac": ("FLAC (recodificado desde la fuente)", "bestaudio/best", "flac", "0"),
}


LOG_FILE = Path(GLib.get_user_cache_dir()) / "vinilo" / "vinilo.log"


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


OFFLINE_MSG = "Sin conexión a internet"
_OFFLINE_HINTS = (
    "name resolution", "name or service not known", "nodename nor servname", "getaddrinfo",
    "network is unreachable", "no route to host", "failed to establish a new connection",
    "failed to resolve", "no address associated", "temporary failure", "max retries exceeded",
    "connection aborted", "connection reset", "timed out", "remote end closed",
)


def is_offline(e):
    """True si el error (o su causa) indica que no hay conexión a internet."""
    seen, cur = set(), e
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, urllib.error.HTTPError):
            return False
        if isinstance(cur, (socket.gaierror, ConnectionError, TimeoutError)):
            return True
        msg = str(cur).lower()
        if any(h in msg for h in _OFFLINE_HINTS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def needs_auth(msg):
    m = (msg or "").lower()
    return any(k in m for k in ("sign in", "confirm your age", "age-restricted", "login required",
                                "inappropriate for some users"))


def friendly_error(e):
    """Texto de error para el usuario: sin conexión, restricción de edad o el mensaje limpio."""
    if is_offline(e):
        return OFFLINE_MSG
    msg = clean_err(e)
    if needs_auth(msg):
        return ("Vídeo con restricción de edad: inicia sesión en YouTube desde Firefox o Chrome "
                "(o guarda un cookies.txt en ~/.config/vinilo/) y vuelve a intentarlo")
    return msg


def clean_err(e):
    return re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "").strip()


def find_js_runtime():
    """Devuelve (nombre, ruta) del runtime JS que yt-dlp necesita para YouTube."""
    cands = [
        ("deno", shutil.which("deno")),
        ("deno", str(Path.home() / ".deno" / "bin" / "deno")),
        ("deno", "/opt/vinilo/bin/deno"),
        ("deno", "/usr/local/bin/deno"),
        ("node", shutil.which("node")),
        ("node", shutil.which("nodejs")),
        ("bun", shutil.which("bun")),
    ]
    for name, path in cands:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return name, path
    return None


# ───────────────────────────── color de acento ─────────────────────────────

# id: (nombre, color inicial, color final) — siempre el mismo degradado diagonal de 135°
ACCENTS = {
    "coral":    ("Coral",    "#ff3d5a", "#ff7a45"),
    "naranja":  ("Naranja",  "#ff7a1a", "#ffb02e"),
    "oro":      ("Oro",      "#f5a300", "#ffd23f"),
    "lima":     ("Lima",     "#1fbf6a", "#8ee05a"),
    "turquesa": ("Turquesa", "#0aa5c4", "#1fe0b0"),
    "azul":     ("Azul",     "#3d6bff", "#2fc0ff"),
    "violeta":  ("Violeta",  "#8a4dff", "#d05cff"),
    "rosa":     ("Rosa",     "#f0388f", "#ff7a8a"),
}
DEFAULT_ACCENT = "coral"


def _hex_rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in rgb)


def _lighten(h, k):
    return _rgb_hex(tuple(v + (255 - v) * k for v in _hex_rgb(h)))


def _darken(h, k):
    return _rgb_hex(tuple(v * k for v in _hex_rgb(h)))


def accent_tokens(accent_id):
    """Colores derivados del acento (hover, pulsado, texto claro…) que usa el CSS."""
    _name, a, b = ACCENTS.get(accent_id) or ACCENTS[DEFAULT_ACCENT]
    r, g, bl = _hex_rgb(a)
    return {
        "@A_RGB@": f"{r},{g},{bl}",
        "@A_H@": _lighten(a, 0.14), "@B_H@": _lighten(b, 0.14),
        "@A_D@": _darken(a, 0.87), "@B_D@": _darken(b, 0.87),
        "@A_L@": _lighten(a, 0.27),
        "@T0@": _lighten(a, 0.90), "@T1@": _lighten(a, 0.88), "@T2@": _lighten(a, 0.69),
        "@A@": a, "@B@": b,
    }


def accent_rgb(accent_id, which=0):
    """Color del acento como tupla cairo (0-1). which: 0 = inicial, 1 = final."""
    _name, a, b = ACCENTS.get(accent_id) or ACCENTS[DEFAULT_ACCENT]
    return tuple(v / 255 for v in _hex_rgb((a, b)[which]))


CURRENT_ACCENT = DEFAULT_ACCENT


LIB_FILE = CONFIG_DIR / "library.json"
AUDIO_EXT = (".m4a", ".opus", ".mp3", ".flac")


def load_library():
    try:
        data = json.loads(LIB_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_library(lib):
    try:
        LIB_FILE.write_text(json.dumps(lib, indent=1, ensure_ascii=False))
    except Exception:
        pass


FAV_FILE = CONFIG_DIR / "favorites.json"
FAV_COVERS = CONFIG_DIR / "favcovers"


def load_favs():
    try:
        data = json.loads(FAV_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_favs(favs):
    try:
        FAV_FILE.write_text(json.dumps(favs, indent=1, ensure_ascii=False))
    except Exception:
        pass


def count_tracks(folder):
    try:
        return sum(1 for f in Path(folder).iterdir() if f.suffix.lower() in AUDIO_EXT)
    except Exception:
        return 0


def scan_folder(root):
    """Detecta álbumes bajados antes por Vinilo: Artista/Álbum (año)/ con cover.jpg y pistas «NN - Título»."""
    found = {}
    try:
        root = Path(root)
        if not root.is_dir():
            return found
        for artist_dir in root.iterdir():
            if not artist_dir.is_dir():
                continue
            for album_dir in artist_dir.iterdir():
                if not album_dir.is_dir() or not (album_dir / "cover.jpg").exists():
                    continue
                files = [f for f in album_dir.iterdir()
                         if f.suffix.lower() in AUDIO_EXT and re.match(r"^\d{2,3} - ", f.name)]
                if not files:
                    continue
                m = re.match(r"^(.*) \((\d{4})\)$", album_dir.name)
                title, year = (m.group(1), m.group(2)) if m else (album_dir.name, "")
                found[str(album_dir)] = {
                    "folder": str(album_dir), "title": title, "artist": artist_dir.name, "year": year,
                    "browse_id": "", "cover": str(album_dir / "cover.jpg"),
                    "format": files[0].suffix[1:].lower(), "tracks": len(files),
                    "date": album_dir.stat().st_mtime,
                }
    except Exception:
        pass
    return found


def _ver_tuple(v):
    m = re.match(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})(?:\.(\d+))?", str(v))
    return tuple(int(x or 0) for x in m.groups()) if m else None


def installed_ytdlp_version():
    try:
        import yt_dlp.version as v

        return v.__version__
    except Exception:
        return None


def latest_ytdlp_version():
    """Última versión publicada de yt-dlp en PyPI (incluye las nightly). Lanza error sin conexión."""
    req = urllib.request.Request("https://pypi.org/pypi/yt-dlp/json", headers={"User-Agent": "Vinilo"})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = json.load(r)
    best = None
    for ver, files in (data.get("releases") or {}).items():
        if files and all(f.get("yanked") for f in files):
            continue
        t = _ver_tuple(ver)
        if t and (best is None or t > best[0]):
            best = (t, ver)
    return best[1] if best else None


def default_music_dir():
    d = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_MUSIC)
    return d or str(Path.home() / "Music")


def load_config():
    cfg = {"folder": default_music_dir(), "format": "m4a", "retries": 3, "retry_delay": 4,
           "accent": DEFAULT_ACCENT, "lib_sort": "date", "lib_desc": True, "vinyl_spin": True,
           "vinyl_arm": True}
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text()))
    except Exception:
        pass
    if cfg["format"] not in FORMATS:
        cfg["format"] = "m4a"
    if cfg.get("accent") not in ACCENTS:
        cfg["accent"] = DEFAULT_ACCENT
    if cfg.get("lib_sort") not in ("name", "date", "year"):
        cfg["lib_sort"] = "date"
    cfg["lib_desc"] = bool(cfg.get("lib_desc", True))
    cfg["vinyl_spin"] = bool(cfg.get("vinyl_spin", True))
    cfg["vinyl_arm"] = bool(cfg.get("vinyl_arm", True))
    return cfg


def save_config(cfg):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


# ───────────────────────────── utilidades ─────────────────────────────

_pool = ThreadPoolExecutor(max_workers=8)


def run_async(fn, cb):
    """Ejecuta fn en un hilo y llama cb(resultado, error) en el hilo principal."""

    def worker():
        try:
            res, err = fn(), None
        except Exception as e:  # noqa: BLE001
            res, err = None, e
        GLib.idle_add(lambda: (cb(res, err), False)[1])

    threading.Thread(target=worker, daemon=True).start()


def sanitize(name, fallback="Desconocido"):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "").strip(" .")
    return name[:120] or fallback


def upscale(url, size):
    if not url:
        return url
    return re.sub(r"=w\d+-h\d+", f"=w{size}-h{size}", url)


def best_thumb(thumbs, size=400):
    if not thumbs:
        return None
    return upscale(thumbs[-1].get("url"), size)


def fetch_bytes(url):
    if url.startswith("/"):
        return Path(url).read_bytes()
    key = hashlib.md5(url.encode()).hexdigest()
    p = CACHE_DIR / key
    if p.exists():
        return p.read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read()
    p.write_bytes(data)
    return data


def pixbuf_square(data, target):
    loader = GdkPixbuf.PixbufLoader()
    loader.write(data)
    loader.close()
    pb = loader.get_pixbuf()
    w, h = pb.get_width(), pb.get_height()
    s = min(w, h)
    pb = pb.new_subpixbuf((w - s) // 2, (h - s) // 2, s, s)
    if s > target:
        pb = pb.scale_simple(target, target, GdkPixbuf.InterpType.BILINEAR)
    return pb


def average_color(pb):
    px = pb.scale_simple(1, 1, GdkPixbuf.InterpType.BILINEAR).get_pixels()
    return px[0], px[1], px[2]


def fmt_artists(artists):
    if not artists:
        return ""
    return ", ".join(a.get("name", "") for a in artists if a.get("name"))


# ───────────────────────────── CSS ─────────────────────────────

CSS = """
window, .root { background-color: #0e0e12; color: #f2f2f5; }

headerbar { background-image: none; background-color: #131318; color: #f2f2f5;
    border-bottom: 1px solid #23232c; box-shadow: none; min-height: 58px; }
headerbar button { color: #e8e8f0; background-image: none; background-color: transparent; border: none;
    box-shadow: none; border-radius: 20px; padding: 7px 13px; transition: all 160ms ease; }
headerbar button:hover { background-color: #262631; color: #ffffff; }
headerbar button:active { background-color: #30303d; }
headerbar button:checked { background-color: rgba(@A_RGB@,0.20); color: @A_L@; }

.tabs { background-color: #1a1a22; border: 1px solid #272732; border-radius: 24px; padding: 3px; }
.tabs button { background-image: none; background-color: transparent; border: none; border-radius: 20px;
    padding: 8px 26px; margin: 0 2px; color: #a4a4b6; font-weight: 700; box-shadow: none;
    transition: all 200ms ease; }
.tabs button:hover { color: #ffffff; background-color: #262631; }
.tabs button:checked { color: #ffffff; background-image: linear-gradient(135deg, @A@, @B@);
    box-shadow: none; }
.tabs button:checked:hover { background-image: linear-gradient(135deg, @A_H@, @B_H@); }
.tabs button:active { background-color: #30303d; }

.seg { background-color: #1a1a22; border: 1px solid #272732; border-radius: 20px; padding: 2px; }
.seg button { background-image: none; background-color: transparent; border: none; border-radius: 16px;
    padding: 5px 16px; margin: 0 1px; color: #a4a4b6; font-weight: 700; box-shadow: none; transition: all 180ms ease; }
.seg button:hover { color: #ffffff; background-color: #262631; }
.seg button:checked { color: #ffffff; background-color: #3a3a4a; }

entry { background-image: none; background-color: #1d1d26; color: #f2f2f5; border: 1px solid #343442;
    border-radius: 14px; padding: 9px 15px; min-height: 24px; box-shadow: inset 0 1px 0 rgba(255,255,255,0.025); transition: all 160ms ease;
    caret-color: @A@; }
entry:hover { border-color: #444456; }
entry:focus { border-color: @A@; box-shadow: 0 0 0 3px rgba(@A_RGB@,0.20); background-color: #20202a; }
entry image { color: #8b8b9c; }
entry selection { background-color: @A@; color: #ffffff; }

button.primary, button.suggested-action { background-image: linear-gradient(135deg, @A@, @B@);
    background-color: @A@; color: #ffffff; font-weight: 700; border: none; border-radius: 26px;
    padding: 11px 28px; box-shadow: none; text-shadow: none; transition: all 170ms ease; }
button.primary:hover, button.suggested-action:hover { background-image: linear-gradient(135deg, @A_H@, @B_H@);
    box-shadow: none; }
button.primary:active, button.suggested-action:active { background-image: linear-gradient(135deg, @A_D@, @B_D@);
    box-shadow: none; }
button.primary:disabled, button.suggested-action:disabled { background-image: none; background-color: #3a2a30;
    color: #8b8b95; box-shadow: none; }

button.ghost { background-image: none; background-color: rgba(255,255,255,0.03); color: #f2f2f5;
    border: 1px solid #3a3a48; border-radius: 26px; padding: 10px 22px; box-shadow: none; font-weight: 600;
    transition: all 170ms ease; }
button.ghost:hover { background-color: rgba(255,255,255,0.09); border-color: #5c5c70; }
button.ghost:active { background-color: rgba(255,255,255,0.15); }
button.ghost:disabled { color: #70707c; border-color: #2a2a34; }

button.round { background-image: none; background-color: transparent; border: none; border-radius: 999px;
    min-width: 40px; min-height: 40px; padding: 0; box-shadow: none; color: #d6d6e2; transition: all 150ms ease; }
button.round:hover { background-color: rgba(255,255,255,0.11); color: #ffffff; }
button.round:active { background-color: rgba(255,255,255,0.20); }

button.play-main { background-image: none; background-color: #ffffff; color: #0e0e12; border: none;
    border-radius: 999px; min-width: 48px; min-height: 48px; padding: 0;
    box-shadow: none; transition: all 160ms ease; }
button.play-main:hover { background-color: @T0@; box-shadow: none; }
button.play-main:active { background-color: #d9d9e0; box-shadow: none; }

button.fab { background-image: linear-gradient(135deg, @A@, @B@); color: #ffffff; border: none;
    border-radius: 999px; min-width: 46px; min-height: 46px; padding: 0;
    box-shadow: none; transition: all 160ms ease; }
button.fab:hover { background-image: linear-gradient(135deg, @A_H@, @B_H@);
    box-shadow: inset 0 0 0 2px rgba(255,255,255,0.30); }
button.fab:active { background-image: linear-gradient(135deg, @A_D@, @B_D@); box-shadow: none; }

button.row-play { background-image: none; background-color: rgba(255,255,255,0.07); color: #e8e8f0; border: none;
    border-radius: 999px; min-width: 34px; min-height: 34px; padding: 0; box-shadow: none; transition: all 150ms ease; }
button.row-play:hover { background-color: @A@; color: #ffffff; box-shadow: none; }
button.row-play:active { background-color: @A_D@; }
row.track.now-playing button.row-play { background-color: @A@; color: #ffffff; box-shadow: none; }
row.track.now-playing button.row-play:hover { background-color: @A_H@; }
button.row-play:disabled { background-color: rgba(255,255,255,0.03); color: #5c5c68; }

button.bare { background-image: none; background-color: transparent; border: none; padding: 0; margin: 0;
    box-shadow: none; min-height: 0; min-width: 0; border-radius: 0; }
button.bare:hover, button.bare:active { background-color: transparent; }

button.card { background-image: none; background-color: transparent; border: none; border-radius: 0;
    padding: 10px; box-shadow: none; transition: all 170ms ease; }
button.card:hover { background-color: rgba(255,255,255,0.06); }
button.card:active { background-color: rgba(255,255,255,0.10); }
.lib-card { background-color: transparent; border-radius: 0; transition: all 170ms ease; }
.lib-card:hover { background-color: rgba(255,255,255,0.06); }

.card-title { font-weight: 700; font-size: 13px; color: #f2f2f5; text-shadow: none; }
button.bare label, button.card label { color: #f2f2f5; text-shadow: none; }
button.bare label.card-sub, button.card label.card-sub { color: #9b9bab; }
.card-sub { color: #9b9bab; font-size: 12px; }
.hero-title { font-size: 46px; font-weight: 800; letter-spacing: -1px; }
.hero-sub { font-size: 16px; color: #9b9bab; }
.collection-title { font-size: 34px; font-weight: 800; letter-spacing: -0.5px; }
.page-title { font-size: 22px; font-weight: 800; }
.section-title { font-size: 18px; font-weight: 800; margin-top: 8px; }
.big-title { font-size: 34px; font-weight: 800; }
.kind { font-size: 11px; font-weight: 800; letter-spacing: 2px; color: #e8e8ee; }
.muted { color: #9b9bab; }
.link { color: #ffffff; font-weight: 700; }
button.link, button.link:link, button.link:visited { background-image: none; background-color: transparent; border: none;
    box-shadow: none; padding: 2px 0; }
button.link label, button.link:link label, button.link:visited label { color: #ffffff; font-weight: 700; text-decoration: none; }
button.link:hover label { color: @A_L@; text-decoration: underline; }
.bar-title { font-weight: 700; font-size: 14px; }
button.bar-link { background-image: none; background-color: transparent; border: none; box-shadow: none;
    padding: 0; margin: 0; min-height: 0; min-width: 0; border-radius: 4px; }
button.bar-link:hover, button.bar-link:active, button.bar-link:focus { background-color: transparent; box-shadow: none; }
button.bar-link label { color: #9b9bab; font-size: 12px; text-shadow: none; }
button.bar-link:hover label { color: #ffffff; text-decoration: underline; }
button.chip { background-image: none; background-color: rgba(@A_RGB@,0.16); border: 1px solid rgba(@A_RGB@,0.38);
    border-radius: 999px; padding: 5px 16px; box-shadow: none; transition: all 150ms ease; }
button.chip label { color: @A_L@; font-weight: 700; text-shadow: none; }
button.chip:hover { background-color: rgba(@A_RGB@,0.30); }
.badge { background-color: rgba(255,255,255,0.08); border-radius: 12px; padding: 2px 10px; color: #c9c9d6; font-size: 12px; }

.search-field-label { color: #8f8fa2; font-size: 11px; font-weight: 700; letter-spacing: .7px; margin: 0 0 4px 4px; }
.search-help { color: #777789; font-size: 12px; }

button:focus, button:active:focus, button:checked:focus, button:hover:focus {
    outline-style: none; outline-width: 0; outline-color: transparent; }
row.vtrack { padding: 3px 8px; border-radius: 0; background-color: transparent; }
row.vtrack:hover { background-color: rgba(255,255,255,0.05); }
row.vtrack label { color: rgba(255,255,255,0.30); font-size: 11px; font-weight: 400;
    font-family: "Courier Prime", "Courier New", "Nimbus Mono PS", "Liberation Mono", "DejaVu Sans Mono", monospace; }
row.vtrack:hover label { color: rgba(255,255,255,0.65); }
row.vtrack-now label { color: @A_L@; font-weight: 400; }
row.vtrack-now:hover label { color: @A_L@; }
.vhead { color: rgba(255,255,255,0.38); font-size: 9px; font-weight: 400; letter-spacing: 2px; margin-bottom: 10px;
    font-family: "Courier Prime", "Courier New", "Nimbus Mono PS", "Liberation Mono", "DejaVu Sans Mono", monospace; }
.player-bar.player-float { background-color: rgba(19,19,24,0.92); }
.online-badge { background-color: rgba(8,8,12,0.80); color: #ffffff; font-size: 9px; font-weight: 700;
    letter-spacing: 1px; padding: 3px 7px; border-bottom: 2px solid @A@; }
button.ghost.liked { border-color: @A@; color: @A_L@; }
button.ghost.liked label { color: @A_L@; }
button.pop-item { background-image: none; background-color: transparent; border: none; border-radius: 0;
    box-shadow: none; padding: 7px 16px; color: #f2f2f5; }
button.pop-item:hover { background-color: rgba(255,255,255,0.09); }
button.ghost:checked { background-color: rgba(255,255,255,0.12); }
list, row { background-color: transparent; }
viewport, scrolledwindow, flowbox, flowboxchild { background-color: transparent; background-image: none; }
.album-root { background-color: transparent; background-image: none; }
.album-root > grid { background-color: transparent; }
.album-root > grid.album-hero { padding: 30px; border-radius: 0; background-color: rgba(8,8,12,0.55);
    box-shadow: inset 0 0 0 1px rgba(255,255,255,0.06); }
row.track { border-radius: 12px; padding: 2px 8px; color: #f2f2f5; transition: all 140ms ease; }
row.track:hover { background-color: rgba(255,255,255,0.07); }
row.track.now-playing { background-color: rgba(@A_RGB@,0.15); }
row.track.now-playing .track-title { color: @A_L@; }
.track-num { color: #9b9bab; }
.track-title { font-weight: 600; color: #f2f2f5; }

progressbar trough { min-height: 6px; background-color: #2a2a34; border-radius: 3px; border: none; }
progressbar progress { min-height: 6px; background-image: linear-gradient(90deg, @A@, @B@); border-radius: 3px; border: none; }

scale trough { min-height: 5px; background-color: #2c2c37; border-radius: 3px; border: none; }
scale highlight { background-image: linear-gradient(90deg, @A@, @B@); border-radius: 3px; border: none; min-height: 5px; }
scale slider { min-width: 14px; min-height: 14px; margin: -5px; background-image: none; background-color: #ffffff;
    border: none; border-radius: 999px; box-shadow: none; transition: all 130ms ease; }
scale slider:hover { background-color: @T1@; box-shadow: none; }
scale slider:active { background-color: @T2@; box-shadow: none; }

scrollbar { background-color: transparent; border: none; }
scrollbar trough { background-color: transparent; border: none; }
scrollbar slider { min-width: 6px; min-height: 6px; border-radius: 3px; background-color: #3a3a47; border: none; }
scrollbar slider:hover { background-color: #5c5c6e; }

spinbutton { background-color: #1d1d26; border: 1px solid #2f2f3c; border-radius: 14px; color: #f2f2f5; box-shadow: none; }
spinbutton entry { border: none; background-color: transparent; box-shadow: none; padding: 5px 8px; }
spinbutton button { background-image: none; background-color: transparent; border: none; box-shadow: none;
    border-radius: 0; color: #f2f2f5; }
spinbutton button:hover { background-color: #30303d; }
combobox button { background-image: none; background-color: #1d1d26; border: 1px solid #2f2f3c; border-radius: 14px;
    color: #f2f2f5; box-shadow: none; padding: 7px 14px; }
combobox button:hover { border-color: #5c5c70; }

menu, .menu { background-color: #1b1b22; border: 1px solid #30303c; border-radius: 12px; color: #f2f2f5; padding: 4px; }
menuitem { border-radius: 8px; padding: 6px 12px; }
menuitem:hover { background-color: #2c2c3a; }
popover, popover.background { background-color: #1b1b22; border: 1px solid #30303c; border-radius: 16px; }

.player-bar { background-color: #131318; border-top: 1px solid #272731; padding: 10px 20px; }
.dl-panel { background-color: #131318; border-left: 1px solid #23232c; }
.dl-row { background-color: #17171d; border-radius: 14px; padding: 10px; }
.toast { background-color: #2b2b36; color: #ffffff; border-radius: 24px; padding: 11px 22px; border: 1px solid #3b3b49;
    box-shadow: 0 8px 24px rgba(0,0,0,0.5); }

button.swatch { background-image: none; background-color: transparent; border: none; box-shadow: none;
    border-radius: 999px; padding: 3px; min-width: 0; min-height: 0; transition: all 140ms ease; }
button.swatch:hover { background-color: rgba(255,255,255,0.10); }
checkbutton check { background-image: none; background-color: #2b2b36; border: 1px solid #44444f; border-radius: 7px;
    min-width: 18px; min-height: 18px; transition: all 140ms ease; }
checkbutton check:hover { border-color: @A@; }
checkbutton check:checked { background-color: @A@; border-color: @A@; color: #ffffff; }
scrolledwindow undershoot, scrolledwindow overshoot { background: none; }
"""


# ───────────────────────────── widgets ─────────────────────────────


def rounded_path(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class Cover(Gtk.DrawingArea):
    """Carátula cuadrada, como la funda de un disco de vinilo (o circular para artistas),
    con aparición suave."""

    def __init__(self, size, circle=False, radius=0):
        super().__init__()
        self.size, self.circle, self.radius = size, circle, radius
        self.pix = None
        self.alpha = 0.0
        self._url = None
        self.set_size_request(size, size)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.START)
        self.connect("draw", self.on_draw)

    def set_url(self, url, on_loaded=None):
        if url != self._url:
            self.pix = None
            self.queue_draw()
        self._url = url
        if not url:
            return
        target = self.size * 2

        def work():
            try:
                pb = pixbuf_square(fetch_bytes(url), target)
            except Exception:
                return
            GLib.idle_add(self._loaded, url, pb, on_loaded)

        _pool.submit(work)

    def _loaded(self, url, pb, cb):
        if url != self._url:
            return False
        self.pix = pb
        self.alpha = 0.0
        GLib.timeout_add(16, self._fade)
        if cb:
            cb(pb)
        return False

    def _fade(self):
        self.alpha = min(1.0, self.alpha + 0.09)
        self.queue_draw()
        return self.alpha < 1.0

    def on_draw(self, widget, cr):
        s = self.size
        r = s / 2 if self.circle else self.radius
        if r > 0:
            rounded_path(cr, 0, 0, s, s, r)
        else:
            cr.rectangle(0, 0, s, s)
        cr.clip()
        g = cairo.LinearGradient(0, 0, s, s)
        g.add_color_stop_rgb(0, 0.20, 0.20, 0.26)
        g.add_color_stop_rgb(1, 0.10, 0.10, 0.14)
        cr.set_source(g)
        cr.paint()
        if self.pix is not None:
            cr.save()
            k = s / self.pix.get_width()
            cr.scale(k, k)
            Gdk.cairo_set_source_pixbuf(cr, self.pix, 0, 0)
            cr.paint_with_alpha(self.alpha)
            cr.restore()
        if not self.circle and r == 0:
            self._sleeve(cr, s)
        return False

    @staticmethod
    def _sleeve(cr, s):
        """Acabado de funda de cartón: brillo suave en diagonal y borde fino."""
        g = cairo.LinearGradient(0, 0, s, s)
        g.add_color_stop_rgba(0.0, 1, 1, 1, 0.10)
        g.add_color_stop_rgba(0.35, 1, 1, 1, 0.0)
        g.add_color_stop_rgba(1.0, 0, 0, 0, 0.16)
        cr.set_source(g)
        cr.paint()
        cr.set_line_width(1)
        cr.set_source_rgba(1, 1, 1, 0.14)
        cr.rectangle(0.5, 0.5, s - 1, s - 1)
        cr.stroke()


CARD_SIZE = 150  # tamaño de portada común a «Mis Álbumes» y «Explorar Álbumes»


def make_card(cover_url, title, subtitle, size=CARD_SIZE, circle=False, on_click=None):
    btn = Gtk.Button()
    btn.set_relief(Gtk.ReliefStyle.NONE)
    btn.get_style_context().add_class("card")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    cover = Cover(size, circle=circle)
    cover.set_url(cover_url)
    box.pack_start(cover, False, False, 0)
    col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    for text, cls in ((title, "card-title"), (subtitle, "card-sub")):
        lbl = Gtk.Label(label=text or "")
        lbl.get_style_context().add_class(cls)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        lbl.set_width_chars(16)
        lbl.set_max_width_chars(16)
        lbl.set_xalign(0.5 if circle else 0.0)
        col.pack_start(lbl, False, False, 0)
    box.pack_start(col, False, False, 0)
    btn.add(box)
    btn.set_halign(Gtk.Align.CENTER)
    btn.set_valign(Gtk.Align.START)
    if on_click:
        btn.connect("clicked", lambda *_: on_click())
    return btn


def make_grid():
    fb = Gtk.FlowBox()
    fb.set_selection_mode(Gtk.SelectionMode.NONE)
    fb.set_homogeneous(True)
    fb.set_min_children_per_line(2)
    fb.set_max_children_per_line(14)
    fb.set_column_spacing(6)
    fb.set_row_spacing(10)
    fb.set_valign(Gtk.Align.START)
    fb.set_halign(Gtk.Align.FILL)
    return fb


def spacer(height):
    """Hueco vacío de alto fijo (mantiene la posición de los textos)."""
    b = Gtk.Box()
    b.set_size_request(-1, height)
    return b


def empty_state(title, sub, button=None):
    """Bloque central de «no hay nada»: mismo formato y posición en Mis Álbumes y en Explorar.
    Se coloca centrado sobre toda la pestaña; `button` ocupa un hueco de alto fijo bajo los textos."""
    b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    b.set_halign(Gtk.Align.CENTER)
    b.set_valign(Gtk.Align.CENTER)
    b.pack_start(spacer(220), False, False, 0)
    b.pack_start(label(title, "hero-title", xalign=0.5), False, False, 10)
    b.pack_start(label(sub, "hero-sub", xalign=0.5), False, False, 0)
    slot = Gtk.Box()
    slot.set_size_request(-1, 54)
    if button is not None:
        button.set_halign(Gtk.Align.CENTER)
        button.set_valign(Gtk.Align.CENTER)
        slot.pack_start(button, True, False, 0)
    b.pack_start(slot, False, False, 0)
    return b


def menu_button(items, text="Más"):
    """Botón que despliega un menú (popover) con las acciones secundarias: items = [(texto, callback)]."""
    mb = Gtk.MenuButton()
    mb.item_btns = []
    box = Gtk.Box(spacing=8)
    box.set_halign(Gtk.Align.CENTER)
    box.pack_start(Gtk.Image.new_from_icon_name("view-more-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
    box.pack_start(Gtk.Label(label=text), False, False, 0)
    mb.add(box)
    mb.get_style_context().add_class("ghost")
    pop = Gtk.Popover()
    vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    vb.set_border_width(4)
    for txt, cb in items:
        b = Gtk.Button(label=txt)
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.get_style_context().add_class("pop-item")
        b.get_child().set_xalign(0)
        b.connect("clicked", lambda _b, cb=cb: (pop.popdown(), cb()))
        b.lbl = b.get_child()
        mb.item_btns.append(b)
        vb.pack_start(b, False, False, 0)
    vb.show_all()
    pop.add(vb)
    mb.set_popover(pop)
    return mb


def label(text, cls=None, xalign=0.0, wrap=False, ellipsize=False):
    lbl = Gtk.Label(label=text)
    lbl.set_xalign(xalign)
    if cls:
        for c in cls.split():
            lbl.get_style_context().add_class(c)
    if wrap:
        lbl.set_line_wrap(True)
    if ellipsize:
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
    return lbl


# ───────────────────────────── descargas ─────────────────────────────


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, browse_id, title, artist, cover_url, track_ids=None):
        self.browse_id = browse_id
        self.title = title
        self.artist = artist
        self.cover_url = cover_url
        self.track_ids = set(track_ids) if track_ids else None
        self.state = "queued"  # queued | running | done | error | cancelled
        self.progress = 0.0
        self.message = "En cola"
        self.cancelled = False
        self.folder = None
        self.row = None
        self.error_detail = ""
        self.meta = None
        self._last_ui = 0.0

    def update(self, progress=None, message=None, state=None, force=False):
        if progress is not None:
            self.progress = progress
        if message is not None:
            self.message = message
        if state is not None:
            self.state = state
            force = True
        now = time.monotonic()
        if force or now - self._last_ui > 0.12:
            self._last_ui = now
            GLib.idle_add(self._refresh_ui)

    def _refresh_ui(self):
        if self.row:
            self.row.refresh()
        return False


class JobRow(Gtk.Box):
    def __init__(self, job, on_cancel, on_open, on_change):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.job, self.on_change = job, on_change
        self.get_style_context().add_class("dl-row")
        cover = Cover(52)
        cover.set_url(upscale(job.cover_url, 120) if job.cover_url else None)
        self.pack_start(cover, False, False, 0)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.title = label(job.title, "card-title", ellipsize=True)
        self.title.set_max_width_chars(24)
        self.sub = label(job.message, "card-sub", ellipsize=True)
        self.sub.set_max_width_chars(28)
        self.sub.set_lines(3)
        self.sub.set_line_wrap(True)
        self.sub.set_ellipsize(Pango.EllipsizeMode.END)
        self.bar = Gtk.ProgressBar()
        col.pack_start(self.title, False, False, 0)
        col.pack_start(self.sub, False, False, 0)
        col.pack_start(self.bar, False, False, 4)
        self.pack_start(col, True, True, 0)
        self.btn = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        self.btn.set_relief(Gtk.ReliefStyle.NONE)
        self.btn.set_valign(Gtk.Align.CENTER)
        self.btn.connect("clicked", lambda *_: self._clicked(on_cancel, on_open))
        self.pack_start(self.btn, False, False, 0)
        self.refresh()

    def _clicked(self, on_cancel, on_open):
        if self.job.state in ("queued", "running"):
            on_cancel(self.job)
        elif self.job.state == "done":
            on_open(self.job)

    def refresh(self):
        j = self.job
        self.bar.set_fraction(max(0.0, min(1.0, j.progress)))
        self.sub.set_text(j.message)
        self.sub.set_tooltip_text(j.error_detail or j.message)
        icon = {
            "queued": "window-close-symbolic",
            "running": "window-close-symbolic",
            "done": "folder-open-symbolic",
        }.get(j.state)
        self.btn.set_visible(icon is not None)
        if icon:
            self.btn.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))
            self.btn.set_tooltip_text("Cancelar" if icon.startswith("window") else "Abrir carpeta")
        self.btn.show_all() if icon else None
        self.on_change()


def tag_file(path, fmt, meta, cover):
    """Escribe etiquetas y carátula. Nunca lanza: un fallo de etiquetado no rompe la descarga."""
    try:
        title, artist, album, albumartist = meta["title"], meta["artist"], meta["album"], meta["albumartist"]
        num, total, year = meta["num"], meta["total"], meta.get("year")
        if fmt == "mp3":
            from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TRCK, ID3NoHeaderError

            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags.add(TIT2(encoding=3, text=title))
            tags.add(TPE1(encoding=3, text=artist))
            tags.add(TALB(encoding=3, text=album))
            tags.add(TPE2(encoding=3, text=albumartist))
            tags.add(TRCK(encoding=3, text=f"{num}/{total}"))
            if year:
                tags.add(TDRC(encoding=3, text=str(year)))
            if cover:
                tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
            tags.save(path, v2_version=3)
        elif fmt == "m4a":
            from mutagen.mp4 import MP4, MP4Cover

            f = MP4(path)
            f["\xa9nam"], f["\xa9ART"], f["\xa9alb"], f["aART"] = [title], [artist], [album], [albumartist]
            f["trkn"] = [(num, total)]
            if year:
                f["\xa9day"] = [str(year)]
            if cover:
                f["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
            f.save()
        elif fmt in ("flac", "opus"):
            from mutagen.flac import FLAC, Picture

            if fmt == "flac":
                f = FLAC(path)
            else:
                from mutagen.oggopus import OggOpus

                f = OggOpus(path)
            f["title"], f["artist"], f["album"], f["albumartist"] = [title], [artist], [album], [albumartist]
            f["tracknumber"], f["tracktotal"] = [str(num)], [str(total)]
            if year:
                f["date"] = [str(year)]
            if cover:
                pic = Picture()
                pic.type, pic.mime, pic.data = 3, "image/jpeg", cover
                if fmt == "flac":
                    f.add_picture(pic)
                else:
                    f["metadata_block_picture"] = [base64.b64encode(pic.write()).decode()]
            f.save()
    except Exception as e:  # noqa: BLE001
        print(f"[aviso] no se pudo etiquetar {path}: {e}", file=sys.stderr)


class DownloadManager:
    def __init__(self, get_cfg, on_jobs_changed, on_finished=None):
        self.get_cfg = get_cfg
        self.on_jobs_changed = on_jobs_changed
        self.on_finished = on_finished
        self.jobs = []
        self.q = queue.Queue()
        threading.Thread(target=self._loop, daemon=True).start()

    def add(self, job):
        self.jobs.append(job)
        self.q.put(job)
        GLib.idle_add(lambda: (self.on_jobs_changed(job), False)[1])

    def cancel(self, job):
        job.cancelled = True
        if job.state == "queued":
            job.update(message="Cancelado", state="cancelled")

    def _loop(self):
        from ytmusicapi import YTMusic

        yt = None
        while True:
            job = self.q.get()
            if job.cancelled:
                continue
            try:
                if yt is None:
                    yt = YTMusic()
                self._run(yt, job)
                if job.state == "done" and job.meta and self.on_finished:
                    job.meta["tracks"] = count_tracks(job.meta["folder"])
                    GLib.idle_add(lambda j=job: (self.on_finished(j), False)[1])
            except Cancelled:
                job.update(message="Cancelado", state="cancelled")
            except Exception as e:  # noqa: BLE001
                msg = friendly_error(e)
                job.update(message=msg if msg == OFFLINE_MSG else f"Error: {msg[:90]}", state="error")

    @staticmethod
    def _sleep(job, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if job.cancelled:
                raise Cancelled()
            time.sleep(0.25)

    def _run(self, yt, job):
        import yt_dlp

        job.update(message="Obteniendo datos del álbum…", state="running", progress=0.0)
        album = yt.get_album(job.browse_id)
        cfg = self.get_cfg()
        fmt = cfg["format"]
        _, selector, codec, quality = FORMATS[fmt]

        tracks = [t for t in album.get("tracks", []) if t.get("videoId") and t.get("isAvailable", True)]
        if job.track_ids:
            tracks = [t for t in tracks if t["videoId"] in job.track_ids]
        if not tracks:
            raise RuntimeError("El álbum no tiene canciones disponibles")

        album_title = album.get("title") or job.title
        album_artist = fmt_artists(album.get("artists")) or job.artist or "Desconocido"
        year = album.get("year")
        folder = Path(cfg["folder"]) / sanitize(album_artist) / sanitize(
            f"{album_title} ({year})" if year else album_title
        )
        folder.mkdir(parents=True, exist_ok=True)
        job.folder = str(folder)
        total = int(album.get("trackCount") or len(tracks))
        job.meta = {
            "folder": str(folder), "title": album_title, "artist": album_artist,
            "year": str(year or ""), "browse_id": job.browse_id,
            "cover": str(folder / "cover.jpg"), "format": codec,
        }

        cover = None
        try:
            curl = best_thumb(album.get("thumbnails"), 1000)
            if curl:
                cover = fetch_bytes(curl)
                (folder / "cover.jpg").write_bytes(cover)
        except Exception:
            pass

        n = len(tracks)
        pad = max(2, len(str(total)))
        retries = max(0, int(cfg.get("retries", 3)))
        delay = max(0, int(cfg.get("retry_delay", 4)))
        rt = find_js_runtime()
        ok_set = set()
        errors = {}

        def attempt(i, t, prefix):
            """Descarga una pista. Devuelve None si va bien, o el texto del error."""
            num = int(t.get("trackNumber") or (i + 1))
            title = t.get("title") or f"Pista {num}"
            base = f"{str(num).zfill(pad)} - {sanitize(title)}"
            final = folder / f"{base}.{codec}"
            label_txt = f"{prefix}{i + 1}/{n} · {title}"
            job.update(progress=len(ok_set) / n, message=label_txt)
            if final.exists():
                return None

            def hook(d):
                if job.cancelled:
                    raise Cancelled()
                if d["status"] == "downloading":
                    tot = d.get("total_bytes") or d.get("total_bytes_estimate")
                    frac = (d.get("downloaded_bytes", 0) / tot) if tot else 0.0
                    job.update(progress=(len(ok_set) + frac * 0.9) / n, message=label_txt)
                elif d["status"] == "finished":
                    job.update(progress=(len(ok_set) + 0.92) / n, message=f"{prefix}{i + 1}/{n} · convirtiendo…")

            opts = {
                "format": selector,
                "outtmpl": str(folder / (base.replace("%", "%%") + ".%(ext)s")),
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "noplaylist": True,
                "retries": 5,
                "remote_components": ["ejs:github"],
                "progress_hooks": [hook],
                "postprocessors": [
                    {"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": quality}
                ],
            }
            if rt:
                opts["js_runtimes"] = {rt[0]: {"path": rt[1]}}
            try:
                url = f"https://music.youtube.com/watch?v={t['videoId']}"
                # clientes de YouTube alternativos y, si pide iniciar sesión, cookies automáticas
                ytdlp_run(opts, url, lambda: job.cancelled, f"{album_title} {title}")
                f_out = final
                if not f_out.exists():
                    cands = [q for q in folder.glob(glob_escape(base) + ".*") if q.suffix != ".part"]
                    if not cands:
                        raise RuntimeError("archivo no generado")
                    f_out = cands[0]
                tag_file(
                    str(f_out),
                    fmt,
                    {
                        "title": title,
                        "artist": fmt_artists(t.get("artists")) or album_artist,
                        "album": album_title,
                        "albumartist": album_artist,
                        "num": num,
                        "total": total,
                        "year": year,
                    },
                    cover,
                )
                return None
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                if job.cancelled:
                    raise Cancelled()
                msg = friendly_error(e)
                log(f"[{album_title}] {title}: FALLO {prefix}{msg}")
                print(f"[error] {title}: {msg}", file=sys.stderr)
                return msg

        pending = list(enumerate(tracks))
        for rnd in range(retries + 1):
            if rnd > 0:
                job.update(message=f"Reintento {rnd}/{retries} · {len(pending)} pista(s) en {delay}s…")
                self._sleep(job, delay)
            still = []
            for i, t in pending:
                if job.cancelled:
                    raise Cancelled()
                err = attempt(i, t, "" if rnd == 0 else f"Reintento {rnd}/{retries} · ")
                if err is None:
                    ok_set.add(i)
                    errors.pop(i, None)
                else:
                    errors[i] = err
                    still.append((i, t))
            pending = still
            if not pending:
                break

        failed = len(pending)
        last_err = next(iter(errors.values()), "") if errors else ""
        extra = f" tras {retries} reintentos" if retries else ""
        if failed == n:
            job.error_detail = last_err
            job.update(progress=1.0, state="error",
                       message=OFFLINE_MSG if last_err == OFFLINE_MSG
                       else f"Error{extra}: {last_err[:140] or 'no se pudo descargar'}")
        elif failed:
            job.error_detail = last_err
            job.update(progress=1.0, message=f"Listo · {n - failed}/{n} canciones ({failed} con error{extra})", state="done")
        else:
            job.update(progress=1.0, message=f"Listo · {n} canciones", state="done")

def glob_escape(s):
    return re.sub(r"([\[\]*?])", r"[\1]", s)


# ───────────────────────────── helpers de interfaz ─────────────────────────────


_css_providers = []


def render_css(css, accent_id):
    # primero los tokens largos (@A_RGB@ antes que @A@) para que no se pisen
    for tok, val in sorted(accent_tokens(accent_id).items(), key=lambda kv: -len(kv[0])):
        css = css.replace(tok, val)
    return css


def apply_css(css, accent_id=None):
    """Carga el CSS regla a regla: si el tema rechaza una propiedad solo se pierde esa regla.
    Se puede volver a llamar para cambiar el color de acento: reemplaza lo cargado antes."""
    global CURRENT_ACCENT
    CURRENT_ACCENT = accent_id if accent_id in ACCENTS else DEFAULT_ACCENT
    screen = Gdk.Screen.get_default()
    for prov in _css_providers:
        Gtk.StyleContext.remove_provider_for_screen(screen, prov)
    _css_providers.clear()
    css = render_css(css, CURRENT_ACCENT)
    for chunk in css.split("}"):
        chunk = chunk.strip()
        if not chunk:
            continue
        prov = Gtk.CssProvider()
        try:
            prov.load_from_data((chunk + "}").encode())
        except GLib.Error as e:
            log(f"CSS ignorado ({e.message[:90]}): {chunk[:70]!r}")
            continue
        Gtk.StyleContext.add_provider_for_screen(screen, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        _css_providers.append(prov)


def fmt_time(sec):
    sec = max(0, int(sec or 0))
    return f"{sec // 60}:{sec % 60:02d}"


def icon_button(icon, text, *classes):
    btn = Gtk.Button()
    box = Gtk.Box(spacing=8)
    box.set_halign(Gtk.Align.CENTER)
    btn.img = Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
    box.pack_start(btn.img, False, False, 0)
    btn.lbl = Gtk.Label(label=text)
    box.pack_start(btn.lbl, False, False, 0)
    btn.add(box)
    for c in classes:
        btn.get_style_context().add_class(c)
    return btn


def set_icon(btn, name, size=Gtk.IconSize.BUTTON):
    btn.set_image(Gtk.Image.new_from_icon_name(name, size))


class VinylArt(Gtk.DrawingArea):
    """Disco de vinilo dibujado con cairo (ilustración original)."""

    def __init__(self, size=200):
        super().__init__()
        self.size = size
        self.set_size_request(size, size)
        self.set_halign(Gtk.Align.CENTER)
        self.connect("draw", self.on_draw)

    def on_draw(self, widget, cr):
        s = self.size
        c = s / 2
        sh = cairo.RadialGradient(c, c + 6, c * 0.6, c, c + 6, c + 6)
        sh.add_color_stop_rgba(0, 0, 0, 0, 0.55)
        sh.add_color_stop_rgba(1, 0, 0, 0, 0.0)
        cr.set_source(sh)
        cr.paint()
        g = cairo.RadialGradient(c, c, s * 0.08, c, c, c)
        g.add_color_stop_rgb(0, 0.13, 0.13, 0.17)
        g.add_color_stop_rgb(1, 0.03, 0.03, 0.05)
        cr.arc(c, c, c - 6, 0, 2 * math.pi)
        cr.set_source(g)
        cr.fill()
        cr.set_line_width(1)
        r, k = s * 0.23, 0
        while r < c - 10:
            cr.arc(c, c, r, 0, 2 * math.pi)
            cr.set_source_rgba(1, 1, 1, 0.05 if k % 2 else 0.025)
            cr.stroke()
            r += 3.3
            k += 1
        for start in (0.12, 1.12):
            cr.move_to(c, c)
            cr.arc(c, c, c - 8, start * math.pi, (start + 0.16) * math.pi)
            cr.close_path()
            cr.set_source_rgba(1, 1, 1, 0.03)
            cr.fill()
        lg = cairo.LinearGradient(c - s * 0.17, c - s * 0.17, c + s * 0.17, c + s * 0.17)
        lg.add_color_stop_rgb(0, *accent_rgb(CURRENT_ACCENT, 0))
        lg.add_color_stop_rgb(1, *accent_rgb(CURRENT_ACCENT, 1))
        cr.arc(c, c, s * 0.17, 0, 2 * math.pi)
        cr.set_source(lg)
        cr.fill()
        cr.arc(c, c, s * 0.024, 0, 2 * math.pi)
        cr.set_source_rgb(0.055, 0.055, 0.07)
        cr.fill()
        return False


class VinylBackdrop(Gtk.DrawingArea):
    """Mitad superior de un disco de vinilo translúcido que asoma por detrás del reproductor.

    Sin música es un disco genérico con la etiqueta en el color de acento; cuando suena
    una canción, la portada del álbum hace de vinilo y gira (33⅓ rpm) mientras suena."""

    ALPHA = 0.13          # transparencia global del disco
    VINYL_ALPHA = 0.60    # en el modo disco de vinilo se ve mucho más
    RPM = 33.3
    ARM_REST = -0.55

    def __init__(self):
        super().__init__()
        self.set_size_request(1, 1)
        self.set_can_focus(False)
        self.cover_pb = None
        self._cover_url = None
        self.vinyl_mode = False
        self.angle = 0.0
        self._spin_id = None
        self._last = 0.0
        self._layer = None
        self._key = None
        # brazo del tocadiscos: 0 = surco exterior, 1 = surco interior, ARM_REST = reposo
        self.arm_on = True
        self.arm_cur = self.ARM_REST
        self.arm_target = self.ARM_REST
        self.arm_lift = 1.0
        self._arm_id = None
        self.connect("draw", self.on_draw)
        self.connect("destroy", lambda *_: (self.set_spinning(False), self._arm_stop()))

    # ── estado ──
    def set_cover(self, url):
        if url == self._cover_url:
            return
        self._cover_url = url
        self.cover_pb, self._key = None, None
        self.queue_draw()
        if not url:
            return

        def work():
            try:
                pb = pixbuf_square(fetch_bytes(url), 900)
            except Exception:
                return
            GLib.idle_add(self._cover_loaded, url, pb)

        _pool.submit(work)

    def _cover_loaded(self, url, pb):
        if url == self._cover_url:
            self.cover_pb, self._key = pb, None
            self.queue_draw()
        return False

    def set_vinyl(self, on):
        self.vinyl_mode = bool(on)
        self._key = None
        if on:  # el brazo parte del reposo y baja hasta el disco
            self.arm_cur, self.arm_lift = self.ARM_REST, 1.0
            self._arm_go()
        self.queue_draw()

    # ── brazo ──
    def set_arm_enabled(self, on):
        self.arm_on = bool(on)
        if self.arm_on:
            self._arm_go()
        self.queue_draw()

    def set_arm_target(self, t):
        """t: posición 0..1 sobre el disco, o None para dejar el brazo en reposo."""
        t = self.ARM_REST if t is None else max(0.0, min(1.0, t))
        if abs(t - self.arm_target) > 1e-4:
            self.arm_target = t
            self._arm_go()

    def _arm_go(self):
        if self._arm_id is None and self.vinyl_mode and self.arm_on:
            self._arm_id = GLib.timeout_add(33, self._arm_step)

    def _arm_stop(self):
        if self._arm_id is not None:
            GLib.source_remove(self._arm_id)
            self._arm_id = None

    def _arm_step(self):
        diff = self.arm_target - self.arm_cur
        moving = abs(diff) > 0.004
        # se mueve con suavidad; si el salto es corto (avance normal) sigue pegado al surco
        step = diff * 0.12
        if abs(step) < 0.0015:
            step = math.copysign(min(abs(diff), 0.0015), diff)
        self.arm_cur += step if moving else diff
        want_lift = 1.0 if (abs(diff) > 0.03 or self.arm_target <= self.ARM_REST + 1e-3) else 0.0
        self.arm_lift += (want_lift - self.arm_lift) * 0.25
        self.queue_draw()
        settled = (not moving) and abs(self.arm_lift - want_lift) < 0.02
        if settled:
            self.arm_lift = want_lift
            self._arm_id = None
            return False
        return self.vinyl_mode and self.arm_on

    def _arm_geom(self, cx, cy, R, a):
        """Geometría del brazo cuando la aguja está en la posición a (0 exterior … 1 interior)."""
        r_out, r_in = R * 0.95, R * 0.40
        r = r_out - (r_out - r_in) * a
        P = (cx + R * 1.12, cy - R * 0.95)                  # pivote, a la derecha del disco
        L = R * 1.25
        dx, dy = P[0] - cx, P[1] - cy
        D = math.hypot(dx, dy)
        c = (D * D + r * r - L * L) / (2 * D * r)
        ang = math.atan2(dy, dx) + math.acos(max(-1.0, min(1.0, c)))
        S = (cx + r * math.cos(ang), cy + r * math.sin(ang))   # punta de la aguja
        hs = R * 0.15                                           # largo del cabezal
        t = None
        H = (S[0] - (S[0] - P[0]) / L * hs, S[1] - (S[1] - P[1]) / L * hs)
        for _ in range(3):  # el cabezal sigue la tangente del tubo (brazo en "S" suave)
            ux, uy = H[0] - P[0], H[1] - P[1]
            ln = math.hypot(ux, uy) or 1.0
            ux, uy = ux / ln, uy / ln
            nx, ny = -uy, ux
            c1 = (P[0] + ux * ln * 0.42, P[1] + uy * ln * 0.42)
            c2 = (H[0] - ux * ln * 0.30 + nx * R * 0.085, H[1] - uy * ln * 0.30 + ny * R * 0.085)
            tx, ty = H[0] - c2[0], H[1] - c2[1]
            tl = math.hypot(tx, ty) or 1.0
            t = (tx / tl, ty / tl)
            H = (S[0] - t[0] * hs, S[1] - t[1] * hs)
        return {"P": P, "S": S, "H": H, "c1": c1, "c2": c2, "t": t, "n": (-t[1], t[0]), "u": (ux, uy)}

    def _draw_arm(self, cr, cx, cy, R):
        g = self._arm_geom(cx, cy, R, self.arm_cur)
        P, S, H, c1, c2, t, n, u = (g[k] for k in ("P", "S", "H", "c1", "c2", "t", "n", "u"))
        lift = self.arm_lift
        CHROME = [(1.0, (0.22, 0.23, 0.26)), (0.80, (0.50, 0.52, 0.56)),
                  (0.55, (0.76, 0.78, 0.82)), (0.28, (0.93, 0.94, 0.96)), (0.10, (1, 1, 1))]
        DARK = [(1.0, (0.03, 0.03, 0.04)), (0.80, (0.09, 0.09, 0.11)),
                (0.55, (0.17, 0.18, 0.21)), (0.28, (0.30, 0.31, 0.35)), (0.10, (0.45, 0.46, 0.50))]
        BRASS = [(1.0, (0.20, 0.17, 0.12)), (0.80, (0.45, 0.38, 0.26)),
                 (0.55, (0.70, 0.62, 0.46)), (0.28, (0.88, 0.82, 0.66)), (0.10, (1, 0.98, 0.9))]

        def curve(ox, oy):
            cr.move_to(P[0] + ox, P[1] + oy)
            cr.curve_to(c1[0] + ox, c1[1] + oy, c2[0] + ox, c2[1] + oy, H[0] + ox, H[1] + oy)

        def seg(a, b):
            def f(ox, oy):
                cr.move_to(a[0] + ox, a[1] + oy)
                cr.line_to(b[0] + ox, b[1] + oy)
            return f

        def metal(path, width, pal, cap=cairo.LINE_CAP_ROUND, alpha=1.0):
            # cilindro: capas cada vez más estrechas y claras, desplazadas hacia la luz (arriba-izquierda)
            cr.set_line_cap(cap)
            for f, col in pal:
                k = width * 0.13 * (1 - f)
                cr.set_line_width(width * f)
                cr.set_source_rgba(*col, alpha)
                path(-k, -k)
                cr.stroke()

        def shadow(path, width, ox, oy):
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            for wmul, a_ in ((2.6, 0.035), (1.9, 0.05), (1.3, 0.07), (1.0, 0.09)):
                cr.set_line_width(width * wmul)
                cr.set_source_rgba(0, 0, 0, a_)
                path(ox, oy)
                cr.stroke()

        W_TUBE, W_CW = R * 0.030, R * 0.135
        back = (P[0] - u[0] * R * 0.46, P[1] - u[1] * R * 0.46)
        cw_a = (P[0] - u[0] * R * 0.20, P[1] - u[1] * R * 0.20)
        stub = seg(P, cw_a)
        cw = seg(cw_a, back)
        tip_back = (S[0] - t[0] * R * 0.075, S[1] - t[1] * R * 0.075)
        shell = seg(H, S)
        cart = seg(tip_back, S)

        # reposabrazos: soporte fijo bajo el tubo cerca de la posición de reposo
        rg = self._arm_geom(cx, cy, R, self.ARM_REST)
        q = 0.70
        bx = ((1 - q) ** 3 * rg["P"][0] + 3 * (1 - q) ** 2 * q * rg["c1"][0]
              + 3 * (1 - q) * q * q * rg["c2"][0] + q ** 3 * rg["H"][0])
        by = ((1 - q) ** 3 * rg["P"][1] + 3 * (1 - q) ** 2 * q * rg["c1"][1]
              + 3 * (1 - q) * q * q * rg["c2"][1] + q ** 3 * rg["H"][1])
        cr.save()
        rest_sh = (R * 0.03, R * 0.045)
        cr.set_source_rgba(0, 0, 0, 0.18)
        cr.arc(bx + rest_sh[0], by + rest_sh[1], R * 0.05, 0, 2 * math.pi)
        cr.fill()
        rad = cairo.RadialGradient(bx - R * 0.015, by - R * 0.015, R * 0.005, bx, by, R * 0.05)
        rad.add_color_stop_rgb(0, 0.85, 0.86, 0.9)
        rad.add_color_stop_rgb(1, 0.18, 0.19, 0.22)
        cr.set_source(rad)
        cr.arc(bx, by, R * 0.045, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(0.04, 0.04, 0.05, 0.9)  # cuna donde descansa el tubo
        cr.arc(bx, by, R * 0.022, 0, 2 * math.pi)
        cr.fill()
        cr.restore()

        # sombras sobre el disco (más lejanas y difusas con el brazo levantado)
        so = (R * (0.02 + 0.05 * lift), R * (0.03 + 0.07 * lift))
        shadow(curve, W_TUBE, *so)
        shadow(cw, W_CW * 0.8, *so)
        shadow(shell, R * 0.05, *so)
        cr.set_source_rgba(0, 0, 0, 0.16)
        cr.arc(P[0] + R * 0.03, P[1] + R * 0.045, R * 0.15, 0, 2 * math.pi)
        cr.fill()

        # base del pivote: anillo cromado, cilindro oscuro y tapa
        rb = cairo.RadialGradient(P[0] - R * 0.05, P[1] - R * 0.05, R * 0.01, P[0], P[1], R * 0.15)
        rb.add_color_stop_rgb(0, 0.97, 0.97, 0.99)
        rb.add_color_stop_rgb(0.55, 0.55, 0.57, 0.62)
        rb.add_color_stop_rgb(1, 0.16, 0.17, 0.20)
        cr.set_source(rb)
        cr.arc(P[0], P[1], R * 0.15, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(0.04, 0.04, 0.05, 1)
        cr.arc(P[0], P[1], R * 0.105, 0, 2 * math.pi)
        cr.fill()
        r2 = cairo.RadialGradient(P[0] - R * 0.03, P[1] - R * 0.03, R * 0.005, P[0], P[1], R * 0.095)
        r2.add_color_stop_rgb(0, 0.30, 0.31, 0.35)
        r2.add_color_stop_rgb(1, 0.06, 0.06, 0.08)
        cr.set_source(r2)
        cr.arc(P[0], P[1], R * 0.095, 0, 2 * math.pi)
        cr.fill()
        ar, ag, ab = accent_rgb(CURRENT_ACCENT, 0)
        cr.set_line_width(R * 0.008)
        cr.set_source_rgba(ar, ag, ab, 0.95)
        cr.arc(P[0], P[1], R * 0.082, 0, 2 * math.pi)
        cr.stroke()

        # contrapeso (latón/cromo con acanaladuras), varilla trasera y tubo
        metal(stub, R * 0.032, CHROME)
        metal(cw, W_CW, CHROME, cairo.LINE_CAP_BUTT)
        cr.set_line_cap(cairo.LINE_CAP_BUTT)
        cr.set_source_rgba(0, 0, 0, 0.45)
        cr.set_line_width(R * 0.006)
        for k in (0.34, 0.46, 0.58, 0.70):
            m = (cw_a[0] + (back[0] - cw_a[0]) * k, cw_a[1] + (back[1] - cw_a[1]) * k)
            cr.move_to(m[0] - n[0] * W_CW * 0.5, m[1] - n[1] * W_CW * 0.5)
            cr.line_to(m[0] + n[0] * W_CW * 0.5, m[1] + n[1] * W_CW * 0.5)
            cr.stroke()
        metal(curve, W_TUBE, CHROME)
        # anillo de ajuste de la anti-skating cerca del pivote
        ring_a = (P[0] + u[0] * R * 0.17, P[1] + u[1] * R * 0.17)
        ring_b = (P[0] + u[0] * R * 0.205, P[1] + u[1] * R * 0.205)
        metal(seg(ring_a, ring_b), R * 0.055, DARK, cairo.LINE_CAP_BUTT)

        # cabezal, cápsula y púa
        metal(shell, R * 0.048, DARK, cairo.LINE_CAP_ROUND)
        fl_a = (H[0] + n[0] * R * 0.01, H[1] + n[1] * R * 0.01)
        fl_b = (H[0] + n[0] * R * 0.095 + t[0] * R * 0.03, H[1] + n[1] * R * 0.095 + t[1] * R * 0.03)
        metal(seg(fl_a, fl_b), R * 0.014, CHROME)  # lengüeta para levantar el brazo
        metal(cart, R * 0.062, BRASS, cairo.LINE_CAP_BUTT)
        cr.set_source_rgba(1, 1, 1, 0.9)
        cr.arc(S[0], S[1], R * 0.009, 0, 2 * math.pi)
        cr.fill()

    def set_spinning(self, on):
        if on and self._spin_id is None:
            self._last = time.monotonic()
            self._spin_id = GLib.timeout_add(40, self._spin)
        elif not on and self._spin_id is not None:
            GLib.source_remove(self._spin_id)
            self._spin_id = None

    def _spin(self):
        now = time.monotonic()
        self.angle = (self.angle + (now - self._last) * 2 * math.pi * self.RPM / 60) % (2 * math.pi)
        self._last = now
        self.queue_draw()
        return True

    # ── dibujo ──
    @staticmethod
    def _render(d, pb, accent):
        """Capa que gira: portada (o cuerpo del disco) + surcos + etiqueta con agujero."""
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, d, d)
        cr = cairo.Context(surf)
        c = d / 2
        R = c - 1
        cr.arc(c, c, R, 0, 2 * math.pi)
        cr.clip_preserve()
        if pb is not None:
            cr.save()
            k = d / pb.get_width()
            cr.scale(k, k)
            Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
            cr.paint()
            cr.restore()
        else:
            body = cairo.RadialGradient(c, c, R * 0.1, c, c, R)
            body.add_color_stop_rgb(0, 0.34, 0.34, 0.42)
            body.add_color_stop_rgb(1, 0.20, 0.20, 0.26)
            cr.set_source(body)
            cr.fill()
        cr.reset_clip()
        cr.new_path()
        # surcos: claros y oscuros alternos para que se vean sobre cualquier portada
        cr.set_line_width(1)
        r, k = R * 0.36, 0
        step = max(2.6, R / 75)
        while r < R - 4:
            cr.arc(c, c, r, 0, 2 * math.pi)
            if k % 2:
                cr.set_source_rgba(1, 1, 1, 0.14)
            else:
                cr.set_source_rgba(0, 0, 0, 0.14)
            cr.stroke()
            r += step
            k += 1
        # borde del disco
        cr.set_line_width(2)
        cr.arc(c, c, R - 1, 0, 2 * math.pi)
        cr.set_source_rgba(0, 0, 0, 0.35)
        cr.stroke()
        # etiqueta central con el color de acento
        lr = R * (0.17 if pb is not None else 0.30)
        lg = cairo.LinearGradient(c - lr, c - lr, c + lr, c + lr)
        lg.add_color_stop_rgb(0, *accent_rgb(accent, 0))
        lg.add_color_stop_rgb(1, *accent_rgb(accent, 1))
        cr.arc(c, c, lr, 0, 2 * math.pi)
        cr.set_source(lg)
        cr.fill()
        # agujero (transparente)
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.arc(c, c, R * 0.028, 0, 2 * math.pi)
        cr.fill()
        return surf

    def on_draw(self, widget, cr):
        w, h = self.get_allocated_width(), self.get_allocated_height()
        if w < 8 or h < 8:
            return False
        if self.vinyl_mode:  # disco entero, centrado y bien visible
            R = max(40, min(w, h) * 0.42)
            if self.arm_on:  # el brazo, a la derecha del disco, debe verse entero
                R = max(40, min(R, (w / 2 - 14) / 1.30, (h / 2 - 10) / 1.12))
            cx, cy, alpha = w / 2, h / 2, self.VINYL_ALPHA
        else:  # el centro del disco está en el borde inferior: solo se ve la mitad de arriba
            R = max(40, min(w * 0.26, h * 0.58))
            cx, cy, alpha = w / 2, h, self.ALPHA
        d = int(R) * 2
        key = (d, CURRENT_ACCENT, id(self.cover_pb))
        if key != self._key:
            self._layer, self._key = self._render(d, self.cover_pb, CURRENT_ACCENT), key
        cr.save()
        cr.translate(cx, cy)
        cr.rotate(self.angle)
        cr.set_source_surface(self._layer, -d / 2, -d / 2)
        cr.paint_with_alpha(alpha)
        cr.restore()
        # reflejos fijos (no giran con el disco)
        cr.save()
        cr.translate(cx, cy)
        for start in (1.10, 1.72):
            cr.move_to(0, 0)
            cr.arc(0, 0, d / 2 - 3, start * math.pi, (start + 0.10) * math.pi)
            cr.close_path()
            cr.set_source_rgba(1, 1, 1, 0.025 * alpha / self.ALPHA)
            cr.fill()
        cr.restore()
        if self.vinyl_mode and self.arm_on:
            self._draw_arm(cr, cx, cy, d / 2)
        return False


class AccentSwatch(Gtk.Button):
    """Círculo con el degradado de un color de acento; un anillo marca el elegido."""

    SIZE = 34

    def __init__(self, accent_id, is_selected, on_pick):
        super().__init__()
        self.accent_id, self.is_selected = accent_id, is_selected
        self.get_style_context().add_class("swatch")
        self.set_relief(Gtk.ReliefStyle.NONE)
        self.set_tooltip_text(ACCENTS[accent_id][0])
        area = Gtk.DrawingArea()
        area.set_size_request(self.SIZE, self.SIZE)
        area.connect("draw", self._draw)
        self.add(area)
        self.connect("clicked", lambda *_: on_pick(accent_id))

    def _draw(self, widget, cr):
        s = self.SIZE
        c = s / 2
        g = cairo.LinearGradient(0, 0, s, s)  # mismo degradado diagonal que el resto de la interfaz
        g.add_color_stop_rgb(0, *accent_rgb(self.accent_id, 0))
        g.add_color_stop_rgb(1, *accent_rgb(self.accent_id, 1))
        cr.arc(c, c, c - 4, 0, 2 * math.pi)
        cr.set_source(g)
        cr.fill()
        if self.is_selected():
            cr.set_line_width(2)
            cr.arc(c, c, c - 1.5, 0, 2 * math.pi)
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.stroke()
            cr.set_line_width(2.4)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.move_to(c - 5.5, c + 0.5)
            cr.line_to(c - 1.5, c + 4.5)
            cr.line_to(c + 6, c - 4)
            cr.set_source_rgba(1, 1, 1, 1)
            cr.stroke()
        return False


class Segmented(Gtk.Box):
    """Selector segmentado (botones tipo píldora con exclusión mutua)."""

    def __init__(self, options, on_change):
        super().__init__(spacing=0)
        self.get_style_context().add_class("seg")
        self.on_change = on_change
        self.btns = {}
        self.value = options[0][0]
        self._guard = False
        for oid, text in options:
            b = Gtk.ToggleButton(label=text)
            b.connect("toggled", self._toggled, oid)
            self.btns[oid] = b
            self.pack_start(b, False, False, 0)
        self._guard = True
        self.btns[self.value].set_active(True)
        self._guard = False

    def select(self, oid, notify=True):
        """Cambia la opción por código (con o sin avisar al callback)."""
        if oid not in self.btns:
            return
        self._guard = True
        for o, b in self.btns.items():
            b.set_active(o == oid)
        self.value = oid
        self._guard = False
        if notify:
            self.on_change(self.value)

    def _toggled(self, btn, oid):
        if self._guard:
            return
        self._guard = True
        if btn.get_active():
            for o, b in self.btns.items():
                if o != oid:
                    b.set_active(False)
            self.value = oid
        else:
            btn.set_active(True)
        self._guard = False
        self.on_change(self.value)


# ───────────────────────────── vista previa y reproductor ─────────────────────────────

PREVIEW_DIR = CACHE_DIR.parent / "preview"
_CLIENTS = (None, ["android_vr"], ["tv"], ["ios"], ["web_safari"])
COOKIES_FILE = CONFIG_DIR / "cookies.txt"
_AUTH = {"good": None, "bad": set()}   # método que funcionó / navegadores sin cookies útiles


def browser_sources():
    """Navegadores instalados de los que yt-dlp puede leer cookies."""
    home = Path.home()
    table = (
        ("firefox", (".mozilla/firefox", "snap/firefox/common/.mozilla/firefox",
                     ".var/app/org.mozilla.firefox/.mozilla/firefox")),
        ("chrome", (".config/google-chrome",)),
        ("chromium", (".config/chromium", "snap/chromium/common/chromium")),
        ("brave", (".config/BraveSoftware/Brave-Browser",)),
        ("edge", (".config/microsoft-edge",)),
        ("vivaldi", (".config/vivaldi",)),
        ("opera", (".config/opera",)),
    )
    return [n for n, ds in table if any((home / d).is_dir() for d in ds)]


def ytdlp_run(opts, url, cancelled=lambda: False, label=""):
    """Descarga con yt-dlp probando clientes de YouTube alternativos. Si YouTube pide iniciar
    sesión (restricción de edad…) reintenta solo con clientes embebidos y con las cookies del
    navegador (o ~/.config/vinilo/cookies.txt). Devuelve la info o lanza el último error."""
    import yt_dlp

    def attempt(step):
        clients, cookies = step
        o = dict(opts)
        if clients:
            o["extractor_args"] = {"youtube": {"player_client": clients}}
        if cookies == "file":
            o["cookiefile"] = str(COOKIES_FILE)
        elif cookies:
            o["cookiesfrombrowser"] = (cookies,)
        with yt_dlp.YoutubeDL(o) as ydl:
            return ydl.extract_info(url, download=True)

    steps = [(c, None) for c in _CLIENTS]
    auth_added, last, i = False, None, 0
    while i < len(steps):
        step = steps[i]
        i += 1
        try:
            info = attempt(step)
            if auth_added and (step[1] or step[0]):
                _AUTH["good"] = step
            if step[0] or step[1]:
                log(f"[{label}] OK con cliente {step[0] or 'default'}"
                    + (f" + cookies {step[1]}" if step[1] else ""))
            return info
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            if cancelled():
                raise Cancelled()
            last = e
            msg = clean_err(e)
            log(f"[{label}] cliente {step[0] or 'default'}"
                f"{' cookies ' + step[1] if step[1] else ''} -> {msg}")
            if is_offline(e):
                break
            if step[1] and step[1] != "file" and not needs_auth(msg):
                _AUTH["bad"].add(step[1])  # no se pudieron leer sus cookies: no insistir
            if needs_auth(msg) and not auth_added:
                auth_added = True
                extra = [(["tv_embedded"], None), (["web_embedded"], None), (["mweb"], None)]
                if COOKIES_FILE.is_file():
                    extra.append((None, "file"))
                for b in browser_sources():
                    if b not in _AUTH["bad"]:
                        extra += [(["web_safari"], b), (None, b)]
                good = _AUTH["good"]
                if good:  # lo que ya funcionó, lo primero
                    if good in extra:
                        extra.remove(good)
                    steps.insert(i, good)
                steps.extend(extra)
                continue
            if not auth_added and not any(k in msg for k in ("403", "not available", "Forbidden")):
                break
    raise last



def clear_cache():
    """Vacía la caché de imágenes y de vistas previas (se llama al cerrar la aplicación)."""
    for d in (CACHE_DIR, PREVIEW_DIR):
        try:
            shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


def prune_preview_cache(max_files=60):
    try:
        files = sorted((f for f in PREVIEW_DIR.iterdir() if f.is_file()), key=lambda f: f.stat().st_mtime, reverse=True)
        for f in files[max_files:]:
            f.unlink()
    except Exception:
        pass


def fetch_preview(video_id):
    """Descarga una copia temporal (caché) de la pista para poder escucharla antes de bajar el álbum."""
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    for f in PREVIEW_DIR.glob(video_id + ".*"):
        if f.suffix not in (".part", ".ytdl") and f.stat().st_size > 0:
            return str(f)
    import yt_dlp

    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(PREVIEW_DIR / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 3,
        "remote_components": ["ejs:github"],
    }
    rt = find_js_runtime()
    if rt:
        opts["js_runtimes"] = {rt[0]: {"path": rt[1]}}
    url = f"https://music.youtube.com/watch?v={video_id}"
    info = ytdlp_run(opts, url, label=f"preview {video_id}")
    path = None
    try:
        path = info["requested_downloads"][0]["filepath"]
    except Exception:
        pass
    if path and os.path.exists(path):
        return path
    for f in PREVIEW_DIR.glob(video_id + ".*"):
        if f.suffix not in (".part", ".ytdl"):
            return str(f)
    raise RuntimeError("archivo de vista previa no generado")


class Player:
    """Reproductor de audio basado en GStreamer (playbin). Funciona con archivos locales y vistas previas."""

    def __init__(self, on_change, resolve, prefetch):
        self.on_change, self.resolve, self.prefetch = on_change, resolve, prefetch
        self.queue, self.index, self.state = [], -1, "idle"  # idle|loading|playing|paused|stopped|error
        self.token, self.available, self.error_msg = 0, False, ""
        try:
            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            Gst.init(None)
            self.Gst = Gst
            self.pipe = Gst.ElementFactory.make("playbin", "vinilo-player")
            if self.pipe is None:
                raise RuntimeError("playbin no disponible")
            self.pipe.set_property("video-sink", Gst.ElementFactory.make("fakesink", "vinilo-novideo"))
            self._psink = None
            self.pipe.connect("deep-element-added", self._on_deep_added)
            bus = self.pipe.get_bus()
            bus.add_signal_watch()
            bus.connect("message::eos", self._on_eos)
            bus.connect("message::error", self._on_error)
            self.available = True
        except Exception as e:  # noqa: BLE001
            self.error_msg = str(e)
            log(f"GStreamer no disponible: {e}")

    @property
    def item(self):
        return self.queue[self.index] if 0 <= self.index < len(self.queue) else None

    def set_volume(self, v):
        if self.available:
            self.pipe.set_property("volume", max(0.0, min(1.0, v)))

    def play_queue(self, items, index=0):
        self.queue = list(items)
        self.play_index(index)

    def play_index(self, i):
        if not self.available or not (0 <= i < len(self.queue)):
            return
        self.index = i
        self.token += 1
        tok = self.token
        item = self.queue[i]
        self.pipe.set_state(self.Gst.State.NULL)
        path = item.get("path")
        if path and os.path.exists(path):
            self._start(path)
        else:
            self.state = "loading"
            self.on_change()
            self.resolve(item, lambda p, err, tok=tok: self._resolved(tok, p, err))

    def _resolved(self, tok, path, err):
        if tok != self.token:
            return
        if err or not path:
            self.state = "error"
            self.error_msg = friendly_error(err) if err else "sin audio"
            self.on_change()
            return
        self._start(path)

    def _on_deep_added(self, pipe, _bin, el):
        try:
            f = el.get_factory()
            if f and f.get_name() == "pulsesink":
                self._psink = el
                self._apply_stream_props()
        except Exception:  # noqa: BLE001
            pass

    def _apply_stream_props(self):
        """Nombre del programa y de la pista en el flujo de audio (lo leen los applets de volumen)."""
        if not self._psink:
            return
        try:
            it = self.item or {}
            st = self.Gst.Structure.new_empty("props")

            def put(k, v):
                val = GObject.Value()
                val.init(GObject.TYPE_STRING)
                val.set_string(str(v))
                st.set_value(k, val)

            put("application.name", APP_NAME)
            put("application.icon_name", "vinilo")
            put("media.role", "music")
            title, artist = it.get("title"), it.get("artist")
            if title:
                put("media.title", title)
                put("media.name", f"{artist} – {title}" if artist else title)
            if artist:
                put("media.artist", artist)
            self._psink.set_property("stream-properties", st)
        except Exception as e:  # noqa: BLE001
            log(f"stream-properties: {e}")

    def _start(self, path):
        self._apply_stream_props()
        self.pipe.set_property("uri", Gio.File.new_for_path(path).get_uri())
        self.pipe.set_state(self.Gst.State.PLAYING)
        self.state = "playing"
        self.on_change()
        nxt = self.index + 1
        if nxt < len(self.queue):
            self.prefetch(self.queue[nxt])

    def toggle(self):
        if not self.available or self.item is None:
            return
        if self.state == "playing":
            self.pipe.set_state(self.Gst.State.PAUSED)
            self.state = "paused"
        elif self.state == "paused":
            self.pipe.set_state(self.Gst.State.PLAYING)
            self.state = "playing"
        elif self.state in ("stopped", "error"):
            self.play_index(self.index)
            return
        self.on_change()

    def next(self):
        if self.index + 1 < len(self.queue):
            self.play_index(self.index + 1)
        else:
            self.pipe.set_state(self.Gst.State.NULL)
            self.state = "stopped"
            self.on_change()

    def prev(self):
        pos, _ = self.position()
        if pos > 3 or self.index == 0:
            self.seek_frac(0)
        else:
            self.play_index(self.index - 1)

    def stop_all(self):
        if self.available:
            self.token += 1
            self.pipe.set_state(self.Gst.State.NULL)
        self.queue, self.index, self.state = [], -1, "idle"
        self.on_change()

    def position(self):
        if not self.available or self.state not in ("playing", "paused"):
            return 0, 0
        ok, pos = self.pipe.query_position(self.Gst.Format.TIME)
        ok2, dur = self.pipe.query_duration(self.Gst.Format.TIME)
        return (pos / 1e9 if ok else 0), (dur / 1e9 if ok2 else 0)

    def seek_frac(self, frac):
        _, dur = self.position()
        if dur > 0:
            self.pipe.seek_simple(self.Gst.Format.TIME, self.Gst.SeekFlags.FLUSH | self.Gst.SeekFlags.KEY_UNIT,
                                  int(max(0.0, min(1.0, frac)) * dur * 1e9))

    def _on_eos(self, bus, msg):
        self.next()

    def _on_error(self, bus, msg):
        err, dbg = msg.parse_error()
        log(f"GStreamer: {err.message} | {dbg}")
        self.pipe.set_state(self.Gst.State.NULL)
        self.state, self.error_msg = "error", err.message
        self.on_change()


MPRIS_XML = """<node>
 <interface name="org.mpris.MediaPlayer2">
  <method name="Raise"/><method name="Quit"/>
  <property name="CanQuit" type="b" access="read"/>
  <property name="CanRaise" type="b" access="read"/>
  <property name="HasTrackList" type="b" access="read"/>
  <property name="Identity" type="s" access="read"/>
  <property name="DesktopEntry" type="s" access="read"/>
  <property name="SupportedUriSchemes" type="as" access="read"/>
  <property name="SupportedMimeTypes" type="as" access="read"/>
 </interface>
 <interface name="org.mpris.MediaPlayer2.Player">
  <method name="Next"/><method name="Previous"/><method name="Pause"/><method name="PlayPause"/>
  <method name="Stop"/><method name="Play"/>
  <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
  <method name="SetPosition"><arg direction="in" name="TrackId" type="o"/><arg direction="in" name="Position" type="x"/></method>
  <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
  <signal name="Seeked"><arg name="Position" type="x"/></signal>
  <property name="PlaybackStatus" type="s" access="read"/>
  <property name="LoopStatus" type="s" access="read"/>
  <property name="Rate" type="d" access="read"/>
  <property name="Shuffle" type="b" access="read"/>
  <property name="Metadata" type="a{sv}" access="read"/>
  <property name="Volume" type="d" access="read"/>
  <property name="Position" type="x" access="read"/>
  <property name="MinimumRate" type="d" access="read"/>
  <property name="MaximumRate" type="d" access="read"/>
  <property name="CanGoNext" type="b" access="read"/>
  <property name="CanGoPrevious" type="b" access="read"/>
  <property name="CanPlay" type="b" access="read"/>
  <property name="CanPause" type="b" access="read"/>
  <property name="CanSeek" type="b" access="read"/>
  <property name="CanControl" type="b" access="read"/>
 </interface>
</node>"""
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER = "org.mpris.MediaPlayer2.Player"


class Mpris:
    """Servicio MPRIS2: los applets de sonido y las teclas multimedia ven «Vinilo», la pista y el artista,
    y pueden pausar / saltar de canción."""

    def __init__(self, win):
        self.win, self.conn, self._len = win, None, 0
        try:
            self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            for iface in Gio.DBusNodeInfo.new_for_xml(MPRIS_XML).interfaces:
                self.conn.register_object(MPRIS_PATH, iface, self._call, self._get, None)
            Gio.bus_own_name_on_connection(self.conn, "org.mpris.MediaPlayer2.vinilo",
                                           Gio.BusNameOwnerFlags.NONE, None, None)
        except Exception as e:  # noqa: BLE001
            log(f"MPRIS no disponible: {e}")
            self.conn = None

    # ── datos ──
    def _status(self):
        st = self.win.player.state
        return {"playing": "Playing", "loading": "Playing", "paused": "Paused"}.get(st, "Stopped")

    def _metadata(self):
        p = self.win.player
        it = p.item
        V = GLib.Variant
        if not it:
            return V("a{sv}", {"mpris:trackid": V("o", MPRIS_PATH + "/TrackList/NoTrack")})
        tid = hashlib.md5(str(it.get("key", "")).encode()).hexdigest()
        md = {
            "mpris:trackid": V("o", "/org/vinilo/track/" + tid),
            "xesam:title": V("s", it.get("title") or ""),
            "xesam:album": V("s", it.get("album") or ""),
            "xesam:artist": V("as", [it["artist"]] if it.get("artist") else []),
        }
        dur = p.position()[1]
        if dur > 0:
            md["mpris:length"] = V("x", int(dur * 1e6))
            self._len = int(dur)
        art = it.get("cover_big") or it.get("cover")
        if art:
            md["mpris:artUrl"] = V("s", Gio.File.new_for_path(art).get_uri() if art.startswith("/") else art)
        return V("a{sv}", md)

    def _get(self, conn, sender, path, iface, name):
        V = GLib.Variant
        p = self.win.player
        if iface == "org.mpris.MediaPlayer2":
            return {"CanQuit": V("b", True), "CanRaise": V("b", True), "HasTrackList": V("b", False),
                    "Identity": V("s", APP_NAME), "DesktopEntry": V("s", "vinilo"),
                    "SupportedUriSchemes": V("as", []), "SupportedMimeTypes": V("as", [])}.get(name)
        has = p.item is not None
        return {
            "PlaybackStatus": lambda: V("s", self._status()),
            "LoopStatus": lambda: V("s", "None"),
            "Rate": lambda: V("d", 1.0), "MinimumRate": lambda: V("d", 1.0), "MaximumRate": lambda: V("d", 1.0),
            "Shuffle": lambda: V("b", False),
            "Metadata": self._metadata,
            "Volume": lambda: V("d", float(self.win.cfg.get("volume", 0.8))),
            "Position": lambda: V("x", int(p.position()[0] * 1e6)),
            "CanGoNext": lambda: V("b", has and p.index + 1 < len(p.queue)),
            "CanGoPrevious": lambda: V("b", has),
            "CanPlay": lambda: V("b", has), "CanPause": lambda: V("b", has),
            "CanSeek": lambda: V("b", has), "CanControl": lambda: V("b", True),
        }.get(name, lambda: None)()

    def _call(self, conn, sender, path, iface, method, params, inv):
        w, p = self.win, self.win.player
        try:
            if method == "PlayPause":
                p.toggle()
            elif method == "Play" and p.state in ("paused", "stopped", "error"):
                p.toggle()
            elif method == "Pause" and p.state == "playing":
                p.toggle()
            elif method == "Stop":
                p.stop_all()
            elif method == "Next":
                p.next()
            elif method == "Previous":
                p.prev()
            elif method == "Seek":
                pos, dur = p.position()
                if dur > 0:
                    p.seek_frac((pos + params.unpack()[0] / 1e6) / dur)
            elif method == "SetPosition":
                dur = p.position()[1]
                if dur > 0:
                    p.seek_frac(params.unpack()[1] / 1e6 / dur)
            elif method == "Raise":
                w.present()
            elif method == "Quit":
                w.close()
        except Exception as e:  # noqa: BLE001
            log(f"MPRIS {method}: {e}")
        inv.return_value(None)

    def update(self):
        """Avisa a los applets de que cambió la pista o el estado."""
        if not self.conn:
            return
        try:
            changed = {n: self._get(None, None, None, MPRIS_PLAYER, n) for n in
                       ("PlaybackStatus", "Metadata", "CanGoNext", "CanGoPrevious", "CanPlay", "CanPause")}
            self.conn.emit_signal(None, MPRIS_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                  GLib.Variant("(sa{sv}as)", (MPRIS_PLAYER, changed, [])))
        except Exception as e:  # noqa: BLE001
            log(f"MPRIS update: {e}")

    def tick(self):
        """Cuando por fin se conoce la duración de la pista, se vuelve a publicar la metadata."""
        p = self.win.player
        if self.conn and p.item and p.state in ("playing", "paused"):
            dur = int(p.position()[1])
            if dur > 0 and dur != self._len:
                self.update()


def make_library_card(name, cover, title, sub, size, on_open, on_play, on_menu, online=False):
    """Tarjeta de la colección: carátula con botón de reproducir al pasar el ratón."""
    ev = Gtk.EventBox()
    ev.set_name(name)
    ev.get_style_context().add_class("lib-card")
    ev.set_halign(Gtk.Align.CENTER)
    ev.set_valign(Gtk.Align.START)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.set_border_width(10)

    overlay = Gtk.Overlay()
    overlay.set_halign(Gtk.Align.CENTER)
    cbtn = Gtk.Button()
    cbtn.get_style_context().add_class("bare")
    cov = Cover(size)
    cov.set_url(cover)
    cbtn.add(cov)
    cbtn.connect("clicked", lambda *_: on_open())
    overlay.add(cbtn)
    if online:  # marca para distinguir los álbumes online (de «me gusta») de los descargados
        badge = Gtk.Label(label="☁ ONLINE")
        badge.get_style_context().add_class("online-badge")
        badge.set_halign(Gtk.Align.START)
        badge.set_valign(Gtk.Align.START)
        badge.set_margin_start(6)
        badge.set_margin_top(6)
        overlay.add_overlay(badge)
        overlay.set_overlay_pass_through(badge, True)
    rev = Gtk.Revealer()
    rev.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
    rev.set_transition_duration(180)
    rev.set_halign(Gtk.Align.END)
    rev.set_valign(Gtk.Align.END)
    rev.set_margin_end(10)
    rev.set_margin_bottom(10)
    fab = Gtk.Button.new_from_icon_name("media-playback-start-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
    fab.get_style_context().add_class("fab")
    fab.set_tooltip_text("Reproducir álbum")
    fab.connect("clicked", lambda *_: on_play())
    rev.add(fab)
    overlay.add_overlay(rev)
    box.pack_start(overlay, False, False, 0)

    tbtn = Gtk.Button()
    tbtn.get_style_context().add_class("bare")
    col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    for text, cls in ((title, "card-title"), (sub, "card-sub")):
        lbl = label(text or "", cls, ellipsize=True)
        lbl.set_width_chars(16)
        lbl.set_max_width_chars(16)
        col.pack_start(lbl, False, False, 0)
    tbtn.add(col)
    tbtn.connect("clicked", lambda *_: on_open())
    box.pack_start(tbtn, False, False, 0)
    ev.add(box)

    ev.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
    ev.connect("enter-notify-event", lambda *_: (rev.set_reveal_child(True), False)[1])

    def leave(w, e):
        if e.detail != Gdk.NotifyType.INFERIOR:
            rev.set_reveal_child(False)
        return False

    ev.connect("leave-notify-event", leave)

    def press(w, e):
        if e.button == 3:
            on_menu(w, e)
            return True
        return False

    # El clic derecho puede caer sobre la portada o el título (botones hijos):
    # se escucha en los tres para que el menú salga siempre, y justo bajo el puntero.
    for wdg in (ev, cbtn, tbtn):
        wdg.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        wdg.connect("button-press-event", press)
    return ev


# ───────────────────────────── ventana ─────────────────────────────


class Win(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1280, 820)
        self.set_size_request(980, 620)
        self.cfg = load_config()
        self.yt = None
        self._api_lock = threading.RLock()
        self._search_lock = threading.Lock()
        self.library = load_library()
        self.favs = load_favs()          # álbumes online marcados con «me gusta»
        self._entry_map = {}
        self._lib_items = []
        self._lib_artists = []
        self._lib_artist = None          # clave del artista cuyo contenido se está viendo
        self._lib_artist_name = ""
        self._lib_prev_mode = "all"
        self.dhist, self.dcur = [], "search"
        self.chist, self.ccur = [], "list"
        self._toast_id = None
        self._dragging = False
        self._seek_value = None
        self._bar_cover = None
        self._vol_save = None
        self._error_shown = False
        self._inflight = set()
        self.rows_local, self.rows_prev = {}, {}
        self.manager = DownloadManager(lambda: self.cfg, self._job_added, self._job_finished)
        self.player = Player(self._player_changed, self._resolve_item, self._prefetch_item)
        self.player.set_volume(float(self.cfg.get("volume", 0.8)))
        self.get_style_context().add_class("root")
        self._ytdlp_latest = None
        self.vinyl_mode = False
        self._vq_key = None
        self._hover_inside = False
        self.connect("delete-event", self._on_close)
        self.connect("key-press-event", self._on_key)

        self._build_header()
        self._build_body()
        self.show_all()
        self.back_btn.hide()
        self.dl_badge_update()
        self.refresh_library()
        self.mpris = Mpris(self)
        threading.Thread(target=self._scan_library, daemon=True).start()
        threading.Thread(target=prune_preview_cache, daemon=True).start()
        GLib.timeout_add(250, self._tick)

        if not shutil.which("ffmpeg"):
            self.toast("⚠ No se encuentra ffmpeg: instala con  sudo apt install ffmpeg")
        elif not find_js_runtime():
            self.toast("⚠ Falta Deno o Node: sin él YouTube no da audio.", 9)
        elif not self.player.available:
            self.toast("⚠ Reproductor no disponible: falta GStreamer (gir1.2-gstreamer-1.0).", 9)
        threading.Thread(target=self._init_api, daemon=True).start()
        threading.Thread(target=self._check_ytdlp, daemon=True).start()

    def _check_ytdlp(self):
        """Avisa si hay una versión nueva de yt-dlp. Sin conexión (o ante cualquier fallo) no dice nada."""
        try:
            cur, new = installed_ytdlp_version(), latest_ytdlp_version()
            if cur and new and _ver_tuple(cur) and _ver_tuple(new) and _ver_tuple(new) > _ver_tuple(cur):
                GLib.idle_add(self._ytdlp_available, new)
        except Exception as e:  # noqa: BLE001
            log(f"Comprobación de yt-dlp: {e}")

    def _ytdlp_available(self, new):
        self._ytdlp_latest = new
        self.toast(f"Hay una versión nueva de yt-dlp ({new}). Actualízala en Configuración → Mantenimiento.", 10)
        return False

    def _on_close(self, *_):
        try:
            self.player.stop_all()  # suelta los archivos de vista previa antes de borrarlos
        except Exception:
            pass
        clear_cache()
        return False

    # ── API ──
    def _init_api(self):
        try:
            self.api()
        except Exception as e:  # noqa: BLE001
            log(f"YTMusic init: {e}")

    def api(self):
        with self._api_lock:
            if self.yt is None:
                from ytmusicapi import YTMusic
                self.yt = YTMusic()
            return self.yt

    # ── cabecera ──
    def _build_header(self):
        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        self.set_titlebar(hb)

        self.back_btn = Gtk.Button.new_from_icon_name("go-previous-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
        self.back_btn.set_tooltip_text("Atrás")
        self.back_btn.connect("clicked", lambda *_: self.go_back())
        hb.pack_start(self.back_btn)

        self.main_stack = Gtk.Stack()
        self.main_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.main_stack.set_transition_duration(200)
        self.switcher = Gtk.StackSwitcher()
        self.switcher.set_stack(self.main_stack)
        ctx = self.switcher.get_style_context()
        ctx.remove_class("linked")
        ctx.add_class("tabs")
        hb.set_custom_title(self.switcher)

        self.settings_btn = Gtk.Button.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
        self.settings_btn.set_tooltip_text("Configuración")
        self.settings_btn.connect("clicked", lambda *_: self.open_preferences())
        hb.pack_end(self.settings_btn)

        self.dl_btn = Gtk.ToggleButton()
        self.dl_btn.connect("toggled", lambda b: self.dl_rev.set_reveal_child(b.get_active()))
        hb.pack_end(self.dl_btn)

    # ── cuerpo ──
    def _build_body(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.shell = Gtk.Overlay()   # capa superior para el modo disco de vinilo
        self.add(self.shell)
        self.shell.add(root)
        overlay = Gtk.Overlay()
        root.pack_start(overlay, True, True, 0)
        # fondo común: disco de vinilo translúcido a todo el ancho de la ventana (centrado igual
        # que el reproductor); las páginas y el panel de descargas se dibujan encima
        self.backdrop = VinylBackdrop()
        overlay.add(self.backdrop)
        outer = Gtk.Box()
        overlay.add_overlay(outer)
        outer.pack_start(self.main_stack, True, True, 0)

        # panel de descargas
        self.dl_rev = Gtk.Revealer()
        self.dl_rev.set_transition_type(Gtk.RevealerTransitionType.SLIDE_LEFT)
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        panel.get_style_context().add_class("dl-panel")
        panel.set_size_request(360, -1)
        panel.set_border_width(14)
        panel.pack_start(label("Descargas", "page-title"), False, False, 0)
        rrow = Gtk.Box(spacing=10)
        rrow.pack_start(label("Reintentos si una pista falla", "card-title"), True, True, 0)
        self.retry_spin = Gtk.SpinButton.new_with_range(0, 10, 1)
        self.retry_spin.set_value(int(self.cfg.get("retries", 3)))
        self.retry_spin.set_tooltip_text("0 = no reintentar. Se repiten solo las pistas que fallaron.")
        self.retry_spin.connect("value-changed", self._on_retry_spin)
        rrow.pack_start(self.retry_spin, False, False, 0)
        panel.pack_start(rrow, False, False, 0)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.dl_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sw.add(self.dl_list)
        panel.pack_start(sw, True, True, 0)
        self.dl_empty = label("Aún no hay descargas.", "muted")
        self.dl_list.pack_start(self.dl_empty, False, False, 8)
        clear = Gtk.Button(label="Limpiar terminadas")
        clear.get_style_context().add_class("ghost")
        clear.connect("clicked", lambda *_: self._clear_finished())
        panel.pack_start(clear, False, False, 0)
        self.dl_rev.add(panel)
        outer.pack_start(self.dl_rev, False, False, 0)

        # toast
        self.toast_rev = Gtk.Revealer()
        self.toast_rev.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.toast_rev.set_halign(Gtk.Align.CENTER)
        self.toast_rev.set_valign(Gtk.Align.END)
        self.toast_lbl = Gtk.Label()
        self.toast_lbl.get_style_context().add_class("toast")
        self.toast_lbl.set_margin_bottom(24)
        self.toast_rev.add(self.toast_lbl)
        overlay.add_overlay(self.toast_rev)
        overlay.set_overlay_pass_through(self.toast_rev, True)

        # barra del reproductor
        self._build_player_bar()
        root.pack_start(self.bar_rev, False, False, 0)
        self._build_vinyl_layer()

        # las dos pestañas
        self.main_stack.add_titled(self._build_collection(), "coleccion", "Mis Álbumes")
        self.main_stack.add_titled(self._build_download(), "descargar", "Explorar Álbumes")
        self.main_stack.connect("notify::visible-child-name", lambda *_: self._update_back())

    def _scrolled_page(self, stack, name):
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        stack.add_named(sw, name)
        return sw

    def _set_page_content(self, sw, widget):
        for c in sw.get_children():
            sw.remove(c)
        sw.add(widget)
        widget.show_all()

    # ── pestaña INICIO: colección ──
    def _build_collection(self):
        self.cstack = Gtk.Stack()
        self.cstack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.cstack.set_transition_duration(160)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(36)

        head = Gtk.Box(spacing=14)
        head.pack_start(label("Álbumes de mi colección", "collection-title"), False, False, 0)
        self.lib_count = label("", "badge")
        self.lib_count.set_valign(Gtk.Align.CENTER)
        self.lib_count.set_no_show_all(True)
        head.pack_start(self.lib_count, False, False, 0)
        self.reload_btn = icon_button("view-refresh-symbolic", "Recargar", "ghost")
        self.reload_btn.set_tooltip_text("Volver a leer la carpeta de descargas y actualizar la lista de álbumes")
        self.reload_btn.set_valign(Gtk.Align.CENTER)
        self.reload_btn.connect("clicked", lambda *_: self.reload_library())
        head.pack_end(self.reload_btn, False, False, 0)
        box.pack_start(head, False, False, 0)

        srow = Gtk.Box(spacing=12)
        srow.set_margin_top(14)
        self.lib_entry = Gtk.SearchEntry()
        self.lib_entry.set_placeholder_text("Buscar en mi colección…")
        self.lib_entry.set_width_chars(36)
        self.lib_entry.connect("search-changed", lambda *_: self._apply_lib_view())
        srow.pack_start(self.lib_entry, False, False, 0)
        self.lib_seg = Segmented([("all", "Todo"), ("artist", "Artista"), ("album", "Álbum")],
                                 lambda _v: self._on_lib_mode())
        self.lib_seg.set_valign(Gtk.Align.CENTER)
        srow.pack_start(self.lib_seg, False, False, 0)
        self.lib_search_row = srow

        # ordenar los álbumes por nombre, fecha en que se añadieron o año de lanzamiento
        self.sort_box = Gtk.Box(spacing=8)
        self.sort_box.set_valign(Gtk.Align.CENTER)
        self.sort_box.set_margin_start(10)
        self.sort_box.pack_start(label("Ordenar por", "card-sub"), False, False, 0)
        self.sort_seg = Segmented([("name", "Nombre"), ("date", "Fecha añadido"), ("year", "Año")],
                                  self._on_sort_key)
        self.sort_seg.select(self.cfg.get("lib_sort", "date"), notify=False)
        self.sort_seg.btns["date"].set_tooltip_text("Fecha en la que el álbum se añadió a tu colección")
        self.sort_seg.btns["year"].set_tooltip_text("Año de lanzamiento del álbum")
        self.sort_box.pack_start(self.sort_seg, False, False, 0)
        self.sort_dir = Gtk.Button()
        self.sort_dir.get_style_context().add_class("round")
        self.sort_dir.connect("clicked", lambda *_: self._on_sort_dir())
        self.sort_box.pack_start(self.sort_dir, False, False, 0)
        srow.pack_start(self.sort_box, False, False, 0)
        self._sort_desc = bool(self.cfg.get("lib_desc", True))
        self._update_sort_dir()
        box.pack_start(srow, False, False, 0)

        # estado vacío (se coloca más abajo, centrado sobre toda la pestaña, igual que en Explorar)
        go = Gtk.Button(label="Explorar álbumes")
        go.get_style_context().add_class("primary")
        go.connect("clicked", lambda *_: self.main_stack.set_visible_child_name("descargar"))
        self.lib_empty_box = empty_state("Tu colección está vacía",
                                         "Los álbumes que descargues aparecerán aquí para que puedas reproducirlos.", go)

        # chip del artista seleccionado (se muestra al entrar en un artista)
        self.lib_chip_row = Gtk.Box(spacing=8)
        self.lib_chip_row.set_margin_top(14)
        self.lib_chip = Gtk.Button()
        self.lib_chip.set_relief(Gtk.ReliefStyle.NONE)
        self.lib_chip.get_style_context().add_class("chip")
        self.lib_chip.set_tooltip_text("Quitar el filtro de artista")
        self.lib_chip_lbl = Gtk.Label()
        self.lib_chip.add(self.lib_chip_lbl)
        self.lib_chip.connect("clicked", lambda *_: self._lib_clear_artist())
        self.lib_chip_row.pack_start(self.lib_chip, False, False, 0)
        box.pack_start(self.lib_chip_row, False, False, 0)

        # sección de álbumes
        self.alb_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.alb_title = label("Álbumes", "section-title")
        self.alb_title.set_margin_top(14)
        self.alb_section.pack_start(self.alb_title, False, False, 0)
        self.lib_grid = make_grid()
        self.lib_grid.set_filter_func(self._lib_filter)
        self.lib_grid.set_sort_func(self._lib_sort)
        self.lib_grid.set_margin_top(10)
        self.alb_section.pack_start(self.lib_grid, False, False, 0)
        box.pack_start(self.alb_section, False, False, 0)

        # sección de artistas
        self.art_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.art_title = label("Artistas", "section-title")
        self.art_title.set_margin_top(22)
        self.art_section.pack_start(self.art_title, False, False, 0)
        self.art_grid = make_grid()
        self.art_grid.set_filter_func(self._art_filter)
        self.art_grid.set_margin_top(10)
        self.art_section.pack_start(self.art_grid, False, False, 0)
        box.pack_start(self.art_section, False, False, 0)

        self.lib_noresults = label("Ningún resultado coincide con la búsqueda.", "muted")
        self.lib_noresults.set_margin_top(14)
        box.pack_start(self.lib_noresults, False, False, 0)
        sw.add(box)
        libpage = Gtk.Overlay()
        libpage.add(sw)
        libpage.add_overlay(self.lib_empty_box)
        self.cstack.add_named(libpage, "list")
        self.page_cdetail = self._scrolled_page(self.cstack, "detail")
        return self.cstack

    # ── pestaña DESCARGAR ÁLBUMES ──
    def _build_download(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_spacing(0)

        # Cabecera con la misma estructura que «Mis Álbumes»: título grande y fila de búsqueda debajo.
        bar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        for side in ("start", "end", "top"):
            getattr(bar, f"set_margin_{side}")(36)
        bar.pack_start(label("Explorar Álbumes", "collection-title"), False, False, 0)

        inner = Gtk.Box(spacing=12)
        inner.set_halign(Gtk.Align.START)
        inner.set_margin_top(14)

        artist_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        artist_col.set_hexpand(True)
        artist_col.set_size_request(270, -1)
        artist_col.pack_start(label("ARTISTA", "search-field-label"), False, False, 0)
        self.e_artist = Gtk.Entry(placeholder_text="Busca un artista…")
        self.e_artist.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, "avatar-default-symbolic")
        self.e_artist.set_icon_tooltip_text(Gtk.EntryIconPosition.PRIMARY, "Buscar artista")
        self.e_artist.set_width_chars(30)
        artist_col.pack_start(self.e_artist, False, False, 0)

        album_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        album_col.set_hexpand(True)
        album_col.set_size_request(270, -1)
        album_col.pack_start(label("ÁLBUM", "search-field-label"), False, False, 0)
        self.e_album = Gtk.Entry(placeholder_text="Busca un álbum…")
        self.e_album.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, "media-optical-symbolic")
        self.e_album.set_icon_tooltip_text(Gtk.EntryIconPosition.PRIMARY, "Buscar álbum")
        self.e_album.set_width_chars(30)
        self.e_album.set_tooltip_text("Puedes dejarlo vacío para buscar todos los álbumes de un artista")
        album_col.pack_start(self.e_album, False, False, 0)

        go = icon_button("edit-find-symbolic", "Buscar", "primary")
        go.set_tooltip_text("Buscar ahora (Enter)")
        go.set_valign(Gtk.Align.END)
        go.set_size_request(112, 44)

        self.e_artist.connect("activate", lambda *_: self.do_search())
        self.e_album.connect("activate", lambda *_: self.do_search())
        go.connect("clicked", lambda *_: self.do_search())

        inner.pack_start(artist_col, True, True, 0)
        inner.pack_start(album_col, True, True, 0)
        inner.pack_start(go, False, False, 0)
        bar.pack_start(inner, False, False, 0)

        help_row = Gtk.Box()
        help_row.set_halign(Gtk.Align.START)
        help_row.set_margin_top(6)
        help_row.pack_start(label("Puedes dejar el álbum vacío para ver todos los álbumes del artista", "search-help"), False, False, 0)
        bar.pack_start(help_row, False, False, 0)

        box.pack_start(bar, False, False, 0)

        self.dstack = Gtk.Stack()
        self.dstack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.dstack.set_transition_duration(160)
        box.pack_start(self.dstack, True, True, 0)

        land = empty_state("Explora álbumes completos",
                           "Busca por artista y álbum, previsualiza las canciones y descarga tu selección.")
        self.dstack.add_named(Gtk.Box(), "search")   # página vacía: el texto va superpuesto, centrado en la pestaña
        self.dstack.connect("notify::visible-child-name",
                            lambda st, _p: land.set_visible(st.get_visible_child_name() == "search"))

        ld = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        ld.set_valign(Gtk.Align.CENTER)
        ld.set_halign(Gtk.Align.CENTER)
        sp = Gtk.Spinner()
        sp.set_size_request(48, 48)
        sp.start()
        ld.pack_start(sp, False, False, 0)
        ld.pack_start(label("Buscando…", "muted", xalign=0.5), False, False, 0)
        self.dstack.add_named(ld, "loading")
        self.page_results = self._scrolled_page(self.dstack, "results")
        self.page_artist = self._scrolled_page(self.dstack, "artist")
        self.page_album = self._scrolled_page(self.dstack, "album")
        tab = Gtk.Overlay()
        tab.add(box)
        tab.add_overlay(land)
        tab.set_overlay_pass_through(land, True)
        return tab

    # ── navegación ──
    def dshow(self, name, push=True):
        if push and name != self.dcur:
            self.dhist.append(self.dcur)
        self.dcur = name
        self.dstack.set_visible_child_name(name)
        self._update_back()

    def cshow(self, name, push=True):
        if push and name != self.ccur:
            self.chist.append(self.ccur)
        self.ccur = name
        self.cstack.set_visible_child_name(name)
        self._update_back()

    def _update_back(self):
        sec = self.main_stack.get_visible_child_name()
        hist = self.chist if sec == "coleccion" else self.dhist
        self.back_btn.set_visible(bool(hist))

    def go_back(self):
        if self.main_stack.get_visible_child_name() == "coleccion":
            if self.chist:
                self.ccur = self.chist.pop()
                self.cstack.set_visible_child_name(self.ccur)
        elif self.dhist:
            self.dcur = self.dhist.pop()
            self.dstack.set_visible_child_name(self.dcur)
        self._update_back()

    def dloading(self):
        self.dstack.set_visible_child_name("loading")

    def fail(self, err):
        self.dstack.set_visible_child_name(self.dcur)
        msg = friendly_error(err)
        self.toast(msg if msg == OFFLINE_MSG else f"Error: {msg[:120]}")

    def toast(self, msg, seconds=4):
        self.toast_lbl.set_text(msg)
        self.toast_lbl.show()
        self.toast_rev.show()
        self.toast_rev.set_reveal_child(True)
        if self._toast_id:
            GLib.source_remove(self._toast_id)
        self._toast_id = GLib.timeout_add_seconds(seconds, self._hide_toast)

    def _hide_toast(self):
        self.toast_rev.set_reveal_child(False)
        self._toast_id = None
        return False

    # ── colección: datos ──
    def _scan_library(self):
        found = scan_folder(self.cfg["folder"])
        if found:
            GLib.idle_add(self._merge_found, found)

    def _merge_found(self, found):
        changed = False
        for folder, entry in found.items():
            if folder not in self.library:
                self.library[folder] = entry
                changed = True
        if changed:
            save_library(self.library)
            self.refresh_library()
        return False

    def _in_folder(self, path):
        """True si path está dentro de la carpeta de descargas configurada."""
        try:
            root = Path(os.path.expanduser(self.cfg["folder"])).resolve()
            return Path(path).resolve().is_relative_to(root)
        except Exception:
            return False

    def reload_library(self):
        """Vuelve a leer la carpeta de descargas configurada: la colección pasa a mostrar solo lo que hay en ella."""
        self.reload_btn.set_sensitive(False)
        folder = self.cfg["folder"]

        def done(found, err):
            self.reload_btn.set_sensitive(True)
            if err:
                self.toast("No se pudo leer la carpeta de álbumes")
                return
            added = 0
            for f, entry in (found or {}).items():
                if f in self.library:
                    self.library[f]["tracks"] = entry["tracks"]
                else:
                    self.library[f] = entry
                    added += 1
            # fuera los álbumes que ya no existen o que no están en la carpeta configurada
            gone = [k for k in self.library if not Path(k).is_dir() or not self._in_folder(k)]
            for k in gone:
                del self.library[k]
            save_library(self.library)
            self.refresh_library()
            if added or gone:
                bits = ([f"{added} nuevo{'s' if added != 1 else ''}"] if added else []) + \
                       ([f"{len(gone)} eliminado{'s' if len(gone) != 1 else ''}"] if gone else [])
                self.toast("Colección actualizada · " + ", ".join(bits))
            else:
                self.toast("Colección actualizada · sin cambios")

        run_async(lambda: scan_folder(folder), done)

    def _job_finished(self, job):
        entry = dict(job.meta)
        entry["date"] = time.time()
        self.library[entry["folder"]] = entry
        save_library(self.library)
        self.refresh_library()

    def refresh_library(self):
        for grid in (self.lib_grid, self.art_grid):
            for c in grid.get_children():
                grid.remove(c)
        items = [e for e in self.library.values()
                 if Path(e.get("folder", "")).is_dir() and self._in_folder(e.get("folder", ""))]
        # álbumes online con «me gusta» (si ya está descargado, solo se muestra la copia local)
        local_ids = {e.get("browse_id") for e in items if e.get("browse_id")}
        items = items + [f for f in self.favs.values() if f.get("browse_id") not in local_ids]
        self._entry_map = {e["folder"]: e for e in items}
        self._lib_items = items

        # artistas agrupados: la portada del artista es la de su álbum más reciente
        groups = {}
        for e in items:
            key = self._artist_key(e)
            cov = e.get("cover") if Path(e.get("cover") or "").exists() else None
            g = groups.get(key)
            if g is None:
                name = (e.get("artist") or "").strip() or "Desconocido"
                g = groups[key] = {"key": key, "name": name, "cover": cov, "n": 0}
            elif g["cover"] is None and cov:
                g["cover"] = cov
            g["n"] += 1
        self._lib_artists = sorted(groups.values(), key=lambda g: g["name"].casefold())

        for e in items:
            self.lib_grid.add(self._library_card(e))
        for g in self._lib_artists:
            self.art_grid.add(self._artist_group_card(g))

        if self._lib_artist and self._lib_artist not in groups:
            self._lib_artist = None
            self.lib_seg.select(self._lib_prev_mode, notify=False)
        self.lib_empty_box.set_visible(not items)
        self.lib_search_row.set_visible(bool(items))
        self.lib_grid.show_all()
        self.art_grid.show_all()
        self._apply_lib_view()

    def _artist_group_card(self, g):
        n = g["n"]
        card = make_card(g["cover"], g["name"], f"{n} álbum{'es' if n != 1 else ''}", circle=True,
                         on_click=lambda: self._lib_set_artist(g["name"]))
        card.artist_name = g["name"]
        return card

    def _library_card(self, e):
        sub = " · ".join(x for x in (e.get("artist"), e.get("year")) if x)
        online = bool(e.get("online"))
        if online:
            cover = e.get("cover") or None   # ruta guardada o, de momento, la URL
        else:
            cover = e.get("cover") if Path(e.get("cover", "")).exists() else None
        card = make_library_card(
            e["folder"], cover, e.get("title", "Álbum"), sub, CARD_SIZE,
            on_open=(lambda: self.open_online_album(e)) if online else (lambda: self.open_local_album(e)),
            on_play=(lambda: self.play_online_album(e)) if online else (lambda: self.play_local_album(e)),
            on_menu=lambda w, ev: self._library_menu(e, w, ev),
            online=online,
        )
        if online:
            card.set_tooltip_text(f"{e.get('title', '')}\nÁlbum online · en «Me gusta»")
            return card
        n = e.get("tracks")
        card.set_tooltip_text(f"{e.get('title', '')}\n{n} canciones · {str(e.get('format', '')).upper()}" if n else e.get("title", ""))
        return card

    @staticmethod
    def _artist_key(e):
        return ((e.get("artist") or "").strip() or "Desconocido").casefold()

    def _match_album(self, e):
        if self._lib_artist and self._artist_key(e) != self._lib_artist:
            return False
        q = self.lib_entry.get_text().strip().casefold()
        if not q:
            return True
        a, t = (e.get("artist") or "").casefold(), (e.get("title") or "").casefold()
        mode = self.lib_seg.value
        if self._lib_artist or mode == "album":
            return q in t
        if mode == "artist":
            return q in a
        return q in a or q in t

    def _match_artist(self, name):
        q = self.lib_entry.get_text().strip().casefold()
        return not q or q in name.casefold()

    def _sort_key(self, e):
        """Clave de orden de un álbum según el criterio elegido."""
        if not e:
            return ((),)
        title = (e.get("title") or "").casefold()
        artist = (e.get("artist") or "").casefold()
        kind = self.sort_seg.value
        if kind == "name":
            return (title, artist)
        if kind == "year":
            y = str(e.get("year") or "")
            return (int(y) if y.isdigit() else 0, title, artist)
        return (float(e.get("date") or 0), title, artist)

    def _lib_sort(self, a, b):
        ka = self._sort_key(self._entry_map.get(a.get_child().get_name()))
        kb = self._sort_key(self._entry_map.get(b.get_child().get_name()))
        r = (ka > kb) - (ka < kb)
        return -r if self._sort_desc else r

    def _update_sort_dir(self):
        desc = self._sort_desc
        set_icon(self.sort_dir, "view-sort-descending-symbolic" if desc else "view-sort-ascending-symbolic")
        kind = self.sort_seg.value
        if kind == "name":
            tip = "Z → A (pulsa para invertir)" if desc else "A → Z (pulsa para invertir)"
        else:
            tip = "Más nuevos primero (pulsa para invertir)" if desc else "Más antiguos primero (pulsa para invertir)"
        self.sort_dir.set_tooltip_text(tip)
        self.sort_dir.show_all()

    def _on_sort_key(self, kind):
        # cada criterio arranca con su orden natural: nombre A→Z, fechas las más nuevas primero
        self._sort_desc = kind != "name"
        self._save_sort()

    def _on_sort_dir(self):
        self._sort_desc = not self._sort_desc
        self._save_sort()

    def _save_sort(self):
        self.cfg["lib_sort"], self.cfg["lib_desc"] = self.sort_seg.value, self._sort_desc
        save_config(self.cfg)
        self._update_sort_dir()
        self.lib_grid.invalidate_sort()

    def _lib_filter(self, child):
        e = self._entry_map.get(child.get_child().get_name())
        return self._match_album(e) if e else True

    def _art_filter(self, child):
        return self._match_artist(getattr(child.get_child(), "artist_name", ""))

    def _on_lib_mode(self):
        # al elegir Todo / Artista / Álbum se sale de la vista de un artista concreto
        if self._lib_artist:
            self._lib_artist = None
            self._rebuild_artists()
            self.lib_grid.show_all()
        self._apply_lib_view()

    def _lib_set_artist(self, name):
        """Muestra los álbumes de un artista de la colección."""
        if not self._lib_artist:
            self._lib_prev_mode = self.lib_seg.value
        self._lib_artist = ((name or "").strip() or "Desconocido").casefold()
        self._lib_artist_name = (name or "").strip() or "Desconocido"
        self.lib_seg.select("album", notify=False)
        self.lib_entry.set_text("")
        self._apply_lib_view()

    def _rebuild_artists(self):
        """Vuelve a crear las tarjetas de todos los artistas de la colección."""
        for ch in self.art_grid.get_children():
            self.art_grid.remove(ch)
        for g in self._lib_artists:
            self.art_grid.add(self._artist_group_card(g))
        self.art_grid.show_all()

    def _lib_clear_artist(self):
        self._lib_artist = None
        self.lib_seg.select(self._lib_prev_mode, notify=False)
        self.lib_entry.set_text("")
        self._rebuild_artists()
        self.lib_grid.show_all()
        self._apply_lib_view()

    def _apply_lib_view(self):
        """Muestra lo que corresponde al modo (Todo / Artista / Álbum) y al texto buscado."""
        total = len(self._lib_items)
        mode = self.lib_seg.value
        focus = bool(self._lib_artist)
        self.lib_grid.invalidate_filter()
        self.art_grid.invalidate_filter()
        n_alb = sum(1 for e in self._lib_items if self._match_album(e))
        n_art = sum(1 for g in self._lib_artists if self._match_artist(g["name"]))
        want_alb = bool(total) and (focus or mode in ("all", "album"))
        want_art = bool(total) and not focus and mode in ("all", "artist")
        show_alb, show_art = want_alb and n_alb > 0, want_art and n_art > 0
        self.alb_section.set_visible(show_alb)
        self.sort_box.set_visible(want_alb)
        self.art_section.set_visible(show_art)
        both = mode == "all" and not focus
        self.alb_title.set_visible(both)
        self.art_title.set_visible(both)
        self.lib_chip_row.set_visible(focus)
        if focus:
            self.lib_chip_lbl.set_text(f"Artista: {self._lib_artist_name}   ✕")
        self.lib_entry.set_placeholder_text(
            "Buscar álbum de este artista…" if focus else
            {"artist": "Buscar artista…", "album": "Buscar álbum…"}.get(mode, "Buscar en mi colección…"))

        if not total:
            self.lib_count.hide()
        else:
            if want_art and not want_alb:
                n, tot, one, many = n_art, len(self._lib_artists), "artista", "artistas"
            else:
                n, tot, one, many = n_alb, total, "álbum", "álbumes"
            txt = f"{tot} {one if tot == 1 else many}"
            if n != tot:
                txt = f"{n} de {txt}"
            self.lib_count.set_text(txt)
            self.lib_count.show()
        self.lib_noresults.set_visible(bool(total) and not (show_alb or show_art))

    def _library_menu(self, e, widget, event=None):
        menu = Gtk.Menu()
        if e.get("online"):
            items = [("Escuchar vista previa", lambda *_: self.play_online_album(e)),
                     ("Ver álbum online", lambda *_: self.show_in_download(e["browse_id"])),
                     ("Descargar álbum", lambda *_: self._download_fav(e)),
                     ("Eliminar álbum", lambda *_: self._delete_album(e))]
        else:
            items = [("Reproducir álbum", lambda *_: self.play_local_album(e)),
                     ("Abrir carpeta", lambda *_: self._open_path(e["folder"]))]
            if e.get("browse_id"):
                items.append(("Ver álbum en Explorar", lambda *_: self.show_in_download(e["browse_id"])))
            items.append(("Eliminar álbum", lambda *_: self._delete_album(e)))
        for text, cb in items:
            mi = Gtk.MenuItem(label=text)
            mi.connect("activate", cb)
            menu.append(mi)
        menu.show_all()
        menu.attach_to_widget(widget, None)
        menu.connect("deactivate", lambda m: GLib.idle_add(m.detach))
        if event is not None:
            menu.popup_at_pointer(event)
        else:
            menu.popup_at_widget(widget, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)

    def _delete_album(self, e):
        """Elimina un álbum tras pedir confirmación: si es online solo se quita de Mis Álbumes;
        si está descargado se borra su carpeta del equipo."""
        online = bool(e.get("online"))
        title = e.get("title") or "Álbum"
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.WARNING,
                                text=f"¿Eliminar «{title}»?")
        dlg.format_secondary_text(
            "El álbum se quitará de Mis Álbumes. No se borra nada de tu equipo." if online else
            "Se borrará del equipo la carpeta del álbum con todas sus canciones. "
            "Esta acción no se puede deshacer.")
        dlg.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        ok = dlg.add_button("Eliminar", Gtk.ResponseType.OK)
        ok.get_style_context().add_class("primary")
        dlg.set_default_response(Gtk.ResponseType.CANCEL)
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        if online:
            if e["browse_id"] in self.favs:
                self._toggle_like(e)
        else:
            try:
                f = Path(e["folder"]).resolve()
                root = Path(os.path.expanduser(self.cfg["folder"])).resolve()
                # seguridad: solo se borra una carpeta de álbum dentro de la carpeta de música configurada
                if not f.is_dir() or f == root or root not in f.parents:
                    raise RuntimeError("la carpeta no está dentro de la carpeta de música")
                cur = self.player.item
                if cur and cur.get("folder") and Path(cur["folder"]).resolve() == f:
                    self.player.stop_all()   # suelta los archivos que se están reproduciendo
                shutil.rmtree(f)
                parent = f.parent
                if parent != root and not any(parent.iterdir()):
                    parent.rmdir()           # carpeta del artista vacía
            except Exception as ex:  # noqa: BLE001
                log(f"Eliminar álbum {e.get('folder')}: {ex}")
                self.toast(f"No se pudo eliminar el álbum: {str(ex)[:90]}")
                return
            self.library.pop(e["folder"], None)
            save_library(self.library)
            self.toast("Álbum eliminado")
        if self.ccur == "detail":
            self.chist, self.ccur = [], "list"
            self.cstack.set_visible_child_name("list")
        self.refresh_library()
        self._update_back()

    def _open_path(self, path):
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)

    def show_artist_in_collection(self, name):
        self.main_stack.set_visible_child_name("coleccion")
        self.chist, self.ccur = [], "list"
        self.cstack.set_visible_child_name("list")
        self._lib_set_artist(name)
        self._update_back()

    def show_in_download(self, browse_id):
        self.main_stack.set_visible_child_name("descargar")
        self.open_album(browse_id)

    # ── colección: álbum local con reproductor ──
    def _local_items(self, e, with_duration=False):
        folder = Path(e["folder"])
        try:
            files = sorted(f for f in folder.iterdir() if f.suffix.lower() in AUDIO_EXT)
        except Exception:
            files = []
        cover = e.get("cover") if Path(e.get("cover", "")).exists() else None
        items = []
        for n, f in enumerate(files, 1):
            m = re.match(r"^(\d+)\s*-\s*(.*)$", f.stem)
            dur = ""
            if with_duration:
                try:
                    import mutagen

                    mf = mutagen.File(str(f))
                    dur = fmt_time(mf.info.length) if mf and mf.info else ""
                except Exception:
                    dur = ""
            items.append({
                "key": str(f), "path": str(f), "title": m.group(2) if m else f.stem,
                "number": int(m.group(1)) if m else n, "artist": e.get("artist", ""),
                "album": e.get("title", ""), "cover": cover, "duration": dur,
                "folder": e["folder"],
            })
        return items

    def play_local_album(self, e, start=0, items=None):
        if not self.player.available:
            self.toast("Falta GStreamer para reproducir (gir1.2-gstreamer-1.0).", 7)
            return
        items = items or self._local_items(e)
        if not items:
            self.toast("No se encontraron canciones en la carpeta del álbum")
            return
        self.player.play_queue(items, start)

    def open_online_album(self, e):
        """Álbum online de «Me gusta» dentro de Mis Álbumes: Reproducir y Me gusta; el resto, en el menú."""
        bid = e["browse_id"]

        def done(a, err):
            if err:
                msg = friendly_error(err)
                self.toast(msg if msg == OFFLINE_MSG else f"Error: {msg[:120]}")
                return
            self._show_online_detail(e, a)

        run_async(lambda: self.api().get_album(bid), done)

    def _show_online_detail(self, e, a):
        bid = e["browse_id"]
        items = self._online_items(bid, a)
        index_of = {it["video_id"]: i for i, it in enumerate(items)}
        self.rows_local = {}
        cu = best_thumb(a.get("thumbnails"), 600)
        if cu and not e.get("cover_url"):
            e["cover_url"] = cu
        c = str(e.get("cover") or "")
        cover = c if (c.startswith("http") or Path(c).is_file()) else cu
        artists = a.get("artists") or []
        artist_txt = fmt_artists(artists) or e.get("artist", "")
        aid = (artists[0] or {}).get("id") if artists else None
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.get_style_context().add_class("album-root")
        root.set_border_width(28)
        if aid:
            who = self._artist_link(artist_txt, lambda: (self.main_stack.set_visible_child_name("descargar"),
                                                         self.open_artist(aid)))
        else:
            who = label(artist_txt, "link")
        tracks = [t for t in a.get("tracks", []) if t.get("videoId")]
        meta = " · ".join(str(b) for b in (a.get("year") or e.get("year"),
                                           f"{a.get('trackCount') or len(tracks)} canciones", a.get("duration")) if b)
        btns = Gtk.Box(spacing=10)
        btns.set_margin_top(10)
        play = icon_button("media-playback-start-symbolic", "Reproducir", "primary")
        play.set_sensitive(bool(items))
        play.connect("clicked", lambda *_: self._play_preview(items, 0))
        btns.pack_start(play, False, False, 0)
        like = icon_button("starred-symbolic", "En Me gusta", "ghost")
        like.connect("clicked", lambda *_: self._toggle_like(e))
        btns.pack_start(like, False, False, 0)
        self._detail_like = (like, bid)
        self._style_like(like, self._is_liked(bid))
        btns.pack_start(menu_button([
            ("Descargar álbum", lambda: self._download_fav(e)),
            ("Ver en Explorar", lambda: self.show_in_download(bid)),
            ("Eliminar álbum", lambda: self._delete_album(e)),
        ]), False, False, 0)
        self._album_header(root, cover, "ÁLBUM ONLINE",
                           a.get("title") or e.get("title", "Álbum"), who, meta, btns)

        lb = Gtk.ListBox()
        lb.set_selection_mode(Gtk.SelectionMode.NONE)
        lb.set_margin_top(28)
        for i, t in enumerate(tracks):
            ok = t.get("isAvailable", True)
            vid = t["videoId"]
            feat = fmt_artists(t.get("artists"))
            lb.add(self._track_row(
                self.rows_local, "yt:" + vid, t.get("trackNumber") or i + 1, t.get("title", ""),
                t.get("duration") or "", feat if feat and feat != artist_txt else "",
                lambda vid=vid: self._play_preview(items, index_of.get(vid, 0)), enabled=ok))
        root.pack_start(lb, False, False, 0)
        self._set_page_content(self.page_cdetail, root)
        self._refresh_rows()
        self.chist = [h for h in self.chist if h != "detail"]
        self.cshow("detail")

    def open_local_album(self, e):
        items = self._local_items(e, with_duration=True)
        self.rows_local = {}
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.get_style_context().add_class("album-root")
        root.set_border_width(28)
        who = self._artist_link(e.get("artist", ""), lambda: self.show_artist_in_collection(e.get("artist", "")))
        meta = " · ".join(str(b) for b in (e.get("year"), f"{len(items)} canciones",
                                           str(e.get("format", "")).upper()) if b)
        btns = Gtk.Box(spacing=10)
        btns.set_margin_top(10)
        play = icon_button("media-playback-start-symbolic", "Reproducir", "primary")
        play.connect("clicked", lambda *_: self.play_local_album(e, 0, items))
        btns.pack_start(play, False, False, 0)
        more = [("Abrir carpeta", lambda: self._open_path(e["folder"]))]
        if e.get("browse_id"):
            more.append(("Ver en Explorar", lambda: self.show_in_download(e["browse_id"])))
        more.append(("Eliminar álbum", lambda: self._delete_album(e)))
        btns.pack_start(menu_button(more), False, False, 0)
        cover = e.get("cover") if Path(e.get("cover", "")).exists() else None
        self._album_header(root, cover, "ÁLBUM EN MI COLECCIÓN", e.get("title", "Álbum"), who, meta, btns)

        lb = Gtk.ListBox()
        lb.set_selection_mode(Gtk.SelectionMode.NONE)
        lb.set_margin_top(28)
        for i, it in enumerate(items):
            lb.add(self._track_row(self.rows_local, it["key"], it["number"], it["title"], it["duration"], "",
                                   lambda i=i: self.play_local_album(e, i, items)))
        root.pack_start(lb, False, False, 0)
        self._set_page_content(self.page_cdetail, root)
        self._refresh_rows()
        self.chist = [h for h in self.chist if h != "detail"]
        self.cshow("detail")

    # ── piezas compartidas de las páginas de álbum ──
    def _album_header(self, root, cover_url, kind, title, who_widget, meta, btns):
        # Grid explícito: en GTK3 un Box horizontal puede asignar a sus hijos
        # toda la altura del contenedor y dejar franjas de fondo del tema.
        # El grid fija la geometría del hero y evita esos artefactos.
        head = Gtk.Grid()
        head.get_style_context().add_class("album-hero")
        head.set_column_spacing(34)
        head.set_row_spacing(0)
        head.set_hexpand(True)
        head.set_halign(Gtk.Align.FILL)

        cover = Cover(260)
        cover.set_valign(Gtk.Align.START)
        head.attach(cover, 0, 0, 1, 1)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        info.set_hexpand(True)
        info.set_halign(Gtk.Align.FILL)
        info.set_valign(Gtk.Align.END)
        kind_lbl = label(kind.upper(), "kind")
        kind_lbl.set_halign(Gtk.Align.START)
        title_lbl = label(title, "big-title", wrap=True)
        title_lbl.set_halign(Gtk.Align.START)
        title_lbl.set_line_wrap(True)
        who_widget.set_halign(Gtk.Align.START)
        meta_lbl = label(meta, "muted")
        meta_lbl.set_halign(Gtk.Align.START)
        info.pack_start(kind_lbl, False, False, 0)
        info.pack_start(title_lbl, False, False, 0)
        info.pack_start(who_widget, False, False, 0)
        info.pack_start(meta_lbl, False, False, 0)
        btns.set_halign(Gtk.Align.START)
        btns.set_valign(Gtk.Align.START)
        info.pack_start(btns, False, False, 0)
        head.attach(info, 1, 0, 1, 1)

        root.pack_start(head, False, False, 0)

        cover.set_url(cover_url)

    def _track_row(self, rows, key, number, title, duration, extra, on_play, check=None, enabled=True):
        row = Gtk.ListBoxRow()
        row.get_style_context().add_class("track")
        h = Gtk.Box(spacing=12)
        h.set_border_width(5)
        if check is not None:
            h.pack_start(check, False, False, 0)
        play = Gtk.Button.new_from_icon_name("media-playback-start-symbolic", Gtk.IconSize.BUTTON)
        play.get_style_context().add_class("row-play")
        play.set_tooltip_text("Escuchar")
        play.set_sensitive(enabled)
        play.connect("clicked", lambda *_: on_play())
        h.pack_start(play, False, False, 0)
        num = label(str(number), "track-num", xalign=1.0)
        num.set_width_chars(3)
        h.pack_start(num, False, False, 0)
        h.pack_start(label(title, "track-title", ellipsize=True), True, True, 0)
        if extra:
            h.pack_start(label(extra, "muted", ellipsize=True), False, False, 8)
        h.pack_end(label(duration or "", "muted", xalign=1.0), False, False, 0)
        row.add(h)
        rows[key] = (row, play)
        return row

    # ── reproductor: barra inferior ──
    def _build_player_bar(self):
        self.bar_rev = Gtk.Revealer()
        self.bar_rev.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.bar_rev.set_transition_duration(240)
        bar = Gtk.Box(spacing=18)
        bar.get_style_context().add_class("player-bar")
        # mismo ancho a ambos lados: así los controles quedan en el centro exacto de la ventana
        side_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)

        left = Gtk.Box(spacing=12)
        left.set_size_request(300, -1)
        side_group.add_widget(left)
        self.bar_cover = Cover(56)
        self.bar_cover.set_valign(Gtk.Align.CENTER)
        left.pack_start(self.bar_cover, False, False, 0)
        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        meta.set_valign(Gtk.Align.CENTER)
        self.bar_title = label("", "bar-title", ellipsize=True)
        self.bar_title.set_max_width_chars(26)
        self.bar_artist_btn = self._bar_link(lambda: self._goto_artist(self.player.item), 15)
        self.bar_sep = label("·", "card-sub")
        self.bar_album_btn = self._bar_link(lambda: self._goto_album(self.player.item), 17)
        self.bar_loading = label("", "card-sub", ellipsize=True)
        self.bar_loading.set_no_show_all(True)
        sub = Gtk.Box(spacing=5)
        sub.pack_start(self.bar_artist_btn, False, False, 0)
        sub.pack_start(self.bar_sep, False, False, 0)
        sub.pack_start(self.bar_album_btn, False, False, 0)
        meta.pack_start(self.bar_title, False, False, 0)
        meta.pack_start(sub, False, False, 0)
        meta.pack_start(self.bar_loading, False, False, 0)
        left.pack_start(meta, True, True, 0)
        bar.pack_start(left, False, False, 0)

        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        ctr = Gtk.Box(spacing=12)
        ctr.set_halign(Gtk.Align.CENTER)
        prev = Gtk.Button.new_from_icon_name("media-skip-backward-symbolic", Gtk.IconSize.BUTTON)
        prev.get_style_context().add_class("round")
        prev.set_tooltip_text("Anterior")
        prev.connect("clicked", lambda *_: self.player.prev())
        self.play_btn = Gtk.Button.new_from_icon_name("media-playback-start-symbolic", Gtk.IconSize.LARGE_TOOLBAR)
        self.play_btn.get_style_context().add_class("play-main")
        self.play_btn.connect("clicked", lambda *_: self.player.toggle())
        nxt = Gtk.Button.new_from_icon_name("media-skip-forward-symbolic", Gtk.IconSize.BUTTON)
        nxt.get_style_context().add_class("round")
        nxt.set_tooltip_text("Siguiente")
        nxt.connect("clicked", lambda *_: self.player.next())
        ctr.pack_start(prev, False, False, 0)
        ctr.pack_start(self.play_btn, False, False, 0)
        ctr.pack_start(nxt, False, False, 0)
        center.pack_start(ctr, False, False, 0)
        prog = Gtk.Box(spacing=10)
        self.t_pos = label("0:00", "muted", xalign=1.0)
        self.t_pos.set_width_chars(5)
        self.pscale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1000, 1)
        self.pscale.set_draw_value(False)
        self.pscale.set_hexpand(True)
        self.pscale.connect("change-value", self._on_seek)
        self.pscale.connect("button-press-event", self._scale_press)
        self.pscale.connect("button-release-event", self._scale_release)
        self.t_dur = label("0:00", "muted")
        self.t_dur.set_width_chars(5)
        prog.pack_start(self.t_pos, False, False, 0)
        prog.pack_start(self.pscale, True, True, 0)
        prog.pack_start(self.t_dur, False, False, 0)
        center.pack_start(prog, False, False, 0)
        bar.pack_start(center, True, True, 0)

        right = Gtk.Box(spacing=8)
        right.set_size_request(300, -1)
        side_group.add_widget(right)
        right.set_halign(Gtk.Align.END)
        right.set_valign(Gtk.Align.CENTER)
        right.pack_start(Gtk.Image.new_from_icon_name("audio-volume-high-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        self.vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.01)
        self.vol_scale.set_draw_value(False)
        self.vol_scale.set_size_request(120, -1)
        self.vol_scale.set_value(float(self.cfg.get("volume", 0.8)))
        self.vol_scale.connect("value-changed", self._on_volume)
        right.pack_start(self.vol_scale, False, False, 0)
        self.vinyl_btn = Gtk.Button.new_from_icon_name("media-optical-symbolic", Gtk.IconSize.BUTTON)
        self.vinyl_btn.get_style_context().add_class("round")
        self.vinyl_btn.set_tooltip_text("Modo disco de vinilo")
        self.vinyl_btn.connect("clicked", lambda *_: self.set_vinyl_mode(not self.vinyl_mode))
        right.pack_start(self.vinyl_btn, False, False, 0)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        close.get_style_context().add_class("round")
        close.set_tooltip_text("Cerrar reproductor")
        close.connect("clicked", lambda *_: self.player.stop_all())
        right.pack_start(close, False, False, 0)
        bar.pack_end(right, False, False, 0)
        self.bar = bar
        self.bar_rev.add(bar)

    # ── modo disco de vinilo ──
    def _build_vinyl_layer(self):
        """Lista de pistas a la izquierda y zona inferior sensible al ratón que muestra el reproductor."""
        self.v_scroll = Gtk.ScrolledWindow()
        self.v_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.v_scroll.set_halign(Gtk.Align.START)
        self.v_scroll.set_valign(Gtk.Align.FILL)
        self.v_scroll.set_size_request(300, -1)
        self.v_scroll.set_margin_start(32)
        self.v_scroll.set_margin_top(40)
        self.v_scroll.set_margin_bottom(110)
        self.v_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.v_head = label("", "vhead", ellipsize=True)
        self.v_box.pack_start(self.v_head, False, False, 0)
        self.v_list = Gtk.ListBox()
        self.v_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.v_list.connect("row-activated", lambda lb, row: self.player.play_index(row.get_index()))
        self.v_box.pack_start(self.v_list, False, False, 0)
        self.v_scroll.add(self.v_box)
        # show_all ANTES de no_show_all: si no, los hijos nunca llegan a mostrarse
        self.v_scroll.show_all()
        self.v_scroll.set_no_show_all(True)
        self.v_scroll.hide()
        self.shell.add_overlay(self.v_scroll)

        # el reproductor flotante: un Revealer pegado abajo (altura 0 mientras está oculto)
        self.hover_rev = Gtk.Revealer()
        self.hover_rev.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.hover_rev.set_transition_duration(220)
        self.hover_rev.set_halign(Gtk.Align.FILL)
        self.hover_rev.set_valign(Gtk.Align.END)
        self.hover_rev.show_all()
        self.hover_rev.set_no_show_all(True)
        self.hover_rev.hide()
        self.shell.add_overlay(self.hover_rev)
        self._hover_timer = None
        self._hover_grace = 0.0
        self._hover_out = 0

    def _pointer_in_bottom_zone(self):
        """True si el puntero está en la franja inferior de la ventana (o sobre el propio reproductor).
        Se lee la posición directamente: los eventos de entrada/salida parpadean al desplegarse la barra."""
        gw = self.get_window()
        if gw is None:
            return False
        seat = Gdk.Display.get_default().get_default_seat()
        res = gw.get_device_position(seat.get_pointer())
        px, py = res[1], res[2]
        off = self.shell.translate_coordinates(self, 0, 0) or (0, 0)
        x, y = px - off[0], py - off[1]
        w, h = self.shell.get_allocated_width(), self.shell.get_allocated_height()
        bar_h = self.bar.get_allocated_height() if self.hover_rev.get_child_revealed() else 0
        zone = max(110, bar_h + 24)
        return 0 <= x <= w and h - zone <= y <= h

    def _hover_poll(self):
        if not self.vinyl_mode:
            self._hover_timer = None
            return False
        try:
            inside = self._pointer_in_bottom_zone()
        except Exception:  # noqa: BLE001
            inside = False
        if inside and self.player.item is not None:
            self._hover_out = 0
            if not self.hover_rev.get_reveal_child():
                self.hover_rev.set_reveal_child(True)
        elif time.monotonic() > self._hover_grace:
            self._hover_out += 1
            # histéresis: ~0,5 s fuera de la zona antes de ocultar
            if self._hover_out >= 5 and self.hover_rev.get_reveal_child():
                self.hover_rev.set_reveal_child(False)
        return True

    def _on_key(self, w, e):
        if self.vinyl_mode and e.keyval == Gdk.KEY_Escape:
            self.set_vinyl_mode(False)
            return True
        return False

    def set_vinyl_mode(self, on):
        """Modo disco de vinilo: sin menús, vinilo grande y centrado, pistas a la izquierda
        y reproductor oculto que aparece al acercar el ratón a la parte inferior."""
        if on == self.vinyl_mode or (on and self.player.item is None):
            return
        self.vinyl_mode = on
        tb = self.get_titlebar()
        sc = self.bar.get_style_context()
        if on:
            self.bar_rev.set_reveal_child(False)
            self.bar_rev.remove(self.bar)
            self.hover_rev.add(self.bar)
            self.bar.show()
            sc.add_class("player-float")
            if tb:
                tb.set_visible(False)
            self.main_stack.set_visible(False)
            self.dl_rev.set_visible(False)
            self.v_scroll.show()      # los hijos ya se mostraron al construir la capa
            self.hover_rev.show()
            self._vq_key = None
            self._rebuild_vinyl_tracks()
            self.vinyl_btn.set_tooltip_text("Salir del modo disco de vinilo (Esc)")
            self.hover_rev.set_reveal_child(True)  # se enseña un momento para que se vea dónde está
            self._hover_grace = time.monotonic() + 2.5
            self._hover_out = 0
            if self._hover_timer is None:
                self._hover_timer = GLib.timeout_add(100, self._hover_poll)
        else:
            self.hover_rev.set_reveal_child(False)
            self.hover_rev.remove(self.bar)
            self.bar_rev.add(self.bar)
            self.bar.show()
            sc.remove_class("player-float")
            self.v_scroll.hide()
            self.hover_rev.hide()
            if tb:
                tb.set_visible(True)
            self.main_stack.set_visible(True)
            self.dl_rev.set_visible(True)
            self.bar_rev.set_reveal_child(self.player.item is not None)
            self.vinyl_btn.set_tooltip_text("Modo disco de vinilo")
        self.backdrop.set_vinyl(on)

    def _rebuild_vinyl_tracks(self):
        p = self.player
        key = tuple(it.get("key") for it in p.queue)
        if key != self._vq_key:
            self._vq_key = key
            for c in self.v_list.get_children():
                self.v_list.remove(c)
            first = p.queue[0] if p.queue else {}
            self.v_head.set_text(" · ".join(x for x in (first.get("album"), first.get("artist")) if x).upper())
            for i, it in enumerate(p.queue):
                row = Gtk.ListBoxRow()
                row.get_style_context().add_class("vtrack")
                row.add(label(f"{i + 1:02d}   {it.get('title', '')}", "vtrack-label", ellipsize=True))
                row.show_all()
                self.v_list.add(row)
        for i, row in enumerate(self.v_list.get_children()):
            ctx = row.get_style_context()
            (ctx.add_class if i == p.index else ctx.remove_class)("vtrack-now")
        GLib.idle_add(self._vinyl_scroll_to_current)

    def _vinyl_scroll_to_current(self):
        rows = self.v_list.get_children()
        cur = self.player.index
        if 0 <= cur < len(rows):
            pos = rows[cur].translate_coordinates(self.v_box, 0, 0)
            adj = self.v_scroll.get_vadjustment()
            if pos:
                target = pos[1] - adj.get_page_size() / 3
                adj.set_value(max(0, min(target, adj.get_upper() - adj.get_page_size())))
        return False

    def _bar_link(self, cb, max_chars):
        b = Gtk.Button()
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.set_focus_on_click(False)
        b.get_style_context().add_class("bar-link")
        b.lbl = label("", "card-sub", ellipsize=True)
        b.lbl.set_max_width_chars(max_chars)
        b.add(b.lbl)
        b.connect("clicked", lambda *_: cb())
        return b

    def _artist_link(self, text, cb):
        """Nombre de artista pulsable (cabecera de álbum)."""
        if not text:
            return label("", "link")
        b = Gtk.Button()
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.set_focus_on_click(False)
        b.get_style_context().add_class("link")
        b.add(label(text, "link"))
        b.set_tooltip_text("Ver artista")
        b.connect("clicked", lambda *_: cb())
        return b

    def _goto_album(self, item):
        """Abre el álbum de la canción que suena."""
        if not item:
            return
        e = self.library.get(item.get("folder") or "")
        if e and Path(e.get("folder", "")).is_dir():
            self.main_stack.set_visible_child_name("coleccion")
            self.open_local_album(e)
        elif item.get("album_id"):
            self.main_stack.set_visible_child_name("descargar")
            self.open_album(item["album_id"])
        else:
            self.toast("No se puede abrir este álbum")

    def _goto_artist(self, item):
        """Abre el artista de la canción que suena."""
        if not item:
            return
        if item.get("folder"):
            self.show_artist_in_collection(item.get("artist", ""))
        elif item.get("artist_id"):
            self.main_stack.set_visible_child_name("descargar")
            self.open_artist(item["artist_id"])
        else:
            self.toast("No se puede abrir este artista")

    def _on_seek(self, scale, scroll, value):
        self._seek_value = max(0.0, min(1000.0, value))
        if not self._dragging:
            self.player.seek_frac(self._seek_value / 1000)
        return False

    def _scale_press(self, *_):
        self._dragging = True
        self._seek_value = None
        return False

    def _scale_release(self, *_):
        self._dragging = False
        if self._seek_value is not None:
            self.player.seek_frac(self._seek_value / 1000)
        return False

    def _on_volume(self, scale):
        v = scale.get_value()
        self.player.set_volume(v)
        self.cfg["volume"] = round(v, 2)
        if self._vol_save:
            GLib.source_remove(self._vol_save)
        self._vol_save = GLib.timeout_add(600, self._save_volume)

    def _save_volume(self):
        self._vol_save = None
        save_config(self.cfg)
        return False

    def _tick(self):
        p = self.player
        if p.available and p.state in ("playing", "paused"):
            pos, dur = p.position()
            if dur > 0 and not self._dragging:
                self.pscale.set_value(pos / dur * 1000)
            self.t_pos.set_text(fmt_time(pos))
            self.t_dur.set_text(fmt_time(dur))
            if getattr(self, "mpris", None):
                self.mpris.tick()
            self._arm_update()
        return True

    def _arm_update(self):
        """El brazo avanza por el disco según la pista actual dentro del álbum."""
        bd = getattr(self, "backdrop", None)
        if not bd:
            return
        p = self.player
        bd.set_arm_enabled(bool(self.cfg.get("vinyl_arm", True)))
        if p.item is None or p.state not in ("playing", "paused", "loading"):
            bd.set_arm_target(None)
            return
        n = max(1, len(p.queue))
        frac = 0.0
        if p.state in ("playing", "paused"):
            try:
                pos, dur = p.position()
                frac = pos / dur if dur > 0 else 0.0
            except Exception:
                frac = 0.0
        bd.set_arm_target((max(0, p.index) + min(1.0, frac)) / n)

    def _player_changed(self):
        p = self.player
        it = p.item
        if self.vinyl_mode and it is None:
            self.set_vinyl_mode(False)  # el reproductor se cerró: se vuelve a la vista normal
        if not self.vinyl_mode:
            self.bar_rev.set_reveal_child(it is not None)
        elif p.state == "loading" or it:
            self._rebuild_vinyl_tracks()
        bd = getattr(self, "backdrop", None)
        if bd:  # el vinilo de fondo toma la portada y gira solo mientras suena
            spin_on = bool(self.cfg.get("vinyl_spin", True))
            bd.set_cover((it.get("cover_big") or it.get("cover")) if it and spin_on else None)
            bd.set_spinning(spin_on and p.state == "playing")
            if not spin_on and bd.angle:
                bd.angle = 0.0
                bd.queue_draw()
            self._arm_update()
        if it:
            self.bar_title.set_text(it.get("title", ""))
            artist, album = it.get("artist") or "", it.get("album") or ""
            loading = p.state == "loading"
            self.bar_artist_btn.lbl.set_text(artist)
            self.bar_album_btn.lbl.set_text(album)
            self.bar_artist_btn.set_tooltip_text(f"Ir al artista: {artist}" if artist else None)
            self.bar_album_btn.set_tooltip_text(f"Ir al álbum: {album}" if album else None)
            self.bar_artist_btn.set_visible(bool(artist) and not loading)
            self.bar_album_btn.set_visible(bool(album) and not loading)
            self.bar_sep.set_visible(bool(artist) and bool(album) and not loading)
            self.bar_loading.set_text("Cargando vista previa…")
            self.bar_loading.set_visible(loading)
            cov = it.get("cover")
            if cov != self._bar_cover:
                self._bar_cover = cov
                self.bar_cover.set_url(cov)
            set_icon(self.play_btn, {"playing": "media-playback-pause-symbolic",
                                     "loading": "view-refresh-symbolic"}.get(p.state, "media-playback-start-symbolic"),
                     Gtk.IconSize.LARGE_TOOLBAR)
            if p.state in ("loading", "stopped", "error"):
                self.pscale.set_value(0)
                self.t_pos.set_text("0:00")
                self.t_dur.set_text("0:00")
        self._refresh_rows()
        mp = getattr(self, "mpris", None)
        if mp:
            mp.update()
        if p.state == "error":
            if not self._error_shown:
                self._error_shown = True
                self.toast(p.error_msg if p.error_msg == OFFLINE_MSG
                           else f"No se pudo reproducir: {p.error_msg[:100]}", 6)
        else:
            self._error_shown = False

    def _refresh_rows(self):
        p = self.player
        cur = p.item["key"] if p.item else None
        for rows in (self.rows_local, self.rows_prev):
            for key, (row, btn) in list(rows.items()):
                sc = row.get_style_context()
                if key == cur:
                    sc.add_class("now-playing")
                    icon = {"playing": "media-playback-pause-symbolic",
                            "loading": "view-refresh-symbolic"}.get(p.state, "media-playback-start-symbolic")
                else:
                    sc.remove_class("now-playing")
                    icon = "media-playback-start-symbolic"
                set_icon(btn, icon)
                btn.show_all()

    # ── vista previa: resolución y precarga ──
    def _resolve_item(self, item, cb):
        vid = item.get("video_id")
        if not vid:
            cb(None, RuntimeError("sin fuente de audio"))
            return
        if vid in self._inflight:
            def check():
                if vid in self._inflight:
                    return True
                p = item.get("path")
                cb(p, None if p else RuntimeError("vista previa no disponible"))
                return False

            GLib.timeout_add(250, check)
            return
        self._inflight.add(vid)

        def done(r, e):
            self._inflight.discard(vid)
            if r:
                item["path"] = r
            cb(r, e)

        run_async(lambda: fetch_preview(vid), done)

    def _prefetch_item(self, item):
        vid = item.get("video_id")
        if not vid or item.get("path") or vid in self._inflight:
            return
        self._inflight.add(vid)

        def done(r, e):
            self._inflight.discard(vid)
            if r:
                item["path"] = r

        run_async(lambda: fetch_preview(vid), done)

    # ── búsqueda ──
    def do_search(self):
        artist = self.e_artist.get_text().strip()
        album = self.e_album.get_text().strip()
        if not artist and not album:
            self.toast("Escribe un artista y/o un álbum")
            return
        self.dloading()

        def work():
            with self._search_lock:
                yt = self.api()
                if artist and not album:
                    res = [r for r in (yt.search(artist, filter="artists", limit=30) or []) if r.get("browseId")]
                    return "artists", res
                res = yt.search(f"{artist} {album}".strip(), filter="albums", limit=40) or []
            res = [r for r in res if r.get("browseId")]
            if artist:
                a = artist.lower()
                res.sort(key=lambda r: 0 if a in fmt_artists(r.get("artists")).lower() else 1)
            return "albums", res

        def done(r, err):
            if err:
                return self.fail(err)
            kind, items = r
            self._show_results(kind, items, artist, album)

        run_async(work, done)

    def _show_results(self, kind, items, artist, album):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(36)
        what = artist if kind == "artists" else " · ".join(x for x in (artist, album) if x)
        box.pack_start(label(f"Resultados para “{what}”", "section-title"), False, False, 0)
        if not items:
            box.pack_start(label("No se encontró nada. Prueba con otra ortografía.", "muted"), False, False, 12)
        else:
            grid = make_grid()
            for it in items:
                grid.add(self._artist_card(it) if kind == "artists" else self._album_card(it, it.get("artists")))
            box.pack_start(grid, False, False, 8)
        self._set_page_content(self.page_results, box)
        self.dhist = [h for h in self.dhist if h != "results"]
        self.dshow("results")

    def _artist_card(self, it):
        return make_card(
            best_thumb(it.get("thumbnails"), 340),
            it.get("artist") or it.get("name") or "Artista",
            "Artista", circle=True,
            on_click=lambda: self.open_artist(it["browseId"]),
        )

    def _album_card(self, it, artists=None, fallback_artist=None):
        bits = []
        who = fmt_artists(artists) or fallback_artist
        if who:
            bits.append(who)
        if it.get("year"):
            bits.append(str(it["year"]))
        if it.get("type") and not who:
            bits.append(it["type"])
        return make_card(
            best_thumb(it.get("thumbnails"), 340),
            it.get("title") or "Álbum", " · ".join(bits),
            on_click=lambda: self.open_album(it["browseId"]),
        )

    # ── artista ──
    def open_artist(self, channel_id):
        self.dloading()

        def work():
            yt = self.api()
            a = yt.get_artist(channel_id)

            def section(key):
                s = a.get(key) or {}
                items = s.get("results", [])
                if s.get("params") and s.get("browseId"):
                    try:
                        items = yt.get_artist_albums(s["browseId"], s["params"]) or items
                    except Exception:
                        pass
                return [i for i in items if i.get("browseId")]

            return a, section("albums"), section("singles")

        def done(r, err):
            if err:
                return self.fail(err)
            self._show_artist(*r)

        run_async(work, done)

    def _show_artist(self, a, albums, singles):
        name = a.get("name", "Artista")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(36)
        head = Gtk.Box(spacing=26)
        cover = Cover(170, circle=True)
        cover.set_url(best_thumb(a.get("thumbnails"), 500))
        head.pack_start(cover, False, False, 0)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        info.set_valign(Gtk.Align.CENTER)
        info.pack_start(label("ARTISTA", "kind"), False, False, 0)
        info.pack_start(label(name, "big-title", wrap=True), False, False, 0)
        if a.get("subscribers"):
            info.pack_start(label(f"{a['subscribers']} suscriptores", "muted"), False, False, 0)
        desc = (a.get("description") or "").strip()
        if desc:
            d = label(desc, "muted", wrap=True)
            d.set_lines(3)
            d.set_ellipsize(Pango.EllipsizeMode.END)
            d.set_max_width_chars(90)
            info.pack_start(d, False, False, 4)
        everything = albums + singles
        if everything:
            b = Gtk.Button(label=f"Descargar discografía ({len(everything)})")
            b.get_style_context().add_class("primary")
            b.set_halign(Gtk.Align.START)
            b.set_margin_top(8)
            b.connect("clicked", lambda *_: self._download_discography(name, everything))
            info.pack_start(b, False, False, 0)
        head.pack_start(info, True, True, 0)
        box.pack_start(head, False, False, 12)
        for title, items in (("Álbumes", albums), ("Sencillos y EPs", singles)):
            if not items:
                continue
            box.pack_start(label(title, "section-title"), False, False, 0)
            grid = make_grid()
            for it in items:
                grid.add(self._album_card(it))
            box.pack_start(grid, False, False, 4)
        if not albums and not singles:
            box.pack_start(label("Este artista no tiene álbumes publicados.", "muted"), False, False, 12)
        self._set_page_content(self.page_artist, box)
        self.dhist = [h for h in self.dhist if h != "artist"]
        self.dshow("artist")

    def _download_discography(self, artist, items):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                buttons=Gtk.ButtonsType.OK_CANCEL,
                                text=f"¿Descargar {len(items)} lanzamientos de {artist}?")
        dlg.format_secondary_text("Se añadirán a la cola y se descargarán uno tras otro.")
        resp = dlg.run()
        dlg.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        for it in items:
            self.manager.add(Job(it["browseId"], it.get("title", "Álbum"), artist, best_thumb(it.get("thumbnails"), 300)))
        self.dl_btn.set_active(True)
        self.toast(f"{len(items)} álbumes añadidos a la cola")

    # ── álbum online: escuchar y descargar ──
    def open_album(self, browse_id):
        self.dloading()

        def work():
            return self.api().get_album(browse_id)

        def done(r, err):
            if err:
                return self.fail(err)
            self._show_album(browse_id, r)

        run_async(work, done)

    def _online_items(self, browse_id, a):
        """Pistas reproducibles (vista previa) de un álbum online."""
        title = a.get("title", "Álbum")
        artists = a.get("artists") or []
        artist_txt = fmt_artists(artists)
        cover_url = best_thumb(a.get("thumbnails"), 600)
        small = best_thumb(a.get("thumbnails"), 200)
        return [{"key": "yt:" + t["videoId"], "video_id": t["videoId"], "title": t.get("title", ""),
                 "artist": fmt_artists(t.get("artists")) or artist_txt, "album": title, "cover": small,
                 "cover_big": cover_url, "album_id": browse_id,
                 "artist_id": ((t.get("artists") or artists or [{}])[0] or {}).get("id")}
                for t in a.get("tracks", []) if t.get("videoId") and t.get("isAvailable", True)]

    def play_online_album(self, e):
        """Escucha (vista previa) un álbum online de «Me gusta»."""
        bid = e["browse_id"]

        def done(a, err):
            if err:
                msg = friendly_error(err)
                self.toast(msg if msg == OFFLINE_MSG else f"Error: {msg[:120]}")
                return
            self._play_preview(self._online_items(bid, a), 0)

        run_async(lambda: self.api().get_album(bid), done)

    def _download_fav(self, e):
        self.manager.add(Job(e["browse_id"], e.get("title", "Álbum"), e.get("artist", ""),
                             upscale(e["cover"], 300) if str(e.get("cover", "")).startswith("http") else None))
        self.dl_btn.set_active(True)

    def _is_liked(self, bid):
        return bid in self.favs

    @staticmethod
    def _style_like(btn, liked):
        btn.img.set_from_icon_name("starred-symbolic" if liked else "non-starred-symbolic", Gtk.IconSize.BUTTON)
        off, on = getattr(btn, "_texts", ("Me gusta", "En Me gusta"))
        btn.lbl.set_text(on if liked else off)
        btn.set_tooltip_text("Quitar de Mis Álbumes" if liked else "Guardar en Mis Álbumes (álbum online)")
        ctx = btn.get_style_context()
        (ctx.add_class if liked else ctx.remove_class)("liked")

    def _refresh_like(self):
        btn = getattr(self, "_like_btn", None)
        if btn is not None and getattr(self, "_album_id", None):
            self._style_like(btn, self._is_liked(self._album_id))
        det = getattr(self, "_detail_like", None)
        if det:
            self._style_like(det[0], self._is_liked(det[1]))

    def _toggle_like(self, entry):
        """Añade o quita un álbum online de «Me gusta»; los marcados aparecen en Mis Álbumes."""
        bid = entry["browse_id"]
        if bid in self.favs:
            old = self.favs.pop(bid)
            try:
                c = Path(old.get("cover") or "")
                if c.is_file() and FAV_COVERS in c.parents:
                    c.unlink()
            except Exception:
                pass
            if old.get("cover_url"):
                old["cover"] = old["cover_url"]   # por si se vuelve a añadir desde la misma página
            self.toast("Quitado de Mis Álbumes")
        else:
            self.favs[bid] = entry
            url = entry.get("cover")
            if not (str(url).startswith("http") or Path(str(url or "")).is_file()) and entry.get("cover_url"):
                url = entry["cover"] = entry["cover_url"]
            if url and str(url).startswith("http"):
                def save_cover():
                    FAV_COVERS.mkdir(parents=True, exist_ok=True)
                    path = FAV_COVERS / (sanitize(bid) + ".jpg")
                    path.write_bytes(fetch_bytes(url))
                    return str(path)

                def done(path, err):
                    if path and bid in self.favs:
                        self.favs[bid]["cover"] = path   # portada guardada: se ve también sin conexión
                        save_favs(self.favs)
                        self.refresh_library()

                run_async(save_cover, done)
            self.toast("Añadido a Mis Álbumes · álbum online")
        save_favs(self.favs)
        self._refresh_like()
        self.refresh_library()

    def _show_album(self, browse_id, a):
        self._album, self._album_id = a, browse_id
        title = a.get("title", "Álbum")
        artists = a.get("artists") or []
        artist_txt = fmt_artists(artists)
        cover_url = best_thumb(a.get("thumbnails"), 600)
        small = best_thumb(a.get("thumbnails"), 200)
        tracks = [t for t in a.get("tracks", []) if t.get("videoId")]
        items = self._online_items(browse_id, a)
        index_of = {it["video_id"]: i for i, it in enumerate(items)}
        self.rows_prev = {}

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.get_style_context().add_class("album-root")
        root.set_border_width(28)
        first = artists[0] if artists else {}
        if first.get("id"):
            who = Gtk.LinkButton.new_with_label("https://music.youtube.com", artist_txt)
            who.get_style_context().add_class("link")
            who.set_halign(Gtk.Align.START)
            who.connect("activate-link", lambda *_: (self.open_artist(first["id"]), True)[1])
        else:
            who = label(artist_txt, "link")
        bits = [str(b) for b in (a.get("year"), f"{a.get('trackCount') or len(tracks)} canciones", a.get("duration")) if b]

        btns = Gtk.Box(spacing=10)
        btns.set_margin_top(10)
        listen = icon_button("media-playback-start-symbolic", "Reproducir", "primary")
        listen.connect("clicked", lambda *_: self._play_preview(items, 0))
        listen.set_sensitive(bool(items))
        btns.pack_start(listen, False, False, 0)
        self._like_btn = icon_button("non-starred-symbolic", "Añadir a mis álbumes", "ghost")
        self._like_btn._texts = ("Añadir a mis álbumes", "En mis álbumes")
        self._like_btn.connect("clicked", lambda *_: self._toggle_like({
            "browse_id": browse_id, "title": title, "artist": artist_txt, "year": str(a.get("year") or ""),
            "cover": cover_url, "cover_url": cover_url, "artist_id": first.get("id"), "tracks": len(items),
            "date": time.time(), "online": True, "folder": "online:" + browse_id}))
        btns.pack_start(self._like_btn, False, False, 0)
        self.album_btn = icon_button("folder-download-symbolic", "Descargar álbum", "ghost")
        self.album_btn.connect("clicked", lambda *_: self._download_album())
        btns.pack_start(self.album_btn, False, False, 0)
        self._album_header(root, cover_url, a.get("type") or "ÁLBUM", title, who, " · ".join(bits), btns)

        tog = Gtk.Button(label="Seleccionar / quitar todo")
        tog.get_style_context().add_class("ghost")
        tog.set_halign(Gtk.Align.START)
        tog.set_margin_top(28)
        tog.connect("clicked", lambda *_: self._toggle_all())
        root.pack_start(tog, False, False, 0)

        lb = Gtk.ListBox()
        lb.set_selection_mode(Gtk.SelectionMode.NONE)
        lb.set_margin_top(10)
        self._checks = []
        for i, t in enumerate(tracks):
            ok = t.get("isAvailable", True)
            chk = Gtk.CheckButton()
            chk.set_active(ok)
            chk.set_sensitive(ok)
            chk.connect("toggled", lambda *_: self._refresh_album_button())
            chk.vid = t["videoId"]
            self._checks.append(chk)
            feat = fmt_artists(t.get("artists"))
            vid = t["videoId"]
            lb.add(self._track_row(
                self.rows_prev, "yt:" + vid, t.get("trackNumber") or i + 1, t.get("title", ""),
                t.get("duration") or "", feat if feat and feat != artist_txt else "",
                lambda vid=vid: self._play_preview(items, index_of.get(vid, 0)),
                check=chk, enabled=ok))
        root.pack_start(lb, False, False, 0)
        self._set_page_content(self.page_album, root)
        self._refresh_album_button()
        self._refresh_like()
        self._refresh_rows()
        self.dhist = [h for h in self.dhist if h != "album"]
        self.dshow("album")

    def _play_preview(self, items, index):
        if not self.player.available:
            self.toast("Falta GStreamer para reproducir (gir1.2-gstreamer-1.0).", 7)
            return
        if not items:
            return
        cur = self.player.item
        if cur and cur["key"] == items[index]["key"] and self.player.state in ("playing", "paused"):
            self.player.toggle()
            return
        self.player.play_queue(items, index)

    def _selected_ids(self):
        return [c.vid for c in self._checks if c.get_active()]

    def _toggle_all(self):
        usable = [c for c in self._checks if c.get_sensitive()]
        target = not all(c.get_active() for c in usable)
        for c in usable:
            c.set_active(target)

    def _refresh_album_button(self):
        if not hasattr(self, "album_btn") or not getattr(self, "_checks", None):
            return
        n = len(self._selected_ids())
        total = len(self._checks)
        fmt = self.cfg["format"].upper()
        if n == 0:
            self.album_btn.lbl.set_text("Selecciona canciones")
            self.album_btn.set_sensitive(False)
        else:
            txt = "Descargar álbum" if n == total else f"Descargar {n} canciones"
            self.album_btn.lbl.set_text(f"{txt} · {fmt}")
            self.album_btn.set_sensitive(True)

    def _download_album(self):
        a = self._album
        ids = self._selected_ids()
        if not ids:
            return
        all_ids = {c.vid for c in self._checks}
        job = Job(self._album_id, a.get("title", "Álbum"), fmt_artists(a.get("artists")),
                  best_thumb(a.get("thumbnails"), 300), track_ids=None if set(ids) == all_ids else ids)
        self.manager.add(job)
        self.dl_btn.set_active(True)

    # ── panel de descargas ──
    def _job_added(self, job):
        if self.dl_empty.get_parent():
            self.dl_list.remove(self.dl_empty)
        row = JobRow(job, self.manager.cancel, lambda j: j.folder and self._open_path(j.folder), self.dl_badge_update)
        job.row = row
        self.dl_list.pack_start(row, False, False, 0)
        row.show_all()
        self.dl_badge_update()

    def _clear_finished(self):
        for j in list(self.manager.jobs):
            if j.state in ("done", "error", "cancelled"):
                self.manager.jobs.remove(j)
                if j.row:
                    self.dl_list.remove(j.row)
                    j.row = None
        if not self.manager.jobs and not self.dl_empty.get_parent():
            self.dl_list.pack_start(self.dl_empty, False, False, 8)
            self.dl_empty.show()
        self.dl_badge_update()

    def dl_badge_update(self):
        active = sum(1 for j in self.manager.jobs if j.state in ("queued", "running"))
        if getattr(self, "_last_active", None) == active and self.dl_btn.get_child():
            return
        self._last_active = active
        box = Gtk.Box(spacing=6)
        box.pack_start(Gtk.Image.new_from_icon_name("folder-download-symbolic", Gtk.IconSize.LARGE_TOOLBAR), False, False, 0)
        box.pack_start(Gtk.Label(label=f"Descargas ({active})" if active else "Descargas"), False, False, 0)
        old = self.dl_btn.get_child()
        if old:
            self.dl_btn.remove(old)
        self.dl_btn.add(box)
        box.show_all()

    # ── configuración ──
    def _on_retry_spin(self, spin):
        self.cfg["retries"] = int(spin.get_value())
        save_config(self.cfg)

    def open_preferences(self):
        dlg = Gtk.Dialog(title="Configuración", transient_for=self, modal=True)
        dlg.set_default_size(560, -1)
        dlg.add_button("Cancelar", Gtk.ResponseType.CANCEL)
        save = dlg.add_button("Guardar", Gtk.ResponseType.OK)
        save.get_style_context().add_class("suggested-action")
        box = dlg.get_content_area()
        box.set_border_width(22)
        box.set_spacing(10)

        box.pack_start(label("Apariencia", "section-title"), False, False, 0)
        box.pack_start(label("Color resaltado", "card-title"), False, False, 0)
        original_accent = self.cfg.get("accent", DEFAULT_ACCENT)
        chosen = {"id": original_accent}
        swatches = []

        def pick_accent(aid):
            chosen["id"] = aid
            apply_css(CSS, aid)  # vista previa inmediata
            for sw_ in swatches:
                sw_.get_child().queue_draw()
            self.queue_draw()

        sw_row = Gtk.Box(spacing=4)
        for aid in ACCENTS:
            sw_ = AccentSwatch(aid, lambda aid=aid: chosen["id"] == aid, pick_accent)
            swatches.append(sw_)
            sw_row.pack_start(sw_, False, False, 0)
        box.pack_start(sw_row, False, False, 0)
        box.pack_start(label("Se aplica a pestañas, botones, barras de progreso y resaltados. "
                             "Se guarda al pulsar Guardar.", "card-sub", wrap=True), False, False, 0)

        spin_chk = Gtk.CheckButton(label="Vinilo de fondo giratorio con la portada del álbum")
        spin_chk.set_active(bool(self.cfg.get("vinyl_spin", True)))
        spin_chk.set_margin_top(6)
        box.pack_start(spin_chk, False, False, 0)
        box.pack_start(label("Si lo desactivas, el disco de fondo se queda quieto y sin portada.",
                             "card-sub", wrap=True), False, False, 0)

        arm_chk = Gtk.CheckButton(label="Brazo del tocadiscos en el modo disco de vinilo")
        arm_chk.set_active(bool(self.cfg.get("vinyl_arm", True)))
        arm_chk.set_margin_top(6)
        box.pack_start(arm_chk, False, False, 0)
        box.pack_start(label("Un brazo recorre el disco según avanzan las pistas del álbum.",
                             "card-sub", wrap=True), False, False, 0)

        box.pack_start(label("Descargas", "section-title"), False, False, 6)
        box.pack_start(label("Carpeta donde se guardan las canciones", "card-title"), False, False, 0)
        row = Gtk.Box(spacing=8)
        entry = Gtk.Entry()
        entry.set_text(self.cfg["folder"])
        entry.set_hexpand(True)
        browse = Gtk.Button(label="Examinar…")
        browse.get_style_context().add_class("ghost")

        def pick(*_):
            fc = Gtk.FileChooserDialog(title="Elegir carpeta de descargas", transient_for=dlg,
                                       action=Gtk.FileChooserAction.SELECT_FOLDER)
            fc.add_button("Cancelar", Gtk.ResponseType.CANCEL)
            fc.add_button("Elegir", Gtk.ResponseType.OK)
            cur = os.path.expanduser(entry.get_text().strip())
            if os.path.isdir(cur):
                fc.set_filename(cur)
            if fc.run() == Gtk.ResponseType.OK:
                entry.set_text(fc.get_filename())
            fc.destroy()

        browse.connect("clicked", pick)
        row.pack_start(entry, True, True, 0)
        row.pack_start(browse, False, False, 0)
        box.pack_start(row, False, False, 0)
        box.pack_start(label("Se guarda como  Carpeta/Artista/Álbum (año)/01 - Título", "card-sub"), False, False, 0)

        box.pack_start(label("Formato de audio", "card-title"), False, False, 6)
        combo = Gtk.ComboBoxText()
        for k, (lab, *_r) in FORMATS.items():
            combo.append(k, lab)
        combo.set_active_id(self.cfg["format"])
        box.pack_start(combo, False, False, 0)
        hint = label("La fuente de YouTube Music ya está comprimida (~128–256 kbps). "
                     "M4A y Opus no pierden calidad al guardarse; MP3/FLAC recodifican.", "card-sub", wrap=True)
        hint.set_max_width_chars(70)
        box.pack_start(hint, False, False, 0)

        box.pack_start(label("Reintentos", "section-title"), False, False, 6)
        grid = Gtk.Grid(column_spacing=14, row_spacing=8)
        sp_retries = Gtk.SpinButton.new_with_range(0, 10, 1)
        sp_retries.set_value(int(self.cfg.get("retries", 3)))
        sp_delay = Gtk.SpinButton.new_with_range(0, 120, 1)
        sp_delay.set_value(int(self.cfg.get("retry_delay", 4)))
        grid.attach(label("Reintentos para pistas que fallen", "card-title"), 0, 0, 1, 1)
        grid.attach(sp_retries, 1, 0, 1, 1)
        grid.attach(label("Espera entre reintentos (segundos)", "card-title"), 0, 1, 1, 1)
        grid.attach(sp_delay, 1, 1, 1, 1)
        box.pack_start(grid, False, False, 0)
        box.pack_start(label("0 = no reintentar. Al terminar el álbum se vuelven a intentar solo las pistas que fallaron.",
                             "card-sub", wrap=True), False, False, 0)

        box.pack_start(label("Mantenimiento", "section-title"), False, False, 6)
        mrow = Gtk.Box(spacing=8)
        upd = Gtk.Button(label=f"Actualizar yt-dlp (nueva: {self._ytdlp_latest})" if self._ytdlp_latest
                         else "Actualizar yt-dlp (nightly)")
        upd.get_style_context().add_class("ghost")
        upd.connect("clicked", self._update_ytdlp)
        logb = Gtk.Button(label="Abrir carpeta del registro")
        logb.get_style_context().add_class("ghost")
        logb.connect("clicked", lambda *_: self._open_path(str(LOG_FILE.parent)))
        mrow.pack_start(upd, False, False, 0)
        mrow.pack_start(logb, False, False, 0)
        box.pack_start(mrow, False, False, 0)

        dlg.show_all()
        saved = False
        while True:
            resp = dlg.run()
            if resp != Gtk.ResponseType.OK:
                break
            folder = os.path.expanduser(entry.get_text().strip())
            if not folder:
                self.toast("Indica una carpeta de descargas")
                continue
            try:
                Path(folder).mkdir(parents=True, exist_ok=True)
                if not os.access(folder, os.W_OK):
                    raise PermissionError(folder)
            except Exception as e:  # noqa: BLE001
                err = Gtk.MessageDialog(transient_for=dlg, modal=True, message_type=Gtk.MessageType.ERROR,
                                        buttons=Gtk.ButtonsType.OK, text="No se puede usar esa carpeta")
                err.format_secondary_text(f"{folder}\n{e}")
                err.run()
                err.destroy()
                continue
            self.cfg["folder"] = folder
            self.cfg["format"] = combo.get_active_id()
            self.cfg["retries"] = int(sp_retries.get_value())
            self.cfg["retry_delay"] = int(sp_delay.get_value())
            self.cfg["accent"] = chosen["id"]
            self.cfg["vinyl_spin"] = spin_chk.get_active()
            self.cfg["vinyl_arm"] = arm_chk.get_active()
            save_config(self.cfg)
            self._player_changed()
            saved = True
            self.retry_spin.set_value(self.cfg["retries"])
            if hasattr(self, "album_btn"):
                self._refresh_album_button()
            threading.Thread(target=self._scan_library, daemon=True).start()
            self.refresh_library()
            self.toast("Configuración guardada")
            break
        if not saved and chosen["id"] != original_accent:
            apply_css(CSS, original_accent)  # cancelado: se deshace la vista previa
            self.queue_draw()
        dlg.destroy()

    def _update_ytdlp(self, btn):
        btn.set_sensitive(False)
        self.toast("Actualizando yt-dlp…")

        def work():
            USER_LIBS.mkdir(parents=True, exist_ok=True)
            subprocess.run([sys.executable, "-m", "pip", "install", "-U", "--pre", "--target", str(USER_LIBS),
                            "yt-dlp[default]", "ytmusicapi"], check=True, capture_output=True)

        def done(_r, err):
            btn.set_sensitive(True)
            self.toast("✓ Actualizado. Reinicia Vinilo para aplicar." if not err else "No se pudo actualizar (¿hay conexión?)")

        run_async(work, done)


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        win = self.props.active_window
        if not win:
            settings = Gtk.Settings.get_default()
            settings.set_property("gtk-application-prefer-dark-theme", True)
            apply_css(CSS, load_config().get("accent"))
            win = Win(self)
        win.present()

    def do_shutdown(self):
        clear_cache()
        Gtk.Application.do_shutdown(self)


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
