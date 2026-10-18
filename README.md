# coniMIX

Biblioteca d'àudio personal amb cua compartida ("jam"). Cerca a YouTube, descarrega l'àudio (yt-dlp + ffmpeg)
i només reprodueix cançons descarregades al 100%. Un sol usuari (propietari) i convidats per QR.

- **Propietari** (`/`, amb contrasenya): cerca, cua, llistes, descàrregues, ordre, esborrat.
- **Convidats** (`/jam`, només amb el QR de l'amfitrió): veuen la cua, cerquen, escolten una prèvia, afegeixen cançons (després de l'actual o al final) i en canvien l'ordre. Amb límit de peticions.
- **Jam privada:** el QR porta un codi secret (`/jam?k=…`) que es canvia per una sessió de convidat (cookie, 12 h). Sense el codi, la jam no mostra res. L'amfitrió pot generar un codi nou o **començar una jam nova** (codi nou + expulsar tots els convidats) des de la finestra del QR.
- **Cerca àmplia:** YouTube + YouTube Music en paral·lel (tolera faltes), amb cerca automàtica a partir de 5 lletres.
- **Interfície mòbil:** llistes amb cercador, gestos de lliscar per reproduir/treure, arrossegar per ordenar i mini-reproductor.
- La cançó que sona no es pot superar a la cua. Encreuament de volum entre cançons (no a iOS: allà és sense silenci però sense fade).
- Esborrar una cançó d'una llista l'esborra del disc si no és a cap altra llista; esborrar la descàrrega la treu de totes les llistes.

## Execució

Requisits: `python3`, `yt-dlp`, `ffmpeg`.

    SPOTITXELL_PASSWORD='la-teva-contrasenya' ./run.sh        # http://<ip>:8791

Variables d'entorn (o fitxers al costat de `server.py`, útil per executar sense consola):

| Variable | Fitxer alternatiu | Descripció |
|---|---|---|
| `SPOTITXELL_PASSWORD` | `owner_password.txt` | Contrasenya del propietari (obligatòria, ≥ 8 caràcters) |
| `PUBLIC_URL` | `public_url.txt` | URL pública per al QR (p. ex. darrere d'un túnel) |
| `PORT` | | Port (8791) |
| `DATA_DIR` | | Base de dades SQLite i MP3 (`./data`) |
| `MAX_DOWNLOADS` | | Descàrregues simultànies (2) |
| `YTDLP`, `YTDLP_ARGS` | `ffmpeg_path.txt` (Windows) | Ordre de yt-dlp i arguments extra |

Actualitza yt-dlp sovint: YouTube bloqueja versions velles (error 403).

## Windows (en segon pla, s'arrenca amb el sistema)

    python -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt yt-dlp imageio-ffmpeg
    .venv\Scripts\python -c "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())" > ffmpeg_path.txt

Registra una tasca programada que executi `.venv\Scripts\pythonw.exe server.py` (directori de treball = la carpeta de l'app),
amb activador "En iniciar el sistema". `pythonw` no obre consola; el registre queda a `data\server.log`.

## Seguretat

Pensada per a ús personal. Si l'exposes a internet (p. ex. amb Cloudflare Tunnel), el login protegeix la biblioteca i el codi del QR protegeix la jam.
Les pàgines (`/`, `/jam`) són públiques però no mostren cap dada sense login o sense QR.
