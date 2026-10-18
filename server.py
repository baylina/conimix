#!/usr/bin/env python3
"""coniMIX: personal audio library with a shared play queue ("jam").

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
import unicodedata
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
CREATE TABLE IF NOT EXISTS guest_sessions(token_hash TEXT PRIMARY KEY, epoch INTEGER, expires REAL);
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


def fail_count(key, window):
    now = time.time()
    with lock:
        hits[key] = [t for t in hits.get(key, []) if now - t < window]
        return len(hits[key]), (window - (now - hits[key][0]) if hits[key] else 0)


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


def _yt_list(target, limit):
    out = subprocess.run([*YTDLP, *YTDLP_ARGS, "--no-update", "--flat-playlist", "--dump-json", "--playlist-end", str(limit), target],
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
    return res, out


_search_cache: dict = {}


def run_search(query, limit=14):
    """Broad search: regular YouTube (has artist and duration) plus YouTube Music songs
    (official tracks, tolerant to typos). Both run in parallel and are merged without duplicates."""
    key = " ".join(query.lower().split())
    hit = _search_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    got = {}

    def job(name, target, n):
        try:
            got[name] = _yt_list(target, n)
        except Exception as e:  # noqa: BLE001
            got[name] = ([], e)
    from urllib.parse import quote_plus
    threads = [threading.Thread(target=job, args=("yt", f"ytsearch{limit}:{query}", limit)),
               threading.Thread(target=job, args=("ytm", f"https://music.youtube.com/search?q={quote_plus(query)}#songs", 8))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    yt, ytm = got.get("yt", ([], None))[0], got.get("ytm", ([], None))[0]
    for r in ytm:
        r["channel"] = r["channel"] or "YouTube Music"
        r["source"] = "ytm"
    merged, seen = [], set()
    for r in ytm[:4] + yt + ytm[4:]:   # a few official tracks first, then the rest
        if r["id"] not in seen:
            seen.add(r["id"])
            merged.append(r)
    if not merged:
        err = got.get("yt", (None, None))[1]
        msg = getattr(err, "stderr", "") or ""
        raise HttpError(502, (msg.strip().splitlines() or ["Error de cerca"])[-1])
    if len(_search_cache) > 300:
        _search_cache.clear()
    _search_cache[key] = (time.time(), merged)
    return merged


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


GUEST_HOURS = 12


def state_get(k):
    r = db.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


def state_set(k, v):
    db.execute("INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def jam_code():
    """(code, epoch). The code goes in the QR; the epoch invalidates every guest session when bumped."""
    with lock:
        code, epoch = state_get("jam_code"), state_get("jam_epoch")
        if not code:
            code = secrets.token_urlsafe(9)
            state_set("jam_code", code)
        if not epoch:
            epoch = "1"
            state_set("jam_epoch", epoch)
    return code, int(epoch)


def jam_rotate(kick):
    """New QR code (old photos of the QR stop working). kick=True also ends every guest session."""
    with lock:
        state_set("jam_code", secrets.token_urlsafe(9))
        if kick:   # a new jam: everybody from the previous party is out
            state_set("jam_epoch", jam_code()[1] + 1)
            state_set("jam_started", time.time())
            db.execute("DELETE FROM guest_sessions")


def new_guest():
    token = secrets.token_urlsafe(32)
    with lock:
        db.execute("DELETE FROM guest_sessions WHERE expires<?", (time.time(),))
        db.execute("INSERT INTO guest_sessions VALUES(?,?,?)",
                   (hashlib.sha256(token.encode()).hexdigest(), jam_code()[1], time.time() + GUEST_HOURS * 3600))
    return token


def valid_guest(token):
    if not token:
        return False
    with lock:
        r = db.execute("SELECT epoch, expires FROM guest_sessions WHERE token_hash=?",
                       (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    return bool(r and r["expires"] > time.time() and r["epoch"] == jam_code()[1])


def guest_count():
    with lock:
        return db.execute("SELECT COUNT(*) FROM guest_sessions WHERE epoch=? AND expires>?",
                          (jam_code()[1], time.time())).fetchone()[0]


def base_url(host_header):
    if PUBLIC_URL:
        return PUBLIC_URL
    name = (host_header or "").split(":")[0]
    host = host_header if name not in ("", "localhost", "127.0.0.1") else f"{lan_ip()}:{PORT}"
    return f"http://{host}"


def jam_url(host_header):
    return f"{base_url(host_header)}/jam?k={jam_code()[0]}"


def full_state(host_header, owner=True):
    songs = {r["id"]: {"id": r["id"], "title": r["title"], "channel": r["channel"], "duration": r["duration"],
                       "status": r["status"], "progress": r["progress"], "error": r["error"]}
             for r in db.execute("SELECT * FROM songs ORDER BY created DESC")}
    queue = q_items()
    if not owner:  # guests only see what is in the shared queue, not the private library
        inq = {q["song_id"] for q in queue}
        songs = {k: v for k, v in songs.items() if k in inq}
        return {"songs": songs, "playlists": [], "queue": queue, "current": get_current(),
                "owner": False}   # never the jam code: only the QR carries it
    playlists = []
    for p in db.execute("SELECT * FROM playlists ORDER BY name COLLATE NOCASE"):
        sids = [r["song_id"] for r in db.execute(
            "SELECT song_id FROM playlist_songs WHERE playlist_id=? ORDER BY pos", (p["id"],))]
        playlists.append({"id": p["id"], "name": p["name"], "songs": sids})
    return {"songs": songs, "playlists": playlists, "queue": queue, "current": get_current(),
            "jam_url": jam_url(host_header), "guests": guest_count(),
            "jam_started": float(state_get("jam_started") or 0), "owner": True}


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "coniMIX/1.0"

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

    def cookie_val(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def session_token(self):
        return self.cookie_val("st_session")

    def cookie(self, value, max_age, name="st_session"):
        flags = f"{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
        return flags + ("; Secure" if self.secure() else "")

    def route(self, method):
        url = urlparse(self.path)
        path = url.path
        owner = valid_session(self.session_token())
        ip = self.peer()

        if method in ("POST", "PATCH") and not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise HttpError(415, "Content-Type no vàlid")  # blocks cross-site form posts

        if path == "/api/login" and method == "POST":
            n, wait = fail_count(("login", ip), 600)   # only wrong passwords count
            if n >= 8:
                raise HttpError(429, f"Massa intents fallits. Torna-ho a provar d'aquí {int(wait // 60) + 1} min.")
            d = self.body()
            given = str(d.get("password", ""))
            norm = lambda x: unicodedata.normalize("NFKC", x).strip()   # tolerate stray spaces / composed characters from phone keyboards
            if not PASSWORD or not hmac.compare_digest(norm(given).encode(), norm(PASSWORD).encode()):
                with lock:
                    hits.setdefault(("login", ip), []).append(time.time())
                # diagnostics only: lengths and a case hint, never the password itself
                print(f"login failed from {ip} ({n + 1} recent) typed_len={len(given)} expected_len={len(norm(PASSWORD))} "
                      f"differs_only_by_case={norm(given).casefold() == norm(PASSWORD).casefold()} ua={(self.headers.get('User-Agent') or '')[:60]!r}", flush=True)
                time.sleep(1)
                raise HttpError(401, "Contrasenya incorrecta")
            with lock:
                hits.pop(("login", ip), None)
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
        in_jam = owner or valid_guest(self.cookie_val("cm_guest"))
        if path == "/api/me":
            return self.send_json({"owner": owner, "guest": in_jam})
        if path == "/api/jam/join" and method == "POST":   # the QR code exchanges into a guest session
            n, wait = fail_count(("join", ip), 600)
            if n >= 20:
                raise HttpError(429, "Massa intents. Torna-ho a provar d'aquí una estona.")
            code = str(self.body().get("k", ""))
            if not hmac.compare_digest(code.encode(), jam_code()[0].encode()):
                with lock:
                    hits.setdefault(("join", ip), []).append(time.time())
                raise HttpError(403, "Aquest codi ja no és vàlid. Torna a escanejar el QR de la festa.")
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", self.cookie(new_guest(), GUEST_HOURS * 3600, "cm_guest"))
            self.end_headers()
            return self.wfile.write(body)

        # Pages and static files are public. Jam guests (who scanned the current QR) may see the shared
        # queue, search, preview, request songs and reorder the queue. Everything else is owner only.
        if not ((method == "GET" and (path in ("/", "/jam") or path.startswith("/static/")))):
            jam_ok = (method == "GET" and (path in ("/api/state", "/api/search", "/api/songs") or path.startswith("/media/"))) \
                or (method == "POST" and path in ("/api/request", "/api/queue/order", "/api/prefetch"))
            if not owner and not (jam_ok and in_jam):
                raise HttpError(401, "Cal escanejar el codi QR de la festa" if jam_ok else "Cal iniciar sessió")
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
                if guest and not rate_ok(("search", ip), 40, 300):
                    raise HttpError(429, "Massa cerques seguides, espera una mica")
                return self.send_json(run_search(q) if q else [])
            if path == "/api/songs":   # download status of a few songs (search results)
                ids = [i for i in parse_qs(url.query).get("ids", [""])[0].split(",") if ID_RE.match(i)][:40]
                with lock:
                    rows = db.execute(f"SELECT id,status,progress FROM songs WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall() if ids else []
                return self.send_json({r["id"]: {"status": r["status"], "progress": r["progress"]} for r in rows})
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
            if path == "/api/jam/rotate":   # owner: new QR code; optionally expel every guest
                jam_rotate(bool(data.get("kick")))
                return self.send_json({"ok": True})
            if path == "/api/prefetch":   # download only (for a preview), without queueing
                if guest and not rate_ok(("pre", ip), 15, 600):
                    raise HttpError(429, "Massa descàrregues seguides, espera una mica")
                sid = request_song(data)
                return self.send_json({"ok": True, "status": db.execute("SELECT status FROM songs WHERE id=?", (sid,)).fetchone()["status"]})
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
                qid, cur = data["qid"], get_current()
                if cur is not None and cur != qid:
                    db.execute("DELETE FROM queue WHERE qid=?", (cur,))
                renumber([qid] + [i["qid"] for i in q_items() if i["qid"] != qid])
                set_current(qid)
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
    print(f"coniMIX · dades: {DATA}\nAmfitrió: http://{lan_ip()}:{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
