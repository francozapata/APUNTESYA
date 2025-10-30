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

# ---------- Comisiones/IIBB defaults (previas a app) ----------
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

# ---------- Config Básica ----------
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

# ---------- DB ----------
# Si tenés DATABASE_URL (Postgres/otro) úsalo; sino SQLite en DATA_DIR
DB_URL = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(DATA_DIR, 'apuntesya.db')}")
engine_kwargs = {}
if DB_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
engine = create_engine(DB_URL, pool_pre_ping=True, future=True, **engine_kwargs)

# Sesión global reutilizable
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))

# Crear tablas si no existen
Base.metadata.create_all(engine)

# ---------- Otros configs ----------
# Mercado Pago / Plataforma
app.config["PLATFORM_FEE_PERCENT"] = float(os.getenv("MP_PLATFORM_FEE_PERCENT", "5.0"))
app.config["MP_ACCESS_TOKEN_PLATFORM"] = os.getenv("MP_ACCESS_TOKEN", "")
app.config["MP_OAUTH_REDIRECT_URL"] = os.getenv("MP_OAUTH_REDIRECT_URL")

# MP & Comisiones/IIBB (mantengo tus nombres)
app.config['MP_PUBLIC_KEY'] = os.getenv('MP_PUBLIC_KEY', '')
app.config['MP_ACCESS_TOKEN'] = os.getenv('MP_ACCESS_TOKEN', app.config["MP_ACCESS_TOKEN_PLATFORM"])
app.config['MP_WEBHOOK_SECRET'] = os.getenv('MP_WEBHOOK_SECRET', '')
app.config['BASE_URL'] = os.getenv('BASE_URL', '')

app.config['MP_COMMISSION_RATE']  = float(os.getenv('MP_COMMISSION_RATE', '0.0774'))
app.config['APY_COMMISSION_RATE'] = float(os.getenv('APY_COMMISSION_RATE', '0.05'))
app.config['IIBB_ENABLED']        = os.getenv('IIBB_ENABLED', str(IIBB_ENABLED_DEFAULT)).lower() in ('1','true','yes')
app.config['IIBB_RATE']           = float(os.getenv('IIBB_RATE', str(IIBB_RATE_DEFAULT)))

# Override seguros
try:
    MP_COMMISSION_RATE = float(app.config.get('MP_COMMISSION_RATE', MP_COMMISSION_RATE_DEFAULT))
    APY_COMMISSION_RATE = float(app.config.get('APY_COMMISSION_RATE', APY_COMMISSION_RATE_DEFAULT))
    IIBB_ENABLED = bool(app.config.get('IIBB_ENABLED', IIBB_ENABLED_DEFAULT))
    IIBB_RATE = float(app.config.get('IIBB_RATE', IIBB_RATE_DEFAULT))
except Exception:
    pass

# Password reset defaults (si tenés blueprint)
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

# ---------- Admin blueprint (si existe) ----------
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

# ---------- Contact widget config ----------
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

@app.route("/search")
def search():
    q = request.args.get("q","").strip()
    university = request.args.get("university","").strip()
    faculty = request.args.get("faculty","").strip()
    career = request.args.get("career","").strip()
    t = request.args.get("type","")

    with SessionLocal() as s:
        stmt = select(Note).where(Note.is_active==True)
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
        with SessionLocal() as s:
            exists = s.execute(select(User).where(User.email==email)).scalar_one_or_none()
            if exists:
                flash("Ese email ya está registrado.")
                return redirect(url_for("register"))
            u = User(name=name, email=email, password_hash=generate_password_hash(password),
                     university=university, faculty=faculty, career=career)
            s.add(u); s.commit()
            login_user(u)
            return redirect(url_for("index"))
    return render_template("register.html")

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        with SessionLocal() as s:
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
    with SessionLocal() as s:
        my_notes = s.execute(select(Note).where(Note.seller_id==current_user.id).order_by(Note.created_at.desc())).scalars().all()
    return render_template("profile.html", my_notes=my_notes)

