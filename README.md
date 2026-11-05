# FB Auto Poster

A self-hosted Python web tool for a Facebook Page: store products (images + details),
let an OpenAI-compatible AI write post drafts, review/approve them, and publish the
approved drafts to your Page on a schedule.

Runs on Python 3.8 (Linux aarch64 friendly, no GPU), Flask + the standard-library
`sqlite3` module, plain HTML/CSS/JS in the browser.

## Features

- **Settings** – Facebook Page ID, Page Access Token, Graph API version, AI base URL /
  key / model, generate + publish intervals, caption language and emoji toggle. Both the
  AI endpoint and the Facebook token have a "Test" button.
- **Products** – create/edit/delete products with name, description, optional price and
  1–5 images (stored in `uploads/products/<id>/`). Editable sort order decides the
  generation cycle.
- **AI drafts** – a background loop generates one draft per interval for the next product
  that has no open draft, cycling through the catalogue and wrapping around at the end.
  Drafts are never posted automatically.
- **Review** – edit captions, approve, reject/delete, or force-regenerate drafts.
  Approved drafts form a FIFO publishing queue.
- **Publishing** – a second loop publishes the oldest approved draft: with images it
  creates unpublished photo containers and a multi-photo feed post, without images a
  plain text feed post. Success stores `fb_post_id` + `posted_at`; failure sets
  `status='failed'` with the Graph API error and offers a **Retry** button.
- **Dashboard** – countdowns to the next generation/publish run, counts per status,
  recent posts, and manual trigger buttons for both loops.

## Setup

```bash
cd fb-auto-poster
python3.8 -m pip install --user -r requirements.txt
./start.sh
```

Then open <http://127.0.0.1:8069> (or `http://<host-ip>:8069` on your network).
The app binds `0.0.0.0:8069`; data lives in `data.db` in this folder, logs in `app.log`.

`start.sh` simply runs `python3.8 run.py`. Keep it running under a process manager or
`tmux`/`screen` if you want the loops to keep working after you log out.

## Getting a Facebook Page Access Token

1. Create a Facebook App at <https://developers.facebook.com/apps> (type: Business).
2. Add the **Facebook Login for Business** or **Messenger/Pages** product so the Pages
   permissions are available, and make sure your app can request:
   `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`.
3. Open **Graph API Explorer** (<https://developers.facebook.com/tools/explorer>):
   - select your app,
   - click **Generate Access Token** and approve the three permissions above for the
     Page you want to post to,
   - optionally call `GET /me/accounts` and copy the `access_token` of the Page — that is
     the **Page Access Token** (not your personal user token).
4. Exchange it for a long-lived token (Page tokens obtained from a long-lived user token
   do not expire; short-lived ones last about an hour):

   ```
   GET https://graph.facebook.com/v21.0/oauth/access_token
       ?grant_type=fb_exchange_token
       &client_id=<APP_ID>
       &client_secret=<APP_SECRET>
       &fb_exchange_token=<SHORT_LIVED_USER_TOKEN>
   ```

   Then call `GET /me/accounts` again with the long-lived user token and copy the Page
   token. Put that in **Settings → Page Access Token**.
5. Find the numeric **Page ID**: it is on the Page's *About → Page transparency* panel,
   or from `GET /me/accounts` (`id` field).
6. In **Settings**, click **Test Facebook connection** — it calls
   `GET /{page-id}?fields=name&access_token=...` and shows the Page name on success.

The Graph API version defaults to `v21.0` and can be changed in Settings.

## AI endpoint

Any OpenAI-compatible Chat Completions server works:

- Base URL, e.g. `https://api.openai.com/v1` (the app appends `/chat/completions`;
  a URL that already ends with `/chat/completions` is used as-is),
- API key (sent as `Authorization: Bearer ...`),
- model name, e.g. `gpt-4o-mini`.

## How the loops work

One daemon thread starts with the Flask app and checks every 20 seconds whether a run is
due (`last run + interval`). Each run records its result in the settings table, which the
dashboard shows. Manual "Generate draft now" / "Publish next now" buttons run the same
code paths immediately; they reset the corresponding timer.

Interval and loop logic live in `scheduler.py`; change a setting and the next run picks it
up without restarting.

## Files

| File | Purpose |
| --- | --- |
| `run.py` | entry point, serves on `0.0.0.0:8069` |
| `app.py` | Flask app factory, routes, logging |
| `models.py` | SQLite schema + queries, settings, products, posts |
| `fb_api.py` | Graph API token test and photo/feed publishing |
| `ai_client.py` | OpenAI-compatible chat completion + caption prompt |
| `scheduler.py` | generation and publishing loops |
| `templates/` | `base`, `dashboard`, `products`, `product_form`, `drafts`, `settings` |
| `static/style.css` | UI styling |
| `start.sh` | `python3.8 run.py` |
| `requirements.txt` | `flask`, `requests` |

## Notes / limitations

- Uploads are limited to 40 MB per request and images to jpg/png/webp/gif.
- Drafts reference the product's images at generation time; deleting a product removes
  its image files, so older drafts will publish text-only if their files are gone.
- Single-user tool with no login: run it on your local network, not the public internet.
