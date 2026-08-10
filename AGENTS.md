# AGENTS.md — TeleAdmin

## Run command

```bash
cd teleadmin_project && python bot.py
```

Must run from `teleadmin_project/` (not repo root) because the Telethon session file path is relative.

## Parse mode: HTML only

Telethon supports `"md"`, `"markdown"`, `"html"`, `"htm"` — but NOT `"md2"` or `"markdownv2"`. The repo uses `parse_mode="html"` everywhere.

Telethon's legacy markdown parser does NOT consume `\` as an escape character; it passes backslashes through literally. Do not reintroduce markdown escaping or markdown parse mode.

HTML formatting conventions used in the code:
- Bold: `<b>text</b>`
- Links: `<a href="url">text</a>`
- Text content is escaped via `_escape_html()` (handles `&`, `<`, `>`)

## Media must preserve file extension

When downloading and re-uploading media, the temp file must include the original extension from `event.message.file.ext` (`bot.py:_media_suffix()`). Without it, Telethon falls back to sending as a generic document attachment instead of an inline photo/video.

## Config and session layout

- `.env` lives at repo root. `config.py` loads it from `Path(__file__).parent.parent / ".env"`.
- Telethon session: if `TELETHON_SESSION_STRING` env var is set, a `StringSession` is used (cloud deployment). Otherwise it falls back to the local file `teleadmin_project/translation_session.session`.
- First run locally prompts for phone number + verification code. After login, run `python export_session.py` to export the session as a string for cloud deployment.
- **Never use the same Telethon user session concurrently from local development and the cloud VPS.** Telegram invalidates it with `AuthKeyDuplicatedError`. For deployment, create a fresh local login, export it, set `TELETHON_SESSION_STRING` only in Coolify, then do not run that same session locally while the VPS is running.
- Keep `.env` out of git. The session file is committed as a convenience, but `TELETHON_SESSION_STRING` takes priority.
- The env var is `OPEN_ROUTER_API_KEY` (with underscore between OPEN and ROUTER). The old specs.md uses `OPENROUTER_API_KEY` (no underscore) — that's wrong.
- `TELEGRAPH_ACCESS_TOKEN` (optional): set this to keep all Telegraph articles under a single account. Without it, a new account is created on every bot restart.
- `TELEGRAPH_EDITOR_BASE_URL` (optional): public HTTPS base URL of the deployed app. `/edit` uses it for short-lived editor links; on Coolify, `COOLIFY_URL`/`COOLIFY_FQDN` is detected automatically.
- `PRICE_PREDICTIONS_ENABLED` (optional, default `true`): set to `false` to pause the nightly price prediction scheduler post (useful during FPL off-season).
- `LEAGUE_CODE` (optional, default `433b70`): FPL league code for the invite link in deadline posts.
- Secrets and identities stay in `.env` / Coolify environment variables: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELETHON_SESSION_STRING`, `TELEGRAM_BOT_TOKEN`, `ADMIN_USER_IDS`, `OPEN_ROUTER_API_KEY`, `TELEGRAPH_ACCESS_TOKEN`, `YOUTUBE_API_KEY`, `X_BEARER_TOKEN`, and `X_RAPIDAPI_KEY`.

## Runtime configuration and admin dashboard

Operational settings are dashboard-editable and persist in `runtime_config.db`, not `.env`:

- `OPEN_ROUTER_MODEL`, source/target/notification channels, `PRICE_PREDICTIONS_ENABLED`, `ARTICLE_MONITOR_ENABLED`, `EPL_LEAGUE_CODE`, `EPL_LEAGUE_ID`, `IRAN_LEAGUE_ID`
- The database has an audit log and every setting change requires a Telegram confirmation.
- `runtime_config.db` is ignored by git. In Coolify it must live in persistent storage: mount a volume at `/app/teleadmin_project/data` and set `RUNTIME_CONFIG_PATH=/app/teleadmin_project/data/runtime_config.db`.
- The FPL database automatically uses a sibling `fpl.db` in that persistent directory when `RUNTIME_CONFIG_PATH` is set. `FPL_DATABASE_PATH` can override it explicitly. The first connection copies the bundled database into the persistent location, preserving the existing Persian names and aliases before future FPL refreshes.
- Without `RUNTIME_CONFIG_PATH`, the bot still runs using a non-persistent DB beside the code; settings reset after a redeploy.
- `article_catalog.py` stores the Telegraph article index in the same database. The public root URL serves searchable article cards; new Telegraph pages are indexed automatically and existing account pages are imported on first catalog visit. Keep `runtime_config.db` on the persistent Coolify volume so the catalog survives redeploys.
- The public catalog is served at the app root (`/`), not `/articles`. `telegraph_editor.py` also serves the repository-root assets `/logo.webp` and `/fav-icon.png`; both files must remain in the repository for the branded header and favicon to work in deployment.
- The BotFather dashboard is enabled only if both `TELEGRAM_BOT_TOKEN` and numeric comma-separated `ADMIN_USER_IDS` are set. It accepts private-chat commands only.
- Main dashboard commands: `/dashboard`, `/guide`, `/channels`, `/target`, `/source`, `/set`, `/league`, `/activity`, `/balance`, `/fixtures`, `/points`, `/eo`, `/prices`, `/lineups`, `/x`, `/y`, `/a`, `/articles`, `/edit`.
- `/a https://...` (or `a/https://...`) extracts an arbitrary article in reader mode, translates it, publishes it to Telegraph, and places the channel post in the normal half-hour review queue.
- `/edit` lists the ten most recently published pages under the shared Telegraph account. After an admin selects one, it sends a private editor link that must be opened within 15 minutes; an opened editor remains valid for two hours or until a successful save. The editor loads and saves the existing page through Telegraph's `getPage`/`editPage` APIs, so the page URL stays unchanged and the raw `TELEGRAPH_ACCESS_TOKEN` never reaches the browser. The link is a temporary capability and must stay in the private admin chat.
- `/players` is a password-protected web table using the same `TELEGRAPH_EDITOR_PASSWORD`. English FPL names are read-only; Persian first, second, and display names are written directly to the live FPL database. Use the production URL for edits so they are immediately shared with the bot.
- Dashboard-generated content always requires an explicit publish confirmation. Lineups are source-driven and publish automatically when detected.

