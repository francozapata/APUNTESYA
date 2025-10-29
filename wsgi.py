# wsgi.py
import os, sys
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from apuntesya2.app import app  # tu app real



# Ruta de health (por si tu app no la trae)
@app.get("/health")
def _health():
    return {"ok": True}