@app.route("/profile/balance")
@login_required
def profile_balance():
    fmt = "%Y-%m-%d"
    today = datetime.utcnow().date()
    default_start = today.replace(day=1)
    start_str = request.args.get("start", default_start.strftime(fmt))
    end_str   = request.args.get("end", today.strftime(fmt))

    try:
        start = datetime.strptime(start_str, fmt)
        end   = datetime.strptime(end_str, fmt) + timedelta(days=1)  # inclusivo
    except Exception:
        start, end = datetime(default_start.year, default_start.month, 1), datetime(today.year, today.month, today.day) + timedelta(days=1)

    with SessionLocal() as s:
        base_filter = and_(
            Note.seller_id == current_user.id,
            Purchase.status == 'approved',
            Purchase.created_at >= start,
            Purchase.created_at < end
        )

        totals = s.execute(
            select(
                func.count(Purchase.id),
                func.coalesce(func.sum(Purchase.amount_cents), 0)
            ).join(Note, Note.id == Purchase.note_id).where(base_filter)
        ).one()
        sold_count = int(totals[0] or 0)
        gross_cents = int(totals[1] or 0)

        mp_commission_cents  = int(round(gross_cents * float(MP_COMMISSION_RATE)))
        apy_commission_cents = int(round(gross_cents * float(APY_COMMISSION_RATE)))
        net_cents = gross_cents - mp_commission_cents - apy_commission_cents

        has_views = hasattr(Note, 'views')
        if has_views:
            rows = s.execute(
                select(
                    Note.id, Note.title, Note.views,
                    func.count(Purchase.id).label("sold_count"),
                    func.coalesce(func.sum(Purchase.amount_cents), 0).label("gross_cents")
                )
                .join(Purchase, Purchase.note_id == Note.id, isouter=True)
                .where(Note.seller_id == current_user.id, Purchase.created_at >= start, Purchase.created_at < end)
                .group_by(Note.id, Note.title, Note.views)
                .order_by(func.count(Purchase.id).desc())
            ).all()
        else:
            rows = s.execute(
                select(
                    Note.id, Note.title,
                    func.count(Purchase.id).label("sold_count"),
                    func.coalesce(func.sum(Purchase.amount_cents), 0).label("gross_cents")
                )
                .join(Purchase, Purchase.note_id == Note.id, isouter=True)
                .where(Note.seller_id == current_user.id, Purchase.created_at >= start, Purchase.created_at < end)
                .group_by(Note.id, Note.title)
                .order_by(func.count(Purchase.id).desc())
            ).all()

        per_note = []
        for r in rows:
            if has_views:
                _id, _title, _views, _sold, _gross = r
                views = int(_views or 0)
                sold  = int(_sold or 0)
                gross = int(_gross or 0)
            else:
                _id, _title, _sold, _gross = r
                views = None
                sold  = int(_sold or 0)
                gross = int(_gross or 0)

            mp_c  = int(round(gross * float(MP_COMMISSION_RATE)))
            apy_c = int(round(gross * float(APY_COMMISSION_RATE)))
            per_note.append({
                "id": _id,
                "title": _title,
                "sold_count": sold,
                "gross_cents": gross,
                "mp_commission_cents": mp_c,
                "apy_commission_cents": apy_c,
                "net_cents": gross - mp_c - apy_c,
                "views": views,
                "conversion": (sold / views * 100.0) if (views and views > 0) else None
            })

    return render_template(
        "profile_balance.html",
        IIBB_ENABLED=IIBB_ENABLED, IIBB_RATE=IIBB_RATE, sold_count=sold_count,
        total_cents=gross_cents,
        mp_commission_cents=mp_commission_cents,
        apy_commission_cents=apy_commission_cents,
        net_cents=net_cents,
        per_note=per_note,
        start=start_str,
        end=(end - timedelta(days=1)).strftime(fmt),
        MP_COMMISSION_RATE=MP_COMMISSION_RATE,
        APY_COMMISSION_RATE=APY_COMMISSION_RATE
    )

@app.route("/profile/purchases")
@login_required
def profile_purchases():
    with SessionLocal() as s:
        purchases = s.execute(
            select(Purchase, Note)
            .join(Note, Note.id == Purchase.note_id)
            .where(Purchase.buyer_id == current_user.id, Purchase.status == 'approved')
            .order_by(Purchase.created_at.desc())
        ).all()

        items = []
        for p, n in purchases:
            items.append(dict(
                id=p.id,
                note_id=n.id,
                title=n.title,
                price_cents=p.amount_cents,
                created_at=p.created_at.strftime("%Y-%m-%d %H:%M")
            ))
    return render_template("profile_purchases.html", items=items)

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
        ensure_dirs()
        filename = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secure_filename(file.filename)}"
        fpath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
        file.save(fpath)
        with SessionLocal() as s:
            note = Note(title=title, description=description, university=university, faculty=faculty, career=career,
                        price_cents=price_cents, file_path=filename, seller_id=current_user.id)
            s.add(note); s.commit()
        flash("Apunte subido correctamente.")
        return redirect(url_for("note_detail", note_id=note.id))
    return render_template("upload.html")

