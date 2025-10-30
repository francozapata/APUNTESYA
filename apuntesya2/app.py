import os, secrets, math
from datetime import datetime, timedelta
from urllib.parse import urlencode

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for, flash,
    send_from_directory, abort, jsonify
)
from flask_login import LoginManager, login_user, logout_user, current_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from sqlalchemy import create_engine, select, or_, and_, func
from sqlalchemy.orm import sessionmaker, scoped_session

# Modelos & MP helpers
from apuntesya2.models import Base, User, Note, Purchase, University, Faculty, Career
from apuntesya2 import mp

load_dotenv()

# ---------- Comisiones/IIBB defaults ----------
MP_COMMISSION_RATE_DEFAULT = 0.05
APY_COMMISSION_RATE_DEFAULT = 0.0
IIBB_ENABLED_DEFAULT = False
IIBB_RATE_DEFAULT = 0.0

MP_COMMISSION_RATE = MP_COMMISSION_RATE_DEFAULT
APY_COMMISSION_RATE = APY_COMMISSION_RATE_DEFAULT
IIBB_ENABLED = IIBB_ENABLED_DEFAULT
IIBB_RATE = IIBB_RATE_DEFAULT
# --------------------------------------------------------------

app = Flask(__name__, instance_relative_config=True)

# ---------- Health ----------
@app.get("/health")
def health():
    return {"ok": True}, 200

# ---------- Config básica ----------
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", secrets.token_hex(16))
app.config["ENV"] = os.getenv("FLASK_ENV", "production")

# Directorios persistentes (Render free: /tmp es writable)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(BASE_DIR)
DATA_DIR = os.environ.get("DATA_DIR", "/tmp/apuntesya")
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(DATA_DIR, "uploads"))

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_DIR
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25MB

# ---------- DB (robusto con fallback automático) ----------
from sqlalchemy.exc import OperationalError

def _ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass

_ensure_dir(DATA_DIR)

raw_db_url = os.getenv("DATABASE_URL", "").strip()

def _sqlite_url_for(path):
    _ensure_dir(os.path.dirname(path))
    return f"sqlite:///{path}"

if not raw_db_url:
    # No DATABASE_URL definido → usar /tmp
    DB_URL = _sqlite_url_for(os.path.join(DATA_DIR, "apuntesya.db"))
elif raw_db_url.startswith("sqlite:///"):
    sqlite_path = raw_db_url[len("sqlite:///"):]
    # Si no está dentro de /tmp, redirigir
    if not sqlite_path.startswith("/tmp/"):
        DB_URL = _sqlite_url_for(os.path.join(DATA_DIR, "apuntesya.db"))
    else:
        DB_URL = raw_db_url
else:
    DB_URL = raw_db_url

