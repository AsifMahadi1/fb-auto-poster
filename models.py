"""Data layer for FB Auto Poster.

Uses the Python standard library ``sqlite3`` only (no ORM) so the app stays
easy to install on aarch64 / Python 3.8 with minimal dependencies.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data.db")
UPLOAD_ROOT = os.path.join(BASE_DIR, "uploads")
PRODUCT_UPLOAD_ROOT = os.path.join(UPLOAD_ROOT, "products")

MAX_IMAGES_PER_PRODUCT = 5
MIN_IMAGES_PER_PRODUCT = 1

POST_STATUSES = ["draft", "approved", "published", "failed"]

DEFAULT_SETTINGS = {
    "fb_page_id": "",
    "fb_access_token": "",
    "graph_version": "v21.0",
    "ai_base_url": "",
    "ai_api_key": "",
    "ai_model": "",
    "generate_interval_minutes": "60",
    "publish_interval_minutes": "60",
    "post_language": "Bangla",
    "use_emoji": "1",
    "custom_system_prompt": "",
    "auto_approve": "0",
    "system_paused": "0",
    "last_generated_sort_order": "0",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    price TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS product_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    caption TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    images TEXT NOT NULL DEFAULT '[]',
    fb_post_id TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    approved_at TEXT NOT NULL DEFAULT '',
    posted_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_product_images_product ON product_images(product_id);
"""


# --------------------------------------------------------------------------
# time helpers
# --------------------------------------------------------------------------

