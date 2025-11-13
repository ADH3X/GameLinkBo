# init.py — Inicializa DB del catálogo (SQLite)
# - Crea admins por defecto
# - Activa FTS5 si está disponible (y no está desactivado por env)
# - Usa rutas escribibles (Render: /data o /var/tmp) para DB y uploads

from __future__ import annotations
import os
import uuid
import datetime
import sys
import pathlib
import bcrypt

# --------------------------------------------------------------------
#  DETECCIÓN ENTORNO / RUTAS ESCRIBIBLES
# --------------------------------------------------------------------
BASE_DIR = pathlib.Path(__file__).resolve().parent


def writable_base() -> pathlib.Path:
    """
    Retorna un directorio escribible adecuado:
    - Si existe /data (Render con Disk), úsalo.
    - Si no, usa /var/tmp (es volátil pero escribible en el free plan).
    - En local, usa el propio proyecto (BASE_DIR).
    """
    # Permite forzar por env si quieres: APP_DATA_DIR
    forced = os.getenv("APP_DATA_DIR")
    if forced:
        p = pathlib.Path(forced)
        p.mkdir(parents=True, exist_ok=True)
        return p

    # Render: Disk persistente
    data = pathlib.Path("/data")
    if data.exists() and os.access(str(data), os.W_OK):
        (data / "uploads").mkdir(parents=True, exist_ok=True)
        return data

    # Render: tmp efímero pero escribible
    vartmp = pathlib.Path("/var/tmp/gamelinkbo")
    try:
        vartmp.mkdir(parents=True, exist_ok=True)
        return vartmp
    except Exception:
        # Local o fallback: directorio del proyecto
        return BASE_DIR


DATA_DIR = writable_base()

# --------------------------------------------------------------------
#  CONFIG
# --------------------------------------------------------------------
DEFAULT_WHATSAPP = os.getenv("WHATSAPP_NUMBER", "59177676446")

# DB en un path escribible (Render: /data/market.db)
DB_PATH = pathlib.Path(os.getenv("MARKET_DB_PATH", str(DATA_DIR / "market.db")))

# Uploads ESCRIBIBLES (Render: /data/uploads)
UPLOAD_BASE = pathlib.Path(os.getenv("UPLOAD_DIR", str(DATA_DIR / "uploads")))
UPLOAD_ORIG = UPLOAD_BASE / "originals"
UPLOAD_THUM = UPLOAD_BASE / "thumbs"

ADMINS = [
    ("Adhex",      os.getenv("ADMIN1_PASS", "A!dhex2025_#Secure")),
    ("CarlFranxx", os.getenv("ADMIN2_PASS", "C@rlXx2025_#Admin")),
]

DISABLE_FTS = os.getenv("DISABLE_FTS", "0") == "1"

# --------------------------------------------------------------------
#  SQLITE (con fallback pysqlite3 moderno si existe)
# --------------------------------------------------------------------
try:
    __import__("pysqlite3")  # pip install pysqlite3-binary (opcional)
    _sys = sys
    _sys.modules["sqlite3"] = _sys.modules.pop("pysqlite3")
except Exception:
    pass

import sqlite3  # noqa: E402  (ya es pysqlite3 si estaba disponible)


# --------------------------------------------------------------------
#  HELPERS
# --------------------------------------------------------------------
def now_iso() -> str:
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat()


def gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def ensure_dirs() -> None:
    """
    Crea carpetas necesarias (DB, uploads originales y thumbs).
    """
    for p in (UPLOAD_ORIG, UPLOAD_THUM, DB_PATH.parent):
        p.mkdir(parents=True, exist_ok=True)


def connect_db() -> sqlite3.Connection:
    """
    Usa SIEMPRE DB_PATH, con foreign_keys y WAL activado.
    """
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON;")
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous = NORMAL;")
    return con


# --------------------------------------------------------------------
#  SCHEMA BASE (sin FTS)
# --------------------------------------------------------------------
SCHEMA_BASE = f"""
PRAGMA foreign_keys = ON;

-- SETTINGS
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
INSERT OR IGNORE INTO settings(key, value) VALUES
  ('site_name','GameLinkBo'),
  ('whatsapp_number','{DEFAULT_WHATSAPP}'),
  ('search_engine','like');  -- 'fts' si activamos FTS5

-- USERS
CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  username      TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL CHECK(role IN ('ADMIN')),
  is_active     INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active);

-- TAXONOMÍAS
CREATE TABLE IF NOT EXISTS platforms (
  id   TEXT PRIMARY KEY,
  name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS genres (
  id   TEXT PRIMARY KEY,
  name TEXT UNIQUE NOT NULL
);

INSERT OR IGNORE INTO platforms(id,name) VALUES
  ('plat_steam','Steam'),
  ('plat_ps','PlayStation'),
  ('plat_xbox','Xbox'),
  ('plat_switch','Switch'),
  ('plat_pc','PC');

INSERT OR IGNORE INTO genres(id,name) VALUES
  ('gen_acc','Acción'),
  ('gen_adv','Aventura'),
  ('gen_rpg','RPG'),
  ('gen_sho','Shooter'),
  ('gen_ind','Indie'),
  ('gen_spo','Deportes'),
  ('gen_str','Estrategia');

-- GAMES
CREATE TABLE IF NOT EXISTS games (
  id                TEXT PRIMARY KEY,
  slug              TEXT UNIQUE NOT NULL,
  title             TEXT NOT NULL,
  description       TEXT NOT NULL DEFAULT '',
  platform_id       TEXT NOT NULL REFERENCES platforms(id) ON DELETE RESTRICT,
  base_price        REAL NOT NULL CHECK(base_price >= 0),
  discount_pct      REAL NOT NULL DEFAULT 0 CHECK(discount_pct BETWEEN 0 AND 95),
  whatsapp_override TEXT,
  is_published      INTEGER NOT NULL DEFAULT 0,
  created_by        TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_games_platform  ON games(platform_id);
CREATE INDEX IF NOT EXISTS idx_games_published ON games(is_published);
CREATE INDEX IF NOT EXISTS idx_games_price     ON games(base_price);

CREATE TABLE IF NOT EXISTS game_genres (
  game_id  TEXT NOT NULL REFERENCES games(id)  ON DELETE CASCADE,
  genre_id TEXT NOT NULL REFERENCES genres(id) ON DELETE RESTRICT,
  PRIMARY KEY (game_id, genre_id)
);

CREATE TABLE IF NOT EXISTS game_images (
  id         TEXT PRIMARY KEY,
  game_id    TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  file_name  TEXT NOT NULL,
  file_path  TEXT NOT NULL,
  thumb_path TEXT NOT NULL,
  is_cover   INTEGER NOT NULL DEFAULT 0,
  order_idx  INTEGER NOT NULL DEFAULT 0
);

-- Auditoría
CREATE TABLE IF NOT EXISTS audit_logs (
  id         TEXT PRIMARY KEY,
  actor_id   TEXT REFERENCES users(id) ON DELETE SET NULL,
  action     TEXT NOT NULL,
  entity     TEXT NOT NULL,
  entity_id  TEXT NOT NULL,
  old_data   TEXT,
  new_data   TEXT,
  created_at TEXT NOT NULL
);
"""