## X post import

- Admins can submit either `/x https://x.com/.../status/...`, `x/https://...`, or `X/https://...`.
- Captions are translated through OpenRouter; hashtags are removed before translation. A canonical `<a>` link labelled `لینک منبع` is appended before the AI signature.
- Prefer `X_RAPIDAPI_KEY` for the subscribed `x-com2` RapidAPI service. `x_posts.py` uses its `TweetDetail/?tweetId=...` endpoint, which supports canonical and `x.com/i/status/...` share URLs, media, and self-authored thread posts. The official `X_BEARER_TOKEN` route is only a fallback.
- RapidAPI may omit media for some X post formats. Never attach media belonging to replies or unrelated posts just because it appears elsewhere in the API response.
- The Python venv lives at `teleadmin_project/.venv/` (not repo root). If missing, create with `python3 -m venv teleadmin_project/.venv`.

## YouTube import

- Admins can submit `/y https://youtube.com/watch?v=...` or `y/https://youtu.be/...`.
- `YOUTUBE_API_KEY` is used only for public video metadata and channel monitoring. The official YouTube captions API cannot download transcripts for arbitrary external videos; it requires OAuth permission to edit the video.
- English transcripts use a quota-aware RapidAPI chain with `X_RAPIDAPI_KEY`, in this order: `youtube-captions-transcript-subtitles-video-combiner`, `youtube-transcripts`, `youtube-transcript3`, `youtube-2-transcript`, and `youtube-transcripts-playlists-channels-search1`. Provider HTTP/auth/quota failures are persisted in `youtube_transcript_provider_health` and skipped until the next UTC calendar month; a valid no-captions response is treated as video-specific and allows the next provider. `transcriptapi` is not subscribed and is excluded.
- The two caption providers are intentionally first because they retrieve YouTube subtitle tracks when available; their normalized SRT/segment output is preferred over generated speech recognition. The additional RapidAPI speech-recognition endpoints require an uploaded `audio_file`, so they are not usable as direct YouTube fallbacks without a separate audio-download/encoding pipeline.
- If every transcript provider is exhausted for an automatically monitored upload, the admin bot sends a one-time private failure notification and leaves the video unseen so a later poll can retry it.
- Before translation, the transcript is passed through an AI correction pass using the full FPL player glossary (`first_name`, `second_name`, `web_name`, aliases, and club) to normalize likely ASR name errors to canonical English names. After translation and final transcript formatting, the visible text is deterministically mapped to `first_name_fa`/`second_name_fa` or `web_name_fa`; HTML tags and attributes (including URLs) are never changed. Keep both stages: correction improves recognition, while the final mapping guarantees Persian player names.
- Every transcript uses the structured article translator. It reconstructs raw captions into paragraphs, inferred topic headings, and genuine lists; short inline posts convert that structure into Telegram-safe bold headings, spacing, and bullets, while long posts retain Telegraph HTML.

