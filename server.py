#!/usr/bin/env python3
"""coniMIX: personal audio library with a shared play queue ("jam").

Search YouTube, download audio with yt-dlp + ffmpeg, play only fully
downloaded songs, manage playlists. Several users (email/password or Google), each with
their own songs, playlists, queue and jam; the admin approves and manages the others.
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
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

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
_from_file("ANTHROPIC_API_KEY", "anthropic_api_key.txt")
_from_file("GOOGLE_CLIENT_ID", "google_client_id.txt")
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
PASSWORD = os.environ.get("SPOTITXELL_PASSWORD", "")   # only used once: the admin's password when the users are created
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "baylina@gmail.com").strip().lower()
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
LOGOS = DATA / "logos"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SESSION_DAYS = 30
MAX_DOWNLOADS = int(os.environ.get("MAX_DOWNLOADS", "2"))
ID_RE = re.compile(r"^[\w-]{11}$")
PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")
mimetypes.add_type("application/manifest+json", ".webmanifest")
MEDIA_RE = re.compile(r"^([\w-]{11})\.(mp3|jpg)$")
# yt-dlp errors that will not go away by trying again (anything else is treated as a network hiccup)
PERMANENT_RE = re.compile(r"Video unavailable|Private video|has been removed|copyright|confirm your age|members[- ]only"
                          r"|not available in your country|This video is not available|account .* terminated", re.I)
RETRY_DELAYS = [15, 45, 120, 300, 600]   # seconds between automatic retries while online
MAX_TRIES = 12   # failed attempts while online before giving up (time offline does not count)
FFMPEG = os.environ.get("FFMPEG") or "ffmpeg"
LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"   # same loudness target as Spotify / YouTube
YT_URL_RE = re.compile(r"(?:https?://)?(?:[\w-]+\.)?(?:youtube\.com|youtube-nocookie\.com|youtu\.be)/\S*", re.I)

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
if "next_try" not in {r["name"] for r in db.execute("PRAGMA table_info(songs)")}:
    db.execute("ALTER TABLE songs ADD COLUMN tries INTEGER DEFAULT 0")
    db.execute("ALTER TABLE songs ADD COLUMN next_try REAL")
    db.execute("UPDATE songs SET next_try=0 WHERE status='error'")   # failures from before retries existed get another go
if "norm" not in {r["name"] for r in db.execute("PRAGMA table_info(songs)")}:
    db.execute("ALTER TABLE songs ADD COLUMN norm INTEGER DEFAULT 0")   # 1 normalized, -1 failed, 0 pending
db.execute("""CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE COLLATE NOCASE, pw TEXT, google_sub TEXT UNIQUE,
  name TEXT, app_name TEXT, logo TEXT, role TEXT DEFAULT 'user', status TEXT DEFAULT 'pending', ai INTEGER DEFAULT 0,
  jam_code TEXT, jam_epoch INTEGER DEFAULT 1, jam_started REAL, current INTEGER, created REAL, last_seen REAL)""")
if "xfade" not in {r["name"] for r in db.execute("PRAGMA table_info(users)")}:
    db.execute("ALTER TABLE users ADD COLUMN xfade REAL DEFAULT 4")   # seconds of crossfade between songs, 0 = off


def hash_pw(pw):
    salt = secrets.token_hex(16)
    return f"pbkdf2${200000}${salt}${hashlib.pbkdf2_hmac('sha256', norm_pw(pw).encode(), salt.encode(), 200000).hex()}"


def check_pw(pw, stored):
    try:
        _, n, salt, h = (stored or "").split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hashlib.pbkdf2_hmac("sha256", norm_pw(pw).encode(), salt.encode(), int(n)).hex(), h)


def norm_pw(x):   # tolerate stray spaces / composed characters from phone keyboards
    return unicodedata.normalize("NFKC", str(x)).strip()


def udir(uid):
    """Each user's songs live in their own folder (data/songs/<user id>/)."""
    d = SONGS / str(uid)
    d.mkdir(exist_ok=True)
    return d


def migrate_to_users():
    """Single user -> several users: everything that existed becomes the admin's."""
    if "user_id" in {r["name"] for r in db.execute("PRAGMA table_info(songs)")}:
        return
    admin = db.execute("SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,)).fetchone()
    if admin:
        aid = admin["id"]
    else:
        st = {r["k"]: r["v"] for r in db.execute("SELECT k, v FROM state")}
        aid = db.execute("INSERT INTO users(email,pw,name,role,status,ai,jam_code,jam_epoch,jam_started,current,created) "
                         "VALUES(?,?,?,'admin','active',1,?,?,?,?,?)",
                         (ADMIN_EMAIL, hash_pw(PASSWORD) if PASSWORD else None, ADMIN_EMAIL.split("@")[0],
                          st.get("jam_code"), int(st.get("jam_epoch") or 1), float(st.get("jam_started") or 0) or None,
                          int(st["current"]) if st.get("current") else None, time.time())).lastrowid
    db.execute("PRAGMA foreign_keys=OFF")
    db.execute("BEGIN")
    for t in ("songs", "playlists", "playlist_songs", "queue", "sessions", "guest_sessions"):
        db.execute(f"ALTER TABLE {t} RENAME TO {t}_old")
    for sql in ("""CREATE TABLE songs(
      user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, id TEXT, title TEXT, channel TEXT, duration REAL,
      status TEXT, progress REAL DEFAULT 0, error TEXT, created REAL, tries INTEGER DEFAULT 0, next_try REAL,
      norm INTEGER DEFAULT 0, PRIMARY KEY(user_id, id))""",
                """CREATE TABLE playlists(id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, name TEXT)""",
                """CREATE TABLE playlist_songs(
      playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE, user_id INTEGER, song_id TEXT, pos INTEGER,
      PRIMARY KEY(playlist_id, song_id), FOREIGN KEY(user_id, song_id) REFERENCES songs(user_id, id) ON DELETE CASCADE)""",
                """CREATE TABLE queue(qid INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, song_id TEXT, pos INTEGER,
      FOREIGN KEY(user_id, song_id) REFERENCES songs(user_id, id) ON DELETE CASCADE)""",
                """CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,
      user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, expires REAL)""",
                """CREATE TABLE guest_sessions(token_hash TEXT PRIMARY KEY,
      host_id INTEGER REFERENCES users(id) ON DELETE CASCADE, epoch INTEGER, expires REAL)""",
                "INSERT INTO songs SELECT ?, id, title, channel, duration, status, progress, error, created, tries, next_try, norm FROM songs_old",
                "INSERT INTO playlists SELECT id, ?, name FROM playlists_old",
                "INSERT INTO playlist_songs SELECT playlist_id, ?, song_id, pos FROM playlist_songs_old",
                "INSERT INTO queue SELECT qid, ?, song_id, pos FROM queue_old",
                "INSERT INTO sessions SELECT token_hash, ?, expires FROM sessions_old",
                "INSERT INTO guest_sessions SELECT token_hash, ?, epoch, expires FROM guest_sessions_old"):
        db.execute(sql, (aid,) if "?" in sql else ())
    for t in ("songs", "playlists", "playlist_songs", "queue", "sessions", "guest_sessions"):
        db.execute(f"DROP TABLE {t}_old")
    db.execute("DELETE FROM state WHERE k IN ('current','jam_code','jam_epoch','jam_started')")
    db.execute("COMMIT")
    db.execute("PRAGMA foreign_keys=ON")
    dest = udir(aid)
    for f in SONGS.glob("*.*"):   # the files move to the admin's folder
        os.replace(f, dest / f.name)


migrate_to_users()
LOGOS.mkdir(exist_ok=True)
db.execute("UPDATE songs SET status='error', error='Interromput', tries=99, next_try=NULL "   # an upload: nothing to download again
           "WHERE status='downloading' AND id LIKE 'up\\_%' ESCAPE '\\'")
db.execute("UPDATE songs SET status='error', error='Interromput', next_try=0 WHERE status='downloading'")
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


def new_session(uid, days):
    token = secrets.token_urlsafe(32)
    with lock:
        db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
        db.execute("INSERT INTO sessions VALUES(?,?,?)",
                   (hashlib.sha256(token.encode()).hexdigest(), uid, time.time() + days * 86400))
    return token


def session_user(token):
    """The user of a session cookie (any status), or None."""
    if not token:
        return None
    with lock:
        u = db.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>?",
                       (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if u and (u["last_seen"] or 0) < time.time() - 300:
            db.execute("UPDATE users SET last_seen=? WHERE id=?", (time.time(), u["id"]))
    return u


def logo_url(u):
    return f"/api/logo/{u['id']}?v={u['logo'].split('.')[0]}" if u and u["logo"] else "/static/avatar.jpg"


def brand(u):
    v = u["logo"].split(".")[0] if u and u["logo"] else "0"
    return {"app_name": (u["app_name"] if u else "") or "coniMIX", "logo": logo_url(u),
            # what the iPhone uses for "Add to Home Screen": the user's logo as the app icon, and their app name
            "icon": f"/api/icon/{u['id']}/180.png?v={v}" if u else "/static/icon-180.png",
            "manifest": f"/api/manifest/{u['id']}?v={v}" if u else "/static/manifest.webmanifest"}


ICON_SIZES = (180, 192, 512)


def app_icon(u, size):
    """The user's logo as a square, opaque PNG app icon (made once with ffmpeg, kept next to the logo)."""
    if not u or not u["logo"] or not (LOGOS / u["logo"]).is_file():
        return STATIC / f"icon-{size}.png"
    out = LOGOS / f"{u['logo'].split('.')[0]}-icon{size}.png"
    if not out.exists():
        tmp = out.with_suffix(".tmp.png")
        r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(LOGOS / u["logo"]), "-vf",
                            f"crop='min(iw,ih)':'min(iw,ih)',scale={size}:{size}:flags=lanczos,format=rgb24",
                            "-frames:v", "1", str(tmp)], capture_output=True, timeout=60, **NO_WINDOW)
        if r.returncode != 0 or not tmp.exists():
            tmp.unlink(missing_ok=True)
            return STATIC / f"icon-{size}.png"
        os.replace(tmp, out)
    return out


def user_public(u):
    return {"id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"], "status": u["status"],
            "ai": bool(u["ai"] or u["role"] == "admin"), "has_password": bool(u["pw"]), "google": bool(u["google_sub"]),
            "xfade": 4 if u["xfade"] is None else u["xfade"], **brand(u)}


class HttpError(Exception):
    def __init__(self, status, msg):
        self.status, self.msg = status, msg


# ---------- queue (each user has their own) ----------

def get_current(uid):
    r = db.execute("SELECT current FROM users WHERE id=?", (uid,)).fetchone()
    cur = r["current"] if r else None
    if cur is not None and not db.execute("SELECT 1 FROM queue WHERE qid=? AND user_id=?", (cur, uid)).fetchone():
        return None
    return cur


def set_current(uid, qid):
    db.execute("UPDATE users SET current=? WHERE id=?", (qid or None, uid))


def q_items(uid):
    return [dict(r) for r in db.execute(
        "SELECT q.qid, q.song_id, s.status FROM queue q JOIN songs s ON s.user_id=q.user_id AND s.id=q.song_id "
        "WHERE q.user_id=? ORDER BY q.pos, q.qid", (uid,))]