# --------------------------------------------------------------------
#  INTENTAR ACTIVAR FTS5 (si no está deshabilitado)
# --------------------------------------------------------------------
SCHEMA_FTS = """
-- Limpieza por si existiera algo viejo
DROP TRIGGER IF EXISTS games_ai;
DROP TRIGGER IF EXISTS games_ad;
DROP TRIGGER IF EXISTS games_au;
DROP VIRTUAL TABLE IF EXISTS games_fts;

-- FTS5: indexa title y description (content=games enlaza por rowid)
CREATE VIRTUAL TABLE games_fts
USING fts5(
  title, description,
  content='games',
  content_rowid='rowid'
);

-- Sincronización básica
CREATE TRIGGER games_ai AFTER INSERT ON games BEGIN
  INSERT INTO games_fts(rowid, title, description)
  VALUES (new.rowid, new.title, new.description);
END;
CREATE TRIGGER games_ad AFTER DELETE ON games BEGIN
  DELETE FROM games_fts WHERE rowid = old.rowid;
END;
CREATE TRIGGER games_au AFTER UPDATE OF title, description ON games BEGIN
  UPDATE games_fts SET title=new.title, description=new.description
  WHERE rowid=old.rowid;
END;

-- Backfill por si ya había datos
INSERT INTO games_fts(rowid, title, description)
  SELECT rowid, title, description
  FROM games
  WHERE rowid NOT IN (SELECT rowid FROM games_fts);
"""


# --------------------------------------------------------------------
#  UTILS DE SCHEMA
# --------------------------------------------------------------------
def exec_schema(con: sqlite3.Connection) -> None:
    """
    Crea/actualiza el esquema base y, si es posible, FTS5.
    Respeta DISABLE_FTS=1 para no intentar FTS.
    """
    con.executescript(SCHEMA_BASE)

    if DISABLE_FTS:
        print("[i] FTS5 deshabilitado por DISABLE_FTS=1. Usando búsqueda LIKE.")
        return

    try:
        con.executescript(SCHEMA_FTS)
        con.execute("UPDATE settings SET value='fts' WHERE key='search_engine'")
        print("[+] Búsqueda FTS5 activa.")
    except sqlite3.Error as e:
        print("[!] FTS5 no disponible, usando búsqueda simple (LIKE). Detalle:", e)


def upsert_setting(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def ensure_admin(con: sqlite3.Connection, username: str, password: str) -> str:
    row = con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if row:
        return row["id"]

    uid = gen_id("usr")
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    con.execute(
        "INSERT INTO users(id,username,password_hash,role,is_active,created_at) "
        "VALUES (?,?,?,?,?,?)",
        (uid, username, pw_hash, "ADMIN", 1, now_iso()),
    )
    return uid


# --------------------------------------------------------------------
#  MAIN (CLI)
# --------------------------------------------------------------------
def main() -> None:
    print(f"[+] DATA_DIR: {DATA_DIR}")
    print(f"[+] Inicializando DB en: {DB_PATH}")
    ensure_dirs()

    con = connect_db()
    try:
        exec_schema(con)
        upsert_setting(con, "whatsapp_number", DEFAULT_WHATSAPP)

        print("[+] Creando/asegurando admins por defecto...")
        for u, p in ADMINS:
            ensure_admin(con, u, p)

        con.commit()
        print("[✓] Esquema creado/actualizado.")
        print("[✓] Admins asegurados:")
        for u, p in ADMINS:
            print(f"   - {u} / {p}")
        print(f"[✓] WhatsApp por defecto: {DEFAULT_WHATSAPP}")
        print("[!] Recuerda cambiar las contraseñas en producción.")
    except Exception as e:
        con.rollback()
        print("[X] Error durante inicialización:", e)
        sys.exit(1)
    finally:
        con.close()


if __name__ == "__main__":
    main()
