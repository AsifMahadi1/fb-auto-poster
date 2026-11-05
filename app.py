"""Flask application factory and routes for FB Auto Poster."""

from __future__ import annotations

import logging
import os
import re
import secrets
import uuid
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Tuple

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import ai_client
import fb_api
import models
import scheduler

BASE_DIR = models.BASE_DIR
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
LOG_PATH = os.path.join(BASE_DIR, "app.log")
LOG_TAIL_LIMIT = 200
LOG_LEVEL_PATTERN = re.compile(r"\s(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s")

log = logging.getLogger("fb_auto_poster")


# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

def setup_logging() -> None:
    root = logging.getLogger("fb_auto_poster")
    if getattr(root, "_fb_auto_poster_configured", False):
        return
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )
    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    root.propagate = False
    setattr(root, "_fb_auto_poster_configured", True)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _int_form(name: str, default: Optional[int] = None) -> Optional[int]:
    raw = (request.form.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _save_uploaded_images(product_id: int, files: List[Any]) -> List[str]:
    folder = os.path.join(models.PRODUCT_UPLOAD_ROOT, str(product_id))
    os.makedirs(folder, exist_ok=True)
    saved = []
    for storage in files:
        original = secure_filename(storage.filename or "")
        extension = os.path.splitext(original)[1].lower()
        filename = uuid.uuid4().hex + extension
        storage.save(os.path.join(folder, filename))
        saved.append("products/%d/%s" % (product_id, filename))
    return saved


def _validate_upload_extensions(files: List[Any]) -> Optional[str]:
    for storage in files:
        original = secure_filename(storage.filename or "")
        extension = os.path.splitext(original)[1].lower()
        if extension not in ALLOWED_IMAGE_EXTENSIONS:
            return (
                "Unsupported image type for '%s'. Use jpg, png, webp or gif."
                % (storage.filename or "file")
            )
    return None


def _form_text(name: str) -> str:
    return (request.form.get(name) or "").strip()


def _form_checkbox(name: str) -> bool:
    return (request.form.get(name) or "").lower() in ("1", "on", "true", "yes")


def _bool_setting(value: str) -> bool:
    return (value or "0").strip() == "1"


def _admin_configured() -> bool:
    return bool(
        (models.get_setting("admin_username") or "").strip()
        and (models.get_setting("admin_password_hash") or "").strip()
    )


def _log_level(line: str) -> str:
    """Map a log line to a css friendly level name."""
    match = LOG_LEVEL_PATTERN.search(line)
    if not match:
        return "info"
    level = match.group(1)
    if level == "WARNING":
        return "warning"
    if level in ("ERROR", "CRITICAL"):
        return "error"
    if level == "DEBUG":
        return "debug"
    return "info"


def _read_log_tail(limit: int = LOG_TAIL_LIMIT) -> List[Dict[str, str]]:
    if not os.path.exists(LOG_PATH):
        return []
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.readlines()
    except OSError:
        return []
    lines = [line.rstrip("\r\n") for line in raw[-limit:]]
    return [{"level": _log_level(line), "text": line} for line in lines]


# --------------------------------------------------------------------------
# factory
# --------------------------------------------------------------------------

def create_app() -> Flask:
    models.init_db()
    setup_logging()

    secret_key = models.get_setting("secret_key", "")
    if not secret_key:
        secret_key = secrets.token_hex(32)
        models.set_setting("secret_key", secret_key)

    app = Flask(__name__)
    app.config["SECRET_KEY"] = secret_key
    app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024
    app.config["JSON_SORT_KEYS"] = False
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    app.jinja_env.filters["fmt_dt"] = models.format_dt
    app.jinja_env.filters["fmt_price"] = lambda value: (
        str(value).strip() if str(value or "").strip() else "-"
    )
    app.jinja_env.globals["max_images"] = models.MAX_IMAGES_PER_PRODUCT

    # ------------------------------------------------------------------
    # authentication
    # ------------------------------------------------------------------

    public_endpoints = {"static", "login", "setup"}

    @app.before_request
    def require_login() -> Any:
        if request.endpoint in public_endpoints:
            return None
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        if not _admin_configured():
            session.clear()
            return redirect(url_for("login"))
        return None

    @app.route("/login", methods=["GET", "POST"])
    def login() -> Any:
        configured = _admin_configured()
        if configured and session.get("authenticated"):
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            if not configured:
                flash("Create the admin account first.", "info")
                return redirect(url_for("setup"))

            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            stored_username = (models.get_setting("admin_username") or "").strip()
            stored_hash = models.get_setting("admin_password_hash") or ""

            valid_password = False
            if stored_hash:
                try:
                    valid_password = check_password_hash(stored_hash, password)
                except (TypeError, ValueError):
                    valid_password = False

            if username == stored_username and valid_password:
                session.clear()
                session["authenticated"] = True
                session["username"] = stored_username
                log.info("Admin login succeeded")
                return redirect(url_for("dashboard"))

            log.warning("Failed admin login attempt")
            flash("Invalid username or password.", "error")

        return render_template(
            "login.html",
            admin_configured=configured,
            username=(request.form.get("username") or ""),
        )

    @app.route("/setup", methods=["GET", "POST"])
    def setup() -> Any:
        if _admin_configured():
            if session.get("authenticated"):
                return redirect(url_for("dashboard"))
            return redirect(url_for("login"))

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            confirm = request.form.get("confirm_password") or ""

            error = None
            if not username:
                error = "Username is required."
            elif len(password) < 8:
                error = "Password must be at least 8 characters."
            elif password != confirm:
                error = "Passwords do not match."

            if error:
                flash(error, "error")
            else:
                models.update_settings(
                    {
                        "admin_username": username,
                        "admin_password_hash": generate_password_hash(password),
                    }
                )
                session.clear()
                session["authenticated"] = True
                session["username"] = username
                log.info("Admin account created")
                flash("Admin account created. You are now signed in.", "success")
                return redirect(url_for("dashboard"))

        return render_template("setup.html", username=(request.form.get("username") or ""))

    @app.route("/logout", methods=["POST"])
    def logout() -> Any:
        session.clear()
        flash("You have been signed out.", "info")
        return redirect(url_for("login"))

    scheduler.start()
    log.info("FB Auto Poster started (db=%s)", models.DB_PATH)

    # ------------------------------------------------------------------
    # dashboard
    # ------------------------------------------------------------------

    @app.route("/")
    def dashboard() -> str:
        settings = models.get_settings()
        next_gen, next_pub = scheduler.next_run_times()
        counts = models.status_counts()
        recent = models.list_posts(limit=8)
        return render_template(
            "dashboard.html",
            active="dashboard",
            counts=counts,
            recent=recent,
            products_count=len(models.list_products()),
            next_generate=next_gen,
            next_publish=next_pub,
            next_generate_minutes=scheduler.minutes_until(next_gen),
            next_publish_minutes=scheduler.minutes_until(next_pub),
            generate_interval=settings.get("generate_interval_minutes", "60"),
            publish_interval=settings.get("publish_interval_minutes", "60"),
            last_generate_at=settings.get("last_generate_at", ""),
            last_publish_at=settings.get("last_publish_at", ""),
            last_generate_result=settings.get("last_generate_result", ""),
            last_publish_result=settings.get("last_publish_result", ""),
            scheduler_running=scheduler.is_running(),
            system_paused=models.is_paused(settings),
            fb_ready=bool(
                (settings.get("fb_page_id") or "").strip()
                and (settings.get("fb_access_token") or "").strip()
            ),
            ai_ready=bool(
                (settings.get("ai_base_url") or "").strip()
                and (settings.get("ai_model") or "").strip()
            ),
        )

    # ------------------------------------------------------------------
    # products
    # ------------------------------------------------------------------

    @app.route("/products")
    def products() -> str:
        return render_template(
            "products.html", active="products", products=models.list_products()
        )

    @app.route("/products/new", methods=["GET", "POST"])
    def product_new() -> Any:
        if request.method == "POST":
            name = _form_text("name")
            description = _form_text("description")
            price = _form_text("price")
            sort_order = _int_form("sort_order")
            files = [
                storage
                for storage in request.files.getlist("images")
                if storage and storage.filename
            ]

            error = _validate_upload_extensions(files)
            if error is None and not name:
                error = "Product name is required."
            if error is None and (len(files) < models.MIN_IMAGES_PER_PRODUCT or len(files) > models.MAX_IMAGES_PER_PRODUCT):
                error = "Upload between %d and %d images." % (
                    models.MIN_IMAGES_PER_PRODUCT,
                    models.MAX_IMAGES_PER_PRODUCT,
                )
            if error:
                flash(error, "error")
                return render_template(
                    "product_form.html",
                    active="products",
                    product=None,
                    form={
                        "name": name,
                        "description": description,
                        "price": price,
                        "sort_order": sort_order if sort_order is not None else models.next_sort_order(),
                    },
                )

            if sort_order is None:
                sort_order = models.next_sort_order()
            product_id = models.create_product(name, description, price, sort_order)
            saved = _save_uploaded_images(product_id, files)
            for index, rel_path in enumerate(saved):
                models.add_image(product_id, rel_path, index)
            log.info("Product created: #%s %s", product_id, name)
            flash("Product '%s' created." % name, "success")
            return redirect(url_for("products"))

        return render_template(
            "product_form.html",
            active="products",
            product=None,
            form={
                "name": "",
                "description": "",
                "price": "",
                "sort_order": models.next_sort_order(),
            },
        )

    @app.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
    def product_edit(product_id: int) -> Any:
        product = models.get_product(product_id)
        if product is None:
            abort(404)

        if request.method == "POST":
            name = _form_text("name")
            description = _form_text("description")
            price = _form_text("price")
            sort_order = _int_form("sort_order", product["sort_order"])
            files = [
                storage
                for storage in request.files.getlist("images")
                if storage and storage.filename
            ]
            existing_ids = [image["id"] for image in product["images"]]
            keep_ids: List[int] = []
            for raw in request.form.getlist("keep_image"):
                try:
                    value = int(raw)
                except ValueError:
                    continue
                if value in existing_ids:
                    keep_ids.append(value)

            error = _validate_upload_extensions(files)
            if error is None and not name:
                error = "Product name is required."
            total_images = len(keep_ids) + len(files)
            if error is None and (
                total_images < models.MIN_IMAGES_PER_PRODUCT
                or total_images > models.MAX_IMAGES_PER_PRODUCT
            ):
                error = "A product needs between %d and %d images." % (
                    models.MIN_IMAGES_PER_PRODUCT,
                    models.MAX_IMAGES_PER_PRODUCT,
                )
            if error:
                flash(error, "error")
                return render_template(
                    "product_form.html",
                    active="products",
                    product=product,
                    form={
                        "name": name,
                        "description": description,
                        "price": price,
                        "sort_order": sort_order,
                    },
                )

            models.update_product(
                product_id, name, description, price,
                sort_order if sort_order is not None else product["sort_order"],
            )

            removed_ids = [image_id for image_id in existing_ids if image_id not in keep_ids]
            removed_paths = models.delete_images(removed_ids)
            models.remove_files(removed_paths)

            # Re-number kept images so order stays stable, then append uploads.
            kept = [image for image in product["images"] if image["id"] in keep_ids]
            for index, image in enumerate(kept):
                models.set_image_sort_order(image["id"], index)
            saved = _save_uploaded_images(product_id, files)
            base = len(kept)
            for offset, rel_path in enumerate(saved):
                models.add_image(product_id, rel_path, base + offset)
            log.info("Product updated: #%s %s", product_id, name)
            flash("Product '%s' updated." % name, "success")
            return redirect(url_for("products"))

        return render_template(
            "product_form.html",
            active="products",
            product=product,
            form={
                "name": product["name"],
                "description": product["description"],
                "price": product["price"],
                "sort_order": product["sort_order"],
            },
        )

    @app.route("/products/<int:product_id>/delete", methods=["POST"])
    def product_delete(product_id: int) -> Any:
        product = models.get_product(product_id)
        if product is None:
            abort(404)
        models.delete_product(product_id)
        log.info("Product deleted: #%s %s", product_id, product["name"])
        flash("Product '%s' deleted." % product["name"], "success")
        return redirect(url_for("products"))

    @app.route("/products/reorder", methods=["POST"])
    def product_reorder() -> Any:
        order_map: Dict[int, int] = {}
        for product in models.list_products():
            raw = (request.form.get("sort_order_%d" % product["id"]) or "").strip()
            if not raw:
                continue
            try:
                order_map[product["id"]] = int(raw)
            except ValueError:
                continue
        models.reorder_products(order_map)
        flash("Product order saved.", "success")
        return redirect(url_for("products"))

    # ------------------------------------------------------------------
    # drafts
    # ------------------------------------------------------------------

    @app.route("/drafts")
    def drafts() -> str:
        status = (request.args.get("status") or "all").strip()
        if status not in models.POST_STATUSES and status != "all":
            status = "all"
        return render_template(
            "drafts.html",
            active="drafts",
            posts=models.list_posts(status=status),
            status=status,
            counts=models.status_counts(),
        )

    @app.route("/drafts/<int:post_id>/caption", methods=["POST"])
    def draft_caption(post_id: int) -> Any:
        post = models.get_post(post_id)
        if post is None:
            abort(404)
        caption = (request.form.get("caption") or "").strip()
        if not caption:
            flash("Caption cannot be empty.", "error")
            return redirect(url_for("drafts", status=request.form.get("status") or "all"))
        models.update_caption(post_id, caption)
        flash("Caption saved for draft #%d." % post_id, "success")
        return redirect(url_for("drafts", status=request.form.get("status") or "all"))

    @app.route("/drafts/<int:post_id>/approve", methods=["POST"])
    def draft_approve(post_id: int) -> Any:
        post = models.get_post(post_id)
        if post is None:
            abort(404)
        if post["status"] == "published":
            flash("Draft #%d is already published." % post_id, "error")
        else:
            models.approve_post(post_id)
            flash("Draft #%d approved and queued for publishing." % post_id, "success")
        return redirect(url_for("drafts", status=request.form.get("status") or "all"))

    @app.route("/drafts/<int:post_id>/reject", methods=["POST"])
    def draft_reject(post_id: int) -> Any:
        post = models.get_post(post_id)
        if post is None:
            abort(404)
        models.delete_post(post_id)
        flash("Draft #%d rejected and deleted." % post_id, "success")
        return redirect(url_for("drafts", status=request.form.get("status") or "all"))

    @app.route("/drafts/<int:post_id>/retry", methods=["POST"])
    def draft_retry(post_id: int) -> Any:
        post = models.get_post(post_id)
        if post is None:
            abort(404)
        models.retry_post(post_id)
        flash("Draft #%d queued for another publish attempt." % post_id, "success")
        return redirect(url_for("drafts", status=request.form.get("status") or "all"))

    @app.route("/drafts/<int:post_id>/regenerate", methods=["POST"])
    def draft_regenerate(post_id: int) -> Any:
        post = models.get_post(post_id)
        if post is None:
            abort(404)
        product = models.get_product(post["product_id"]) if post["product_id"] else None
        if product is None:
            flash("Cannot regenerate: the product for this draft no longer exists.", "error")
            return redirect(url_for("drafts", status=request.form.get("status") or "all"))

        settings = models.get_settings()
        if not (settings.get("ai_base_url") or "").strip() or not (settings.get("ai_model") or "").strip():
            flash("Cannot regenerate: AI endpoint is not configured (see Settings).", "error")
            return redirect(url_for("drafts", status=request.form.get("status") or "all"))
        try:
            caption = ai_client.generate_caption(settings, product)
        except ai_client.AIError as exc:
            flash("Regeneration failed: %s" % exc, "error")
            return redirect(url_for("drafts", status=request.form.get("status") or "all"))

        models.update_caption(post_id, caption)
        models.set_post_images(post_id, [image["path"] for image in product["images"]])
        models.reset_to_draft(post_id)
        flash("Draft #%d regenerated from product '%s'." % (post_id, product["name"]), "success")
        return redirect(url_for("drafts", status=request.form.get("status") or "all"))

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------

    @app.route("/settings", methods=["GET", "POST"])
    def settings_page() -> Any:
        if request.method == "POST":
            errors = []
            gen_interval = _int_form("generate_interval_minutes", 60)
            pub_interval = _int_form("publish_interval_minutes", 60)
            if gen_interval is None or gen_interval < 1:
                errors.append("Generation interval must be a whole number of minutes (>= 1).")
            if pub_interval is None or pub_interval < 1:
                errors.append("Publish interval must be a whole number of minutes (>= 1).")
            if errors:
                for error in errors:
                    flash(error, "error")
            else:
                models.update_settings(
                    {
                        "fb_page_id": _form_text("fb_page_id"),
                        "fb_access_token": _form_text("fb_access_token"),
                        "graph_version": _form_text("graph_version") or "v21.0",
                        "ai_base_url": _form_text("ai_base_url"),
                        "ai_api_key": _form_text("ai_api_key"),
                        "ai_model": _form_text("ai_model"),
                        "generate_interval_minutes": str(gen_interval),
                        "publish_interval_minutes": str(pub_interval),
                        "post_language": _form_text("post_language") or "Bangla",
                        "use_emoji": "1" if _form_checkbox("use_emoji") else "0",
                        "auto_approve": "1" if _form_checkbox("auto_approve") else "0",
                        "custom_system_prompt": _form_text("custom_system_prompt"),
                    }
                )
                log.info("Settings updated")
                flash("Settings saved.", "success")
            return redirect(url_for("settings_page"))

        return render_template(
            "settings.html",
            active="settings",
            settings=models.get_settings(),
            ai_ready=bool(
                (models.get_setting("ai_base_url") or "").strip()
                and (models.get_setting("ai_model") or "").strip()
            ),
        )

    def _payload() -> Dict[str, Any]:
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            return data
        return request.form.to_dict()

    @app.route("/settings/test-ai", methods=["POST"])
    def settings_test_ai() -> Any:
        payload = _payload()
        settings = models.get_settings()
        for key in ("ai_base_url", "ai_api_key", "ai_model"):
            if key in payload:
                settings[key] = str(payload.get(key) or "").strip()
        ok, message = ai_client.test_connection(settings)
        return jsonify({"ok": ok, "message": message})

    @app.route("/settings/test-fb", methods=["POST"])
    def settings_test_fb() -> Any:
        payload = _payload()
        settings = models.get_settings()
        for key in ("fb_page_id", "fb_access_token", "graph_version"):
            if key in payload:
                settings[key] = str(payload.get(key) or "").strip()
        ok, message = fb_api.test_token(settings)
        return jsonify({"ok": ok, "message": message})

    # ------------------------------------------------------------------
    # manual loop triggers
    # ------------------------------------------------------------------

    @app.route("/trigger/generate", methods=["POST"])
    def trigger_generate() -> Any:
        result = scheduler.run_generation(trigger="manual")
        flash(result["message"], "success" if result["ok"] else "error")
        return redirect(request.referrer or url_for("dashboard"))

    @app.route("/trigger/publish", methods=["POST"])
    def trigger_publish() -> Any:
        result = scheduler.run_publish(trigger="manual")
        flash(result["message"], "success" if result["ok"] else "error")
        return redirect(request.referrer or url_for("dashboard"))

    # ------------------------------------------------------------------
    # system run / stop
    # ------------------------------------------------------------------

    @app.route("/system/stop", methods=["POST"])
    def system_stop() -> Any:
        scheduler.set_paused(True)
        flash(
            "System stopped: draft generation and publishing are paused "
            "(takes effect within one loop tick).",
            "success",
        )
        return redirect(request.referrer or url_for("dashboard"))

    @app.route("/system/start", methods=["POST"])
    def system_start() -> Any:
        scheduler.set_paused(False)
        flash("System running: both loops are active again.", "success")
        return redirect(request.referrer or url_for("dashboard"))

    # ------------------------------------------------------------------
    # logs
    # ------------------------------------------------------------------

    @app.route("/logs")
    def logs_page() -> str:
        try:
            size = os.path.getsize(LOG_PATH)
        except OSError:
            size = 0
        return render_template(
            "logs.html",
            active="logs",
            lines=_read_log_tail(),
            limit=LOG_TAIL_LIMIT,
            log_path=LOG_PATH,
            log_size=size,
        )

    @app.route("/logs/data")
    def logs_data() -> Any:
        return jsonify({"lines": _read_log_tail(), "limit": LOG_TAIL_LIMIT})

    @app.route("/logs/download")
    def logs_download() -> Any:
        if not os.path.exists(LOG_PATH):
            flash("No log file yet - nothing to download.", "error")
            return redirect(url_for("logs_page"))
        return send_from_directory(
            BASE_DIR, "app.log", as_attachment=True, download_name="app.log"
        )

    # ------------------------------------------------------------------
    # uploads
    # ------------------------------------------------------------------

    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename: str) -> Any:
        return send_from_directory(models.UPLOAD_ROOT, filename)

    @app.errorhandler(404)
    def not_found(error: Any) -> Any:
        if request.path.startswith("/settings/test") or request.path.startswith("/trigger"):
            return jsonify({"ok": False, "message": "Not found."}), 404
        return render_template("404.html", active="", message="Page not found."), 404

    @app.errorhandler(413)
    def too_large(error: Any) -> Any:
        flash("Upload is too large (40 MB max).", "error")
        return redirect(url_for("products"))

    return app
