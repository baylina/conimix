# coniMIX

Biblioteca d'àudio personal amb cua compartida ("jam"). Cerca a YouTube, descarrega l'àudio (yt-dlp + ffmpeg)
i només reprodueix cançons descarregades al 100%. Diversos usuaris, i convidats per QR a la jam de cadascú.

- **Usuaris** (`/`): es registren amb correu i contrasenya (o amb Google). Cada compte té les seves cançons
  (`data/songs/<id d'usuari>/`), llistes, cua, jam, nom de l'app i logo. Els comptes nous queden **pendents**
  fins que l'administrador els aprova.
- **Administrador** (`ADMIN_EMAIL`, per defecte baylina@gmail.com): des del perfil → **Usuaris** aprova, bloqueja,
  esborra comptes, dona contrasenyes temporals, fa altres administradors i activa la **IA** per usuari
  (la IA es paga amb la clau de Claude del servidor; per defecte desactivada als nous).
- **Mode sense connexió:** a l'iPhone (app afegida a la pantalla d'inici) les llistes marcades «Desa al mòbil»
  sonen sense connexió, i en tornar la connexió es reconnecta sola i continua la cua pel mateix punt.
- **CarPlay i pantalla de bloqueig:** amb l'iPhone connectat a CarPlay, la música surt pels altaveus del cotxe i la pantalla «Ara sona» del cotxe mostra títol, canal, portada i progrés. Funcionen play/pausa, següent, anterior (torna a començar la cançó) i arrossegar la barra, també des dels botons del volant. coniMIX no surt com a icona pròpia a CarPlay: Apple només ho permet a apps natives.
- Cada usuari: cerca, cua, llistes, descàrregues, ordre, esborrat, «No la vull escoltar més».
- **Convidats** (`/jam`, només amb el QR de l'amfitrió): veuen la cua, cerquen, escolten una prèvia, afegeixen cançons (després de l'actual o al final) i en canvien l'ordre. Amb límit de peticions.
- **Jam privada:** el QR porta un codi secret (`/jam?k=…`) que es canvia per una sessió de convidat (cookie, 12 h). Sense el codi, la jam no mostra res. L'amfitrió pot generar un codi nou o **començar una jam nova** (codi nou + expulsar tots els convidats) des de la finestra del QR.
- **Cerca àmplia:** YouTube + YouTube Music en paral·lel (tolera faltes), amb cerca automàtica a partir de 5 lletres.
- **Enllaços:** es pot enganxar un enllaç de YouTube (vídeo, short o llista) al cercador; amb una llista surten fins a 50 cançons.
- **Resultats detallats:** miniatura gran, títol complet, usuari que l'ha pujat, visualitzacions i data de pujada.
- **Cerca amb IA:** el botó **IA** de cada cercador permet descriure la cançó amb paraules (escrit o dictat amb el micròfon); Claude tria les cançons i es busquen a YouTube. Cal la clau de Claude a `anthropic_api_key.txt`. Dictar necessita https (p. ex. l'adreça pública); amb http es pot fer servir el micròfon del teclat.
- **Descàrregues fallides:** si una descàrrega falla per la connexió, es torna a provar sola (de seguida que torna la connexió). Des de la cua o la llista es poden reintentar a mà o descartar.
- **Interfície mòbil:** llistes amb cercador, gestos de lliscar per reproduir/treure, arrossegar per ordenar i mini-reproductor.
- **Lletres:** el botó de lletra («Ara sona» i la cua) mostra la lletra de la cançó que sona, de [LRCLIB](https://lrclib.net) (gratuït, sense clau), amb l'idioma detectat (anglès, castellà, català…). Si està sincronitzada, la frase que sona s'il·lumina; tocar una frase hi salta, «repeteix» la torna a fer sonar en bucle i hi ha un botó de 5 s enrere (per aprendre idiomes). Si la lletra va desfasada (els videoclips sovint tenen una intro més llarga que la versió d'àlbum), «Sincronitza» i tocar la frase que sona en aquell moment la quadra; els botons ±0,5 s l'afinen. L'ajust es desa per a cada cançó. Si no la troba (o no és la bona) es pot escriure «Artista - Títol». Amb la IA activada, Claude endevina l'artista i el títol dels títols de YouTube confusos. Les lletres obertes queden al mòbil per llegir-les sense connexió.
- **Volum normalitzat:** cada cançó descarregada es normalitza amb ffmpeg (`loudnorm`, −14 LUFS, com Spotify/YouTube) perquè totes sonin igual de fort. Les que ja hi havia abans es normalitzen soles en segon pla en arrencar el servidor.
- La cançó que sona no es pot superar a la cua. Encreuament de volum entre cançons (no a iOS: allà és sense silenci però sense fade).
- Esborrar una cançó d'una llista l'esborra del disc si no és a cap altra llista; esborrar la descàrrega la treu de totes les llistes.

## Execució

Requisits: `python3`, `yt-dlp`, `ffmpeg`.

    SPOTITXELL_PASSWORD='la-teva-contrasenya' ./run.sh        # http://<ip>:8791

Variables d'entorn (o fitxers al costat de `server.py`, útil per executar sense consola):

| Variable | Fitxer alternatiu | Descripció |
|---|---|---|
| `SPOTITXELL_PASSWORD` | `owner_password.txt` | Només en crear els usuaris per primer cop: contrasenya inicial de l'administrador |
| `ADMIN_EMAIL` | | Correu de l'administrador (`baylina@gmail.com`) |
| `GOOGLE_CLIENT_ID` | `google_client_id.txt` | ID de client OAuth (tipus «Aplicació web») de Google Cloud per a «Continua amb Google» (opcional) |
| `PUBLIC_URL` | `public_url.txt` | URL pública per al QR (p. ex. darrere d'un túnel) |
| `PORT` | | Port (8791) |
| `DATA_DIR` | | Base de dades SQLite i MP3 (`./data`) |
| `MAX_DOWNLOADS` | | Descàrregues simultànies (2) |
| `ANTHROPIC_API_KEY` | `anthropic_api_key.txt` | Clau de Claude per a la cerca amb IA (opcional) |
| `AI_MODEL` | | Model de Claude (`claude-sonnet-5-5`) |
| `YTDLP`, `YTDLP_ARGS` | `ffmpeg_path.txt` (Windows) | Ordre de yt-dlp i arguments extra |

Actualitza yt-dlp sovint: YouTube bloqueja versions velles (error 403).

## Windows (en segon pla, s'arrenca amb el sistema)

    python -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt yt-dlp imageio-ffmpeg
    .venv\Scripts\python -c "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())" > ffmpeg_path.txt

Registra una tasca programada que executi `.venv\Scripts\pythonw.exe server.py` (directori de treball = la carpeta de l'app),
amb activador "En iniciar el sistema". `pythonw` no obre consola; el registre queda a `data\server.log`.

## Seguretat

Pensada per a amics i família. Si l'exposes a internet (p. ex. amb Cloudflare Tunnel), cada compte només veu les seves dades,
els comptes nous no poden fer res fins que l'administrador els aprova, i el codi del QR protegeix cada jam.
Les contrasenyes es guarden amb PBKDF2. Les pàgines (`/`, `/jam`) són públiques però no mostren cap dada sense login o sense QR.

### Entrar amb Google
1. A https://console.cloud.google.com → APIs i serveis → Credencials → Crea un **ID de client OAuth**, tipus **Aplicació web**.
2. A «Orígens de JavaScript autoritzats» hi poses l'adreça pública (p. ex. `https://conimix.calcotades.cat`).
3. Copia l'ID de client (acaba en `.apps.googleusercontent.com`) a `google_client_id.txt` i reinicia el servidor.
