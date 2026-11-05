"""OpenAI-compatible chat completions client used for draft generation."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import requests

REQUEST_TIMEOUT = 90


class AIError(Exception):
    """Raised when the configured AI endpoint cannot produce a caption."""


def _first_setting(settings: Dict[str, str], key: str, default: str = "") -> str:
    value = settings.get(key)
    if value is None:
        return default
    return str(value).strip()


def chat_endpoint(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise AIError("AI Base URL is not configured. Set it on the Settings page.")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _headers(settings: Dict[str, str]) -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = _first_setting(settings, "ai_api_key")
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    return headers


def _error_detail(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return (response.text or "").strip()[:500] or "HTTP %s" % response.status_code
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return str(message)
        if isinstance(error, str):
            return error
        if payload.get("message"):
            return str(payload["message"])
    return str(payload)[:500]


def _chat(settings: Dict[str, str], messages: List[Dict[str, str]], max_tokens: int) -> str:
    model = _first_setting(settings, "ai_model")
    if not model:
        raise AIError("AI model name is not configured. Set it on the Settings page.")
    url = chat_endpoint(_first_setting(settings, "ai_base_url"))
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.8,
        "max_tokens": max_tokens,
    }
    try:
        response = requests.post(
            url, json=payload, headers=_headers(settings), timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        raise AIError("Could not reach the AI endpoint: %s" % exc)
    if response.status_code >= 400:
        raise AIError(
            "AI endpoint returned HTTP %s: %s"
            % (response.status_code, _error_detail(response))
        )
    try:
        data = response.json()
    except ValueError:
        raise AIError("AI endpoint did not return JSON.")
    try:
        choices = data["choices"]
        message = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        try:
            message = data["choices"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise AIError("Unexpected AI response: %s" % str(data)[:300])
    text = (message or "").strip()
    if not text:
        raise AIError("The AI returned an empty caption.")
    return text


def build_messages(settings: Dict[str, str], product: Dict[str, Any]) -> List[Dict[str, str]]:
    language = _first_setting(settings, "post_language", "Bangla") or "Bangla"
    use_emoji = _first_setting(settings, "use_emoji", "1") == "1"

    price = str(product.get("price") or "").strip()
    description = str(product.get("description") or "").strip()
    name = str(product.get("name") or "").strip()

    materials = ["Product name: %s" % name]
    if description:
        materials.append("Product details: %s" % description)
    materials.append("Price: %s" % (price if price else "not specified"))

    default_system = (
        "You are an expert social media copywriter who writes Facebook page posts "
        "that sell products. Reply with the caption only: no titles, no quotes, no "
        "markdown, no explanations."
    )
    # A custom system prompt (Settings) replaces the built-in instructions.
    custom_system = _first_setting(settings, "custom_system_prompt")
    system = custom_system if custom_system else default_system
    rules = [
        "Write the caption in %s." % language,
        "Make it engaging and friendly, with a clear call to action (message us to order).",
        "Keep it between 2 and 5 short sentences or lines.",
        "Do not invent prices, discounts, guarantees or specifications that are not given.",
        "Do not use hashtags unless they read naturally; at most 3 short hashtags at the end.",
    ]
    if use_emoji:
        rules.append("Use a few tasteful emoji that fit the product.")
    else:
        rules.append("Do not use any emoji.")

    user = "\n".join(materials + ["", "Requirements:", *["- " + rule for rule in rules]])
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def generate_caption(settings: Dict[str, str], product: Dict[str, Any]) -> str:
    messages = build_messages(settings, product)
    return _chat(settings, messages, max_tokens=700)


def test_connection(settings: Dict[str, str]) -> Tuple[bool, str]:
    """Small round-trip used by the Settings page test button."""
    try:
        reply = _chat(
            settings,
            [{"role": "user", "content": "Reply with the single word OK."}],
            max_tokens=10,
        )
    except AIError as exc:
        return False, str(exc)
    model = _first_setting(settings, "ai_model")
    return True, "AI endpoint responded using model %s (%s)." % (model, reply[:80])