@app.route("/note/<int:note_id>")
def note_detail(note_id):
    with SessionLocal() as s:
        note = s.get(Note, note_id)
        if not note or not note.is_active:
            abort(404)
        can_download = False
        if current_user.is_authenticated:
            if note.price_cents==0 or note.seller_id==current_user.id:
                can_download = True
            else:
                p = s.execute(select(Purchase).where(
                    Purchase.buyer_id==current_user.id,
                    Purchase.note_id==note.id,
                    Purchase.status=='approved'
                )).scalar_one_or_none()
                can_download = p is not None
    return render_template("note_detail.html", note=note, can_download=can_download)

@app.route("/download/<int:note_id>")
@login_required
def download_note(note_id):
    with SessionLocal() as s:
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

# ---------- Mercado Pago OAuth ----------
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
    with SessionLocal() as s:
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
    with SessionLocal() as s:
        u = s.get(User, current_user.id)
        u.mp_user_id = None
        u.mp_access_token = None
        u.mp_refresh_token = None
        u.mp_token_expires_at = None
        s.commit()
    flash("Se desvinculó Mercado Pago.")
    return redirect(url_for("profile"))

def get_valid_seller_token(seller: User) -> str | None:
    return seller.mp_access_token if seller and seller.mp_access_token else None

# ---------- Comprar ----------
@app.route("/buy/<int:note_id>")
@login_required
def buy_note(note_id):
    with SessionLocal() as s:
        note = s.get(Note, note_id)
        if not note or not note.is_active: abort(404)
        if note.seller_id == current_user.id:
            flash("No podés comprar tu propio apunte.")
            return redirect(url_for("note_detail", note_id=note.id))
        if note.price_cents == 0:
            flash("Este apunte es gratuito.")
            return redirect(url_for("download_note", note_id=note.id))
        seller = s.get(User, note.seller_id)

        p = Purchase(buyer_id=current_user.id, note_id=note.id, status="pending", amount_cents=note.price_cents)
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
                title=note.title,
                unit_price=price_ars,
                quantity=1,
                marketplace_fee=marketplace_fee,
                external_reference=f"purchase:{p.id}",
                back_urls=back_urls,
                notification_url=url_for("mp_webhook", _external=True)
            )
            with SessionLocal() as s2:
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
    pref_id = request.args.get("preference_id", "")

    token = app.config["MP_ACCESS_TOKEN_PLATFORM"]

    pay = None
    if payment_id:
        try:
            pay = mp.get_payment(token, str(payment_id))
        except Exception as e:
            flash(f"No se pudo verificar el pago aún: {e}")
            return redirect(url_for("note_detail", note_id=note_id))
    elif ext_ref:
        try:
            res = mp.search_payments_by_external_reference(token, ext_ref)
            results = (res or {}).get("results") or []
            if results:
                pay = results[0].get("payment") or results[0]
                payment_id = str(pay.get("id")) if pay else None
        except Exception:
            pass
    if not pay:
        with SessionLocal() as s:
            p_last = s.execute(select(Purchase).where(Purchase.note_id==note_id).order_by(Purchase.created_at.desc())).scalars().first()
            if p_last:
                try:
                    res = mp.search_payments_by_external_reference(token, f"purchase:{p_last.id}")
                    results = (res or {}).get("results") or []
                    if results:
                        pay = results[0].get("payment") or results[0]
                        payment_id = str(pay.get("id")) if pay else None
                        ext_ref = f"purchase:{p_last.id}"
                except Exception:
                    pass

    status = (pay or {}).get("status")
    external_reference = (pay or {}).get("external_reference") or ext_ref or ""
    purchase_id = None
    if external_reference and external_reference.startswith("purchase:"):
        try:
            purchase_id = int(external_reference.split(":")[1])
        except Exception:
            purchase_id = None

    with SessionLocal() as s:
        if purchase_id:
            p = s.get(Purchase, purchase_id)
        else:
            p = s.execute(select(Purchase).where(Purchase.note_id==note_id).order_by(Purchase.created_at.desc())).scalars().first()

        if p:
            p.payment_id = str(payment_id)
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
    topic = request.args.get("topic") or request.args.get("type")
    payment_id = request.args.get("id") or (request.json.get("data",{}).get("id") if request.is_json else None)
    if not payment_id:
        return ("ok", 200)
    token = app.config["MP_ACCESS_TOKEN_PLATFORM"]
    try:
        pay = mp.get_payment(token, str(payment_id))
    except Exception:
        return ("ok", 200)
    status = pay.get("status")
    external_reference = pay.get("external_reference") or ""
    if external_reference.startswith("purchase:"):
        pid = int(external_reference.split(":")[1])
        with SessionLocal() as s:
            purchase = s.get(Purchase, pid)
            if purchase:
                purchase.payment_id = str(payment_id)
                purchase.status = status
                s.commit()
    return ("ok", 200)