def now_iso() -> str:
    """Current local time as an ISO-ish string ('' never returned)."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse a stored timestamp; returns None when empty/invalid."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def format_dt(value: Optional[str]) -> str:
    dt = parse_dt(value)
    if dt is None:
        return "-"
    return dt.strftime("%d %b %Y, %H:%M")


# --------------------------------------------------------------------------
# connections / schema
# --------------------------------------------------------------------------

def connect() -> sqlite3.Connection:
    """Open a connection. Safe to call from the background loop thread."""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    os.makedirs(PRODUCT_UPLOAD_ROOT, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode = WAL")
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        # Seed the loop timers so the dashboard has a countdown right away.
        for key in ("last_generate_at", "last_publish_at"):
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, now_iso()),
            )
        for key in (
            "last_generate_result",
            "last_publish_result",
            "last_generated_product_id",
            "secret_key",
            "admin_username",
            "admin_password_hash",
        ):
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, ""),
            )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def _get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return row["value"] if row["value"] is not None else default


def get_setting(key: str, default: str = "") -> str:
    conn = connect()
    try:
        return _get_setting(conn, key, default)
    finally:
        conn.close()


def get_settings() -> Dict[str, str]:
    conn = connect()
    try:
        values = dict(DEFAULT_SETTINGS)
        for row in conn.execute("SELECT key, value FROM settings").fetchall():
            values[row["key"]] = row["value"]
        return values
    finally:
        conn.close()


def set_setting(key: str, value: str) -> None:
    conn = connect()
    try:
        _set_setting(conn, key, value)
        conn.commit()
    finally:
        conn.close()


def _set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, "" if value is None else str(value)),
    )


def update_settings(values: Dict[str, Any]) -> None:
    conn = connect()
    try:
        for key, value in values.items():
            _set_setting(conn, key, value)
        conn.commit()
    finally:
        conn.close()


def get_interval_minutes(key: str, default: int = 60) -> int:
    raw = get_setting(key, str(default))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(1, value)


def is_paused(settings: Optional[Dict[str, str]] = None) -> bool:
    """True when both background loops are switched off from the dashboard."""
    if settings is None:
        raw = get_setting("system_paused", "0")
    else:
        raw = settings.get("system_paused") or "0"
    return str(raw).strip() == "1"


def set_paused(paused: bool) -> None:
    set_setting("system_paused", "1" if paused else "0")


# --------------------------------------------------------------------------
# products
# --------------------------------------------------------------------------

def _images_for(conn: sqlite3.Connection, product_id: int) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, path FROM product_images WHERE product_id = ? "
        "ORDER BY sort_order ASC, id ASC",
        (product_id,),
    ).fetchall()
    images = []
    for row in rows:
        images.append(
            {
                "id": row["id"],
                "path": row["path"],
                "abs_path": os.path.join(UPLOAD_ROOT, row["path"]),
            }
        )
    return images


def _product_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> Dict[str, Any]:
    product = dict(row)
    product["images"] = _images_for(conn, product["id"])
    product["image_count"] = len(product["images"])
    return product


def list_products() -> List[Dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM products ORDER BY sort_order ASC, id ASC"
        ).fetchall()
        return [_product_from_row(conn, row) for row in rows]
    finally:
        conn.close()


def get_product(product_id: int) -> Optional[Dict[str, Any]]:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        if row is None:
            return None
        return _product_from_row(conn, row)
    finally:
        conn.close()


def next_sort_order() -> int:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 AS value FROM products"
        ).fetchone()
        return int(row["value"])
    finally:
        conn.close()


def create_product(
    name: str, description: str, price: str, sort_order: int
) -> int:
    stamp = now_iso()
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT INTO products (name, description, price, sort_order, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, description, price, sort_order, stamp, stamp),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def update_product(
    product_id: int, name: str, description: str, price: str, sort_order: int
) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE products SET name = ?, description = ?, price = ?, sort_order = ?, "
            "updated_at = ? WHERE id = ?",
            (name, description, price, sort_order, now_iso(), product_id),
        )
        conn.commit()
    finally:
        conn.close()


def reorder_products(order_map: Dict[int, int]) -> None:
    conn = connect()
    try:
        for product_id, sort_order in order_map.items():
            conn.execute(
                "UPDATE products SET sort_order = ?, updated_at = ? WHERE id = ?",
                (sort_order, now_iso(), product_id),
            )
        conn.commit()
    finally:
        conn.close()


def add_image(product_id: int, rel_path: str, sort_order: int) -> int:
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT INTO product_images (product_id, path, sort_order) VALUES (?, ?, ?)",
            (product_id, rel_path, sort_order),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def set_image_sort_order(image_id: int, sort_order: int) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE product_images SET sort_order = ? WHERE id = ?",
            (sort_order, image_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_images(image_ids: List[int]) -> List[str]:
    """Delete image rows, returning the relative paths that were removed."""
    if not image_ids:
        return []
    conn = connect()
    try:
        placeholders = ",".join("?" for _ in image_ids)
        rows = conn.execute(
            "SELECT path FROM product_images WHERE id IN (%s)" % placeholders,
            image_ids,
        ).fetchall()
        paths = [row["path"] for row in rows]
        conn.execute(
            "DELETE FROM product_images WHERE id IN (%s)" % placeholders, image_ids
        )
        conn.commit()
        return paths
    finally:
        conn.close()


def remove_files(rel_paths: List[str]) -> None:
    for rel_path in rel_paths:
        abs_path = os.path.join(UPLOAD_ROOT, rel_path)
        try:
            if os.path.isfile(abs_path):
                os.remove(abs_path)
        except OSError:
            pass


def delete_product(product_id: int) -> None:
    product = get_product(product_id)
    conn = connect()
    try:
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        conn.commit()
    finally:
        conn.close()
    if product is not None:
        remove_files([image["path"] for image in product["images"]])
        folder = os.path.join(PRODUCT_UPLOAD_ROOT, str(product_id))
        try:
            if os.path.isdir(folder) and not os.listdir(folder):
                os.rmdir(folder)
        except OSError:
            pass


# --------------------------------------------------------------------------
# posts / drafts
# --------------------------------------------------------------------------

def _post_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    post = dict(row)
    try:
        stored = json.loads(post.get("images") or "[]")
    except ValueError:
        stored = []
    if not isinstance(stored, list):
        stored = []
    post["images"] = [str(item) for item in stored]
    post["image_abs_paths"] = [
        os.path.join(UPLOAD_ROOT, item) for item in post["images"]
    ]
    post["product_name"] = post.get("product_name") or "(product removed)"
    return post


POST_SELECT = (
    "SELECT p.*, pr.name AS product_name FROM posts p "
    "LEFT JOIN products pr ON pr.id = p.product_id "
)


def list_posts(status: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    conn = connect()
    try:
        sql = POST_SELECT
        params: List[Any] = []
        if status and status != "all":
            sql += "WHERE p.status = ? "
            params.append(status)
        sql += "ORDER BY p.created_at DESC, p.id DESC "
        if limit:
            sql += "LIMIT ? "
            params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        return [_post_from_row(row) for row in rows]
    finally:
        conn.close()


def get_post(post_id: int) -> Optional[Dict[str, Any]]:
    conn = connect()
    try:
        row = conn.execute(POST_SELECT + "WHERE p.id = ?", (post_id,)).fetchone()
        if row is None:
            return None
        return _post_from_row(row)
    finally:
        conn.close()


def create_post(product_id: int, caption: str, images: List[str]) -> int:
    stamp = now_iso()
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT INTO posts (product_id, caption, status, images, created_at, updated_at) "
            "VALUES (?, ?, 'draft', ?, ?, ?)",
            (product_id, caption, json.dumps(list(images)), stamp, stamp),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def update_caption(post_id: int, caption: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE posts SET caption = ?, updated_at = ? WHERE id = ?",
            (caption, now_iso(), post_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_post_images(post_id: int, images: List[str]) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE posts SET images = ?, updated_at = ? WHERE id = ?",
            (json.dumps(list(images)), now_iso(), post_id),
        )
        conn.commit()
    finally:
        conn.close()


def approve_post(post_id: int) -> None:
    stamp = now_iso()
    conn = connect()
    try:
        conn.execute(
            "UPDATE posts SET status = 'approved', error = '', approved_at = ?, "
            "updated_at = ? WHERE id = ?",
            (stamp, stamp, post_id),
        )
        conn.commit()
    finally:
        conn.close()


def reset_to_draft(post_id: int) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE posts SET status = 'draft', error = '', approved_at = '', "
            "updated_at = ? WHERE id = ?",
            (now_iso(), post_id),
        )
        conn.commit()
    finally:
        conn.close()


def retry_post(post_id: int) -> None:
    """Put a failed post back at the front of the approved queue."""
    stamp = now_iso()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT approved_at FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        approved_at = ""
        if row is not None and row["approved_at"]:
            approved_at = row["approved_at"]
        conn.execute(
            "UPDATE posts SET status = 'approved', error = '', "
            "approved_at = ?, updated_at = ? WHERE id = ?",
            (approved_at or stamp, stamp, post_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_published(post_id: int, fb_post_id: str) -> None:
    stamp = now_iso()
    conn = connect()
    try:
        conn.execute(
            "UPDATE posts SET status = 'published', fb_post_id = ?, error = '', "
            "posted_at = ?, updated_at = ? WHERE id = ?",
            (fb_post_id, stamp, stamp, post_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_failed(post_id: int, error: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "UPDATE posts SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (error[:2000], now_iso(), post_id),
        )
        conn.commit()
    finally:
        conn.close()


def delete_post(post_id: int) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        conn.commit()
    finally:
        conn.close()


def oldest_approved_post() -> Optional[Dict[str, Any]]:
    conn = connect()
    try:
        row = conn.execute(
            POST_SELECT
            + "WHERE p.status = 'approved' "
            "ORDER BY COALESCE(NULLIF(p.approved_at, ''), p.created_at) ASC, p.id ASC "
            "LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return _post_from_row(row)
    finally:
        conn.close()


def status_counts() -> Dict[str, int]:
    counts = {status: 0 for status in POST_STATUSES}
    conn = connect()
    try:
        for row in conn.execute(
            "SELECT status, COUNT(*) AS total FROM posts GROUP BY status"
        ).fetchall():
            counts[row["status"]] = int(row["total"])
        return counts
    finally:
        conn.close()


def product_has_active_post(conn: sqlite3.Connection, product_id: int) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) AS total FROM posts "
        "WHERE product_id = ? AND status IN ('draft', 'approved')",
        (product_id,),
    ).fetchone()
    return int(row["total"]) > 0


def products_missing_draft() -> List[int]:
    """Product ids without a current draft, in generation order."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT p.id FROM products p WHERE NOT EXISTS ("
            "SELECT 1 FROM posts po "
            "WHERE po.product_id = p.id AND po.status = 'draft'"
            ") ORDER BY p.sort_order ASC, p.id ASC"
        ).fetchall()
        return [int(row["id"]) for row in rows]
    finally:
        conn.close()


def pick_next_product_for_generation() -> Optional[int]:
    """Next product (by sort_order) after the last generated one, wrapping.

    The cursor is stored in settings (`last_generated_sort_order`, default 0)
    and advances every time a draft is created. Existing drafts are ignored so
    the loop keeps cycling through the catalogue forever.
    """
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, sort_order FROM products ORDER BY sort_order ASC, id ASC"
        ).fetchall()
        if not rows:
            return None
        try:
            cursor_order = int(
                _get_setting(conn, "last_generated_sort_order", "0") or 0
            )
        except ValueError:
            cursor_order = 0
        try:
            cursor_id = int(_get_setting(conn, "last_generated_product_id", "0") or 0)
        except ValueError:
            cursor_id = 0

        # Prefer the exact product that was generated last (handles duplicate
        # sort_order values); fall back to the stored sort_order cursor.
        start = None
        for index, row in enumerate(rows):
            if int(row["id"]) == cursor_id and int(row["sort_order"]) == cursor_order:
                start = index + 1
                break
        if start is None:
            start = 0
            for index, row in enumerate(rows):
                if int(row["sort_order"]) <= cursor_order:
                    start = index + 1
        return int(rows[start % len(rows)]["id"])
    finally:
        conn.close()
