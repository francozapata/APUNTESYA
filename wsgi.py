# wsgi.py
import os
from flask import request, abort
from sqlalchemy import create_engine, text

# Importa la app real
from apuntesya2.app import app  # NO toques app.py

# 🔒 Activamos este endpoint SOLO si la variable está a "1"
if os.getenv("ENABLE_PROMOTE", "").strip() == "1":
    # Usa la misma DB que tu app (Postgres en DATABASE_URL o SQLite local)
    DB_URL = os.getenv("DATABASE_URL") or "sqlite:///instance/apuntesya.db"
    engine = create_engine(DB_URL, future=True)

    def _promote_admin_once():
        # Protegido por secreto en variable de entorno
        secret = os.getenv("PROMOTE_SECRET", "")
        if not secret or request.args.get("secret") != secret:
            abort(403)

        email = (request.args.get("email") or "").strip().lower()
        if not email:
            return "Falta ?email=...", 400

        # Intento booleano True/False
        try:
            with engine.begin() as conn:
                conn.execute(text("UPDATE users SET is_admin=TRUE WHERE lower(email)=:e"), {"e": email})
        except Exception:
            # Fallback por si tu columna usa 0/1
            with engine.begin() as conn:
                conn.execute(text("UPDATE users SET is_admin=1 WHERE lower(email)=:e"), {"e": email})

        return f"OK: {email} ahora es admin", 200

    # Evita error por doble registro al recargar
    if "_promote_admin_once" not in app.view_functions:
        app.add_url_rule(
            "/_promote_admin_once",
            endpoint="_promote_admin_once",
            view_func=_promote_admin_once,
            methods=["GET"]
        )

# Expuesto para gunicorn
if __name__ == "__main__":
    app.run()
