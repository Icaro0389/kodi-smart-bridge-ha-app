#!/usr/bin/env python3
import base64
import binascii
import fcntl
import hashlib
import json
import os
import sqlite3
import tempfile
import unicodedata
import xml.etree.ElementTree as ET

from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

BASE = Path(os.environ.get("BRIDGE_BASE_DIR", "/data"))
DATA = Path(os.environ.get("BRIDGE_DATA_DIR", "/data/catalogs"))
SETTINGS = Path(os.environ.get("IPTVXC_SETTINGS_PATH", "/data/iptvxc-settings.xml"))

def normalize(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(
        c for c in s if not unicodedata.combining(c)
    ).casefold()

def decode(s):
    s = (s or "").strip()
    try:
        return base64.b64decode(s, validate=True).decode("utf-8")
    except (ValueError, UnicodeError, binascii.Error):
        return s

def settings():
    root = ET.parse(SETTINGS).getroot()
    values = {
        e.get("id"): e.get("value", e.text or "")
        for e in root.iter("setting") if e.get("id")
    }
    dns = (values.get("DNS") or "").strip().rstrip("/")
    user = values.get("Username") or ""
    password = values.get("Password") or ""
    parsed = urlsplit(dns)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("DNS IPTVXC non valido")
    if not user or not password:
        raise ValueError("Credenziali IPTVXC mancanti")
    return dns, user, password

def download(url, limit=110_000_000):
    if urlsplit(url).scheme not in ("http", "https"):
        raise ValueError("Protocollo sorgente non supportato")
    req = Request(url, headers={"User-Agent": "Kodi"})
    chunks = []
    size = 0
    with urlopen(req, timeout=75) as response:
        while True:
            part = response.read(1024 * 1024)
            if not part:
                break
            size += len(part)
            if size > limit:
                raise ValueError("Catalogo oltre il limite previsto")
            chunks.append(part)
    return b"".join(chunks)

def vod_url(dns, user, password):
    url = dns + "/enigma2.php?" + urlencode({
        "username": user,
        "password": password,
        "type": "get_vod_categories"
    })
    root = ET.fromstring(download(url, 12_000_000))

    candidates = []
    for ch in root.findall(".//channel"):
        title = decode(ch.findtext("title"))
        link = (ch.findtext("playlist_url") or "").strip()
        label = normalize(title).strip("- []")
        if link and label in (
            "all", "all movies", "all vod",
            "tutti", "tutti i film"
        ):
            candidates.append(link)

    if len(candidates) != 1:
        raise ValueError(
            "Categoria VOD All non identificata univocamente"
        )

    link = candidates[0]
    link = link.replace("USERNAME", user).replace("PASSWORD", password)

    # Replica il workaround già presente in IPTVXC.
    vod_host = os.environ.get("VOD_FIX_HOST", "").strip()
    vod_port = os.environ.get("VOD_FIX_PORT", "24000").strip()
    if vod_host and vod_port.isdigit() and 1 <= int(vod_port) <= 65535:
        link = link.replace(
            "://" + vod_host + "/",
            "://" + vod_host + ":" + vod_port + "/",
            1
        )
    return link

def existing_count(filename, table):
    path = DATA / filename
    if not path.is_file():
        return 0
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        return db.execute(
            "SELECT COUNT(*) FROM " + table
        ).fetchone()[0]

def current_hash(filename):
    path = DATA / filename
    if not path.is_file():
        return None
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        try:
            row = db.execute(
                "SELECT value FROM metadata WHERE key='source_sha256'"
            ).fetchone()
            return row[0] if row else None
        except sqlite3.OperationalError:
            return None

def stage_database(filename, schema, rows, digest, count):
    fd, temporary = tempfile.mkstemp(
        prefix=".catalog-", suffix=".sqlite3", dir=DATA
    )
    os.close(fd)
    os.chmod(temporary, 0o600)

    try:
        with sqlite3.connect(temporary) as db:
            db.executescript(schema)
            if filename.startswith("vod-"):
                db.executemany(
                    "INSERT INTO movies "
                    "(title,title_search,stream_url,poster,category_id) "
                    "VALUES (?,?,?,?,?)",
                    rows
                )
                table = "movies"
            else:
                db.executemany(
                    "INSERT INTO series "
                    "(series_id,title,title_search,poster,category_id,plot) "
                    "VALUES (?,?,?,?,?,?)",
                    rows
                )
                table = "series"

            db.execute(
                "INSERT INTO metadata VALUES (?,?)",
                ("source_sha256", digest)
            )
            db.execute(
                "INSERT INTO metadata VALUES (?,?)",
                ("source_count", str(count))
            )

            verified = db.execute(
                "SELECT COUNT(*) FROM " + table
            ).fetchone()[0]

            if verified != count:
                raise ValueError("Conteggio SQLite non corrispondente")

            integrity = db.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0]

            if integrity != "ok":
                raise ValueError("Controllo integrità SQLite fallito")

            db.commit()

        return Path(temporary)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise

MOVIES_SCHEMA = """
CREATE TABLE movies (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    title_search TEXT NOT NULL,
    stream_url TEXT,
    poster TEXT,
    category_id TEXT
);
CREATE INDEX idx_movies_search ON movies(title_search);
CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""

SERIES_SCHEMA = """
CREATE TABLE series (
    id INTEGER PRIMARY KEY,
    series_id TEXT NOT NULL,
    title TEXT NOT NULL,
    title_search TEXT NOT NULL,
    poster TEXT,
    category_id TEXT,
    plot TEXT
);
CREATE INDEX idx_series_title ON series(title_search);
CREATE INDEX idx_series_id ON series(series_id);
CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""

def refresh_movies(url):
    raw = download(url)
    digest = hashlib.sha256(raw).hexdigest()

    root = ET.fromstring(raw)
    rows = []
    for ch in root.findall(".//channel"):
        title = decode(ch.findtext("title"))
        rows.append((
            title,
            normalize(title),
            ch.findtext("stream_url") or "",
            ch.findtext("desc_image") or "",
            ch.findtext("category_id") or ""
        ))

    previous = existing_count("vod-xml-index.sqlite3", "movies")

    if len(rows) < 1000 or (
        previous and len(rows) < previous * 0.95
    ):
        raise ValueError("Catalogo Movies/VOD incompleto")

    if current_hash("vod-xml-index.sqlite3") == digest:
        print("Movies/VOD invariato:", len(rows))
        return None

    print("Movies/VOD validato:", len(rows))
    return stage_database(
        "vod-xml-index.sqlite3",
        MOVIES_SCHEMA, rows, digest, len(rows)
    )

def refresh_series(dns, user, password):
    url = dns + "/player_api.php?" + urlencode({
        "username": user,
        "password": password,
        "action": "get_series"
    })

    raw = download(url)
    digest = hashlib.sha256(raw).hexdigest()
    catalog = json.loads(raw)

    if not isinstance(catalog, list):
        raise ValueError("Catalogo Series non valido")

    seen = set()
    rows = []

    for item in catalog:
        if not isinstance(item, dict):
            continue

        sid = str(item.get("series_id") or "").strip()
        title = str(item.get("name") or "").strip()
        if not sid or not title or sid in seen:
            continue

        seen.add(sid)
        rows.append((
            sid, title, normalize(title),
            str(item.get("cover") or ""),
            str(item.get("category_id") or ""),
            str(item.get("plot") or "")[:2000]
        ))

    previous = existing_count(
        "series-json-index.sqlite3", "series"
    )

    if len(rows) < 1000 or (
        previous and len(rows) < previous * 0.95
    ):
        raise ValueError("Catalogo Series incompleto")

    if current_hash("series-json-index.sqlite3") == digest:
        print("Series invariato:", len(rows))
        return None

    print("Series validato:", len(rows))
    return stage_database(
        "series-json-index.sqlite3",
        SERIES_SCHEMA, rows, digest, len(rows)
    )

def main():
    lock_path = DATA / "direct-refresh.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

        dns, user, password = settings()
        staged = []

        try:
            # Prima validiamo entrambi i cataloghi.
            movies = refresh_movies(vod_url(dns, user, password))
            if movies:
                staged.append((movies, DATA / "vod-xml-index.sqlite3"))

            series = refresh_series(dns, user, password)
            if series:
                staged.append((series, DATA / "series-json-index.sqlite3"))

            # Solo dopo il successo pubblichiamo gli indici.
            for temporary, destination in staged:
                os.replace(temporary, destination)
                print("Pubblicato:", destination.name)

            print("REFRESH_COMPLETATO")
        finally:
            for temporary, _ in staged:
                temporary.unlink(missing_ok=True)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Evita di stampare URL o credenziali nei log.
        print(
            "REFRESH_FALLITO:",
            type(exc).__name__,
            flush=True
        )
        raise SystemExit(1)
