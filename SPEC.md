# FB Auto Poster — Product Spec

A self-hosted Python web tool that: stores products (images + details), uses AI to generate
Facebook page posts per product, lets the user review/approve drafts, and publishes approved
drafts to the user's Facebook Page on a schedule.

## Stack (HARD CONSTRAINTS)
- Python 3.8 compatible syntax ONLY (`from __future__ import annotations`; no `dict | None`,
  no walrus, no `X | Y` unions). It will be started with `python3.8`.
- Flask + Flask-SQLAlchemy (or raw sqlite3 with stdlib) for backend. Keep dependencies minimal:
  flask, requests. Use `python3.8 -m pip install --user` compatible versions.
- No external JS frameworks — plain HTML/CSS/JS served by Flask templates.
- Target environment: Linux aarch64, no GPU. Local network use.

## Features

### 1. Settings page
- Fields: Facebook Page ID, Facebook Page Access Token, Graph API version (default v21.0),
  AI API Base URL, AI API Key, AI model name.
- Stored in DB (Settings table, key/value). "Test connection" button for both AI endpoint
  and FB token (GET /{page-id}?fields=name&access_token=...).

### 2. Products
- Add/edit/delete products: name, description (product details), price (optional),
  1..5 images (upload via file input, stored under `uploads/products/<id>/`).
- Products are ordered by index (sort_order column, editable).

### 3. Draft generation (AI)
- A background generation loop runs every N minutes (configurable via Settings key
  `generate_interval_minutes`, editable from UI). When it fires it takes the NEXT product
  that has no draft yet (by sort_order, looping back after last product so generation cycles),
  and asks the AI (OpenAI-compatible chat completions endpoint at AI Base URL) to write a
  Facebook post: engaging caption in Bangla or as configured language + optional emoji,
  based on name/description/price. Which images to use: all product images (selected via UI
  later).
- Generated drafts are NOT posted automatically. They are saved as Post rows with
  status='draft', the product id, generated caption, and the product's images copied/referenced.
- The generation loop runs as a background thread started with the Flask app.

### 4. Review / approval (Drafts page)
- List of drafts with status: draft | approved | published | failed.
- User can: edit caption, approve, reject (delete), or force-regenerate a draft.
- Approved drafts work as a first-in-first-out queue.

### 5. Facebook publishing (scheduler)
- Publisher loop: every `publish_interval_minutes` (Settings key, UI-editable), take the OLDEST
  approved draft and publish it:
  - If the draft has images: multi-photo post via Graph API — create unpublished photo
    containers (`POST /{page-id}/photos?published=false`) for each image, then
    `POST /{page-id}/feed` with `message` + `attached_media[0]={"media_fbid":...}` ...
  - If no images: plain text feed post.
- On success: status='published', store fb_post_id and posted_at. On failure: status='failed',
  store error text, so the queue is not silently stuck; a "Retry" button is provided.
- Loop thread with a simple check every 20s of whether the interval has elapsed (next_run_at).

### 6. Dashboard
- Shows: next generation in X min, next publish in X min, counts by status, recent posts.

## Technical notes
- All loops = one daemon thread running loops with try/except and logging to app.log.
- Expose a manual trigger button for both loops (for testing).
- DB: sqlite file `data.db` in project root.
- Port: 8069, bind 0.0.0.0.
- A `README.md` with setup + FB token instructions.
- Start script: `start.sh` running `python3.8 run.py`.

## Deliverable
- run.py, app.py (factory), models.py, fb_api.py (Graph API calls), ai_client.py,
  scheduler.py (loops), templates/ (base, dashboard, products, product_form, drafts, settings),
  static/style.css, requirements.txt, README.md, start.sh.
