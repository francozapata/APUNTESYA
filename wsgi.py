# wsgi.py
import os, sys
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from apuntesya2.app import app  # tu app real

# --- RUTA TEMPORAL PARA PROMOVER A ADMIN (SIN 'db') ---
import os
from flask import request, abort, jsonify
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from apuntesya2.models import User  # ajustá si tu modelo está en otro módulo

# Detecta la URL de base de datos desde env o desde config de la app
DB_URL = (
    os.environ.get("DATABASE_URL")
    or os.environ.get("DB_URL")
    or app.config.get("SQLALCHEMY_DATABASE_URI")
    or "sqlite:///instance/app.db"
)

# Crea engine y fábrica de sesiones propias de esta ruta
_engine = create_engine(DB_URL, pool_pre_ping=True, future=True)
_SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)

@app.route("/_promote_admin", methods=["GET","POST"])
def _promote_admin():
    token = request.args.get("token") or request.headers.get("X-Admin-Token")
    if not token or token != os.environ.get("ADMIN_SETUP_TOKEN"):
        abort(403)

    email = request.args.get("email")
    if not email and request.is_json:
        body = request.get_json(silent=True) or {}
        email = body.get("email")
    if not email:
        return jsonify(ok=False, error="Falta email"), 400

    with _SessionLocal() as session:
        user = session.query(User).filter_by(email=email).first()
        if not user:
            return jsonify(ok=False, error="Usuario no encontrado"), 404

        if hasattr(user, "is_admin"):
            user.is_admin = True
        elif hasattr(user, "role"):
            user.role = "admin"
        else:
            return jsonify(ok=False, error="Modelo User sin campo admin"), 500

        session.commit()
        return jsonify(ok=True, user_id=getattr(user, "id", None), email=getattr(user, "email", email))
# --- FIN RUTA TEMPORAL ---



# Ruta de health (por si tu app no la trae)
@app.get("/health")
def _health():
    return {"ok": True}