## Deployment

- Deployed on a Coolify-managed VPS. The Procfile at root (`web: cd teleadmin_project && python bot.py`) is used as the start command.
- **Health server**: `bot.py:_start_health_server()` runs a minimal async HTTP server on `PORT` env var. This enables uptime monitoring on any platform.
- **StringSession**: Export with `python export_session.py`, add as `TELETHON_SESSION_STRING` env var. The committed `.session` file won't work reliably across deployment machines.
- **Root `requirements.txt`**: points to `teleadmin_project/requirements.txt`. Required for buildpack-based deployment that runs `pip install -r requirements.txt`.

## URL extraction from messages

URLs come from two sources (`bot.py:_extract_urls()`):
1. Raw text regex: `(?:https?://|t\.me/)\S+`
2. Message entities: `MessageEntityTextUrl` (link text + hidden URL) — accessed via `getattr(entity, "url", None)`

Both must be checked to catch all link types.

## OpenRouter API health check

```bash
curl -s https://openrouter.ai/api/v1/auth/key \
  -H "Authorization: Bearer $OPEN_ROUTER_API_KEY"
```

Returns `limit`, `usage`, `is_free_tier`, and `limit_remaining` fields.

## Translation prompt

The LLM prompt lives in `teleadmin_project/prompt.txt` (not hardcoded). `{text}` placeholder is replaced at runtime. The model and fallback model are configured in `.env` (`OPEN_ROUTER_MODEL`) and `config.py` (`fallback_model`) respectively.

For article translations, `article_prompt.txt` asks the LLM for structured JSON output (`title`, `summary`, `body`). `translator.translate_article()` parses the JSON with fallback to regular translation + auto-generated title/summary.

## Git push

Git remote is HTTPS (`https://github.com/aminboldi/FPL-TeleAdmin.git`). SSH was tested and failed — do not switch to SSH URLs. `gh auth` is configured and uses HTTPS.

## No test/lint/typecheck infrastructure

There are no tests, no CI, no pre-commit hooks, and no typechecker config. Verification is manual (run bot, check Telegram channels).

## FPL database (`fpl.db`)

SQLite database at `teleadmin_project/fpl.db`. Schema and query helpers in `database.py`. Populated from FPL API at `https://fantasy.premierleague.com/api/`.

In production, when `RUNTIME_CONFIG_PATH` points to the persistent Coolify volume, the effective path is the sibling `fpl.db` there rather than the code checkout. This is what makes `/players` edits survive redeploys and keeps the web editor and bot on the same database.