def renumber(qids):
    for i, qid in enumerate(qids):
        db.execute("UPDATE queue SET pos=? WHERE qid=?", (i, qid))


def ensure_current(uid):
    """If nothing is playing, start the first fully downloaded queue item."""
    if get_current(uid) is not None:
        return
    items = q_items(uid)
    for k, it in enumerate(items):
        if it["status"] == "ready":
            if k:  # items skipped because they were not downloaded yet wait right after the one that starts
                ids = [i["qid"] for i in items]
                renumber([ids[k]] + ids[:k] + ids[k + 1:])
            set_current(uid, it["qid"])
            return
    set_current(uid, None)


def add_to_queue(uid, song_id, mode):
    song = db.execute("SELECT status FROM songs WHERE user_id=? AND id=?", (uid, song_id)).fetchone()
    if not song:
        raise HttpError(404, "Cançó desconeguda")
    ready = song["status"] == "ready"
    items = q_items(uid)
    ids = [i["qid"] for i in items]
    cur = get_current(uid)
    qid = db.execute("INSERT INTO queue(user_id,song_id,pos) VALUES(?,?,?)", (uid, song_id, len(ids))).lastrowid
    if mode == "end":
        ids.append(qid)
    elif mode == "now" and ready:
        idx = ids.index(cur) if cur in ids else 0
        ids.insert(idx, qid)
        set_current(uid, qid)
    else:  # "next", or "now" for a song that is not downloaded yet
        idx = ids.index(cur) + 1 if cur in ids else 0
        ids.insert(idx, qid)
    renumber(ids)
    ensure_current(uid)
    return qid


def advance(uid, from_qid=None):
    """Finish the current item and move to the next fully downloaded one.
    Items that are not ready yet are skipped and parked right after the new current."""
    cur = get_current(uid)
    if cur is None:
        ensure_current(uid)
        return
    if from_qid is not None and from_qid != cur:
        return
    items = q_items(uid)
    ids = [i["qid"] for i in items]
    status = {i["qid"]: i["status"] for i in items}
    idx = ids.index(cur)
    before, after = ids[:idx], ids[idx + 1:]
    db.execute("DELETE FROM queue WHERE qid=?", (cur,))
    nxt = next((q for q in after if status[q] == "ready"), None)
    if nxt is None:
        renumber(before + after)
        set_current(uid, None)
        ensure_current(uid)
        return
    k = after.index(nxt)
    skipped, rest = after[:k], after[k + 1:]
    renumber(before + [nxt] + skipped + rest)
    set_current(uid, nxt)


def delete_song(uid, song_id):
    cur = get_current(uid)
    if cur is not None:
        r = db.execute("SELECT song_id FROM queue WHERE qid=?", (cur,)).fetchone()
        if r and r["song_id"] == song_id:
            advance(uid, cur)
    db.execute("DELETE FROM songs WHERE user_id=? AND id=?", (uid, song_id))  # cascades to playlists and queue
    for ext in ("mp3", "jpg"):
        (udir(uid) / f"{song_id}.{ext}").unlink(missing_ok=True)
    ensure_current(uid)


# ---------- downloads ----------

def download_worker(uid, song_id):
    cmd = [
        *YTDLP, *YTDLP_ARGS, "--no-update", "--newline", "--no-playlist",
        # the original YouTube audio (Opus ~160 kbps when there is one), not converted: it is encoded once, below
        "-f", "bestaudio[acodec=opus]/bestaudio/best", "--embed-metadata", "--write-thumbnail", "--convert-thumbnails", "jpg",
        "-o", str(udir(uid) / "%(id)s.src.%(ext)s"), "-o", "thumbnail:" + str(udir(uid) / "%(id)s.%(ext)s"),
        f"https://www.youtube.com/watch?v={song_id}",
    ]
    last, pct, tail = "", -1, []
    dl_sem.acquire()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **NO_WINDOW)
        for line in proc.stdout:
            last = line.strip() or last
            tail = (tail + [line])[-8:]
            m = PROGRESS_RE.search(line)
            if m and int(float(m.group(1))) != pct:
                pct = int(float(m.group(1)))
                with lock:
                    db.execute("UPDATE songs SET progress=? WHERE user_id=? AND id=?", (min(pct, 99), uid, song_id))
        proc.wait()
        src = [f for f in udir(uid).glob(f"{song_id}.src.*") if f.suffix not in (".part", ".jpg", ".ytdl")]
        ok = proc.returncode == 0 and bool(src)
        if ok:
            mp3 = udir(uid) / f"{song_id}.mp3"
            norm = 1 if normalize_audio(mp3, src[0]) else -1
            ok = norm == 1 or encode_mp3(src[0], mp3)
            if not ok:
                last = "No s'ha pogut convertir l'àudio"
    except Exception as e:  # noqa: BLE001
        ok, last = False, str(e)
    finally:
        dl_sem.release()
        for f in udir(uid).glob(f"{song_id}.src.*"):
            f.unlink(missing_ok=True)
    permanent = bool(PERMANENT_RE.search("".join(tail) + last))
    errs = [ln.strip() for ln in tail if ln.startswith("ERROR")]
    last = errs[-1] if errs else last   # the real error, not yt-dlp's trailing hints (e.g. the cookies FAQ)
    if not ok:
        net["online"] = is_online()
    with lock:
        row = db.execute("SELECT tries FROM songs WHERE user_id=? AND id=?", (uid, song_id)).fetchone()
        if row:
            if ok:
                db.execute("UPDATE songs SET status='ready', progress=100, error=NULL, tries=0, next_try=NULL, norm=? "
                           "WHERE user_id=? AND id=?", (norm, uid, song_id))
                ensure_current(uid)
            else:
                # offline failures do not use up attempts: the retry loop waits for the connection instead
                tries = (row["tries"] or 0) + (1 if net["online"] else 0)
                retry = not permanent and tries < MAX_TRIES
                nxt = time.time() + RETRY_DELAYS[min(max(tries - 1, 0), len(RETRY_DELAYS) - 1)] if retry else None
                db.execute("UPDATE songs SET status='error', error=?, tries=?, next_try=? WHERE user_id=? AND id=?",
                           (last[-200:], tries, nxt, uid, song_id))
        else:   # deleted (or its user was) while it was downloading
            for ext in ("mp3", "jpg"):
                (SONGS / str(uid) / f"{song_id}.{ext}").unlink(missing_ok=True)


# the best MP3 there is: 320 kbps CBR with LAME's slowest, most careful encoding
MP3_OUT = ["-c:a", "libmp3lame", "-b:a", "320k", "-compression_level", "0", "-id3v2_version", "3"]


def encode_mp3(src, path):
    """Plain conversion to MP3 (no loudness change), for when the normalization fails."""
    tmp = path.with_suffix(".norm.mp3")
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-y", "-i", str(src), "-map", "0:a", "-map_metadata", "0",
                            *MP3_OUT, str(tmp)], capture_output=True, timeout=900, **NO_WINDOW)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 10000:
            return False
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        tmp.unlink(missing_ok=True)


def normalize_audio(path, src=None):
    """Two-pass EBU R128 loudness normalization into a 320 kbps MP3 at `path`, read from `src` (default: in place).
    Returns False (file untouched) if ffmpeg fails."""
    src = src or path
    tmp = path.with_suffix(".norm.mp3")
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", str(src), "-af", LOUDNORM + ":print_format=json",
                            "-f", "null", "-"], capture_output=True, text=True, errors="replace", timeout=900, **NO_WINDOW)
        m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr)
        if r.returncode != 0 or not m:
            return False
        s = json.loads(m.group(0))
        rate = re.search(r"Audio: .*?(\d+) Hz", r.stderr)
        af = (f"{LOUDNORM}:linear=true:measured_I={s['input_i']}:measured_TP={s['input_tp']}"
              f":measured_LRA={s['input_lra']}:measured_thresh={s['input_thresh']}:offset={s['target_offset']}")
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-y", "-i", str(src), "-map", "0:a", "-map_metadata", "0",
                            "-af", af, "-ar", rate.group(1) if rate else "44100", *MP3_OUT, str(tmp)],
                           capture_output=True, timeout=900, **NO_WINDOW)
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 10000:
            return False
        for _ in range(30):   # Windows: the file can be open by a player request for a moment
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                time.sleep(2)
        return False
    except Exception:  # noqa: BLE001
        return False
    finally:
        tmp.unlink(missing_ok=True)


UPLOAD_MAX = 80_000_000


def upload_song(uid, raw, filename, pid=None):
    """A song from a file of the user's (MP3, M4A, WAV, FLAC...): it gets an id like a YouTube one and is encoded
    in the background exactly like a download (320 kbps, same loudness)."""
    sid = "up_" + secrets.token_urlsafe(6)[:8].replace("-", "x")
    src = udir(uid) / f"{sid}.src.upload"
    src.write_bytes(raw)
    base = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", filename or "").replace("_", " ").strip() or "Cançó pujada"
    artist, _, title = base.partition(" - ") if " - " in base else ("", "", base)
    db.execute("INSERT INTO songs(user_id,id,title,channel,duration,status,progress,created,tries,next_try,norm) "
               "VALUES(?,?,?,?,NULL,'downloading',50,?,0,NULL,0)", (uid, sid, title[:200], artist[:100], time.time()))
    if pid:
        nxt = db.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM playlist_songs WHERE playlist_id=?", (pid,)).fetchone()[0]
        db.execute("INSERT INTO playlist_songs(playlist_id,user_id,song_id,pos) VALUES(?,?,?,?)", (pid, uid, sid, nxt))
    threading.Thread(target=encode_upload, args=(uid, sid, src), daemon=True).start()
    return sid


def encode_upload(uid, sid, src):
    mp3, jpg = udir(uid) / f"{sid}.mp3", udir(uid) / f"{sid}.jpg"
    meta, dur = {}, None
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-i", str(src)], capture_output=True, text=True, errors="replace",
                           timeout=120, **NO_WINDOW)
        for ln in r.stderr.splitlines():   # the file's own tags, when it has them
            mm = re.match(r"^\s{4}(title|artist)\s*:\s*(.+)$", ln, re.I)
            if mm and mm.group(1).lower() not in meta:
                meta[mm.group(1).lower()] = mm.group(2).strip()
        mm = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", r.stderr)
        dur = int(mm.group(1)) * 3600 + int(mm.group(2)) * 60 + float(mm.group(3)) if mm else None
        # the cover embedded in the file; without one, the app logo
        subprocess.run([FFMPEG, "-hide_banner", "-y", "-i", str(src), "-an", "-frames:v", "1", "-vf", "scale=480:-2",
                        str(jpg)], capture_output=True, timeout=60, **NO_WINDOW)
        if not jpg.exists() or jpg.stat().st_size < 1000:
            jpg.write_bytes((STATIC / "logo.jpg").read_bytes())
        ok = normalize_audio(mp3, src)
        norm = 1 if ok else -1
        ok = ok or encode_mp3(src, mp3)
    except Exception:  # noqa: BLE001
        ok = False
    finally:
        src.unlink(missing_ok=True)
    with lock:
        if not db.execute("SELECT 1 FROM songs WHERE user_id=? AND id=?", (uid, sid)).fetchone():
            mp3.unlink(missing_ok=True)   # deleted while it was being encoded
            jpg.unlink(missing_ok=True)
            return
        if ok:
            db.execute("UPDATE songs SET status='ready', progress=100, error=NULL, duration=?, norm=?"
                       + (", title=?" if meta.get("title") else "") + (", channel=?" if meta.get("artist") else "")
                       + " WHERE user_id=? AND id=?",
                       [dur, norm, *([meta["title"][:200]] if meta.get("title") else []),
                        *([meta["artist"][:100]] if meta.get("artist") else []), uid, sid])
            ensure_current(uid)
        else:   # no retries: there is nothing to download again
            db.execute("UPDATE songs SET status='error', error=?, tries=99, next_try=NULL WHERE user_id=? AND id=?",
                       ("No s'ha pogut llegir el fitxer d'àudio", uid, sid))


