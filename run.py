"""Entry point for FB Auto Poster."""

from __future__ import annotations

from app import create_app

app = create_app()

if __name__ == "__main__":
    # use_reloader is off so the background scheduler thread is only started once.
    app.run(host="0.0.0.0", port=8069, debug=False, threaded=True, use_reloader=False)
