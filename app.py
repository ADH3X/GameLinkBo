# app.py — Catálogo + Panel Admin con subida de imágenes (Flask + SQLite + Pillow)
import os, uuid, bcrypt, datetime
from io import BytesIO
from PIL import Image
from flask import (
    Flask, render_template, g, request, redirect, url_for,
    session, flash, abort, send_from_directory
)
from slugify import slugify
from pathlib import Path
import pathlib
from init import connect_db  # usa SIEMPRE esta conexión (apunta a /data/market.db)

# =============================
# CONFIG
# =============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Render monta el disco persistente en /data
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))

UPLOADS_DIR = DATA_DIR / "uploads"
ORIG_DIR    = UPLOADS_DIR / "originals"
THUM_DIR    = UPLOADS_DIR / "thumbs"

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp"}
THUMB_SIZE  = (512, 512)

os.makedirs(ORIG_DIR, exist_ok=True)
os.makedirs(THUM_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = "cambia-esto-en-produccion"

# =============================
# DB
# =============================
def get_db():
    if "db" not in g:
        g.db = connect_db()     # ahora siempre /data/market.db
    return g.db

@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

# =============================
# HELPERS
# =============================
def now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat()

def gen_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex}"

def allowed_file(filename: str) -> bool:
    ext = os.path.splitext(filename.lower())[1]
    return ext in ALLOWED_EXT

def save_image(file_storage, slug: str):
    """
    Guarda original y genera thumbnail 512x512 recortado al centro.
    Retorna (file_path_rel, thumb_path_rel) relativos a la carpeta /data/uploads.
    """
    filename = file_storage.filename
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise ValueError("Formato no permitido")

    uid = uuid.uuid4().hex
    base_name = f"{slug}-{uid}{ext}"

    # Rutas físicas en el disco persistente
    orig_abs = ORIG_DIR / base_name
    thum_abs = THUM_DIR / base_name

    # Guardar original
    file_storage.save(orig_abs)

    # Generar thumbnail
    with Image.open(orig_abs) as im:
        im = im.convert("RGB")
        w, h = im.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        im = im.crop((left, top, left + side, top + side))
        im = im.resize(THUMB_SIZE, Image.Resampling.LANCZOS)
        im.save(thum_abs, format="JPEG", quality=90)

    # Lo que guardamos en BD: rutas RELATIVAS dentro de uploads
    orig_rel = f"originals/{base_name}"
    thum_rel = f"thumbs/{base_name}"
    return orig_rel, thum_rel

def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper

def file_safe_delete(path_str: str):
    """
    Borra un archivo dentro de /data/uploads si existe (ignora errores).
    path_str: 'originals/xxx.jpg', 'thumbs/xxx.jpg', etc.
    """
    try:
        if not path_str:
            return
        p = UPLOADS_DIR / pathlib.Path(path_str)
        if p.exists() and p.is_file():
            p.unlink(missing_ok=True)
    except Exception:
        pass  # no romper el flujo por un archivo

def delete_game_files(db, game_id: str):
    """
    Elimina del disco todas las imágenes asociadas al juego (covers y thumbs).
    Luego, al borrar el juego, las filas de game_images y game_genres
    se van por ON DELETE CASCADE.
    """
    cur = db.execute(
        "SELECT file_path, thumb_path FROM game_images WHERE game_id=?",
        (game_id,)
    )
    for row in cur.fetchall():
        file_safe_delete(row["file_path"])
        file_safe_delete(row["thumb_path"])

# =============================
# RUTA PARA SERVIR IMÁGENES
# =============================
@app.route("/media/<path:filename>")
def media(filename):
    # Sirve archivos desde el disco persistente /data/uploads
    return send_from_directory(UPLOADS_DIR, filename)

# =============================
# AUTH ADMIN
# =============================
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").encode("utf-8")

        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username=? AND is_active=1",
            (username,)
        ).fetchone()

        if user and bcrypt.checkpw(password, user["password_hash"].encode("utf-8")):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            return redirect(url_for("admin_dashboard"))

        flash("Usuario o contraseña incorrectos", "error")

    return render_template("admin/login.html")

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    flash("Sesión cerrada", "info")
    return redirect(url_for("admin_login"))