- Player name quirks: `search_name` column stores ASCII-normalized `second_name` (strips diacritics). Use this for lookups, not raw `second_name`.
- Team Farsi names live in `teams.name_fa` / `teams.short_name_fa`
- Player Farsi names in `players.first_name_fa`, `second_name_fa`, `web_name_fa` (populated by `translate_names.py`)
- YouTube transcript imports use those player Farsi columns as the authoritative final English-name → Persian-name mapping. Missing values mean a player name cannot be deterministically translated, so keep these columns populated when refreshing the player database.
- Player community aliases in `players.alias` (populated by `generate_aliases.py`)
- Country flags stored in `players.flag` — resolved from `regions.json` at DB import time via `database._region_to_flag()`

### DB rebuild procedure

To refresh the DB with the latest FPL API data while preserving manual edits:

```bash
# 1. Fetch fresh data
curl -s "https://fantasy.premierleague.com/api/bootstrap-static/" -o /tmp/fpl_bootstrap.json
curl -s "https://fantasy.premierleague.com/api/fixtures/" -o /tmp/fpl_fixtures.json

# 2. Rebuild (backup_manual_data → import → restore_manual_data)
python database.py

# 3. Re-populate auto-generated columns (only fills NULLs — won't overwrite manual edits)
python translate_teams.py
python translate_names.py
python generate_aliases.py
```

`database.py` automatically backs up `alias`, `*_fa` player columns and `*_fa` team columns to `manual_data.json` before import, then restores them after. Manual data survives normal `python database.py` reruns.

`generate_aliases.py` adapts to fresh seasons (where `total_points = 0` for all players) by processing all players instead of only active ones.

## Game-action alerts

Source channels post live FPL game events in English format (`sample-alerts.txt`). The bot detects these and formats them in Farsi without LLM translation.

Detection: `alerts.is_game_alert()` checks for action lines (Goal/Assist/Red card/...) + score line.
Parsing: `alerts.parse()` extracts actions, teams, minute, scores.
Formatting: `alerts.format_farsi()` looks up player names/prices in the DB and outputs Farsi format.

Alerts are posted immediately (not scheduled for review) since they're time-sensitive live events.

## Price-change alerts

Source channels post price changes in two separate messages (one for risers, one for fallers) in English format (`price-change.txt`). The bot buffers them and merges into a single Farsi post.

Detection: `price_changes.is_price_change()` checks for "Price Risers!" or "Price Fallers!" headers.
Parsing: `price_changes.parse_price_change()` extracts player name, team code, and new price.
Buffering: `price_changes.accumulate()` collects risers + fallers using today's date as key. Posts immediately when both arrive, or after a 120s timeout with whatever was received.
Formatting: `price_changes.format_price_changes_farsi()` outputs Farsi format with day-of-week header, risers section, fallers section, and `@EPL_Fantasy` signature. Each player row is wrapped in `<blockquote>`.

## Lineups

Source channels post lineups in English format (`LINE-UPS | #TOTEVE`). The bot parses and resolves each player to Farsi name + price/position, grouped by team with a separator.

Detection: `alerts.is_lineup()` checks for `LINE-UPS | #TEAMA_TEAMB` header.
Formatting: `alerts.format_lineup()` includes kickoff time (converted to Iran time, UTC+3:30). Each player row is wrapped in `<blockquote>`.

## Deadline automation

An event-driven loop in `deadlines.py` that posts a deadline-passed message at each gameweek's deadline time.

- **Deadline-passed post**: At deadline time, posts `deadline.jpg` with caption announcing the deadline passed, including the league invite link.

The FPL league code is stored in `LEAGUE_CODE` env var (default `433b70`). The full link is `https://fantasy.premierleague.com/leagues/auto-join/{code}`.

## LiveFPL API integration (`livefpl.py`)

The bot fetches data from `livefpl.us` APIs — **no Playwright needed**. Two API endpoints:

- `https://livefpl.us/api/games.json` — per-game player points, EO%, stats, events. Each player entry: `[web_name, eo%, ?, points, [[stat_name, value, points], ...], element_id, name, pos_code]`. The `minutes` stat in `p[4]` determines who started.
- `https://livefpl.us/api/prices.json` — player price change predictions. Key fields: `name`, `team`, `type`, `cost`, `progress` (decimal where 1.0 = 100%), `progress_tonight`

Key functions:
- `build_game_text(fixture)` — per-game player points with blockquote formatting, color circles, and starter/sub split
- `build_eo_text()` — global EO leaderboard (players with ≥10% EO, sorted descending)
- `build_price_changes_text()` — predicted price risers/fallers for tonight
- `get_finished_fixtures(gameweek_id)` — DB query for finished fixtures

Player matching uses `search_name` (ASCII-normalized) + `alias` + `web_name` against the DB — same as alerts.

### Blockquote formatting for game points

Telegram limits blockquotes to ~25 per message. Game points use this layout:

- **Top 11 players per team by minutes** → individual `<blockquote>` rows, sorted by **EO descending**
- **Remaining players (subs)** → grouped into a single `<blockquote>`, sorted by EO descending
- Within the starters group, high-EO (≥10%) players appear first in **bold**, then low-EO players

### Color circles (points indicator)

Per-player emoji prefix in game points:

| Points | Circle |
|---|---|
| 5+ | 🟢 |
| 3-4 | ⚪ |
| 0-2 | 🟡 |
| Negative | 🔴 |

### Stat emojis

Helper: `_build_stat_emojis()` in livefpl.py. Emojis:

| Stat | Emoji |
|---|---|
| goals_scored | ⚽ |
| assists | 🅰️ |
| clean_sheets | 🚫 |
| yellow_cards | 🔸 |
| red_cards | ♦️ |
| own_goals | 🅾 |
| defensive_contribution | ✅ |
| penalty_saved | 📛 |
| penalty_missed | ❌ |

Divider between team sections: `ـ ـ ـ` using RTL Arabic tatweel (`_DIVIDER` in `livefpl.py`), so it follows Persian text direction in Telegram.

**Known issue**: The EO leaderboard heading is hardcoded to "GW38" in `livefpl.py:303` (`build_eo_text()`). It doesn't reflect the actual gameweek. Fix by reading the current GW from the DB.

### HTML escaping

`_esc(text)` exists in `livefpl.py`, `price_changes.py`, and `alerts.py` — each has its own copy. Always use before interpolating untrusted text into HTML.

## Scheduler (`scheduler.py`)

Runs alongside the bot in `asyncio.gather()`. Automated posts:

| Post | Trigger | Source |
|---|---|---|
| Price predictions | 23:30 Iran time nightly (or 30min after last live game ends) | `livefpl.build_price_changes_text()` |
| EO leaderboard | 75 minutes after each deadline | `livefpl.build_eo_text()` |
| Game points | When game status becomes "Done" in API (polled every 60s) | `livefpl.build_game_text()` |
| Deadline-passed | At deadline time | `deadlines.py` (unchanged) |

Deduplication uses the `last_updated` DB table (same as deadline posts).

Price predictions can be paused by setting `PRICE_PREDICTIONS_ENABLED=false` in `.env`. The scheduler loop still runs, but `_check_price_post()` is skipped.

## Translated post queue

All messages that go through LLM translation (forwarded source posts, articles, X imports, and YouTube imports) use the shared half-hour publishing queue in `post_queue.py`:

- Publishing slots run every 30 minutes from 08:30 through 00:30 Iran time.
- The next upcoming slot is treated as the current slot and skipped. For example, a post received at 13:12 starts at 14:00, then later posts take 14:30, 15:00, and so on.
- During the 00:30–08:00 blackout, queued posts start at 08:30.
- Existing Telegram scheduled messages are read before each allocation, so occupied slots remain respected after a bot restart.
- Slot allocation and sending share an async lock. The process also remembers the last reserved slot per target, ensuring successive queued posts advance by 30 minutes even if Telegram has not yet returned a just-created scheduled message; Telegram's scheduled list remains the restart-safe baseline.