def normalize_backlog():
    """Normalizes songs downloaded before normalization existed, one at a time, in the background."""
    while True:
        with lock:
            row = db.execute("SELECT user_id, id FROM songs WHERE status='ready' AND norm=0 ORDER BY created DESC").fetchone()
        if not row:
            return
        f = SONGS / str(row["user_id"]) / f"{row['id']}.mp3"
        ok = normalize_audio(f)
        with lock:
            if not db.execute("UPDATE songs SET norm=? WHERE user_id=? AND id=?", (1 if ok else -1, row["user_id"], row["id"])).rowcount:
                f.unlink(missing_ok=True)   # deleted while it was being normalized


def request_song(uid, meta):
    sid = meta.get("video_id", "")
    if not ID_RE.match(sid):
        raise HttpError(400, "Identificador de vídeo no vàlid")
    row = db.execute("SELECT status FROM songs WHERE user_id=? AND id=?", (uid, sid)).fetchone()
    if row is None or row["status"] == "error":
        db.execute(
            "INSERT INTO songs(user_id,id,title,channel,duration,status,progress,created) VALUES(?,?,?,?,?,?,0,?) "
            "ON CONFLICT(user_id,id) DO UPDATE SET tries=0",
            (uid, sid, (meta.get("title") or sid)[:200], (meta.get("channel") or "")[:100],
             meta.get("duration"), "downloading", time.time()))
        start_download(uid, sid)
    return sid


def start_download(uid, sid):
    db.execute("UPDATE songs SET status='downloading', progress=0, error=NULL, next_try=NULL WHERE user_id=? AND id=?", (uid, sid))
    threading.Thread(target=download_worker, args=(uid, sid), daemon=True).start()


net = {"online": True}


def is_online():
    try:
        socket.create_connection(("www.youtube.com", 443), timeout=4).close()
        return True
    except OSError:
        return False


def retry_loop():
    """Failed downloads that are in the queue or a playlist are tried again on their own:
    right away when the connection comes back, otherwise with a growing delay."""
    while True:
        time.sleep(15)
        with lock:
            pending = db.execute(
                "SELECT user_id, id, next_try FROM songs s WHERE status='error' AND next_try IS NOT NULL AND "
                "(EXISTS(SELECT 1 FROM queue q WHERE q.user_id=s.user_id AND q.song_id=s.id) OR "
                "EXISTS(SELECT 1 FROM playlist_songs p WHERE p.user_id=s.user_id AND p.song_id=s.id))").fetchall()
        if not pending:
            continue
        was = net["online"]
        net["online"] = is_online()
        if not net["online"]:
            continue
        now = time.time()
        with lock:
            for r in pending:
                if not was or r["next_try"] <= now:
                    if db.execute("SELECT 1 FROM songs WHERE user_id=? AND id=? AND status='error'", (r["user_id"], r["id"])).fetchone():
                        start_download(r["user_id"], r["id"])


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
            ud = d.get("upload_date") or ""   # only present when the video itself was extracted (pasted links)
            res.append({"id": d["id"], "title": d.get("title"), "channel": d.get("channel") or d.get("uploader"),
                        "uploader": d.get("uploader") or d.get("channel"), "views": d.get("view_count"),
                        "date": f"{ud[:4]}-{ud[4:6]}-{ud[6:]}" if len(ud) == 8 else None,
                        "duration": d.get("duration"),
                        "thumbnail": f"https://i.ytimg.com/vi/{d['id']}/mqdefault.jpg"})
    return res, out


_search_cache: dict = {}


def parse_yt_url(text):
    """(video_id, playlist_id) of a YouTube link pasted in the search box, or (None, None) for a normal search."""
    m = YT_URL_RE.search(text)
    if not m:
        return None, None
    raw = m.group(0)
    u = urlparse(raw if "://" in raw else "https://" + raw)
    qs = parse_qs(u.query)
    if u.netloc.lower().endswith("youtu.be"):
        vid = u.path.strip("/").split("/")[0]
    else:
        p = re.match(r"^/(?:shorts|live|embed|v)/([\w-]{11})", u.path)
        vid = p.group(1) if p else qs.get("v", [""])[0]
    vid = vid if ID_RE.match(vid or "") else None
    pl = qs.get("list", [""])[0]
    pl = pl if re.match(r"^[\w-]{10,64}$", pl) else None
    if vid and pl and pl.startswith("RD"):   # an automatic "mix" from a song link: only the song is wanted
        pl = None
    return vid, pl


def url_search(vid, plist):
    """Results for a pasted link: the video itself, then the items of its playlist (if any)."""
    res, err = [], None
    targets = ([(f"https://www.youtube.com/watch?v={vid}", 1)] if vid else []) + \
        ([(f"https://www.youtube.com/playlist?list={plist}", 50)] if plist else [])
    for target, n in targets:
        try:
            r, out = _yt_list(target, n)
        except Exception as e:  # noqa: BLE001
            r, out = [], e
        res += [x for x in r if x["id"] not in {y["id"] for y in res}]
        err = err or (out if not r else None)
    if not res:
        msg = getattr(err, "stderr", "") or ""
        raise HttpError(502, (msg.strip().splitlines() or ["No s'ha trobat aquest enllaç de YouTube"])[-1])
    return res


def run_search(query, limit=14):
    """Broad search: regular YouTube (has artist and duration) plus YouTube Music songs
    (official tracks, tolerant to typos). Both run in parallel and are merged without duplicates."""
    key = " ".join(query.lower().split())
    hit = _search_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    vid, plist = parse_yt_url(query)
    if vid or plist:
        merged = url_search(vid, plist)
        _search_cache[key] = (time.time(), merged)
        return merged
    query = query[:150]
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


_meta: dict = {}
META_RE = {"date": re.compile(r'"(?:publishDate|uploadDate)":"(\d{4}-\d\d-\d\d)'),
           "views": re.compile(r'"viewCount":"(\d+)"'),
           "duration": re.compile(r'"lengthSeconds":"(\d+)"'),
           "uploader": re.compile(r'"ownerChannelName":"((?:[^"\\]|\\.)*)"')}


def video_meta(vid):
    """Upload date, views and uploader of a video. Search results lack the date (and YouTube Music ones
    the views and uploader too), so they are read from the watch page."""
    if vid in _meta:
        return _meta[vid]
    try:
        req = urllib.request.Request(f"https://www.youtube.com/watch?v={vid}&hl=en",
                                     headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
        html = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None   # not cached: try again next time
    m = {k: r.search(html) for k, r in META_RE.items()}
    meta = {}
    if m["date"]:
        meta["date"] = m["date"].group(1)
    if m["views"]:
        meta["views"] = int(m["views"].group(1))
    if m["duration"]:
        meta["duration"] = int(m["duration"].group(1))
    if m["uploader"]:
        try:
            meta["uploader"] = json.loads(f'"{m["uploader"].group(1)}"')
        except json.JSONDecodeError:
            pass
    if len(_meta) > 5000:
        _meta.clear()
    _meta[vid] = meta
    return meta


def videos_meta(ids):
    out, todo = {}, [i for i in ids if i not in _meta]

    def job(chunk):
        for i in chunk:
            video_meta(i)
    threads = [threading.Thread(target=job, args=(todo[k::6],)) for k in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for i in ids:
        if _meta.get(i):
            out[i] = _meta[i]
    return out


# ---------- AI search (Claude) ----------

AI_MODEL = os.environ.get("AI_MODEL", "claude-sonnet-5-5")
AI_SYSTEM = """You help find songs for a personal music player that downloads audio from YouTube.
The user describes what they want, usually in Catalan or Spanish, e.g. "a song by ABC about XYZ" or
"an Italian song from the 80s that was an anthem for teenagers back then".
Suggest up to 8 real, existing songs that best match, best match first. Only suggest songs you are confident exist,
with the exact artist name and song title as published, so a YouTube search finds them. If the user names a specific
song, put it first. Write "why" and "note" in the same language the user wrote in, one short sentence each:
"why" says why that song fits; "note" is an optional one-line comment on the whole answer (empty if nothing to add)."""
AI_SCHEMA = {
    "type": "object",
    "properties": {
        "note": {"type": "string"},
        "songs": {"type": "array", "items": {
            "type": "object",
            "properties": {"artist": {"type": "string"}, "title": {"type": "string"},
                           "year": {"type": "string"}, "why": {"type": "string"}},
            "required": ["artist", "title", "year", "why"], "additionalProperties": False}},
    },
    "required": ["note", "songs"], "additionalProperties": False,
}
_ai: dict = {}


AI_PL_SYSTEM = """You build playlists for a personal music player that downloads audio from YouTube.
The user describes the playlist they want, usually in Catalan or Spanish, typed or as a dictated voice note.
- name: if the user gives the playlist a name, use exactly that name; otherwise propose a short, fitting name
  in the user's language.
- target_minutes: the length the user asked for, in minutes ("about half an hour" -> 30), or 0 if they did not ask.
- songs: real, existing songs that fit, in a good listening order, with the exact artist name and song title as
  published so a YouTube search finds them. If a length was asked, give about 25% more songs than needed
  (assume ~4 minutes per song) so the length can be adjusted afterwards; if a number of songs was asked, give that
  many; otherwise give 12-20. Never more than 40.
- note: one short sentence in the user's language about the choice (empty if nothing to add).
Use web search only when the request depends on recent releases, charts or current popularity you are unsure of."""
AI_PL_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "note": {"type": "string"},
        "target_minutes": {"type": "integer"},
        "songs": {"type": "array", "items": {
            "type": "object",
            "properties": {"artist": {"type": "string"}, "title": {"type": "string"}, "year": {"type": "string"}},
            "required": ["artist", "title", "year"], "additionalProperties": False}},
    },
    "required": ["name", "note", "target_minutes", "songs"], "additionalProperties": False,
}


def ai_ready():
    _from_file("ANTHROPIC_API_KEY", "anthropic_api_key.txt")   # the key can be added without restarting
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HttpError(503, "La IA no està configurada: falta la clau de Claude (anthropic_api_key.txt)")


