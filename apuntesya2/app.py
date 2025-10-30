import os, secrets, math
from datetime import datetime, timedelta
from urllib.parse import urlencode

from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_from_directory, abort, jsonify
)
from flask_login import (
    LoginManager, login_user, logout_user, current_user, login_required
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from sqlalchemy import create_engine, select, or_, and_, func
from sqlalchemy.orm import sessionmaker, scoped_session

# ──────────────────────────────────────────────────────────────────────────────
# Carga de envs
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()

# ──────────────────────────────────────────────────────────────────────────────
# App (una sola instancia)
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__, instance_relative_config=True)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", secrets.token_hex(16))
app.config["ENV"] = os.getenv("FLASK_ENV", "production")

# ──────────────────────────────────────────────────────────────────────────────
# Rutas de archivos persistentes (Render: usar /data)
# ──────────────────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", "/data")
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", os.path.join(DATA_DIR, "uploads"))
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_DIR
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25MB

# ──────────────────────────────────────────────────────────────────────────────
# Base de datos
#   - Si DATABASE_URL no está, usamos sqlite:///data/apuntesya.db
#   - Para SQLite habilitamos check_same_thread=False
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_DB = f"sqlite:///{os.path.join(DATA_DIR, 'apuntesya.db')}"
DB_URL = os.getenv("DATABASE_URL", DEFAULT_DB)

engine_kwargs = {"pool_pre_ping": True, "future": True}
if DB_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DB_URL, **engine_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Session = scoped_session(SessionLocal)

# ──────────────────────────────────────────────────────────────────────────────
# Modelos
# ──────────────────────────────────────────────────────────────────────────────
from apuntesya2.models import Base, User, Note, Purchase, University, Faculty, Career
Base.metadata.create_all(engine)

# ──────────────────────────────────────────────────────────────────────────────
# Login manager
# ──────────────────────────────────────────────────────────────────────────────
login_manager = LoginManager(app)
login_manager.login_view = "login"

@login_manager.user_loader
def load_user(user_id):
    with Session() as s:
        return s.get(User, int(user_id))

# ──────────────────────────────────────────────────────────────────────────────
# Config de Mercado Pago / comisiones
# ──────────────────────────────────────────────────────────────────────────────
from apuntesya2 import mp

app.config["MP_ACCESS_TOKEN_PLATFORM"] = os.getenv("MP_ACCESS_TOKEN", "")
app.config["MP_PUBLIC_KEY"] = os.getenv("MP_PUBLIC_KEY", "")
app.config["MP_OAUTH_REDIRECT_URL"] = os.getenv("MP_OAUTH_REDIRECT_URL")

# tasas (por si las usás en templates/cálculos)
MP_COMMISSION_RATE_DEFAULT = 0.0774
APY_COMMISSION_RATE_DEFAULT = 0.05
IIBB_ENABLED_DEFAULT = False
IIBB_RATE_DEFAULT = 0.0

MP_COMMISSION_RATE = float(os.getenv("MP_COMMISSION_RATE", MP_COMMISSION_RATE_DEFAULT))
APY_COMMISSION_RATE = float(os.getenv("APY_COMMISSION_RATE", APY_COMMISSION_RATE_DEFAULT))
IIBB_ENABLED = os.getenv("IIBB_ENABLED", str(IIBB_ENABLED_DEFAULT)).lower() in ("1","true","yes")
IIBB_RATE = float(os.getenv("IIBB_RATE", IIBB_RATE_DEFAULT))

app.config["PLATFORM_FEE_PERCENT"] = float(os.getenv("MP_PLATFORM_FEE_PERCENT", "5.0"))

# ──────────────────────────────────────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"ok": True}, 200