@app.post("/admin/games/<game_id>/delete")
def admin_delete_game(game_id):
    if not session.get("user_id"):
        flash("Inicia sesión.", "error")
        return redirect(url_for("admin_login"))

    db = get_db()

    # 1) borrar archivos físicos
    delete_game_files(db, game_id)

    # 2) borrar registros relacionados a mano
    db.execute("DELETE FROM game_images WHERE game_id=?", (game_id,))
    db.execute("DELETE FROM game_genres WHERE game_id=?", (game_id,))

    # 3) borrar el juego
    db.execute("DELETE FROM games WHERE id=?", (game_id,))
    db.commit()

    flash("Juego eliminado correctamente.", "success")
    return redirect(url_for("admin_games"))

# =============================
# ADMIN — DASH, LISTA, CREAR, EDITAR, IMÁGENES
# =============================
@app.route("/admin/dashboard")
@login_required
def admin_dashboard():
    db = get_db()
    total_games = db.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    published   = db.execute("SELECT COUNT(*) FROM games WHERE is_published=1").fetchone()[0]
    unpublished = total_games - published
    return render_template(
        "admin/dashboard.html",
        total_games=total_games,
        published=published,
        unpublished=unpublished
    )

@app.route("/admin/games")
@login_required
def admin_games():
    db = get_db()

    q = request.args.get("q", "").strip()
    params = []

    base_sql = """
        SELECT g.id,
               g.slug,
               g.title,
               g.base_price,
               g.is_published,
               (SELECT '/media/' || thumb_path
                  FROM game_images
                 WHERE game_id = g.id AND is_cover = 1
                 LIMIT 1) AS cover,
               p.name AS platform_name
        FROM games g
        JOIN platforms p ON p.id = g.platform_id
    """

    # Si hay búsqueda, filtramos por título o slug (similar, no exacto)
    if q:
        base_sql += " WHERE LOWER(g.title) LIKE ? OR LOWER(g.slug) LIKE ?"
        like_q = f"%{q.lower()}%"
        params.extend([like_q, like_q])

    base_sql += " ORDER BY g.created_at DESC"

    rows = db.execute(base_sql, params).fetchall()

    return render_template(
        "admin/game_list.html",
        games=rows,
        q=q  # para rellenar el input
    )