def ask_claude(prompt, system=AI_SYSTEM, schema=AI_SCHEMA, web=False):
    """One structured answer from Claude. web=True lets it search the internet (recent albums, charts...)."""
    import anthropic
    if "client" not in _ai:
        _ai["client"] = anthropic.Anthropic(timeout=120, max_retries=1)
    messages = [{"role": "user", "content": prompt}]
    extra = {"tools": [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}]} if web else {}
    try:
        for _ in range(4):
            resp = _ai["client"].beta.messages.create(
                model=AI_MODEL, max_tokens=8000, system=system,
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
                messages=messages, **extra)
            if resp.stop_reason != "pause_turn":   # a long web search can pause: resend to let it continue
                break
            messages = messages[:1] + [{"role": "assistant", "content": resp.content}]
    except anthropic.AuthenticationError:
        raise HttpError(502, "La clau de Claude no és vàlida")
    except anthropic.PermissionDeniedError:
        raise HttpError(502, "La clau de Claude no té permís (revisa el crèdit del compte)")
    except anthropic.RateLimitError:
        raise HttpError(429, "Claude està saturat, torna-ho a provar d'aquí una estona")
    except anthropic.APIStatusError as e:
        raise HttpError(502, f"Error de Claude ({e.status_code})")
    except anthropic.APIConnectionError:
        raise HttpError(502, "No es pot connectar amb Claude")
    if resp.stop_reason == "refusal":
        raise HttpError(422, "Claude no ha volgut fer aquesta petició")
    texts = [b.text for b in resp.content if b.type == "text"]
    try:
        return json.loads(texts[-1] if texts else "")
    except json.JSONDecodeError:
        raise HttpError(502, "Claude no ha respost bé, torna-ho a provar")


_yt_slots = threading.Semaphore(6)   # parallel yt-dlp lookups (a 40-song playlist would otherwise start 40 at once)


def yt_lookup(picks):
    """First YouTube result for each {artist, title} (None when not found), in the same order."""
    found = [None] * len(picks)

    def job(i, p):
        with _yt_slots:
            try:
                r, _ = _yt_list(f"ytsearch1:{p['artist']} {p['title']}", 1)
            except Exception:  # noqa: BLE001
                r = []
        if r:
            found[i] = r[0]
    threads = [threading.Thread(target=job, args=(i, p)) for i, p in enumerate(picks)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return found


def ai_search(prompt):
    """Claude picks the songs; each one is then looked up on YouTube (in parallel)."""
    ai_ready()
    key = "ai:" + " ".join(prompt.lower().split())
    hit = _search_cache.get(key)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    data = ask_claude(prompt)
    picks = [p for p in data.get("songs", []) if p.get("title")][:8]
    res, seen = [], set()
    for p, r in zip(picks, yt_lookup(picks)):
        if r and r["id"] not in seen:
            seen.add(r["id"])
            res.append({**r, "why": p.get("why", ""),
                        "ai": f"{p['artist']} · {p['title']}" + (f" ({p['year']})" if p.get("year") else "")})
    if not res:
        raise HttpError(502, "Claude ha suggerit cançons però no les he trobat a YouTube")
    out = {"note": data.get("note", ""), "results": res}
    _search_cache[key] = (time.time(), out)
    return out


def ai_playlist(uid, prompt):
    """Claude designs the playlist (name, songs, length); the songs are found on YouTube, the list is created
    and every song starts downloading."""
    ai_ready()
    data = ask_claude(prompt, AI_PL_SYSTEM, AI_PL_SCHEMA, web=True)
    picks = [p for p in data.get("songs", []) if p.get("title")][:40]
    target = max(0, int(data.get("target_minutes") or 0)) * 60
    found = yt_lookup(picks)
    chosen, seen, total = [], set(), 0
    for r in found:
        if not r or r["id"] in seen:
            continue
        d = r.get("duration") or 240
        if target and total >= target - 90:   # long enough (within a minute and a half)
            break
        if target and total + d > target + 180:   # this one would overshoot: try a shorter one
            continue
        seen.add(r["id"])
        chosen.append(r)
        total += d
    if not chosen:
        raise HttpError(502, "Claude ha proposat cançons però no les he trobat a YouTube")
    name = " ".join((data.get("name") or "").split())[:80] or "Llista feta amb IA"
    with lock:
        pid = db.execute("INSERT INTO playlists(user_id,name) VALUES(?,?)", (uid, name)).lastrowid
        for pos, r in enumerate(chosen):
            sid = request_song(uid, {"video_id": r["id"], "title": r["title"], "channel": r["channel"], "duration": r["duration"]})
            db.execute("INSERT OR IGNORE INTO playlist_songs(playlist_id,user_id,song_id,pos) VALUES(?,?,?,?)", (pid, uid, sid, pos))
    return {"id": pid, "name": name, "count": len(chosen), "minutes": round(total / 60),
            "missing": found.count(None), "note": data.get("note", "")}


# ---------- import a Spotify playlist or album: each song is looked up on YouTube and downloaded ----------

SPOTIFY_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-\w+/)?(?:embed/)?|spotify:)(playlist|album|artist|track)[/:]([A-Za-z0-9]{22})")
imports: dict = {}   # playlist id -> {"total", "done", "missing"} while its songs are being looked up


SPOTIFY_TRACK_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-\w+/)?(?:embed/)?track/|spotify:track:)([A-Za-z0-9]{22})")
SPOTIFY_PRIVATE = ("Aquesta llista és privada i Spotify no la deixa veure sense iniciar sessió. Dues solucions: a Spotify, "
                   "fes-la pública (⋯ → Afegeix al perfil) i torna-ho a provar; o bé, al Spotify de l'ordinador, obre la "
                   "llista, prem Ctrl+A i després Ctrl+C, i enganxa aquí les cançons copiades.")


def spotify_entity(kind, sid):
    """What a Spotify embed page shows (no API key), or None when Spotify does not show it (private or removed)."""
    req = urllib.request.Request(f"https://open.spotify.com/embed/{kind}/{sid}",
                                 headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"})
    try:
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        raise HttpError(502, "No he pogut connectar amb Spotify. Torna-ho a provar d'aquí una estona.") from None
    try:
        d = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
        return json.loads(d.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except Exception:  # noqa: BLE001
        return None


def spotify_song(e):
    return {"artist": ", ".join(a.get("name", "") for a in e.get("artists") or []), "title": e["title"],
            "duration": (e.get("duration") or 0) / 1000}


def spotify_tracks(link):
    """(name, [{artist, title, duration}]) of a public Spotify playlist, album, artist (their ~10 most played songs)
    or single song (the embed lists up to 100 songs); or of a pasted list of song links, which is how the songs of a
    private playlist get here (Ctrl+A, Ctrl+C in the desktop app: each song is public even if the list is not)."""
    ids = list(dict.fromkeys(SPOTIFY_TRACK_RE.findall(link or "")))[:300]
    if len(ids) > 1:
        found = [None] * len(ids)
        slots = threading.Semaphore(8)

        def job(i):
            with slots:
                try:
                    e = spotify_entity("track", ids[i])
                except HttpError:
                    e = None
            if e and e.get("title"):
                found[i] = spotify_song(e)
        threads = [threading.Thread(target=job, args=(i,)) for i in range(len(ids))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        tracks = [t for t in found if t]
        if not tracks:
            raise HttpError(502, "No he pogut llegir cap d'aquestes cançons de Spotify")
        return f"Spotify {time.strftime('%d/%m %H:%M')}", tracks
    m = SPOTIFY_RE.search(link or "")
    if not m:
        raise HttpError(400, "Enganxa l'enllaç d'una llista, un àlbum, un artista o una cançó de Spotify (Comparteix → Copia l'enllaç)")
    e = spotify_entity(m.group(1), m.group(2))
    if not e:
        raise HttpError(404, SPOTIFY_PRIVATE if m.group(1) == "playlist" else "Spotify no mostra aquest enllaç. Comprova que sigui correcte.")
    if m.group(1) == "track":
        tracks = [spotify_song(e)] if e.get("title") else []
    else:
        tracks = [{"artist": t.get("subtitle") or "", "title": t["title"], "duration": (t.get("duration") or 0) / 1000}
                  for t in e.get("trackList") or [] if t.get("title")]
    if not tracks:
        raise HttpError(404, SPOTIFY_PRIVATE if m.group(1) == "playlist" else "Spotify no hi mostra cap cançó")
    name = " ".join((e.get("name") or e.get("title") or "").split())[:70] or "Llista de Spotify"
    if m.group(1) == "artist":
        name += " · Èxits"
    elif m.group(1) == "track":
        name = f"{tracks[0]['artist']} · {name}"[:80]
    return name, tracks


def yt_match(t):
    """The YouTube video for a Spotify song: of the first results, the one whose length is closest to the song's."""
    with _yt_slots:
        try:
            r, _ = _yt_list(f"ytsearch4:{t['artist']} - {t['title']}", 4)
        except Exception:  # noqa: BLE001
            r = []
    if not r or not t["duration"]:
        return r[0] if r else None
    best = min(r[:4], key=lambda x: abs((x.get("duration") or 0) - t["duration"]))
    return best if abs((best.get("duration") or 0) - t["duration"]) <= 30 else r[0]


SHOTS_SYSTEM = """You get one or more screenshots of a playlist in a music app (usually Spotify, sometimes Apple Music,
YouTube Music...), in the order they were taken while scrolling down. List every song you can read, in the order it
appears on screen, across all screenshots. Consecutive screenshots usually overlap: a song that appears at the end of
one and the start of the next is ONE song, list it once. Ignore anything that is not a song of the list (suggested or
recommended songs below the list, ads, buttons, the "now playing" bar). Give the artist(s) and title exactly as shown,
completing a title cut off with "..." only when you are sure of the song. duration_seconds: the length if it is shown,
else 0. name: the playlist title if it is visible, else an empty string."""
SHOTS_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "songs": {"type": "array", "items": {
            "type": "object",
            "properties": {"artist": {"type": "string"}, "title": {"type": "string"}, "duration_seconds": {"type": "integer"}},
            "required": ["artist", "title", "duration_seconds"], "additionalProperties": False}},
    },
    "required": ["name", "songs"], "additionalProperties": False,
}


def screenshots_import(uid, images):
    """Claude reads the songs from screenshots of a playlist (a private one, or from any app); then like a link."""
    ai_ready()
    blocks = []
    for img in images[:15]:
        mm = re.match(r"^data:(image/(?:png|jpeg|webp));base64,(.+)$", str(img), re.S)
        if not mm:
            raise HttpError(400, "Cal una imatge PNG, JPG o WebP")
        if len(mm.group(2)) * 3 // 4 > 5_000_000:
            raise HttpError(413, "Una de les captures és massa gran")
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": mm.group(1), "data": mm.group(2)}})
    if not blocks:
        raise HttpError(400, "Tria almenys una captura de pantalla")
    blocks.append({"type": "text", "text": f"{len(blocks)} screenshot(s) of the playlist, in order. List its songs."})
    data = ask_claude(blocks, SHOTS_SYSTEM, SHOTS_SCHEMA)
    tracks = [{"artist": t.get("artist") or "", "title": t["title"], "duration": max(0, t.get("duration_seconds") or 0)}
              for t in data.get("songs", []) if (t.get("title") or "").strip()][:300]
    if not tracks:
        raise HttpError(422, "No he sabut llegir cap cançó a les captures. Fes-les de la llista de cançons, ben enfocades.")
    name = " ".join((data.get("name") or "").split())[:80] or f"Captures {time.strftime('%d/%m %H:%M')}"
    return import_tracks(uid, name, tracks)


def spotify_import(uid, link):
    return import_tracks(uid, *spotify_tracks(link))


def import_tracks(uid, name, tracks):
    """Creates the playlist now and fills it in the background, in the original order, as each song is found."""
    with lock:
        pid = db.execute("INSERT INTO playlists(user_id,name) VALUES(?,?)", (uid, name)).lastrowid
    st = imports[pid] = {"total": len(tracks), "done": 0, "missing": 0}
    found, ready = [None] * len(tracks), [threading.Event() for _ in tracks]

    def look(i):
        try:
            found[i] = yt_match(tracks[i])
        finally:
            ready[i].set()

    def fill():
        try:
            for i in range(len(tracks)):
                threading.Thread(target=look, args=(i,), daemon=True).start()
            for i in range(len(tracks)):
                ready[i].wait()
                r = found[i]
                with lock:
                    if not db.execute("SELECT 1 FROM playlists WHERE id=?", (pid,)).fetchone():
                        return   # the list was deleted meanwhile
                    if r:
                        sid = request_song(uid, {"video_id": r["id"], "title": r["title"], "channel": r["channel"],
                                                 "duration": r["duration"]})
                        if not db.execute("SELECT 1 FROM playlist_songs WHERE playlist_id=? AND song_id=?", (pid, sid)).fetchone():
                            nxt = db.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM playlist_songs WHERE playlist_id=?",
                                             (pid,)).fetchone()[0]
                            db.execute("INSERT INTO playlist_songs(playlist_id,user_id,song_id,pos) VALUES(?,?,?,?)",
                                       (pid, uid, sid, nxt))
                    else:
                        st["missing"] += 1
                    st["done"] += 1
        finally:
            time.sleep(10)   # the page shows "done" for a moment
            imports.pop(pid, None)
    threading.Thread(target=fill, daemon=True).start()
    return {"id": pid, "name": name, "count": len(tracks)}