# ──────────────────────────────────────────────────────────────────────────────
# Ruta de promoción a admin (SEGURA y opcional por ENV)
#   Usar solo si:
#     PROMOTE_ADMIN_ENABLED=1
#     PROMOTE_ADMIN_SECRET=<secreto_largo_unico>
#   GET /_promote_admin_once?email=...&secret=...
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/_promote_admin_once", methods=["GET"])
def _promote_admin_once():
    if os.getenv("PROMOTE_ADMIN_ENABLED", "0") != "1":
        abort(404)

    secret_env = os.getenv("PROMOTE_ADMIN_SECRET", "")
    secret_arg = (request.args.get("secret") or "").strip()
    email = (request.args.get("email") or "").strip().lower()

    if not secret_env or secret_arg != secret_env:
        abort(403)
    if not email:
        return "Falta ?email=", 400

    with Session() as s:
        user = s.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if not user:
            return "Usuario no encontrado", 404

        # setea admin: soporta is_admin o role
        if hasattr(user, "is_admin"):
            user.is_admin = True
        elif hasattr(user, "role"):
            user.role = "admin"
        else:
            return "El modelo User no tiene campo admin/role", 500

        s.commit()

    # Log leve para auditoría
    app.logger.warning("Promovido a admin: %s", email)
    return f"OK. {email} ahora es admin."

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def allowed_pdf(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() == "pdf"

# ──────────────────────────────────────────────────────────────────────────────
# Contact widget (vars)
# ──────────────────────────────────────────────────────────────────────────────
app.config.from_mapping(
    CONTACT_EMAILS=os.getenv("CONTACT_EMAILS", "soporte.apuntesya@gmail.com"),
    CONTACT_WHATSAPP=os.getenv("CONTACT_WHATSAPP", "+543510000000"),
    SUGGESTIONS_URL=os.getenv(
        "SUGGESTIONS_URL",
        "https://docs.google.com/forms/d/e/1FAIpQLScDEukn0sLtjOoWgmvTNaF_qG0iDHue9EOqCYxz_z6bGxzErg/viewform?usp=header"
    ),
)

@app.context_processor
def inject_contacts():
    emails = [e.strip() for e in str(app.config.get("CONTACT_EMAILS","")).split(",") if e.strip()]
    return dict(
        CONTACT_EMAILS=emails,
        CONTACT_WHATSAPP=app.config.get("CONTACT_WHATSAPP"),
        SUGGESTIONS_URL=app.config.get("SUGGESTIONS_URL"),
        MP_COMMISSION_RATE=MP_COMMISSION_RATE,
        APY_COMMISSION_RATE=APY_COMMISSION_RATE,
        IIBB_ENABLED=IIBB_ENABLED,
        IIBB_RATE=IIBB_RATE
    )

# ──────────────────────────────────────────────────────────────────────────────
# Rutas principales (catálogo, auth, perfil, upload)
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    with Session() as s:
        notes = (
            s.execute(
                select(Note).where(Note.is_active == True)
                .order_by(Note.created_at.desc())
                .limit(30)
            ).scalars().all()
        )
    return render_template("index.html", notes=notes)

@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    university = request.args.get("university", "").strip()
    faculty = request.args.get("faculty", "").strip()
    career = request.args.get("career", "").strip()
    t = request.args.get("type", "")

    with Session() as s:
        stmt = select(Note).where(Note.is_active == True)
        if q:
            stmt = stmt.where(or_(Note.title.ilike(f"%{q}%"), Note.description.ilike(f"%{q}%")))
        if university: stmt = stmt.where(Note.university.ilike(f"%{university}%"))
        if faculty: stmt = stmt.where(Note.faculty.ilike(f"%{faculty}%"))
        if career: stmt = stmt.where(Note.career.ilike(f"%{career}%"))
        if t == "free":
            stmt = stmt.where(Note.price_cents == 0)
        elif t == "paid":
            stmt = stmt.where(Note.price_cents > 0)
        notes = s.execute(stmt.order_by(Note.created_at.desc()).limit(100)).scalars().all()
    return render_template("index.html", notes=notes)

@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        university = request.form["university"].strip()
        faculty = request.form["faculty"].strip()
        career = request.form["career"].strip()
        with Session() as s:
            exists = s.execute(select(User).where(User.email==email)).scalar_one_or_none()
            if exists:
                flash("Ese email ya está registrado.")
                return redirect(url_for("register"))
            u = User(
                name=name, email=email,
                password_hash=generate_password_hash(password),
                university=university, faculty=faculty, career=career
            )
            s.add(u); s.commit()
            login_user(u); return redirect(url_for("index"))
    return render_template("register.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        with Session() as s:
            u = s.execute(select(User).where(User.email==email)).scalar_one_or_none()
            if not u or not check_password_hash(u.password_hash, password):
                flash("Credenciales inválidas.")
                return redirect(url_for("login"))
            login_user(u); return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/logout")
def logout():
    logout_user(); return redirect(url_for("index"))

@app.route("/profile")
@login_required
def profile():
    with Session() as s:
        my_notes = (
            s.execute(
                select(Note).where(Note.seller_id==current_user.id)
                .order_by(Note.created_at.desc())
            ).scalars().all()
        )
    return render_template("profile.html", my_notes=my_notes)

@app.route("/upload", methods=["GET","POST"])
@login_required
def upload_note():
    if request.method == "POST":
        title = request.form["title"].strip()
        description = request.form["description"].strip()
        university = request.form["university"].strip()
        faculty = request.form["faculty"].strip()
        career = request.form["career"].strip()
        price = request.form.get("price","").strip()
        price_cents = int(round(float(price)*100)) if price else 0
        file = request.files.get("file")
        if not file or file.filename == "":
            flash("Seleccioná un PDF.")
            return redirect(url_for("upload_note"))
        if not allowed_pdf(file.filename):
            flash("Sólo PDF.")
            return redirect(url_for("upload_note"))
        filename = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secure_filename(file.filename)}"
        fpath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(fpath)
        with Session() as s:
            note = Note(
                title=title, description=description, university=university,
                faculty=faculty, career=career, price_cents=price_cents,
                file_path=filename, seller_id=current_user.id
            )
            s.add(note); s.commit()
        flash("Apunte subido correctamente.")
        return redirect(url_for("note_detail", note_id=note.id))
    return render_template("upload.html")

@app.route("/note/<int:note_id>")
def note_detail(note_id):
    with Session() as s:
        note = s.get(Note, note_id)
        if not note or not note.is_active: abort(404)
        can_download = False
        if current_user.is_authenticated:
            if note.price_cents==0 or note.seller_id==current_user.id:
                can_download = True
            else:
                p = s.execute(
                    select(Purchase).where(
                        Purchase.buyer_id==current_user.id,
                        Purchase.note_id==note.id,
                        Purchase.status=='approved'
                    )
                ).scalar_one_or_none()
                can_download = p is not None
    return render_template("note_detail.html", note=note, can_download=can_download)

@app.route("/download/<int:note_id>")
@login_required
def download_note(note_id):
    with Session() as s:
        note = s.get(Note, note_id)
        if not note or not note.is_active: abort(404)
        allowed = False
        if note.seller_id==current_user.id or note.price_cents==0:
            allowed = True
        else:
            p = s.execute(select(Purchase).where(
                Purchase.buyer_id==current_user.id,
                Purchase.note_id==note.id,
                Purchase.status=='approved'
            )).scalar_one_or_none()
            allowed = p is not None
        if not allowed:
            flash("Necesitás comprar este apunte para descargarlo.")
            return redirect(url_for("note_detail", note_id=note.id))
        return send_from_directory(app.config["UPLOAD_FOLDER"], note.file_path, as_attachment=True)

# ──────────────────────────────────────────────────────────────────────────────
# Mercado Pago: OAuth + Compra + Return + Webhook
# ──────────────────────────────────────────────────────────────────────────────
def get_valid_seller_token(seller:User) -> str|None:
    return seller.mp_access_token if seller and seller.mp_access_token else None

@app.route("/mp/connect")
@login_required
def connect_mp():
    return redirect(mp.oauth_authorize_url())

@app.route("/mp/oauth/callback")
@login_required
def mp_oauth_callback():
    if not current_user.is_authenticated:
        flash("Necesitás iniciar sesión para vincular Mercado Pago.")
        return redirect(url_for("login"))
    code = request.args.get("code")
    if not code:
        flash("No se recibió 'code' de autorización.")
        return redirect(url_for("profile"))
    try:
        data = mp.oauth_exchange_code(code)
    except Exception as e:
        flash(f"Error al intercambiar código: {e}")
        return redirect(url_for("profile"))
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    user_id = str(data.get("user_id"))
    expires_in = int(data.get("expires_in", 0))
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in-60)
    with Session() as s:
        u = s.get(User, current_user.id)
        u.mp_user_id = user_id
        u.mp_access_token = access_token
        u.mp_refresh_token = refresh_token
        u.mp_token_expires_at = expires_at
        s.commit()
    flash("¡Cuenta de Mercado Pago conectada!")
    return redirect(url_for("profile"))

