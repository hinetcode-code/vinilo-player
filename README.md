# Vinilo

Reproductor de música para Linux: escucha tu colección local y álbumes de YouTube Music en streaming, con un modo «disco de vinilo» a pantalla completa. Opcionalmente, puedes guardar álbumes en tu equipo.

🌐 **Web:** https://hinetcode-code.github.io/vinilo-player/

## Características
- Mis Álbumes: colección local y álbumes online marcados con «Me gusta»
- Explorar Álbumes: búsqueda con autocompletado por artista y álbum
- Modo disco de vinilo con brazo de tocadiscos opcional (`Esc` para salir)
- Integración MPRIS2 (teclas multimedia y applets de sonido)
- 8 colores de acento
- Descarga opcional en M4A, Opus, MP3 320 kbps o FLAC, con portada y etiquetas

## Instalación (Debian, Ubuntu, Linux Mint)
Descarga el `.deb` desde [Releases](https://github.com/hinetcode-code/vinilo-player/releases/latest) y ejecuta:

```bash
sudo apt install ./vinilo_1_10_2_all.deb
vinilo
```

Requiere Python ≥ 3.10 y conexión a internet durante la instalación (se descargan yt-dlp, ytmusicapi y Deno en `/opt/vinilo`).

Desinstalar:
```bash
sudo apt remove vinilo
sudo rm -rf /opt/vinilo
```

## Código fuente

El repositorio contiene el árbol completo del paquete Debian:

```
package/
├── DEBIAN/            control y postinst (instala yt-dlp, ytmusicapi y Deno en /opt/vinilo)
└── usr/
    ├── bin/vinilo                         lanzador
    ├── lib/vinilo/vinilo.py               aplicación (Python + GTK3 + GStreamer)
    ├── lib/vinilo/setup-deps.sh           reparación de componentes
    └── share/                             icono, acceso directo y copyright
build-deb.sh                               construye el .deb
```

Construir el paquete (requiere `dpkg-deb`):

```bash
./build-deb.sh
sudo apt install ./vinilo_1_10_2_all.deb
```

Ejecutar sin instalar (necesita `python3-gi`, GStreamer, ffmpeg y mutagen en el sistema):

```bash
python3 package/usr/lib/vinilo/vinilo.py
```

## Licencia
GPL-3.0. Proyecto independiente, sin relación con YouTube ni Google. Si descargas contenido, hazlo solo si tienes derecho a ello y para uso personal.