Exceptions (sent immediately, no delay):
- Game-action alerts (`alerts.py`)
- Price-change alerts from source channels (`price_changes.py`)
- Lineups (`alerts.py`)
- All scheduler/automated posts

## Translated post signature

Translated posts append `@EPL_Fantasy | ✨AI` (`AI_SIGNATURE`). Automated posts (alerts, price changes, deadlines, scheduler) use plain `@EPL_Fantasy` (`SIGNATURE`).

Telegraph article posts now also include the AI signature in `_format_telegraph_post()`.

## Number formatting (all automated posts)

All numbers in automated posts use English digits and are wrapped in `<b>` tags. Prices show one decimal + position letter (e.g., `<b>6.5M</b>`).

## Iran timezone

UTC+3:30 year-round (Iran does not observe DST). All times from the FPL API (GMT/UTC) are converted to Iran time.

## Reply chain preservation

If a source post is a reply to another source post, the target post replies to the corresponding translated post. The `message_map` table stores source→target message ID pairs. `_get_reply_to()` resolves the target reply ID, `_save_mapping()` records it after each post.

## Article translation

The bot has two separate article processing pipelines:

### 1. URL article handling

When a source message contains a URL, `_maybe_post_article()` runs **after** the main message translation pipeline. It fetches readable content, translates the full HTML, and publishes to Telegraph. The dashboard exposes the same pipeline through `/a`.

- `articles.is_pl_article_url()` selects the site-specific Premier League extractor for `premierleague.com/en/news/...`, short `preml.ge/...`, and `t.co/...` links.
- `articles.fetch_article()` uses BeautifulSoup for Premier League pages and Trafilatura reader mode for other article-like pages. General pages need at least 500 readable characters and are skipped if inaccessible or likely paywalled.
- Source-specific cleanup removes known promotional blocks and article footers before translation. PL cards/widgets, FFFix Premium blocks, FFScout `READ MORE`/trailing content, and AllAboutFPL `Further Read`/FFHUB/footer blocks are excluded.
- A feature image is selected from the source social/header image, or the first article image, and sent with the Telegram post. It is removed from Telegraph so users do not see it twice. Remaining inline images are replaced with positional `[[TELEADMIN_IMAGE_N]]` markers and restored at those positions afterward.
- Source hyperlinks are removed while retaining visible text. The original article URL is appended only at the end of the Telegraph article, never in the Telegram caption.

### 2. Long-text / merged-chunk articles (>940 chars)

When a single text message exceeds 940 source characters (`_ARTICLE_SOURCE_THRESHOLD`), `translator.translate_article()` is used for structured JSON output, then published to Telegraph.

## Telegraph articles

Long-form content (>940 source chars) and merged text chunks are published as Telegraph articles via `articles.publish_to_telegraph()`.

- `bot.py:_format_telegraph_post()` produces the Telegram post layout: `✍ مقاله جدید <source>` header, title, AI-generated summary, and `👈👈متن کامل فارسی مقاله👉👉` linked to the Telegraph URL. URL article posts include their feature image as Telegram media.
- `translator.translate_article()` uses `article_prompt.txt` for structured JSON output (`title`/`summary`/`body`), falling back to regular translation if JSON parsing fails
- Set `TELEGRAPH_ACCESS_TOKEN` env var to keep articles under a single Telegraph account; without it a new account is created on every restart