@app.route("/mp/disconnect")
@login_required
def disconnect_mp():
    with Session() as s:
        u = s.get(User, current_user.id)
        u.mp_user_id = None
        u.mp_access_token = None
        u.mp_refresh_token = None
        u.mp_token_expires_at = None
        s.commit()
    flash("Se desvinculó Mercado Pago.")
    return redirect(url_for("profile"))

@app.route("/buy/<int:note_id>")
@login_required
def buy_note(note_id):
    with Session() as s:
        note = s.get(Note, note_id)
        if not note or not note.is_active: abort(404)
        if note.seller_id == current_user.id:
            flash("No podés comprar tu propio apunte.")
            return redirect(url_for("note_detail", note_id=note.id))
        if note.price_cents == 0:
            flash("Este apunte es gratuito.")
            return redirect(url_for("download_note", note_id=note.id))
        seller = s.get(User, note.seller_id)

        p = Purchase(buyer_id=current_user.id, note_id=note.id,
                     status="pending", amount_cents=note.price_cents)
        s.add(p); s.commit()

        price_ars = round(note.price_cents/100, 2)
        platform_fee_percent = (app.config["PLATFORM_FEE_PERCENT"]/100.0)
        back_urls = {
            "success": url_for("mp_return", note_id=note.id, _external=True) + f"?external_reference=purchase:{p.id}",
            "failure": url_for("mp_return", note_id=note.id, _external=True) + f"?external_reference=purchase:{p.id}",
            "pending": url_for("mp_return", note_id=note.id, _external=True) + f"?external_reference=purchase:{p.id}",
        }

        try:
            seller_token = get_valid_seller_token(seller)
            if seller_token is None:
                use_token = app.config["MP_ACCESS_TOKEN_PLATFORM"]
                marketplace_fee = 0.0
                flash("El vendedor no tiene Mercado Pago vinculado. Se procesa con token de la plataforma y sin comisión.", "info")
            else:
                use_token = seller_token
                marketplace_fee = round(price_ars * platform_fee_percent, 2)

            pref = mp.create_preference_for_seller_token(
                seller_access_token=use_token,
                title=note.title, unit_price=price_ars, quantity=1,
                marketplace_fee=marketplace_fee,
                external_reference=f"purchase:{p.id}",
                back_urls=back_urls,
                notification_url=url_for("mp_webhook", _external=True)
            )
            with Session() as s2:
                p2 = s2.get(Purchase, p.id)
                if p2:
                    p2.preference_id = pref.get("id") or pref.get("preference_id")
                    s2.commit()
            init_point = pref.get("init_point") or pref.get("sandbox_init_point")
            return redirect(init_point)
        except Exception as e:
            flash(f"Error al crear preferencia en Mercado Pago: {e}")
            return redirect(url_for("note_detail", note_id=note.id))

