# wsgi.py
import os, sys
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from apuntesya2.app import app  # tu app real

# --- RUTA TEMPORAL PARA PROMOVER A ADMIN (BORRAR LUEGO) ---
import os
from flask import request, abort, jsonify
from apuntesya2.app import db
from apuntesya2.models import User

@app.route("/_promote_admin", methods=["GET","POST"])
def _promote_admin():
    token = request.args.get("token") or request.headers.get("X-Admin-Token")
    if not token or token != os.environ.get("ADMIN_SETUP_TOKEN"):
        abort(403)

    # email por querystring o por JSON {"email": "..."}
    email = request.args.get("email")
    if not email and request.is_json:
        body = request.get_json(silent=True) or {}
        email = body.get("email")
    if not email:
        return jsonify(ok=False, error="Falta email"), 400

    user = User.query.filter_by(email=email).first()
    if not user:
        return jsonify(ok=False, error="Usuario no encontrado"), 404

    # Soporta ambos esquemas de admin
    if hasattr(user, "is_admin"):
        user.is_admin = True
    elif hasattr(user, "role"):
        user.role = "admin"
    else:
        return jsonify(ok=False, error="Modelo User sin campo admin conocido"), 500

    db.session.commit()
    return jsonify(ok=True, user_id=user.id, email=user.email)
# --- FIN RUTA TEMPORAL ---


# Ruta de health (por si tu app no la trae)
@app.get("/health")
def _health():
    return {"ok": True}
