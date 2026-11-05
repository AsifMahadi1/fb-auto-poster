"""Facebook Graph API helpers (page token, multi-photo + text feed posts)."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Tuple

import requests

log = logging.getLogger("fb_auto_poster.fb_api")

GRAPH_ROOT = "https://graph.facebook.com"
DEFAULT_VERSION = "v21.0"
TIMEOUT = 90


class FbError(Exception):
    """Raised for any Graph API failure with a human readable message."""


def _setting(settings: Dict[str, str], key: str, default: str = "") -> str:
    value = settings.get(key)
    if value is None:
        return default
    return str(value).strip()


def graph_base(settings: Dict[str, str]) -> str:
    version = _setting(settings, "graph_version", DEFAULT_VERSION) or DEFAULT_VERSION
    version = version.strip().strip("/")
    if not version.startswith("v"):
        version = "v" + version
    return "%s/%s" % (GRAPH_ROOT, version)


def _credentials(settings: Dict[str, str]) -> Tuple[str, str]:
    page_id = _setting(settings, "fb_page_id")
    token = _setting(settings, "fb_access_token")
    if not page_id:
        raise FbError("Facebook Page ID is not configured. Set it on the Settings page.")
    if not token:
        raise FbError("Facebook Page Access Token is not configured. Set it on the Settings page.")
    return page_id, token


def _error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        body = (response.text or "").strip()
        return body[:500] or "HTTP %s" % response.status_code
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or "Unknown Graph API error"
            code = error.get("code")
            subcode = error.get("error_subcode")
            detail = str(message)
            if code is not None:
                detail += " (code %s" % code
                if subcode is not None:
                    detail += ", subcode %s" % subcode
                detail += ")"
            return detail
    return str(payload)[:500]


def _json_or_error(response: requests.Response) -> Dict[str, Any]:
    if response.status_code >= 400:
        raise FbError(
            "Graph API returned HTTP %s: %s"
            % (response.status_code, _error_message(response))
        )
    try:
        data = response.json()
    except ValueError:
        raise FbError("Graph API did not return JSON: %s" % (response.text or "")[:300])
    if isinstance(data, dict) and data.get("error"):
        raise FbError(_error_message(response))
    if not isinstance(data, dict):
        raise FbError("Unexpected Graph API response: %s" % str(data)[:300])
    return data


def test_token(settings: Dict[str, str]) -> Tuple[bool, str]:
    """GET /{page-id}?fields=name&access_token=..."""
    try:
        page_id, token = _credentials(settings)
    except FbError as exc:
        return False, str(exc)
    url = "%s/%s" % (graph_base(settings), page_id)
    try:
        response = requests.get(
            url,
            params={"fields": "name", "access_token": token},
            timeout=30,
        )
    except requests.RequestException as exc:
        return False, "Could not reach the Facebook Graph API: %s" % exc
    try:
        data = _json_or_error(response)
    except FbError as exc:
        return False, str(exc)
    name = data.get("name") or "(unnamed page)"
    return True, "Connected to Facebook Page: %s" % name


def _upload_photo(settings: Dict[str, str], page_id: str, token: str, path: str) -> str:
    url = "%s/%s/photos" % (graph_base(settings), page_id)
    try:
        with open(path, "rb") as handle:
            response = requests.post(
                url,
                data={"published": "false", "access_token": token},
                files={"source": handle},
                timeout=TIMEOUT,
            )
    except OSError as exc:
        raise FbError("Could not read image file %s: %s" % (path, exc))
    except requests.RequestException as exc:
        raise FbError("Image upload failed: %s" % exc)
    data = _json_or_error(response)
    media_id = data.get("id")
    if not media_id:
        raise FbError("Facebook did not return a photo id for %s." % path)
    return str(media_id)


def publish_post(
    settings: Dict[str, str], caption: str, image_paths: List[str]
) -> str:
    """Publish a caption with optional photos. Returns the new Facebook post id."""
    page_id, token = _credentials(settings)
    message = (caption or "").strip()
    if not message:
        raise FbError("The draft has an empty caption; nothing was published.")

    media_ids: List[str] = []
    for path in image_paths:
        try:
            media_ids.append(_upload_photo(settings, page_id, token, path))
        except FbError as exc:
            if "Could not read image file" in str(exc):
                log.warning("Skipping missing image %s", path)
                continue
            raise

    feed_url = "%s/%s/feed" % (graph_base(settings), page_id)
    data: List[Tuple[str, str]] = [
        ("message", message),
        ("access_token", token),
    ]
    for index, media_id in enumerate(media_ids):
        data.append(
            ("attached_media[%d]" % index, json.dumps({"media_fbid": media_id}))
        )
    try:
        response = requests.post(feed_url, data=data, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise FbError("Feed post request failed: %s" % exc)
    result = _json_or_error(response)
    post_id = result.get("id")
    if not post_id:
        raise FbError("Facebook did not return a post id: %s" % str(result)[:300])
    return str(post_id)