@app.route("/admin/games/new", methods=["GET", "POST"])
@login_required
def admin_games_new():
    db = get_db()
    platforms = db.execute("SELECT id, name FROM platforms ORDER BY name").fetchall()
    genres    = db.execute("SELECT id, name FROM genres ORDER BY name").fetchall()

    if request.method == "POST":
        title       = request.form["title"].strip()
        platform_id = request.form["platform_id"]
        price       = float(request.form.get("base_price", 0) or 0)
        discount    = float(request.form.get("discount_pct", 0) or 0)
        description = request.form.get("description", "").strip()
        genre_ids   = request.form.getlist("genres")
        publish     = 1 if request.form.get("publish") == "on" else 0

        if not title or not platform_id:
            flash("Título y plataforma son obligatorios", "error")
            return redirect(url_for("admin_games_new"))

        slug = slugify(title)
        game_id = gen_id("game")
        now = now_iso()
        db.execute("""
            INSERT INTO games (id, slug, title, description, platform_id, base_price, discount_pct,
                               is_published, created_by, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (game_id, slug, title, description, platform_id, price, discount, publish,
              session["user_id"], now, now))

        for gid in genre_ids:
            db.execute(
                "INSERT OR IGNORE INTO game_genres (game_id, genre_id) VALUES (?,?)",
                (game_id, gid)
            )

        files = request.files.getlist("images")
        order_idx = 0
        cover_set = False
        for f in files:
            if f and allowed_file(f.filename):
                try:
                    file_path, thumb_path = save_image(f, slug)
                    img_id = gen_id("img")
                    is_cover = 1 if not cover_set else 0
                    db.execute("""
                        INSERT INTO game_images
                            (id, game_id, file_name, file_path, thumb_path, is_cover, order_idx)
                        VALUES (?,?,?,?,?,?,?)
                    """, (
                        img_id,
                        game_id,
                        os.path.basename(file_path),
                        file_path,
                        thumb_path,
                        is_cover,
                        order_idx,
                    ))
                    cover_set = True if is_cover == 1 else cover_set
                    order_idx += 1
                except Exception as e:
                    flash(f"Error subiendo imagen: {e}", "error")

        db.commit()
        flash("Juego creado", "success")
        return redirect(url_for("admin_games"))

    return render_template(
        "admin/game_form.html",
        platforms=platforms,
        genres=genres,
        form_action=url_for("admin_games_new"),
        game=None
    )

@app.route("/admin/games/<game_id>/edit", methods=["GET", "POST"])
@login_required
def admin_games_edit(game_id):
    db = get_db()
    game = db.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
    if not game:
        abort(404)

    platforms = db.execute("SELECT id, name FROM platforms ORDER BY name").fetchall()
    genres    = db.execute("SELECT id, name FROM genres ORDER BY name").fetchall()

    current_genres = {
        r["genre_id"]
        for r in db.execute(
            "SELECT genre_id FROM game_genres WHERE game_id=?",
            (game_id,)
        ).fetchall()
    }

    images = db.execute("""
        SELECT * ,
               '/media/' || thumb_path AS thumb_url,
               '/media/' || file_path  AS file_url
        FROM game_images
        WHERE game_id = ?
        ORDER BY is_cover DESC, order_idx ASC
    """, (game_id,)).fetchall()

    if request.method == "POST":
        title       = request.form["title"].strip()
        platform_id = request.form["platform_id"]
        price       = float(request.form.get("base_price", 0) or 0)
        discount    = float(request.form.get("discount_pct", 0) or 0)
        description = request.form.get("description", "").strip()
        publish     = 1 if request.form.get("publish") == "on" else 0
        genre_ids   = set(request.form.getlist("genres"))

        if not title or not platform_id:
            flash("Título y plataforma son obligatorios", "error")
            return redirect(url_for("admin_games_edit", game_id=game_id))

        slug = slugify(title)

        # Actualizar datos básicos del juego
        db.execute("""
            UPDATE games
               SET slug=?,
                   title=?,
                   description=?,
                   platform_id=?,
                   base_price=?,
                   discount_pct=?,
                   is_published=?,
                   updated_at=?
             WHERE id=?
        """, (
            slug,
            title,
            description,
            platform_id,
            price,
            discount,
            publish,
            now_iso(),
            game_id,
        ))

        # Sincronizar géneros
        for gid in (genre_ids - current_genres):
            db.execute(
                "INSERT OR IGNORE INTO game_genres (game_id, genre_id) VALUES (?,?)",
                (game_id, gid)
            )
        for gid in (current_genres - genre_ids):
            db.execute(
                "DELETE FROM game_genres WHERE game_id=? AND genre_id=?",
                (game_id, gid)
            )

        # ============================
        # IMÁGENES NUEVAS
        # ============================
        files = request.files.getlist("images")

        # ¿El admin subió al menos una imagen válida?
        have_new_files = any(f and allowed_file(f.filename) for f in files)

        if have_new_files:
            # 1) Borrar TODAS las imágenes actuales del juego (DB + archivos)
            old_imgs = db.execute(
                "SELECT id, file_path, thumb_path FROM game_images WHERE game_id=?",
                (game_id,)
            ).fetchall()

            for row in old_imgs:
                file_safe_delete(row["file_path"])
                file_safe_delete(row["thumb_path"])

            db.execute("DELETE FROM game_images WHERE game_id=?", (game_id,))

            # 2) Insertar las nuevas con el sistema /data/uploads/... 
            order_idx = 0
            cover_set = False

            for f in files:
                if f and allowed_file(f.filename):
                    try:
                        file_path, thumb_path = save_image(f, slug)
                        img_id = gen_id("img")

                        is_cover = 1 if not cover_set else 0

                        db.execute("""
                            INSERT INTO game_images
                                (id, game_id, file_name, file_path, thumb_path, is_cover, order_idx)
                            VALUES (?,?,?,?,?,?,?)
                        """, (
                            img_id,
                            game_id,
                            os.path.basename(file_path),
                            file_path,
                            thumb_path,
                            is_cover,
                            order_idx,
                        ))

                        if is_cover == 1:
                            cover_set = True
                        order_idx += 1

                    except Exception as e:
                        flash(f"Error subiendo imagen: {e}", "error")

        db.commit()
        flash("Juego actualizado", "success")
        return redirect(url_for("admin_games_edit", game_id=game_id))

    return render_template(
        "admin/game_form.html",
        platforms=platforms,
        genres=genres,
        game=game,
        current_genres=current_genres,
        images=images,
        form_action=url_for("admin_games_edit", game_id=game_id),
    )

@app.post("/admin/games/<game_id>/publish")
@login_required
def admin_games_publish(game_id):
    db = get_db()
    game = db.execute("SELECT is_published FROM games WHERE id=?", (game_id,)).fetchone()
    if not game:
        abort(404)
    new_state = 0 if game["is_published"] == 1 else 1
    db.execute(
        "UPDATE games SET is_published=?, updated_at=? WHERE id=?",
        (new_state, now_iso(), game_id)
    )
    db.commit()
    flash("Estado actualizado", "success")
    return redirect(url_for("admin_games"))

@app.post("/admin/images/<img_id>/cover")
@login_required
def admin_image_set_cover(img_id):
    db = get_db()
    row = db.execute(
        "SELECT game_id FROM game_images WHERE id=?",
        (img_id,)
    ).fetchone()
    if not row:
        abort(404)
    game_id = row["game_id"]
    db.execute("UPDATE game_images SET is_cover=0 WHERE game_id=?", (game_id,))
    db.execute("UPDATE game_images SET is_cover=1 WHERE id=?", (img_id,))
    db.commit()
    flash("Portada actualizada", "success")
    return redirect(url_for("admin_games_edit", game_id=game_id))

@app.post("/admin/images/<img_id>/delete")
@login_required
def admin_image_delete(img_id):
    db = get_db()
    row = db.execute(
        "SELECT game_id, file_path, thumb_path FROM game_images WHERE id=?",
        (img_id,)
    ).fetchone()
    if not row:
        abort(404)

    # borrar registro
    db.execute("DELETE FROM game_images WHERE id=?", (img_id,))
    db.commit()

    # borrar archivos físicos
    file_safe_delete(row["file_path"])
    file_safe_delete(row["thumb_path"])

    flash("Imagen eliminada", "success")
    return redirect(url_for("admin_games_edit", game_id=row["game_id"]))

# =============================
# STORE PÚBLICO
# =============================
@app.route("/")
def home():
    db = get_db()
    games = db.execute("""
        SELECT g.id,
               g.slug,
               g.title,
               g.base_price,
               g.discount_pct,
               (SELECT '/media/' || thumb_path
                  FROM game_images
                 WHERE game_id = g.id AND is_cover = 1
                 LIMIT 1) AS cover
        FROM games g
        WHERE g.is_published = 1
        ORDER BY g.created_at DESC
        LIMIT 8;
    """).fetchall()
    return render_template("store/home.html", games=games)

@app.route("/games")
def catalog():
    db = get_db()
    q        = request.args.get("q", "").strip()
    platform = request.args.get("platform", "").strip() or None

    # Para filtros (todas las plataformas)
    platforms = db.execute(
        "SELECT id, name FROM platforms ORDER BY name"
    ).fetchall()

    search_engine = db.execute(
        "SELECT value FROM settings WHERE key='search_engine'"
    ).fetchone()["value"]

    base_sql = """
        SELECT g.id,
               g.slug,
               g.title,
               g.base_price,
               g.discount_pct,
               g.platform_id,
               (SELECT '/media/' || thumb_path
                  FROM game_images
                 WHERE game_id = g.id AND is_cover = 1
                 LIMIT 1) AS cover
        FROM games g
        WHERE g.is_published = 1
    """
    params = []

    if platform:
        base_sql += " AND g.platform_id = ?"
        params.append(platform)

    if q:
        if search_engine == "fts":
            base_sql += " AND g.rowid IN (SELECT rowid FROM games_fts WHERE games_fts MATCH ?)"
            params.append(q)
        else:
            base_sql += " AND (g.title LIKE ? OR g.description LIKE ?)"
            like_q = f"%{q}%"
            params.extend([like_q, like_q])

    base_sql += " ORDER BY g.created_at DESC LIMIT 60;"
    games = db.execute(base_sql, params).fetchall()

    return render_template(
        "store/catalog.html",
        games=games,
        q=q,
        platform=platform,
        platforms=platforms
    )


@app.route("/game/<slug>")
def game_detail(slug):
    db = get_db()
    game = db.execute("""
        SELECT g.*, p.name AS platform_name
        FROM games g
        JOIN platforms p ON g.platform_id = p.id
        WHERE g.slug = ? AND g.is_published = 1
    """, (slug,)).fetchone()
    if not game:
        abort(404)

    images = db.execute("""
        SELECT id,
               file_path,
               thumb_path,
               is_cover,
               '/media/' || thumb_path AS thumb_url,
               '/media/' || file_path  AS file_url
        FROM game_images
        WHERE game_id = ?
        ORDER BY is_cover DESC, order_idx ASC
    """, (game["id"],)).fetchall()

    wa = game["whatsapp_override"] or db.execute(
        "SELECT value FROM settings WHERE key='whatsapp_number'"
    ).fetchone()["value"]
    msg = f"Hola, quiero comprar {game['title']} ({game['platform_name']})"
    wa_link = f"https://wa.me/{wa}?text={msg.replace(' ', '%20')}"

    return render_template(
        "store/game_detail.html",
        game=game,
        images=images,
        wa_link=wa_link
    )

# =============================
# RUN
# =============================
if __name__ == "__main__":
    app.run(debug=True)