# ---------- lyrics (LRCLIB: free, no key; synced lyrics when it has them) ----------

LRCLIB = "https://lrclib.net/api"
db.execute("""CREATE TABLE IF NOT EXISTS lyrics(
  id TEXT PRIMARY KEY, artist TEXT, title TEXT, plain TEXT, synced TEXT, lang TEXT, fetched REAL)""")
if "offset" not in {r["name"] for r in db.execute("PRAGMA table_info(lyrics)")}:
    # seconds the synced lyrics are moved by: a video's intro is often longer than the album version's
    db.execute("ALTER TABLE lyrics ADD COLUMN offset REAL DEFAULT 0")
# "(Official Video)", "[Lyrics]", "| 4K"... that YouTube titles carry and the lyrics database does not
TITLE_JUNK_RE = re.compile(r"\s*[(\[【][^)\]】]*(official|video|vídeo|audio|àudio|lyric|letra|lletra|visuali[sz]er|clip|hd|hq|4k"
                           r"|remaster|explicit|color coded|sub|traduc|en vivo|live)[^)\]】]*[)\]】]", re.I)
LYRICS_AI_SYSTEM = """You get the title and channel of a YouTube music video. Give the real artist name and song
title as published (no "official video", no featured artists unless they are part of the title). If it is not a
song, give empty strings."""
LYRICS_AI_SCHEMA = {"type": "object", "properties": {"artist": {"type": "string"}, "title": {"type": "string"}},
                    "required": ["artist", "title"], "additionalProperties": False}
# a few very common words per language; the one with most hits is the language of the song
LANG_WORDS = {
    "en": "the you i and to me my it is in on that your love be don't i'm we what all know just like oh baby",
    "es": "el la que y de me te no mi en un es por lo tu yo una se con amor quiero corazón más",
    "ca": "el la que i de em et no el meu en un és per tu jo una amb amor vull cor més els les",
    "fr": "le la que et de je tu ne pas mon en un est pour moi toi une avec amour c'est les des",
    "it": "il la che e di mi ti non mio in un è per tu io una con amore sono cuore più",
    "pt": "o a que e de me te não meu em um é por você eu uma com amor quero coração mais",
    "de": "der die das und ich du nicht mein in ein ist mit dich mich liebe wir sie auf",
}
LANG_SETS = {k: set(v.split()) for k, v in LANG_WORDS.items()}


def lyrics_lang(text):
    words = re.findall(r"[\w'’]+", (text or "").lower().replace("’", "'"))[:400]
    if len(words) < 8:
        return ""
    score = {k: sum(w in s for w in words) for k, s in LANG_SETS.items()}
    best = max(score, key=score.get)
    return best if score[best] >= 4 else ""


def guess_artist_title(title, channel):
    t = TITLE_JUNK_RE.sub("", title or "")
    t = re.split(r"\s+[|｜]\s+", t)[0]
    t = re.sub(r"\s*[(\[]\d{4}[)\]]|\s+(videoclip|official (music )?video|lyrics?)\s*$", "", t, flags=re.I)
    t = re.sub(r"\s+(ft|feat|featuring)\.?\s.*$", "", t, flags=re.I).strip()
    ch = re.sub(r"\s*(- Topic|VEVO|Official|Oficial)\s*$", "", channel or "", flags=re.I).strip()
    m = re.match(r"(.+?)\s+[-–—]\s+(.+)", t)
    artist, song = (m.group(1), m.group(2)) if m else (ch, t)
    return artist.strip(), song.strip(" \"'“”")