# ---------- Terms ----------
@app.route("/terms")
def terms():
    return render_template("terms.html")

# ---------- Reporte ----------
@app.route('/note/<int:note_id>/report', methods=['POST'])
@login_required
def report_note(note_id):
    with SessionLocal() as s:
        n = s.get(Note, note_id)
        if not n:
            abort(404)
        if hasattr(n, "is_reported"):
            n.is_reported = True
            s.commit()
    flash('Gracias por tu reporte. Un administrador lo revisará.')
    return redirect(url_for('note_detail', note_id=note_id))

# ---------- Taxonomías (API) ----------
from sqlalchemy.exc import IntegrityError

def _norm(s: str) -> str:
    return (s or "").strip()

@app.get("/api/academics/universities")
def api_list_universities():
    with SessionLocal() as s:
        rows = s.execute(select(University).order_by(University.name)).scalars().all()
        return jsonify([{"id": u.id, "name": u.name} for u in rows])

@app.get("/api/academics/faculties")
def api_list_faculties():
    uid = request.args.get("university_id", type=int)
    with SessionLocal() as s:
        q = select(Faculty)
        if uid:
            q = q.where(Faculty.university_id == uid)
        rows = s.execute(q.order_by(Faculty.name)).scalars().all()
        return jsonify([{"id": f.id, "name": f.name, "university_id": f.university_id} for f in rows])

@app.get("/api/academics/careers")
def api_list_careers():
    fid = request.args.get("faculty_id", type=int)
    with SessionLocal() as s:
        q = select(Career)
        if fid:
            q = q.where(Career.faculty_id == fid)
        rows = s.execute(q.order_by(Career.name)).scalars().all()
        return jsonify([{"id": c.id, "name": c.name, "faculty_id": c.faculty_id} for c in rows])

@app.post("/api/academics/universities")
def api_add_university():
    data = request.get_json(silent=True) or {}
    name = _norm(data.get("name"))
    if not name:
        return jsonify({"error": "name required"}), 400
    with SessionLocal() as s:
        u = s.execute(select(University).where(func.lower(University.name)==name.lower())).scalar_one_or_none()
        if u:
            return jsonify({"id": u.id, "name": u.name})
        u = University(name=name)
        s.add(u)
        s.commit()
        return jsonify({"id": u.id, "name": u.name})

@app.post("/api/academics/faculties")
def api_add_faculty():
    data = request.get_json(silent=True) or {}
    name = _norm(data.get("name"))
    uid = data.get("university_id")
    if not (name and uid):
        return jsonify({"error": "name and university_id required"}), 400
    with SessionLocal() as s:
        f = s.execute(select(Faculty).where(
            func.lower(Faculty.name)==name.lower(),
            Faculty.university_id==uid
        )).scalar_one_or_none()
        if f:
            return jsonify({"id": f.id, "name": f.name, "university_id": f.university_id})
        f = Faculty(name=name, university_id=uid)
        s.add(f)
        s.commit()
        return jsonify({"id": f.id, "name": f.name, "university_id": f.university_id})

@app.post("/api/academics/careers")
def api_add_career():
    data = request.get_json(silent=True) or {}
    name = _norm(data.get("name"))
    fid = data.get("faculty_id")
    if not (name and fid):
        return jsonify({"error": "name and faculty_id required"}), 400
    with SessionLocal() as s:
        c = s.execute(select(Career).where(
            func.lower(Career.name)==name.lower(),
            Career.faculty_id==fid
        )).scalar_one_or_none()
        if c:
            return jsonify({"id": c.id, "name": c.name, "faculty_id": c.faculty_id})
        c = Career(name=name, faculty_id=fid)
        s.add(c)
        s.commit()
        return jsonify({"id": c.id, "name": c.name, "faculty_id": c.faculty_id})

# ---------- Promover admin (sólo habilitado por ENV) ----------
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

# ---------- Main ----------
if __name__ == "__main__":
    app.run(debug=True)