engine_kwargs = {}
if DB_URL.startswith("sqlite:///"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

try:
    engine = create_engine(DB_URL, pool_pre_ping=True, future=True, **engine_kwargs)
    # Test de conexión
    with engine.begin() as conn:
        pass
except OperationalError:
    # Si falla, fallback garantizado a /tmp
    DB_URL = _sqlite_url_for(os.path.join(DATA_DIR, "apuntesya.db"))
    engine = create_engine(DB_URL, pool_pre_ping=True, future=True, connect_args={"check_same_thread": False})

SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
from apuntesya2.models import Base, User, Note, Purchase, University, Faculty, Career

Base.metadata.create_all(engine)
with engine.connect() as conn:
    tables = conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
    print("[DB] Tablas detectadas:", tables)


# ---------- Otros configs ----------
app.config["PLATFORM_FEE_PERCENT"] = float(os.getenv("MP_PLATFORM_FEE_PERCENT", "5.0"))
app.config["MP_ACCESS_TOKEN_PLATFORM"] = os.getenv("MP_ACCESS_TOKEN", "")
app.config["MP_OAUTH_REDIRECT_URL"] = os.getenv("MP_OAUTH_REDIRECT_URL")

app.config['MP_PUBLIC_KEY'] = os.getenv('MP_PUBLIC_KEY', '')
app.config['MP_ACCESS_TOKEN'] = os.getenv('MP_ACCESS_TOKEN', app.config["MP_ACCESS_TOKEN_PLATFORM"])
app.config['MP_WEBHOOK_SECRET'] = os.getenv('MP_WEBHOOK_SECRET', '')
app.config['BASE_URL'] = os.getenv('BASE_URL', '')

app.config['MP_COMMISSION_RATE']  = float(os.getenv('MP_COMMISSION_RATE', '0.0774'))
app.config['APY_COMMISSION_RATE'] = float(os.getenv('APY_COMMISSION_RATE', '0.05'))
app.config['IIBB_ENABLED']        = os.getenv('IIBB_ENABLED', str(IIBB_ENABLED_DEFAULT)).lower() in ('1','true','yes')
app.config['IIBB_RATE']           = float(os.getenv('IIBB_RATE', str(IIBB_RATE_DEFAULT)))

try:
    MP_COMMISSION_RATE = float(app.config.get('MP_COMMISSION_RATE', MP_COMMISSION_RATE_DEFAULT))
    APY_COMMISSION_RATE = float(app.config.get('APY_COMMISSION_RATE', APY_COMMISSION_RATE_DEFAULT))
    IIBB_ENABLED = bool(app.config.get('IIBB_ENABLED', IIBB_ENABLED_DEFAULT))
    IIBB_RATE = float(app.config.get('IIBB_RATE', IIBB_RATE_DEFAULT))
except Exception:
    pass

# ---------- Password reset (si existe blueprint) ----------
app.config.setdefault('SECURITY_PASSWORD_SALT', os.environ.get('SECURITY_PASSWORD_SALT', 'pw-reset'))
app.config.setdefault('PASSWORD_RESET_EXPIRATION', int(os.environ.get('PASSWORD_RESET_EXPIRATION', '3600')))
app.config.setdefault('ENABLE_SMTP', os.environ.get('ENABLE_SMTP', 'false'))
app.config.setdefault('MAIL_SERVER', os.environ.get('MAIL_SERVER', 'smtp.gmail.com'))
app.config.setdefault('MAIL_PORT', int(os.environ.get('MAIL_PORT', '587')))
app.config.setdefault('MAIL_USERNAME', os.environ.get('MAIL_USERNAME'))
app.config.setdefault('MAIL_PASSWORD', os.environ.get('MAIL_PASSWORD'))
app.config.setdefault('MAIL_USE_TLS', True)
app.config.setdefault('MAIL_DEFAULT_SENDER', os.environ.get('MAIL_DEFAULT_SENDER', 'no-reply@localhost'))
try:
    from .auth_reset.routes import bp as auth_reset_bp
    app.register_blueprint(auth_reset_bp)
except Exception as e:
    print("[ApuntesYa] Warning: could not register auth_reset blueprint:", e)

# ---------- Admin blueprint ----------
try:
    from .admin.routes import admin_bp
    app.register_blueprint(admin_bp)
except Exception:
    try:
        from admin.routes import admin_bp
        app.register_blueprint(admin_bp)
    except Exception:
        pass

# ---------- Login Manager ----------
login_manager = LoginManager(app)
login_manager.login_view = "login"

@login_manager.user_loader
def load_user(user_id):
    with SessionLocal() as s:
        return s.get(User, int(user_id))

# ---------- Helpers ----------
def allowed_pdf(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() == "pdf"

def ensure_dirs():
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

# ---------- Contact widget ----------
app.config.from_mapping(
    CONTACT_EMAILS=os.getenv("CONTACT_EMAILS", "soporte.apuntesya@gmail.com"),
    CONTACT_WHATSAPP=os.getenv("CONTACT_WHATSAPP", "+543510000000"),
    SUGGESTIONS_URL=os.getenv("SUGGESTIONS_URL", "https://docs.google.com/forms/d/e/1FAIpQLScDEukn0sLtjOoWgmvTNaF_qG0iDHue9EOqCYxz_z6bGxzErg/viewform?usp=header"),
)

@app.context_processor
def inject_contacts():
    emails = [e.strip() for e in str(app.config.get("CONTACT_EMAILS","")).split(",") if e.strip()]
    return dict(
        CONTACT_EMAILS=emails,
        CONTACT_WHATSAPP=app.config.get("CONTACT_WHATSAPP"),
        SUGGESTIONS_URL=app.config.get("SUGGESTIONS_URL")
    )

# ---------- Rutas principales ----------
@app.route("/")
def index():
    with SessionLocal() as s:
        notes = s.execute(
            select(Note).where(Note.is_active == True).order_by(Note.created_at.desc()).limit(30)
        ).scalars().all()
    return render_template("index.html", notes=notes)

# (… resto del archivo igual que el anterior, incluyendo rutas de login, upload, MP, balance, report, taxonomías, y /_promote_admin_once …)

# ---------- Promover admin ----------
@app.route("/_promote_admin_once", methods=["GET"])
def _promote_admin_once():
    if os.getenv("PROMOTE_ADMIN_ENABLED", "0") != "1":
        abort(404)

    secret_env = os.getenv("PROMOTE_ADMIN_SECRET", "")
    secret_arg = request.args.get("secret", "")
    email = (request.args.get("email") or "").strip().lower()

    if not secret_env or secret_arg != secret_env:
        abort(403)
    if not email:
        return "Falta ?email=", 400

    with SessionLocal() as session:
        user = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if not user:
            return "Usuario no encontrado", 404
        if not hasattr(user, "is_admin"):
            return "El modelo User no tiene campo is_admin", 500
        user.is_admin = True
        session.commit()

    app.logger.warning("Promovido a admin: %s", email)
    return f"OK. {email} ahora es admin."

# --- Ensure 'login' endpoint exists (defensive) ---
try:
    endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
    if "login" not in endpoints:
        # Si por algún motivo no quedó registrada, la registramos explícitamente
        app.add_url_rule("/login", endpoint="login", view_func=login, methods=["GET", "POST"])
except Exception as _e:
    app.logger.warning("No se pudo verificar/forzar endpoint 'login': %s", _e)



# ---------- Main ----------
if __name__ == "__main__":
    app.run(debug=True)