@app.route("/mp/return/<int:note_id>")
def mp_return(note_id):
    payment_id = request.args.get("payment_id") or request.args.get("collection_id") or request.args.get("id")
    ext_ref = request.args.get("external_reference", "")
    token = app.config["MP_ACCESS_TOKEN_PLATFORM"]

    pay = None
    if payment_id:
        try:
            pay = mp.get_payment(token, str(payment_id))
        except Exception:
            pass
    if not pay and ext_ref:
        try:
            res = mp.search_payments_by_external_reference(token, ext_ref)
            results = (res or {}).get("results") or []
            if results:
                pay = results[0].get("payment") or results[0]
        except Exception:
            pass

    status = (pay or {}).get("status")
    external_reference = (pay or {}).get("external_reference") or ext_ref or ""
    purchase_id = None
    if external_reference.startswith("purchase:"):
        try:
            purchase_id = int(external_reference.split(":")[1])
        except Exception:
            purchase_id = None

    with Session() as s:
        if purchase_id:
            p = s.get(Purchase, purchase_id)
            if p:
                p.payment_id = str((pay or {}).get("id") or "")
                if status:
                    p.status = status
                s.commit()
        if status == "approved":
            flash("¡Pago verificado! Descargando el apunte...")
            return redirect(url_for("download_note", note_id=note_id))

    flash("Pago registrado. Si ya figura aprobado, el botón de descarga estará disponible.")
    return redirect(url_for("note_detail", note_id=note_id))

@app.route("/mp/webhook", methods=["POST","GET"])
def mp_webhook():
    payment_id = request.args.get("id") or (request.json.get("data",{}).get("id") if request.is_json else None)
    if not payment_id:
        return ("ok", 200)
    token = app.config["MP_ACCESS_TOKEN_PLATFORM"]
    try:
        pay = mp.get_payment(token, str(payment_id))
    except Exception:
        return ("ok", 200)
    status = pay.get("status")
    external_reference = pay.get("external_reference

    # Continuación del webhook
    if external_reference and external_reference.startswith("purchase:"):
        try:
            purchase_id = int(external_reference.split(":")[1])
        except Exception:
            purchase_id = None

        if purchase_id:
            with Session() as s:
                p = s.get(Purchase, purchase_id)
                if p:
                    p.payment_id = str(pay.get("id") or "")
                    p.status = status or "unknown"
                    s.commit()
    return ("ok", 200)

# ──────────────────────────────────────────────────────────────────────────────
# Main (modo local)
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("[DB] Tablas detectadas:", Base.metadata.tables.keys())
    print("[INFO] App corriendo en modo local 🚀")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
