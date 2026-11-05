"""Background loops: AI draft generation and Facebook publishing.

A single daemon thread wakes up every TICK_SECONDS and checks whether either
interval has elapsed. Every run records its outcome in the settings table so
the dashboard can show what happened and when the next run is due.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import ai_client
import fb_api
import models

log = logging.getLogger("fb_auto_poster.scheduler")

TICK_SECONDS = 20
_thread = None  # type: Optional[threading.Thread]
_thread_lock = threading.Lock()
_generate_lock = threading.Lock()
_publish_lock = threading.Lock()


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

def start() -> None:
    """Start the single scheduler daemon thread (idempotent)."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(
            target=_loop, name="fb-auto-poster-scheduler", daemon=True
        )
        _thread.start()
        log.info("Scheduler thread started (tick %ss)", TICK_SECONDS)


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()


def is_paused() -> bool:
    """True when the Stop button has switched both loops off."""
    return models.is_paused()


def set_paused(paused: bool) -> None:
    """Toggle the loops. Both pick this up on their next tick (<= 20s)."""
    models.set_paused(paused)
    log.info("System %s from the dashboard", "STOPPED" if paused else "RUNNING")


def _loop() -> None:
    # Give Flask a moment to finish booting before the first check.
    threading.Event().wait(3)
    while True:
        try:
            tick()
        except Exception:
            log.exception("Scheduler tick failed")
        try:
            threading.Event().wait(TICK_SECONDS)
        except Exception:
            log.exception("Scheduler sleep interrupted")


def tick() -> None:
    """Run whichever loop is due. Exceptions are contained by the caller."""
    settings = models.get_settings()
    if models.is_paused(settings):
        # Re-read every tick, so the dashboard toggle applies without a restart.
        return
    now = datetime.now()

    gen_interval = models.get_interval_minutes("generate_interval_minutes", 60)
    pub_interval = models.get_interval_minutes("publish_interval_minutes", 60)

    last_gen = models.parse_dt(settings.get("last_generate_at"))
    last_pub = models.parse_dt(settings.get("last_publish_at"))

    if last_gen is None or now >= last_gen + timedelta(minutes=gen_interval):
        run_generation(trigger="auto")
    if last_pub is None or now >= last_pub + timedelta(minutes=pub_interval):
        run_publish(trigger="auto")


# --------------------------------------------------------------------------
# timers
# --------------------------------------------------------------------------

def next_run_times() -> Tuple[Optional[datetime], Optional[datetime]]:
    settings = models.get_settings()
    gen_interval = models.get_interval_minutes("generate_interval_minutes", 60)
    pub_interval = models.get_interval_minutes("publish_interval_minutes", 60)
    last_gen = models.parse_dt(settings.get("last_generate_at")) or datetime.now()
    last_pub = models.parse_dt(settings.get("last_publish_at")) or datetime.now()
    return (
        last_gen + timedelta(minutes=gen_interval),
        last_pub + timedelta(minutes=pub_interval),
    )


