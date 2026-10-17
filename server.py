#!/usr/bin/env python3
"""SpotiTxell: personal audio library with a shared play queue ("jam").

Search YouTube, download audio with yt-dlp + ffmpeg, play only fully
downloaded songs, manage playlists. Single user, runs on the local network.
"""
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import shlex
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
DATA = Path(os.environ.get("DATA_DIR", HERE / "data")).expanduser()
SONGS = DATA / "songs"
PORT = int(os.environ.get("PORT", "8791"))


def _from_file(env, filename):
    f = HERE / filename
    if not os.environ.get(env) and f.exists():
        os.environ[env] = f.read_text(encoding="utf-8-sig").strip()


_from_file("SPOTITXELL_PASSWORD", "owner_password.txt")
_from_file("PUBLIC_URL", "public_url.txt")
if os.name == "nt":  # Windows deploy: yt-dlp lives in the venv, ffmpeg path is stored by the installer
    _from_file("FFMPEG", "ffmpeg_path.txt")
    _venv_ytdlp = Path(sys.executable).parent / "yt-dlp.exe"
    if _venv_ytdlp.exists() and not os.environ.get("YTDLP"):
        os.environ["YTDLP"] = str(_venv_ytdlp)
    if os.environ.get("FFMPEG") and not os.environ.get("YTDLP_ARGS"):
        os.environ["YTDLP_ARGS"] = f"--js-runtimes node --ffmpeg-location {os.environ['FFMPEG']}"
NO_WINDOW = {"creationflags": 0x08000000, "stdin": subprocess.DEVNULL} if os.name == "nt" else {"stdin": subprocess.DEVNULL}
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
YTDLP = [os.environ["YTDLP"]] if os.environ.get("YTDLP") else ["yt-dlp"]
# extra yt-dlp flags, e.g. "--js-runtimes node --ffmpeg-location C:\\path\\ffmpeg.exe"
YTDLP_ARGS = shlex.split(os.environ.get("YTDLP_ARGS", ""), posix=os.name != "nt")
PASSWORD = os.environ.get("SPOTITXELL_PASSWORD", "")
SESSION_DAYS = 30
MAX_DOWNLOADS = int(os.environ.get("MAX_DOWNLOADS", "2"))
ID_RE = re.compile(r"^[\w-]{11}$")
PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")
mimetypes.add_type("application/manifest+json", ".webmanifest")
MEDIA_RE = re.compile(r"^([\w-]{11})\.(mp3|jpg)$")

SONGS.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DATA / "spotitxell.db", check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
db.execute("PRAGMA foreign_keys=ON")
db.executescript("""
CREATE TABLE IF NOT EXISTS songs(
  id TEXT PRIMARY KEY, title TEXT, channel TEXT, duration REAL,
  status TEXT, progress REAL DEFAULT 0, error TEXT, created REAL);
CREATE TABLE IF NOT EXISTS playlists(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);
CREATE TABLE IF NOT EXISTS playlist_songs(
  playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
  song_id TEXT REFERENCES songs(id) ON DELETE CASCADE,
  pos INTEGER, PRIMARY KEY(playlist_id, song_id));
CREATE TABLE IF NOT EXISTS queue(
  qid INTEGER PRIMARY KEY AUTOINCREMENT,
  song_id TEXT REFERENCES songs(id) ON DELETE CASCADE, pos INTEGER);
CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, expires REAL);
""")
db.execute("UPDATE songs SET status='error', error='Interromput' WHERE status='downloading'")
lock = threading.RLock()
dl_sem = threading.Semaphore(MAX_DOWNLOADS)
hits: dict = {}


def rate_ok(key, limit, window):
    """Sliding-window limiter. Returns False when `key` exceeded `limit` hits in `window` seconds."""
    now = time.time()
    with lock:
        recent = [t for t in hits.get(key, []) if now - t < window]
        if len(recent) >= limit:
            hits[key] = recent
            return False
        recent.append(now)
        hits[key] = recent
        return True


def new_session(days):
    token = secrets.token_urlsafe(32)
    with lock:
        db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
        db.execute("INSERT INTO sessions VALUES(?,?)",
                   (hashlib.sha256(token.encode()).hexdigest(), time.time() + days * 86400))
    return token