def lrclib(path, **params):
    from urllib.parse import urlencode
    req = urllib.request.Request(f"{LRCLIB}/{path}?{urlencode(params)}",
                                 headers={"User-Agent": "coniMIX (personal music player)"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.load(r)


def lyrics_lookup(artist, title, duration):
    """The best LRCLIB match: it has lyrics, its length is close to the video's, synced beats plain."""
    tries = [{"track_name": title, "artist_name": artist}] if artist else []
    tries.append({"q": f"{artist} {title}".strip()})
    for params in tries:
        try:
            res = lrclib("search", **params)
        except Exception:  # noqa: BLE001
            continue
        best, best_score = None, None
        for r in res[:20]:
            if not (r.get("plainLyrics") or r.get("syncedLyrics")):
                continue
            off = abs((r.get("duration") or 0) - duration) if duration and r.get("duration") else 30
            if off > 25:   # another version of the song (or another song)
                continue
            score = off - (8 if r.get("syncedLyrics") else 0)
            if best_score is None or score < best_score:
                best, best_score = r, score
        if best:
            return best
    return None


def get_lyrics(uid, sid, query="", ai=False):
    """Lyrics of one of the user's songs, kept in the database (also "not found", for a day)."""
    with lock:
        s = db.execute("SELECT title, channel, duration FROM songs WHERE user_id=? AND id=?", (uid, sid)).fetchone()
        row = db.execute("SELECT * FROM lyrics WHERE id=?", (sid,)).fetchone()
    if not s:
        raise HttpError(404, "Cançó desconeguda")
    if row and not query and (row["plain"] or row["synced"] or time.time() - row["fetched"] < 86400):
        return dict(row)
    if query:   # typed by the user: "artist - title" or just words
        artist, title = guess_artist_title(query, "")
    else:
        artist, title = guess_artist_title(s["title"], s["channel"])
    hit = lyrics_lookup(artist, title, s["duration"])
    if not hit and not query and ai:   # messy YouTube title: Claude says which song it is
        try:
            ai_ready()
            g = ask_claude(f"Title: {s['title']}\nChannel: {s['channel']}", LYRICS_AI_SYSTEM, LYRICS_AI_SCHEMA)
            if g.get("title"):
                artist, title = g.get("artist", ""), g["title"]
                hit = lyrics_lookup(artist, title, s["duration"])
        except HttpError:
            pass
    out = {"id": sid, "artist": (hit or {}).get("artistName") or artist, "title": (hit or {}).get("trackName") or title,
           "plain": (hit or {}).get("plainLyrics") or "", "synced": (hit or {}).get("syncedLyrics") or "",
           "fetched": time.time()}
    out["lang"] = lyrics_lang(out["plain"] or re.sub(r"\[[\d:.]+\]", "", out["synced"]))
    if hit or not row or not (row["plain"] or row["synced"]):   # a failed manual search keeps the lyrics there were
        with lock:
            db.execute("INSERT OR REPLACE INTO lyrics(id,artist,title,plain,synced,lang,fetched) VALUES(?,?,?,?,?,?,?)",
                       (sid, out["artist"], out["title"], out["plain"], out["synced"], out["lang"], out["fetched"]))
    return out if hit or not row else {**dict(row), "missed": True}


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


def jam_code(uid):
    """(code, epoch) of a user's jam. The code goes in the QR; the epoch invalidates every guest session when bumped."""
    with lock:
        r = db.execute("SELECT jam_code, jam_epoch FROM users WHERE id=?", (uid,)).fetchone()
        code = r["jam_code"]
        if not code:
            code = secrets.token_urlsafe(9)
            db.execute("UPDATE users SET jam_code=? WHERE id=?", (code, uid))
    return code, r["jam_epoch"] or 1


def jam_rotate(uid, kick):
    """New QR code (old photos of the QR stop working). kick=True also ends every guest session."""
    with lock:
        db.execute("UPDATE users SET jam_code=? WHERE id=?", (secrets.token_urlsafe(9), uid))
        if kick:   # a new jam: everybody from the previous party is out
            db.execute("UPDATE users SET jam_epoch=COALESCE(jam_epoch,1)+1, jam_started=? WHERE id=?", (time.time(), uid))
            db.execute("DELETE FROM guest_sessions WHERE host_id=?", (uid,))


def new_guest(host):
    token = secrets.token_urlsafe(32)
    with lock:
        db.execute("DELETE FROM guest_sessions WHERE expires<?", (time.time(),))
        db.execute("INSERT INTO guest_sessions VALUES(?,?,?,?)",
                   (hashlib.sha256(token.encode()).hexdigest(), host, jam_code(host)[1], time.time() + GUEST_HOURS * 3600))
    return token


def guest_host(token):
    """The host (user row) of a valid jam guest session, or None."""
    if not token:
        return None
    with lock:
        r = db.execute("SELECT u.* FROM guest_sessions g JOIN users u ON u.id=g.host_id "
                       "WHERE g.token_hash=? AND g.expires>? AND g.epoch=COALESCE(u.jam_epoch,1) AND u.status='active'",
                       (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
    return r


def guest_count(uid):
    with lock:
        return db.execute("SELECT COUNT(*) FROM guest_sessions WHERE host_id=? AND epoch=? AND expires>?",
                          (uid, jam_code(uid)[1], time.time())).fetchone()[0]


def base_url(host_header):
    if PUBLIC_URL:
        return PUBLIC_URL
    name = (host_header or "").split(":")[0]
    host = host_header if name not in ("", "localhost", "127.0.0.1") else f"{lan_ip()}:{PORT}"
    return f"http://{host}"


def jam_url(host_header, uid):
    return f"{base_url(host_header)}/jam?k={jam_code(uid)[0]}"


def full_state(host_header, u, owner=True):
    uid = u["id"]
    # auto: a failed download that the retry loop will try again by itself
    songs = {r["id"]: {"id": r["id"], "title": r["title"], "channel": r["channel"], "duration": r["duration"],
                       "status": r["status"], "progress": r["progress"], "error": r["error"], "auto": bool(r["auto"])}
             for r in db.execute(
                 "SELECT s.*, (s.status='error' AND s.next_try IS NOT NULL AND "
                 "(EXISTS(SELECT 1 FROM queue q WHERE q.user_id=s.user_id AND q.song_id=s.id) OR "
                 "EXISTS(SELECT 1 FROM playlist_songs p WHERE p.user_id=s.user_id AND p.song_id=s.id))) AS auto "
                 "FROM songs s WHERE s.user_id=? ORDER BY created DESC", (uid,))}
    queue = q_items(uid)
    if not owner:  # guests only see what is in the shared queue, not the private library
        inq = {q["song_id"] for q in queue}
        songs = {k: v for k, v in songs.items() if k in inq}
        return {"songs": songs, "playlists": [], "queue": queue, "current": get_current(uid),
                "online": net["online"], "owner": False, "brand": brand(u), "ai": bool(u["ai"] or u["role"] == "admin")}
    playlists = []
    for p in db.execute("SELECT * FROM playlists WHERE user_id=? ORDER BY name COLLATE NOCASE", (uid,)):
        sids = [r["song_id"] for r in db.execute(
            "SELECT song_id FROM playlist_songs WHERE playlist_id=? ORDER BY pos", (p["id"],))]
        playlists.append({"id": p["id"], "name": p["name"], "songs": sids, "importing": imports.get(p["id"])})
    return {"songs": songs, "playlists": playlists, "queue": queue, "current": get_current(uid),
            "jam_url": jam_url(host_header, uid), "guests": guest_count(uid),
            "jam_started": u["jam_started"] or 0, "online": net["online"], "owner": True}


def dir_size(d):
    return sum(f.stat().st_size for f in d.glob("*") if f.is_file()) if d.exists() else 0


def admin_users():
    out = []
    for u in db.execute("SELECT * FROM users ORDER BY (status='pending') DESC, created"):
        n = db.execute("SELECT COUNT(*) FROM songs WHERE user_id=?", (u["id"],)).fetchone()[0]
        pl = db.execute("SELECT COUNT(*) FROM playlists WHERE user_id=?", (u["id"],)).fetchone()[0]
        out.append({**user_public(u), "created": u["created"], "last_seen": u["last_seen"], "songs": n, "playlists": pl,
                    "bytes": dir_size(SONGS / str(u["id"]))})
    return out


def delete_user(uid):
    db.execute("DELETE FROM users WHERE id=?", (uid,))   # cascades to songs, lists, queue and sessions
    d = SONGS / str(uid)
    if d.exists():
        for f in d.glob("*"):
            f.unlink(missing_ok=True)
        d.rmdir()
    for f in LOGOS.glob(f"{uid}-*"):
        f.unlink(missing_ok=True)


def google_identity(credential):
    """Checks a Google sign-in token with Google itself. Returns (sub, email, name)."""
    if not GOOGLE_CLIENT_ID:
        raise HttpError(503, "L'accés amb Google no està configurat")
    try:
        from urllib.parse import quote
        with urllib.request.urlopen("https://oauth2.googleapis.com/tokeninfo?id_token=" + quote(credential), timeout=10) as r:
            t = json.loads(r.read())
    except Exception:  # noqa: BLE001
        raise HttpError(401, "No s'ha pogut validar l'accés amb Google")
    if t.get("aud") != GOOGLE_CLIENT_ID or t.get("iss") not in ("accounts.google.com", "https://accounts.google.com") \
            or str(t.get("email_verified")).lower() != "true" or float(t.get("exp", 0)) < time.time():
        raise HttpError(401, "No s'ha pogut validar l'accés amb Google")
    return t["sub"], t["email"].lower(), t.get("name") or t["email"].split("@")[0]


def new_account(email, name, pw=None, google_sub=None):
    """A new user: waiting for the admin's approval (the admin's own address is approved at once)."""
    first_admin = email == ADMIN_EMAIL and not db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone()
    return db.execute("INSERT INTO users(email,pw,google_sub,name,role,status,ai,created) VALUES(?,?,?,?,?,?,?,?)",
                      (email, hash_pw(pw) if pw else None, google_sub, name[:60], "admin" if first_admin else "user",
                       "active" if first_admin else "pending", 1 if first_admin else 0, time.time())).lastrowid


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

    def send_login(self, uid, remember):
        token = new_session(uid, SESSION_DAYS if remember else 1)
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", self.cookie(token, SESSION_DAYS * 86400 if remember else 86400))
        self.end_headers()
        return self.wfile.write(body)

    def route(self, method):
        url = urlparse(self.path)
        path = url.path
        me = session_user(self.session_token())
        owner = bool(me and me["status"] == "active")
        if self.headers.get("X-Jam") == "1" or parse_qs(url.query).get("jam") == ["1"]:   # media (<audio>/<img>) use ?jam=1
            owner = False   # the jam page (/jam): a logged-in user who scanned a QR is a guest of that host, not in their own library
        ip = self.peer()

        ctype = self.headers.get("Content-Type") or ""
        upload = path == "/api/upload" and method == "POST" and ctype.startswith(("audio/", "video/", "application/octet-stream"))
        if method in ("POST", "PATCH") and not upload and not ctype.startswith("application/json"):
            raise HttpError(415, "Content-Type no vàlid")  # blocks cross-site form posts

        # -- accounts ----------------------------------------------
        if path == "/api/config":
            return self.send_json({"google_client_id": GOOGLE_CLIENT_ID})
        if path == "/api/login" and method == "POST":
            n, wait = fail_count(("login", ip), 600)   # only wrong passwords count
            if n >= 8:
                raise HttpError(429, f"Massa intents fallits. Torna-ho a provar d'aquí {int(wait // 60) + 1} min.")
            d = self.body()
            email = str(d.get("email", "")).strip().lower()
            with lock:
                u = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            if not u or not check_pw(d.get("password", ""), u["pw"]):
                with lock:
                    hits.setdefault(("login", ip), []).append(time.time())
                print(f"login failed from {ip} ({n + 1} recent) known_email={bool(u)}", flush=True)
                time.sleep(1)
                raise HttpError(401, "Correu o contrasenya incorrectes")
            if u["status"] == "blocked":
                raise HttpError(403, "Aquest compte està bloquejat")
            with lock:
                hits.pop(("login", ip), None)
            return self.send_login(u["id"], bool(d.get("remember")))
        if path == "/api/register" and method == "POST":
            if not rate_ok(("register", ip), 5, 3600):
                raise HttpError(429, "Massa registres seguits, espera una mica")
            d = self.body()
            email, pw = str(d.get("email", "")).strip().lower(), str(d.get("password", ""))
            name = " ".join(str(d.get("name", "")).split())[:60] or email.split("@")[0]
            if not EMAIL_RE.match(email) or len(email) > 200:
                raise HttpError(400, "Aquest correu no és vàlid")
            if len(norm_pw(pw)) < 8:
                raise HttpError(400, "La contrasenya ha de tenir almenys 8 caràcters")
            with lock:
                u = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                if u and (u["pw"] or u["google_sub"]):
                    raise HttpError(409, "Ja hi ha un compte amb aquest correu. Inicia la sessió.")
                if u:   # the admin created at install time without a password
                    db.execute("UPDATE users SET pw=?, name=COALESCE(name,?) WHERE id=?", (hash_pw(pw), name, u["id"]))
                    uid = u["id"]
                else:
                    uid = new_account(email, name, pw=pw)
            print(f"new account {email} from {ip}", flush=True)
            return self.send_login(uid, True)
        if path == "/api/google" and method == "POST":
            if not rate_ok(("google", ip), 20, 600):
                raise HttpError(429, "Massa intents, espera una mica")
            sub, email, name = google_identity(str(self.body().get("credential", "")))
            with lock:
                u = db.execute("SELECT * FROM users WHERE google_sub=?", (sub,)).fetchone() or \
                    db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
                if u and u["status"] == "blocked":
                    raise HttpError(403, "Aquest compte està bloquejat")
                if u:
                    db.execute("UPDATE users SET google_sub=? WHERE id=?", (sub, u["id"]))
                    uid = u["id"]
                else:
                    uid = new_account(email, name, google_sub=sub)
                    print(f"new account {email} (Google) from {ip}", flush=True)
            return self.send_login(uid, True)
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
        host = me if owner else guest_host(self.cookie_val("cm_guest"))   # whose music: the user's own, or the jam host's
        if path == "/api/me":
            return self.send_json({"user": user_public(me) if me else None, "owner": owner, "guest": bool(host),
                                   "brand": brand(host)})
        if path == "/api/jam/join" and method == "POST":   # the QR code exchanges into a guest session
            n, wait = fail_count(("join", ip), 600)
            if n >= 20:
                raise HttpError(429, "Massa intents. Torna-ho a provar d'aquí una estona.")
            code = str(self.body().get("k", ""))
            with lock:
                h = db.execute("SELECT id FROM users WHERE jam_code=? AND status='active'", (code,)).fetchone() if code else None
            if not h:
                with lock:
                    hits.setdefault(("join", ip), []).append(time.time())
                raise HttpError(403, "Aquest codi ja no és vàlid. Torna a escanejar el QR de la festa.")
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", self.cookie(new_guest(h["id"]), GUEST_HOURS * 3600, "cm_guest"))
            self.end_headers()
            return self.wfile.write(body)
        m = re.match(r"^/api/logo/(\d+)$", path)
        if m and method == "GET":   # public: jam guests see the host's logo too
            with lock:
                u = db.execute("SELECT logo FROM users WHERE id=?", (int(m.group(1)),)).fetchone()
            if not u or not u["logo"] or not (LOGOS / u["logo"]).is_file():
                raise HttpError(404, "no trobat")
            return self.serve_file(LOGOS / u["logo"], cache=True)
        m = re.match(r"^/api/icon/(\d+)/(\d+)\.png$", path)
        if m and method == "GET" and int(m.group(2)) in ICON_SIZES:   # public, like the logo: the home screen icon
            with lock:
                u = db.execute("SELECT id, logo FROM users WHERE id=?", (int(m.group(1)),)).fetchone()
            return self.serve_file(app_icon(u, int(m.group(2))), cache=True)
        m = re.match(r"^/api/manifest/(\d+)$", path)
        if m and method == "GET":   # the installed app's name and icons, per user
            with lock:
                u = db.execute("SELECT * FROM users WHERE id=?", (int(m.group(1)),)).fetchone()
            if not u:
                raise HttpError(404, "no trobat")
            b, v = brand(u), (u["logo"] or "0").split(".")[0]
            body = json.dumps({"name": b["app_name"], "short_name": b["app_name"][:12], "start_url": "/", "scope": "/",
                               "display": "standalone", "orientation": "portrait", "background_color": "#0b0b0d",
                               "theme_color": "#0b0b0d", "icons": [{"src": f"/api/icon/{u['id']}/{n}.png?v={v}", "sizes": f"{n}x{n}",
                                                                    "type": "image/png", "purpose": "any"} for n in (192, 512)]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)

        # Pages and static files are public. Jam guests (who scanned the current QR) may see the host's
        # queue, search, preview, request songs and reorder the queue. Everything else needs an approved account.
        if not ((method == "GET" and (path in ("/", "/jam", "/sw.js") or path.startswith("/static/")))):
            jam_ok = (method == "GET" and (path in ("/api/state", "/api/search", "/api/songs", "/api/meta") or path.startswith("/media/"))) \
                or (method == "POST" and path in ("/api/request", "/api/queue/order", "/api/prefetch", "/api/ai"))
            if not owner and not (jam_ok and host):
                if me and me["status"] == "pending":
                    raise HttpError(403, "El teu compte encara no està aprovat")
                raise HttpError(401, "Cal escanejar el codi QR de la festa" if jam_ok else "Cal iniciar sessió")
        guest = not owner
        uid = host["id"] if host else None
        admin = owner and me["role"] == "admin"

        if method == "GET":
            if path in ("/", "/jam"):
                return self.serve_file(STATIC / "index.html")
            if path == "/sw.js":   # from the root, so it covers the whole app
                return self.serve_file(STATIC / "sw.js")
            if path.startswith("/static/"):
                p = (STATIC / path[8:]).resolve()
                if p.parent == STATIC.resolve() and p.is_file():
                    return self.serve_file(p, cache=True)
                raise HttpError(404, "no trobat")
            m = MEDIA_RE.match(path[7:]) if path.startswith("/media/") else None
            if m:
                p = SONGS / str(uid) / path[7:]
                if m.group(2) == "mp3":
                    r = db.execute("SELECT status FROM songs WHERE user_id=? AND id=?", (uid, m.group(1))).fetchone()
                    if not r or r["status"] != "ready":
                        raise HttpError(404, "Cançó no disponible")
                if p.exists():
                    return self.serve_file(p, cache=m.group(2) == "jpg")
                raise HttpError(404, "no trobat")
            if path == "/api/state":
                with lock:
                    return self.send_json(full_state(self.headers.get("Host"), host, owner))
            if path == "/api/search":
                q = parse_qs(url.query).get("q", [""])[0].strip()[:500]   # long enough for a pasted link
                if not rate_ok(("search", ip), 40 if guest else 200, 300):
                    raise HttpError(429, "Massa cerques seguides, espera una mica")
                return self.send_json(run_search(q) if q else [])
            if path == "/api/meta":   # date (and missing views/uploader) of search results, filled in after they are shown
                ids = [i for i in parse_qs(url.query).get("ids", [""])[0].split(",") if ID_RE.match(i)][:60]
                return self.send_json(videos_meta(ids))
            if path == "/api/songs":   # download status of a few songs (search results)
                ids = [i for i in parse_qs(url.query).get("ids", [""])[0].split(",") if ID_RE.match(i)][:40]
                with lock:
                    rows = db.execute(f"SELECT id,status,progress FROM songs WHERE user_id=? AND id IN ({','.join('?' * len(ids))})",
                                      [uid, *ids]).fetchall() if ids else []
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
            if path == "/api/lyrics":   # owner only; outside the lock: it waits for the lyrics database
                qs = parse_qs(url.query)
                sid = qs.get("id", [""])[0]
                if not ID_RE.match(sid):
                    raise HttpError(400, "Identificador de vídeo no vàlid")
                if not rate_ok(("lyrics", uid), 120, 600):
                    raise HttpError(429, "Massa lletres seguides, espera una mica")
                return self.send_json(get_lyrics(uid, sid, qs.get("q", [""])[0].strip()[:200],
                                                 bool(host["ai"] or host["role"] == "admin")))
            if path == "/api/admin/users" and admin:
                with lock:
                    return self.send_json(admin_users())
            raise HttpError(404, "no trobat")

        # mutations -------------------------------------------------
        if upload:   # owner only (not a jam route): an audio file from the phone, into a playlist (or "Sense llista")
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                raise HttpError(400, "El fitxer és buit")
            if n > UPLOAD_MAX:
                raise HttpError(413, f"El fitxer és massa gran (màxim {UPLOAD_MAX // 1_000_000} MB)")
            if not rate_ok(("upload", uid), 60, 600):
                raise HttpError(429, "Massa fitxers seguits, espera una mica")
            raw = self.rfile.read(n)
            qs = parse_qs(url.query)
            with lock:
                pid = own_playlist(uid, qs["pid"][0]) if qs.get("pid", [""])[0] else None
                sid = upload_song(uid, raw, unquote(self.headers.get("X-File-Name") or "")[:200], pid)
            return self.send_json({"ok": True, "id": sid})
        data = self.body() if method in ("POST", "PATCH") else {}
        ai_on = bool(host["ai"] or host["role"] == "admin")   # the host decides for the guests of their jam

        if path == "/api/ai/playlist":   # owner only (not a jam route); outside the lock: it waits for Claude
            if not ai_on:
                raise HttpError(403, "La IA no està activada per al teu compte")
            q = str(data.get("q", "")).strip()[:1500]
            if not q:
                raise HttpError(400, "Descriu la llista que vols")
            if not rate_ok(("aipl", uid), 20, 600):
                raise HttpError(429, "Massa llistes seguides, espera una mica")
            return self.send_json(ai_playlist(uid, q))
        if path == "/api/import/spotify" and method == "POST":   # owner only (not a jam route); outside the lock: it reads Spotify
            if not rate_ok(("spimport", uid), 10, 600):
                raise HttpError(429, "Massa importacions seguides, espera una mica")
            return self.send_json(spotify_import(uid, str(data.get("url", "")).strip()[:30000]))
        if path == "/api/import/screens" and method == "POST":   # owner only; outside the lock: it waits for Claude
            if not ai_on:
                raise HttpError(403, "La IA no està activada per al teu compte")
            if not rate_ok(("shots", uid), 10, 600):
                raise HttpError(429, "Massa importacions seguides, espera una mica")
            imgs = data.get("images")
            return self.send_json(screenshots_import(uid, imgs if isinstance(imgs, list) else []))
        if path == "/api/ai":   # outside the lock: it waits for Claude and YouTube
            if not ai_on:
                raise HttpError(403, "La IA no està activada per a aquest compte")
            q = str(data.get("q", "")).strip()[:1000]
            if not q:
                raise HttpError(400, "Descriu què vols escoltar")
            if not rate_ok(("ai", ip if guest else uid), 6 if guest else 40, 600):
                raise HttpError(429, "Massa cerques amb IA seguides, espera una mica")
            return self.send_json(ai_search(q))

        with lock:
            # -- profile: the name, logo and app name each user sees ----------
            if path == "/api/profile" and method == "PATCH":
                if "app_name" in data:
                    db.execute("UPDATE users SET app_name=? WHERE id=?", (" ".join(str(data["app_name"]).split())[:30] or None, uid))
                if "name" in data:
                    db.execute("UPDATE users SET name=? WHERE id=?", (" ".join(str(data["name"]).split())[:60] or me["name"], uid))
                if "xfade" in data:   # crossfade between songs: 0 (off) to 12 seconds
                    try:
                        xf = min(12.0, max(0.0, float(data["xfade"])))
                    except (TypeError, ValueError):
                        raise HttpError(400, "Valor de fosa no vàlid") from None
                    db.execute("UPDATE users SET xfade=? WHERE id=?", (xf, uid))
                return self.send_json({"ok": True})
            if path == "/api/profile/logo":
                for f in LOGOS.glob(f"{uid}-*"):
                    f.unlink(missing_ok=True)
                if method == "DELETE":
                    db.execute("UPDATE users SET logo=NULL WHERE id=?", (uid,))
                    return self.send_json({"ok": True})
                mm = re.match(r"^data:image/(png|jpeg|webp);base64,(.+)$", str(data.get("data", "")), re.S)
                if not mm:
                    raise HttpError(400, "Cal una imatge PNG, JPG o WebP")
                import base64
                raw = base64.b64decode(mm.group(2), validate=False)
                if len(raw) > 2_000_000:
                    raise HttpError(413, "La imatge és massa gran (màxim 2 MB)")
                name = f"{uid}-{secrets.token_hex(4)}.{'jpg' if mm.group(1) == 'jpeg' else mm.group(1)}"
                (LOGOS / name).write_bytes(raw)
                db.execute("UPDATE users SET logo=? WHERE id=?", (name, uid))
                return self.send_json({"ok": True, "logo": logo_url(db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())})
            if path == "/api/profile/password" and method == "POST":
                new = str(data.get("new", ""))
                if me["pw"] and not check_pw(data.get("old", ""), me["pw"]):
                    raise HttpError(403, "La contrasenya actual no és correcta")
                if len(norm_pw(new)) < 8:
                    raise HttpError(400, "La contrasenya ha de tenir almenys 8 caràcters")
                db.execute("UPDATE users SET pw=? WHERE id=?", (hash_pw(new), uid))
                tok = hashlib.sha256(self.session_token().encode()).hexdigest()
                db.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (uid, tok))   # other devices are logged out
                return self.send_json({"ok": True})

            # -- admin: approve, block and manage the other users ----------
            m = re.match(r"^/api/admin/users/(\d+)(/password)?$", path)
            if m:
                if not admin:
                    raise HttpError(403, "Només per a l'administrador")
                tid = int(m.group(1))
                t = db.execute("SELECT * FROM users WHERE id=?", (tid,)).fetchone()
                if not t:
                    raise HttpError(404, "Usuari desconegut")
                if tid == uid and (method == "DELETE" or data.get("status") not in (None, "active") or data.get("role") == "user"):
                    raise HttpError(400, "No et pots treure l'accés a tu mateix")
                if m.group(2) and method == "POST":   # a temporary password to give the user
                    pw = "-".join(secrets.token_hex(2) for _ in range(3))
                    db.execute("UPDATE users SET pw=? WHERE id=?", (hash_pw(pw), tid))
                    db.execute("DELETE FROM sessions WHERE user_id=?", (tid,))
                    return self.send_json({"password": pw})
                if method == "PATCH":
                    if data.get("status") in ("active", "pending", "blocked"):
                        db.execute("UPDATE users SET status=? WHERE id=?", (data["status"], tid))
                        if data["status"] == "blocked":
                            db.execute("DELETE FROM sessions WHERE user_id=?", (tid,))
                    if "ai" in data:
                        db.execute("UPDATE users SET ai=? WHERE id=?", (1 if data["ai"] else 0, tid))
                    if data.get("role") in ("admin", "user"):
                        db.execute("UPDATE users SET role=? WHERE id=?", (data["role"], tid))
                    return self.send_json({"ok": True})
                if method == "DELETE":
                    delete_user(tid)
                    return self.send_json({"ok": True})

            # -- music ----------
            if path == "/api/request":
                mode = data.get("mode", "end")
                if mode not in ("now", "next", "end"):
                    raise HttpError(400, "mode no vàlid")
                if guest:
                    if mode == "now":
                        mode = "next"
                    if not rate_ok(("req", ip), 8, 600):
                        raise HttpError(429, "Massa peticions seguides, espera una mica")
                sid = request_song(uid, data)
                add_to_queue(uid, sid, mode)
                return self.send_json({"ok": True})
            if path == "/api/jam/rotate":   # owner: new QR code; optionally expel every guest
                jam_rotate(uid, bool(data.get("kick")))
                return self.send_json({"ok": True})
            if path == "/api/prefetch":   # download only (for a preview), without queueing
                if guest and not rate_ok(("pre", ip), 15, 600):
                    raise HttpError(429, "Massa descàrregues seguides, espera una mica")
                sid = request_song(uid, data)
                return self.send_json({"ok": True, "status": db.execute("SELECT status FROM songs WHERE user_id=? AND id=?", (uid, sid)).fetchone()["status"]})
            if path == "/api/queue/add":
                add_to_queue(uid, data.get("song_id", ""), data.get("mode", "end"))
                return self.send_json({"ok": True})
            if path == "/api/queue/next":
                advance(uid, data.get("from"))
                return self.send_json({"ok": True})
            if path == "/api/queue/play":
                r = db.execute("SELECT s.status FROM queue q JOIN songs s ON s.user_id=q.user_id AND s.id=q.song_id "
                               "WHERE q.qid=? AND q.user_id=?", (data.get("qid"), uid)).fetchone()
                if not r or r["status"] != "ready":
                    raise HttpError(409, "La cançó encara no s'ha descarregat")
                qid, cur = data["qid"], get_current(uid)
                if cur is not None and cur != qid:
                    db.execute("DELETE FROM queue WHERE qid=?", (cur,))
                renumber([qid] + [i["qid"] for i in q_items(uid) if i["qid"] != qid])
                set_current(uid, qid)
                return self.send_json({"ok": True})
            if path == "/api/queue/order":
                if guest and not rate_ok(("order", ip), 60, 60):
                    raise HttpError(429, "Massa canvis seguits, espera una mica")
                ids = data.get("ids", [])
                old = [i["qid"] for i in q_items(uid)]
                if sorted(ids) != sorted(old):
                    raise HttpError(409, "La cua ha canviat, torna-ho a provar")
                cur = get_current(uid)
                if cur in old and ids[:old.index(cur) + 1] != old[:old.index(cur) + 1]:
                    raise HttpError(409, "No es pot posar cap cançó davant de la que està sonant")
                renumber(ids)
                return self.send_json({"ok": True})
            if path == "/api/queue/remove":
                qid = data.get("qid")
                if qid == get_current(uid):
                    advance(uid, qid)
                else:
                    db.execute("DELETE FROM queue WHERE qid=? AND user_id=?", (qid, uid))
                return self.send_json({"ok": True})
            if path == "/api/queue/clear":
                db.execute("DELETE FROM queue WHERE user_id=?", (uid,))
                set_current(uid, None)
                return self.send_json({"ok": True})
            if path == "/api/queue/set":   # back online: the queue the phone played without a connection carries on here
                sids = [s for s in data.get("song_ids", []) if isinstance(s, str)][:2000]
                known = {r["id"] for r in db.execute(
                    "SELECT id FROM songs WHERE user_id=? AND status='ready' AND id IN (%s)" % ",".join("?" * len(sids)),
                    [uid, *sids])} if sids else set()
                db.execute("DELETE FROM queue WHERE user_id=?", (uid,))
                set_current(uid, None)
                qids = [db.execute("INSERT INTO queue(user_id,song_id,pos) VALUES(?,?,?)", (uid, sid, i)).lastrowid
                        if sid in known else None for i, sid in enumerate(sids)]
                ci = data.get("current")
                if isinstance(ci, int) and 0 <= ci < len(qids) and qids[ci] is not None:
                    set_current(uid, qids[ci])
                ensure_current(uid)
                return self.send_json({"qids": qids, "current": get_current(uid)})
            if path == "/api/queue/load":
                pl = own_playlist(uid, data.get("playlist_id"))
                sids = [r["song_id"] for r in db.execute(
                    "SELECT song_id FROM playlist_songs WHERE playlist_id=? ORDER BY pos", (pl,))]
                if data.get("mode", "replace") == "replace":
                    db.execute("DELETE FROM queue WHERE user_id=?", (uid,))
                    set_current(uid, None)
                for sid in sids:
                    add_to_queue(uid, sid, "end")
                return self.send_json({"ok": True})

            if path == "/api/playlists" and method == "POST":
                name = (data.get("name") or "").strip()[:80]
                if not name:
                    raise HttpError(400, "Cal un nom")
                pid = db.execute("INSERT INTO playlists(user_id,name) VALUES(?,?)", (uid, name)).lastrowid
                return self.send_json({"id": pid})
            m = re.match(r"^/api/playlists/(\d+)$", path)
            if m:
                pid = own_playlist(uid, m.group(1))
                if method == "PATCH":
                    db.execute("UPDATE playlists SET name=? WHERE id=?", ((data.get("name") or "").strip()[:80], pid))
                elif method == "DELETE":
                    db.execute("DELETE FROM playlists WHERE id=?", (pid,))
                return self.send_json({"ok": True})
            m = re.match(r"^/api/playlists/(\d+)/request$", path)
            if m and method == "POST":
                pid = own_playlist(uid, m.group(1))
                existing = db.execute("SELECT status FROM songs WHERE user_id=? AND id=?", (uid, data.get("video_id", ""))).fetchone()
                sid = request_song(uid, data)  # downloads only if we do not have it yet
                already = bool(db.execute("SELECT 1 FROM playlist_songs WHERE playlist_id=? AND song_id=?",
                                          (pid, sid)).fetchone())
                if not already:
                    nxt = db.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM playlist_songs WHERE playlist_id=?",
                                     (pid,)).fetchone()[0]
                    db.execute("INSERT INTO playlist_songs(playlist_id,user_id,song_id,pos) VALUES(?,?,?,?)", (pid, uid, sid, nxt))
                return self.send_json({"ok": True, "already": already,
                                       "had_download": bool(existing and existing["status"] == "ready")})
            m = re.match(r"^/api/playlists/(\d+)/songs$", path)
            if m and method == "POST":
                pid = own_playlist(uid, m.group(1))
                for sid in data.get("song_ids", []):
                    s = db.execute("SELECT status FROM songs WHERE user_id=? AND id=?", (uid, sid)).fetchone()
                    if not s or s["status"] == "error":
                        raise HttpError(409, "Aquesta cançó no s'ha pogut descarregar")
                    nxt = db.execute("SELECT COALESCE(MAX(pos),-1)+1 FROM playlist_songs WHERE playlist_id=?",
                                     (pid,)).fetchone()[0]
                    db.execute("INSERT OR IGNORE INTO playlist_songs(playlist_id,user_id,song_id,pos) VALUES(?,?,?,?)",
                               (pid, uid, sid, nxt))
                return self.send_json({"ok": True})
            m = re.match(r"^/api/playlists/(\d+)/songs/([\w-]{11})$", path)
            if m and method == "DELETE":
                pid, sid = own_playlist(uid, m.group(1)), m.group(2)
                db.execute("DELETE FROM playlist_songs WHERE playlist_id=? AND song_id=?", (pid, sid))
                still = db.execute("SELECT 1 FROM playlist_songs WHERE user_id=? AND song_id=?", (uid, sid)).fetchone()
                if not still:
                    delete_song(uid, sid)  # in no playlist any more: remove the download too
                return self.send_json({"ok": True})
            m = re.match(r"^/api/playlists/(\d+)/order$", path)
            if m and method == "POST":
                pid = own_playlist(uid, m.group(1))
                ids = data.get("ids", [])
                have = [r["song_id"] for r in db.execute("SELECT song_id FROM playlist_songs WHERE playlist_id=?", (pid,))]
                if sorted(ids) != sorted(have):
                    raise HttpError(409, "La llista ha canviat, torna-ho a provar")
                for i, sid in enumerate(ids):
                    db.execute("UPDATE playlist_songs SET pos=? WHERE playlist_id=? AND song_id=?", (i, pid, sid))
                return self.send_json({"ok": True})
            if path == "/api/songs/retry":   # failed downloads: try again now, with a fresh set of attempts
                for sid in data.get("ids", []):
                    if str(sid).startswith("up_"):   # an uploaded file: not on YouTube
                        continue
                    if db.execute("SELECT 1 FROM songs WHERE user_id=? AND id=? AND status='error'", (uid, sid)).fetchone():
                        db.execute("UPDATE songs SET tries=0 WHERE user_id=? AND id=?", (uid, sid))
                        start_download(uid, sid)
                return self.send_json({"ok": True})
            if path == "/api/lyrics/offset":   # owner only: moves the synced lyrics to match this video
                sid = str(data.get("id", ""))
                try:
                    off = max(-120.0, min(120.0, round(float(data.get("offset", 0)), 2)))
                except (TypeError, ValueError):
                    raise HttpError(400, "Desfasament no vàlid")
                if not db.execute("SELECT 1 FROM songs WHERE user_id=? AND id=?", (uid, sid)).fetchone():
                    raise HttpError(404, "Cançó desconeguda")
                db.execute("UPDATE lyrics SET offset=? WHERE id=?", (off, sid))
                return self.send_json({"ok": True, "offset": off})
            if path == "/api/songs/discard":   # failed downloads: forget them (out of the queue and every playlist)
                for sid in data.get("ids", []):
                    if db.execute("SELECT 1 FROM songs WHERE user_id=? AND id=? AND status='error'", (uid, sid)).fetchone():
                        delete_song(uid, sid)
                return self.send_json({"ok": True})
            if path == "/api/songs/delete":   # several downloads at once ("Sense llista"): out of the queue and every playlist
                for sid in data.get("ids", [])[:2000]:
                    if isinstance(sid, str) and ID_RE.match(sid):
                        delete_song(uid, sid)
                return self.send_json({"ok": True})
            m = re.match(r"^/api/songs/([\w-]{11})$", path)
            if m and method == "DELETE":
                delete_song(uid, m.group(1))
                return self.send_json({"ok": True})
        raise HttpError(404, "no trobat")


def own_playlist(uid, pid):
    """A playlist id of this user (404 for anyone else's)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        raise HttpError(404, "Llista desconeguda")
    if not db.execute("SELECT 1 FROM playlists WHERE id=? AND user_id=?", (pid, uid)).fetchone():
        raise HttpError(404, "Llista desconeguda")
    return pid


if __name__ == "__main__":
    if sys.stdout is None or sys.stderr is None:  # started with pythonw: no console, log to a file
        _log = open(DATA / "server.log", "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = _log
    print(f"coniMIX · dades: {DATA}\nAdreça: http://{lan_ip()}:{PORT}", flush=True)
    threading.Thread(target=retry_loop, daemon=True).start()
    threading.Thread(target=normalize_backlog, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
