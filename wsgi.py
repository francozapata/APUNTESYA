try:
    from apuntesya2.app import app
except Exception:
    try:
        from app import app
    except Exception:
        from run import app