def minutes_until(target: Optional[datetime]) -> int:
    if target is None:
        return 0
    remaining = (target - datetime.now()).total_seconds()
    if remaining <= 0:
        return 0
    return int((remaining + 59) // 60)


# --------------------------------------------------------------------------
# AI draft generation
# --------------------------------------------------------------------------

def _record_generation(result: str) -> None:
    models.update_settings(
        {
            "last_generate_at": models.now_iso(),
            "last_generate_result": result,
        }
    )


def run_generation(trigger: str = "auto") -> Dict[str, Any]:
    """Fill every missing draft, then rotate through all products forever."""
    if not _generate_lock.acquire(False):
        return {"ok": False, "message": "A draft generation is already running."}
    try:
        settings = models.get_settings()
        base_url = (settings.get("ai_base_url") or "").strip()
        model = (settings.get("ai_model") or "").strip()
        if not base_url or not model:
            message = "Generation skipped: AI Base URL / model not configured."
            _record_generation(message)
            log.warning(message)
            return {"ok": False, "message": message}

        # Exactly ONE draft per tick: the next product by sort_order, wrapping around.
        product_id = models.pick_next_product_for_generation()
        if product_id is None:
            message = "Generation skipped: no products exist."
            _record_generation(message)
            log.info(message)
            return {"ok": False, "message": message}
        product_ids = [product_id]

        generated = []
        generated_names = []
        failures = []
        for product_id in product_ids:
            product = models.get_product(product_id)
            if product is None:
                continue
            try:
                caption = ai_client.generate_caption(settings, product)
            except ai_client.AIError as exc:
                message = "Generation failed for '%s': %s" % (product["name"], exc)
                failures.append(message)
                log.error(message)
                continue

            image_paths = [image["path"] for image in product["images"]]
            post_id = models.create_post(product_id, caption, image_paths)
            auto_approved = str(settings.get("auto_approve") or "0").strip() == "1"
            if auto_approved:
                models.approve_post(post_id)
            models.update_settings(
                {
                    "last_generated_product_id": str(product_id),
                    "last_generated_sort_order": str(product["sort_order"]),
                }
            )
            message = "Draft #%s generated for '%s' (%s trigger)." % (
                post_id,
                product["name"],
                trigger,
            )
            if auto_approved:
                message += " auto-approved."
            generated.append(message)
            generated_names.append(product["name"])
            log.info(message)

        if not generated:
            message = (
                "; ".join(failures)
                if failures
                else "Generation skipped: no products exist."
            )
            _record_generation(message)
            return {"ok": False, "message": message}

        if len(generated) == 1:
            message = generated[0]
        else:
            message = "Generated %d drafts (%s trigger): %s." % (
                len(generated),
                trigger,
                ", ".join(generated_names),
            )
            if failures:
                message += " %d failed." % len(failures)
        _record_generation(message)
        return {"ok": True, "message": message}
    finally:
        _generate_lock.release()


# --------------------------------------------------------------------------
# Facebook publishing
# --------------------------------------------------------------------------

def _record_publish(result: str) -> None:
    models.update_settings(
        {
            "last_publish_at": models.now_iso(),
            "last_publish_result": result,
        }
    )


def run_publish(trigger: str = "auto") -> Dict[str, Any]:
    """Publish the oldest approved draft, FIFO."""
    if not _publish_lock.acquire(False):
        return {"ok": False, "message": "A publish run is already in progress."}
    try:
        settings = models.get_settings()
        page_id = (settings.get("fb_page_id") or "").strip()
        token = (settings.get("fb_access_token") or "").strip()
        if not page_id or not token:
            message = "Publishing skipped: Facebook Page ID / token not configured."
            _record_publish(message)
            log.warning(message)
            return {"ok": False, "message": message}

        post = models.oldest_approved_post()
        if post is None:
            message = "Publishing skipped: the approved queue is empty."
            _record_publish(message)
            log.info(message)
            return {"ok": False, "message": message}

        try:
            fb_post_id = fb_api.publish_post(
                settings, post["caption"], post["image_abs_paths"]
            )
        except fb_api.FbError as exc:
            models.mark_failed(post["id"], str(exc))
            message = "Publishing draft #%s failed: %s" % (post["id"], exc)
            _record_publish(message)
            log.error(message)
            return {"ok": False, "message": message}
        except Exception as exc:  # safety net: never leave the queue stuck
            models.mark_failed(post["id"], str(exc))
            message = "Publishing draft #%s failed unexpectedly: %s" % (post["id"], exc)
            _record_publish(message)
            log.exception(message)
            return {"ok": False, "message": message}

        models.mark_published(post["id"], fb_post_id)
        message = "Draft #%s published to Facebook as %s (%s trigger)." % (
            post["id"],
            fb_post_id,
            trigger,
        )
        _record_publish(message)
        log.info(message)
        return {"ok": True, "message": message}
    finally:
        _publish_lock.release()