- `articles.publish_to_telegraph()` indexes each new page with its AI summary, source tag, original source URL, and feature-image URL. The feature image is intentionally removed from the Telegraph body when it is sent separately with the Telegram post; the catalog must use the stored URL rather than expecting an image inside Telegraph HTML.
- YouTube article pages use the fetched YouTube thumbnail URL. URL-imported articles use the selected source/header image URL. No local image copy is required for catalog cards; images are loaded from their public HTTPS URLs. Articles without a recoverable image render as text-only cards.
- `_enrich_article_catalog()` backfills older imported pages: it generates an AI summary from the full Telegraph content, recovers the original source link when present, derives YouTube thumbnails, and attempts to refetch source-article images. Recovery is impossible when an old page contains neither an image nor an original source link.
- `article_monitor.py` polls Premier League and Fantasy Football Fix listing pages plus the Fantasy Football Scout and AllAboutFPL RSS feeds every 15 minutes. It seeds the current backlog on first startup, then sends only newly discovered URLs through `_publish_article_from_url()` and the normal half-hour review queue. Seen URLs and retry state live in `runtime_config.db`; `/set ARTICLE_MONITOR_ENABLED false` pauses polling.

## Public article catalog

- The catalog is the root-domain landing page served by `telegraph_editor.py`; it is read-only and links each card to the Telegraph page. `/articles` is only the private dashboard command that sends admins the public catalog URL.
- Current public branding: header background `#310b34`, logo `/logo.webp`, favicon `/fav-icon.png`, title `مقالات فوتبال فانتزی لیگ برتر انگلیس FPL`, and subtitle `آرشیو مقالات منتشر شده در کانال تلگرامی @EPL_Fantasy`. The `FPL` title text links to `https://fantasy.premierleague.com/`, and the `@EPL_Fantasy` subtitle text links to `https://t.me/EPL_Fantasy`.
- The search button and catalog hyperlinks use `#02eefe`.
- Feature images use responsive natural dimensions (`width: 100%; height: auto`) with no forced crop, so card heights may differ. Preserve this behavior unless a deliberate design change is requested.

## Text chunk merging

Telegram splits long messages into chunks for non-premium accounts. `bot.py` buffers sent text messages from the same chat for 3 seconds (`_CHUNK_TIMEOUT`), merges them, then processes as a single message.

**Important**: Merged chunks ALWAYS go through `translate_article()` → Telegraph (no length threshold). The 940-char `_ARTICLE_SOURCE_THRESHOLD` only applies to single messages, not chunks.

## Rich formatting preservation

`_message_to_html()` converts Telegram message entities to HTML before translation. Currently handles:
- `MessageEntityBlockquote` → `<blockquote>`
- `MessageEntityTextUrl` → `<a href="...">`
- All other formatting (bold, italic, etc.) is stripped — unnecessary for the LLM

Post-processing: `_strip_quotes()` removes 11 Unicode quote variants, `_fix_unclosed_tags()` ensures blockquotes are properly closed.
`_strip_html_tags()` strips all HTML to measure raw text length for the article threshold.

## Notification system

When `NOTIF_CHANNEL_ID` is set, every translated post generates a preview notification (first 300 chars, truncated with `...`). Includes source channel name and media/text label. Sent immediately after the translated post.

## Media album handling

Telegram groups multiple media files into albums. `bot.py` buffers album messages for 5 seconds (`_ALBUM_TIMEOUT`), processes them together through a single translation cycle, then forwards as a single album post to the target channel.

## Utility/test scripts

All run from `teleadmin_project/`:

| Script | Purpose |
|---|---|
| `send_livefpl.py` | Manually post game points/EOLB/price predictions (CLI args: `--all` or GW number) |
| `send_test_deadline.py` | Send a test deadline-passed post (GW39) with `deadline.jpg` |
| `send_test_reminder.py` | **Broken** — imports `deadline_scheduled_text` from `deadlines.py` which doesn't define that function |
| `export_session.py` | Export the local `.session` file as a `TELETHON_SESSION_STRING` for cloud deployment |
| `generate_aliases.py` | Populate `players.alias` column — community nicknames for player matching |
| `translate_names.py` | Populate `players.*_fa` columns — Farsi name translations |
| `translate_teams.py` | Populate `teams.*_fa` columns — Farsi team name translations |
| `database.py` (standalone) | Rebuild `fpl.db` from `/tmp/fpl_bootstrap.json` and `/tmp/fpl_fixtures.json` |