def valid_session(token):
    if not token:
        return False
    with lock:
        r = db.execute("SELECT expires FROM sessions WHERE token_hash=?",
                       (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    return bool(r and r["expires"] > time.time())


class HttpError(Exception):
    def __init__(self, status, msg):
        self.status, self.msg = status, msg


# ---------- queue ----------

def get_current():
    r = db.execute("SELECT v FROM state WHERE k='current'").fetchone()
    cur = int(r["v"]) if r and r["v"] else None
    if cur is not None and not db.execute("SELECT 1 FROM queue WHERE qid=?", (cur,)).fetchone():
        return None
    return cur


def set_current(qid):
    db.execute("INSERT INTO state(k,v) VALUES('current',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (str(qid) if qid else "",))


def q_items():
    return [dict(r) for r in db.execute(
        "SELECT q.qid, q.song_id, s.status FROM queue q JOIN songs s ON s.id=q.song_id ORDER BY q.pos, q.qid")]


def renumber(qids):
    for i, qid in enumerate(qids):
        db.execute("UPDATE queue SET pos=? WHERE qid=?", (i, qid))


def ensure_current():
    """If nothing is playing, start the first fully downloaded queue item."""
    if get_current() is not None:
        return
    items = q_items()
    for k, it in enumerate(items):
        if it["status"] == "ready":
            if k:  # items skipped because they were not downloaded yet wait right after the one that starts
                ids = [i["qid"] for i in items]
                renumber([ids[k]] + ids[:k] + ids[k + 1:])
            set_current(it["qid"])
            return
    set_current(None)


def add_to_queue(song_id, mode):
    song = db.execute("SELECT status FROM songs WHERE id=?", (song_id,)).fetchone()
    if not song:
        raise HttpError(404, "Cançó desconeguda")
    ready = song["status"] == "ready"
    items = q_items()
    ids = [i["qid"] for i in items]
    cur = get_current()
    qid = db.execute("INSERT INTO queue(song_id,pos) VALUES(?,?)", (song_id, len(ids))).lastrowid
    if mode == "end":
        ids.append(qid)
    elif mode == "now" and ready:
        idx = ids.index(cur) if cur in ids else 0
        ids.insert(idx, qid)
        set_current(qid)
    else:  # "next", or "now" for a song that is not downloaded yet
        idx = ids.index(cur) + 1 if cur in ids else 0
        ids.insert(idx, qid)
    renumber(ids)
    ensure_current()
    return qid


def advance(from_qid=None):
    """Finish the current item and move to the next fully downloaded one.
    Items that are not ready yet are skipped and parked right after the new current."""
    cur = get_current()
    if cur is None:
        ensure_current()
        return
    if from_qid is not None and from_qid != cur:
        return
    ids = [i["qid"] for i in q_items()]
    status = {i["qid"]: i["status"] for i in q_items()}
    idx = ids.index(cur)
    before, after = ids[:idx], ids[idx + 1:]
    db.execute("DELETE FROM queue WHERE qid=?", (cur,))
    nxt = next((q for q in after if status[q] == "ready"), None)
    if nxt is None:
        renumber(before + after)
        set_current(None)
        ensure_current()
        return
    k = after.index(nxt)
    skipped, rest = after[:k], after[k + 1:]
    renumber(before + [nxt] + skipped + rest)
    set_current(nxt)


def delete_song(song_id):
    cur = get_current()
    if cur is not None:
        r = db.execute("SELECT song_id FROM queue WHERE qid=?", (cur,)).fetchone()
        if r and r["song_id"] == song_id:
            advance(cur)
    db.execute("DELETE FROM songs WHERE id=?", (song_id,))  # cascades to playlists and queue
    for ext in ("mp3", "jpg"):
        (SONGS / f"{song_id}.{ext}").unlink(missing_ok=True)
    ensure_current()


# ---------- downloads ----------

def download_worker(song_id):
    cmd = [
        *YTDLP, *YTDLP_ARGS, "--no-update", "--newline", "--no-playlist",
        "-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "0",
        "--embed-metadata", "--write-thumbnail", "--convert-thumbnails", "jpg",
        "-o", str(SONGS / "%(id)s.%(ext)s"),
        f"https://www.youtube.com/watch?v={song_id}",
    ]
    last, pct = "", -1
    dl_sem.acquire()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **NO_WINDOW)
        for line in proc.stdout:
            last = line.strip() or last
            m = PROGRESS_RE.search(line)
            if m and int(float(m.group(1))) != pct:
                pct = int(float(m.group(1)))
                with lock:
                    db.execute("UPDATE songs SET progress=? WHERE id=?", (min(pct, 99), song_id))
        proc.wait()
        ok = proc.returncode == 0 and (SONGS / f"{song_id}.mp3").exists()
    except Exception as e:  # noqa: BLE001
        ok, last = False, str(e)
    finally:
        dl_sem.release()
    with lock:
        if db.execute("SELECT 1 FROM songs WHERE id=?", (song_id,)).fetchone():
            if ok:
                db.execute("UPDATE songs SET status='ready', progress=100, error=NULL WHERE id=?", (song_id,))
                ensure_current()
            else:
                db.execute("UPDATE songs SET status='error', error=? WHERE id=?", (last[-200:], song_id))
        else:
            for ext in ("mp3", "jpg"):
                (SONGS / f"{song_id}.{ext}").unlink(missing_ok=True)


def request_song(meta):
    sid = meta.get("video_id", "")
    if not ID_RE.match(sid):
        raise HttpError(400, "Identificador de vídeo no vàlid")
    row = db.execute("SELECT status FROM songs WHERE id=?", (sid,)).fetchone()
    if row is None or row["status"] == "error":
        db.execute(
            "INSERT INTO songs(id,title,channel,duration,status,progress,created) VALUES(?,?,?,?,?,0,?) "
            "ON CONFLICT(id) DO UPDATE SET status='downloading', progress=0, error=NULL",
            (sid, (meta.get("title") or sid)[:200], (meta.get("channel") or "")[:100],
             meta.get("duration"), "downloading", time.time()))
        threading.Thread(target=download_worker, args=(sid,), daemon=True).start()
    return sid


def run_search(query, limit=12):
    out = subprocess.run([*YTDLP, *YTDLP_ARGS, "--no-update", "--flat-playlist", "--dump-json", f"ytsearch{limit}:{query}"],
                         capture_output=True, text=True, timeout=60, **NO_WINDOW)
    res = []
    for line in out.stdout.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("id") and ID_RE.match(d["id"]):
            res.append({"id": d["id"], "title": d.get("title"), "channel": d.get("channel") or d.get("uploader"),
                        "duration": d.get("duration"),
                        "thumbnail": f"https://i.ytimg.com/vi/{d['id']}/mqdefault.jpg"})
    if not res and out.returncode != 0:
        raise HttpError(502, (out.stderr.strip().splitlines() or ["Error de cerca"])[-1])
    return res


# ---------- state ----------

_lan = {}


def lan_ip():
    if "ip" not in _lan:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            _lan["ip"] = s.getsockname()[0]
        except OSError:
            _lan["ip"] = "127.0.0.1"
        finally:
            s.close()
    return _lan["ip"]


def jam_url(host_header):
    if PUBLIC_URL:
        return PUBLIC_URL + "/jam"
    name = (host_header or "").split(":")[0]
    host = host_header if name not in ("", "localhost", "127.0.0.1") else f"{lan_ip()}:{PORT}"
    return f"http://{host}/jam"


def full_state(host_header, owner=True):
    songs = {r["id"]: {"id": r["id"], "title": r["title"], "channel": r["channel"], "duration": r["duration"],
                       "status": r["status"], "progress": r["progress"], "error": r["error"]}
             for r in db.execute("SELECT * FROM songs ORDER BY created DESC")}
    queue = q_items()
    if not owner:  # guests only see what is in the shared queue, not the private library
        inq = {q["song_id"] for q in queue}
        songs = {k: v for k, v in songs.items() if k in inq}
        return {"songs": songs, "playlists": [], "queue": queue, "current": get_current(),
                "jam_url": jam_url(host_header), "owner": False}
    playlists = []
    for p in db.execute("SELECT * FROM playlists ORDER BY name COLLATE NOCASE"):
        sids = [r["song_id"] for r in db.execute(
            "SELECT song_id FROM playlist_songs WHERE playlist_id=? ORDER BY pos", (p["id"],))]
        playlists.append({"id": p["id"], "name": p["name"], "songs": sids})
    return {"songs": songs, "playlists": playlists, "queue": queue, "current": get_current(),
            "jam_url": jam_url(host_header), "owner": True}


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "SpotiTxell/1.0"

    def log_message(self, *a):
        pass

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, path, cache=False):
        size = path.stat().st_size
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        start, end, status = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if cache:
            self.send_header("Cache-Control", "public, max-age=86400")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def guard(self, method):
        try:
            return self.route(method)
        except HttpError as e:
            self.send_json({"error": e.msg}, e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": str(e)}, 500)

    do_GET = lambda self: self.guard("GET")  # noqa: E731
    do_POST = lambda self: self.guard("POST")  # noqa: E731
    do_DELETE = lambda self: self.guard("DELETE")  # noqa: E731
    do_PATCH = lambda self: self.guard("PATCH")  # noqa: E731

    # -- auth helpers ----------------------------------------------
    def peer(self):
        """Client IP; forwarded headers are trusted only from the local tunnel (cloudflared)."""
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1"):
            return (self.headers.get("CF-Connecting-IP") or self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or ip)
        return ip

    def secure(self):
        return self.client_address[0] in ("127.0.0.1", "::1") and self.headers.get("X-Forwarded-Proto") == "https"

    def session_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "st_session":
                return v
        return ""

    def cookie(self, value, max_age):
        flags = f"st_session={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
        return flags + ("; Secure" if self.secure() else "")

    def route(self, method):
        url = urlparse(self.path)
        path = url.path
        owner = valid_session(self.session_token())
        ip = self.peer()

        if method in ("POST", "PATCH") and not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise HttpError(415, "Content-Type no vàlid")  # blocks cross-site form posts

        if path == "/api/login" and method == "POST":
            if not rate_ok(("login", ip), 5, 600):
                raise HttpError(429, "Massa intents. Torna-ho a provar d'aquí una estona.")
            d = self.body()
            if not PASSWORD or not hmac.compare_digest(str(d.get("password", "")).encode(), PASSWORD.encode()):
                time.sleep(1)
                raise HttpError(401, "Contrasenya incorrecta")
            remember = bool(d.get("remember"))
            token = new_session(SESSION_DAYS if remember else 1)
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", self.cookie(token, SESSION_DAYS * 86400 if remember else 86400))
            self.end_headers()
            return self.wfile.write(body)
        if path == "/api/logout" and method == "POST":
            tok = self.session_token()
            if tok:
                with lock:
                    db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(tok.encode()).hexdigest(),))
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", self.cookie("", 0))
            self.end_headers()
            return self.wfile.write(body)
        if path == "/api/me":
            return self.send_json({"owner": owner})

        # Anyone may: open the pages, see the shared queue, search, and request songs (/api/request).
        # Everything else needs the owner session.
        public = (method == "GET" and (path in ("/", "/jam", "/api/state", "/api/search") or path.startswith("/static/"))) \
            or (method == "POST" and path in ("/api/request", "/api/queue/order")) \
            or (method == "GET" and MEDIA_RE.match(path[7:] if path.startswith("/media/") else "") and path.endswith(".jpg"))
        if not public and not owner:
            raise HttpError(401, "Cal iniciar sessió")
        guest = not owner

        if method == "GET":
            if path in ("/", "/jam"):
                return self.serve_file(STATIC / "index.html")
            if path.startswith("/static/"):
                p = (STATIC / path[8:]).resolve()
                if p.parent == STATIC.resolve() and p.is_file():
                    return self.serve_file(p, cache=True)
                raise HttpError(404, "no trobat")
            m = MEDIA_RE.match(path[7:]) if path.startswith("/media/") else None
            if m:
                p = SONGS / path[7:]
                if m.group(2) == "mp3":
                    r = db.execute("SELECT status FROM songs WHERE id=?", (m.group(1),)).fetchone()
                    if not r or r["status"] != "ready":
                        raise HttpError(404, "Cançó no disponible")
                if p.exists():
                    return self.serve_file(p, cache=m.group(2) == "jpg")
                raise HttpError(404, "no trobat")
            if path == "/api/state":
                with lock:
                    return self.send_json(full_state(self.headers.get("Host"), owner))
            if path == "/api/search":
                q = parse_qs(url.query).get("q", [""])[0].strip()[:150]
                if guest and not rate_ok(("search", ip), 15, 300):
                    raise HttpError(429, "Massa cerques seguides, espera una mica")
                return self.send_json(run_search(q) if q else [])
            if path == "/api/qr.svg":
                import qrcode
                import qrcode.image.svg
                u = parse_qs(url.query).get("u", [""])[0]
                buf = io.BytesIO()
                qrcode.make(u, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2).save(buf)
                data = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            raise HttpError(404, "no trobat")

        # mutations -------------------------------------------------
        data = self.body() if method in ("POST", "PATCH") else {}

        with lock:
            if path == "/api/request":
                mode = data.get("mode", "end")
                if mode not in ("now", "next", "end"):
                    raise HttpError(400, "mode no vàlid")
                if guest:
                    if mode == "now":
                        mode = "next"
                    if not rate_ok(("req", ip), 8, 600):
                        raise HttpError(429, "Massa peticions seguides, espera una mica")
                sid = request_song(data)
                add_to_queue(sid, mode)
                return self.send_json({"ok": True})
            if path == "/api/queue/add":
                add_to_queue(data.get("song_id", ""), data.get("mode", "end"))
                return self.send_json({"ok": True})
            if path == "/api/queue/next":
                advance(data.get("from"))
                return self.send_json({"ok": True})
            if path == "/api/queue/play":
                r = db.execute("SELECT s.status FROM queue q JOIN songs s ON s.id=q.song_id WHERE q.qid=?",
                               (data.get("qid"),)).fetchone()
                if not r or r["status"] != "ready":
                    raise HttpError(409, "La cançó encara no s'ha descarregat")
                set_current(data["qid"])
                return self.send_json({"ok": True})
            if path == "/api/queue/order":
                if guest and not rate_ok(("order", ip), 60, 60):
                    raise HttpError(429, "Massa canvis seguits, espera una mica")
                ids = data.get("ids", [])
                old = [i["qid"] for i in q_items()]
                if sorted(ids) != sorted(old):
                    raise HttpError(409, "La cua ha canviat, torna-ho a provar")
                cur = get_current()
                if cur in old and ids[:old.index(cur) + 1] != old[:old.index(cur) + 1]:
                    raise HttpError(409, "No es pot posar cap cançó davant de la que està sonant")
                renumber(ids)
                return self.send_json({"ok": True})
            if path == "/api/queue/remove":
                qid = data.get("qid")
                if qid == get_current():
                    advance(qid)
                else:
                    db.execute("DELETE FROM queue WHERE qid=?", (qid,))
                return self.send_json({"ok": True})
            if path == "/api/queue/clear":
                db.execute("DELETE FROM queue")
                set_current(None)
                return self.send_json({"ok": True})
            if path == "/api/queue/load":
                pl = data.get("playlist_id")
                sids = [r["song_id"] for r in db.execute(
                    "SELECT song_id FROM playlist_songs WHERE playlist_id=? ORDER BY pos", (pl,))]
                if data.get("mode", "replace") == "replace":
                    db.execute("DELETE FROM queue")
                    set_current(None)
                for sid in sids:
                    add_to_queue(sid, "end")
                return self.send_json({"ok": True})

            if path == "/api/playlists" and method == "POST":
                name = (data.get("name") or "").strip()[:80]
                if not name:
                    raise HttpError(400, "Cal un nom")
                pid = db.execute("INSERT INTO playlists(name) VALUES(?)", (name,)).lastrowid
                return self.send_json({"id": pid})
            m = re.match(r"^/api/playlists/(\d+)$", path)
            if m:
                pid = int(m.group(1))
                if method == "PATCH":
                    db.execute("UPDATE playlists SET name=? WHERE id=?", ((data.get("name") or "").strip()[:80], pid))
                elif method == "DELETE":
                    db.execute("DELETE FROM playlists WHERE id=?", (pid,))
                return self.send_json({"ok": True})
            m = re.match(r"^/api/playlists/(\d+)/request$", path)
            if m and method == "POST":
                pid = int(m.group(1))
                if not db.execute("SELECT 1 FROM playlists WHERE id=?", (pid,)).fetchone():
                    raise HttpError(404, "Llista desconeguda")
                existing = db.execute("SELECT status FROM songs WHERE id=?", (data.get("video_id", ""),)).fetchone()
                sid = request_song(data)  # downloads only if we do not have it yet
                already = bool(db.execute("SELECT 1 FROM playlist_songs WHERE playlist_id=? AND song_id=?",
                                          (pid, sid)).fetchone())
                if not already:
                    nxt = db.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM playlist_songs WHERE playlist_id=?",
                                     (pid,)).fetchone()[0]
                    db.execute("INSERT INTO playlist_songs(playlist_id,song_id,pos) VALUES(?,?,?)", (pid, sid, nxt))
                return self.send_json({"ok": True, "already": already,
                                       "had_download": bool(existing and existing["status"] == "ready")})
            m = re.match(r"^/api/playlists/(\d+)/songs$", path)
            if m and method == "POST":
                pid = int(m.group(1))
                for sid in data.get("song_ids", []):
                    s = db.execute("SELECT status FROM songs WHERE id=?", (sid,)).fetchone()
                    if not s or s["status"] == "error":
                        raise HttpError(409, "Aquesta cançó no s'ha pogut descarregar")
                    nxt = db.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM playlist_songs WHERE playlist_id=?",
                                     (pid,)).fetchone()[0]
                    db.execute("INSERT OR IGNORE INTO playlist_songs(playlist_id,song_id,pos) VALUES(?,?,?)",
                               (pid, sid, nxt))
                return self.send_json({"ok": True})
            m = re.match(r"^/api/playlists/(\d+)/songs/([\w-]{11})$", path)
            if m and method == "DELETE":
                pid, sid = int(m.group(1)), m.group(2)
                db.execute("DELETE FROM playlist_songs WHERE playlist_id=? AND song_id=?", (pid, sid))
                still = db.execute("SELECT 1 FROM playlist_songs WHERE song_id=?", (sid,)).fetchone()
                if not still:
                    delete_song(sid)  # in no playlist any more: remove the download too
                return self.send_json({"ok": True})
            m = re.match(r"^/api/playlists/(\d+)/order$", path)
            if m and method == "POST":
                pid = int(m.group(1))
                ids = data.get("ids", [])
                have = [r["song_id"] for r in db.execute("SELECT song_id FROM playlist_songs WHERE playlist_id=?", (pid,))]
                if sorted(ids) != sorted(have):
                    raise HttpError(409, "La llista ha canviat, torna-ho a provar")
                for i, sid in enumerate(ids):
                    db.execute("UPDATE playlist_songs SET pos=? WHERE playlist_id=? AND song_id=?", (i, pid, sid))
                return self.send_json({"ok": True})
            m = re.match(r"^/api/songs/([\w-]{11})$", path)
            if m and method == "DELETE":
                delete_song(m.group(1))
                return self.send_json({"ok": True})
        raise HttpError(404, "no trobat")


if __name__ == "__main__":
    if len(PASSWORD) < 8:
        raise SystemExit("Defineix SPOTITXELL_PASSWORD (mínim 8 caràcters) abans d'arrencar.")
    if sys.stdout is None or sys.stderr is None:  # started with pythonw: no console, log to a file
        _log = open(DATA / "server.log", "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = _log
    print(f"Dades: {DATA}\nPlayer: http://{lan_ip()}:{PORT}   Jam: {jam_url(None)}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
