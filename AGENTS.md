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
- Keep `.env` out of git. The session file is committed as a convenience, but `TELETHON_SESSION_STRING` takes priority.
- The env var is `OPEN_ROUTER_API_KEY` (with underscore between OPEN and ROUTER). The old specs.md uses `OPENROUTER_API_KEY` (no underscore) — that's wrong.
- `TELEGRAPH_ACCESS_TOKEN` (optional): set this to keep all Telegraph articles under a single account. Without it, a new account is created on every bot restart.
- `PRICE_PREDICTIONS_ENABLED` (optional, default `true`): set to `false` to pause the nightly price prediction scheduler post (useful during FPL off-season).
- `LEAGUE_CODE` (optional, default `433b70`): FPL league code for the invite link in deadline posts.
- The Python venv lives at `teleadmin_project/.venv/` (not repo root). If missing, create with `python3 -m venv teleadmin_project/.venv`.

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

- `python database.py` rebuilds from FPL API JSON (expects files at `/tmp/fpl_bootstrap.json` and `/tmp/fpl_fixtures.json`)
- Player name quirks: `search_name` column stores ASCII-normalized `second_name` (strips diacritics). Use this for lookups, not raw `second_name`.
- Team Farsi names live in `teams.name_fa` / `teams.short_name_fa`
- Player Farsi names in `players.first_name_fa`, `second_name_fa`, `web_name_fa` (populated by `translate_names.py`)
- Player community aliases in `players.alias` (populated by `generate_aliases.py`)
- Country flags stored in `players.flag` — resolved from `regions.json` at DB import time via `database._region_to_flag()`

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

Divider between team sections: `➖ ➖ ➖` (`_DIVIDER` in livefpl.py).

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

## Translated post delay

All messages that go through LLM translation (forwarded from source channels) are **scheduled 10 minutes ahead** via Telethon's `schedule` parameter (`SCHEDULE_DELAY_MINUTES = 10`). This gives admins time to review before publication.

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

### 1. Inline article handling (premierleague.com URLs)

When a source message contains a Premier League article URL (`premierleague.com/en/news/...`, short link `preml.ge/...`, or `t.co/...`), `_maybe_post_article()` runs **after** the main message translation pipeline. It fetches the article page, extracts content, translates the full HTML, and publishes to Telegraph.

- `articles.is_pl_article_url()` detects Premier League article URLs
- `articles.fetch_article()` uses BeautifulSoup to extract title, publish date, summary, and body paragraphs from the `.article__content` div
- Widgets stripped: `.articleWidget`, `.embeddable-article`, `.article-related-content`, `.media-actions`, `.article__share-container`
- The translated HTML is published to Telegraph; the Telegram post links to it
- **Bug**: `bot.py:_maybe_post_article()` calls `articles.fetch_article(url)` twice (lines 603 and 606) — the second call overwrites the first result. Remove the first call if fixing.

### 2. Long-text / merged-chunk articles (>350 chars)

When a single text message or merged chunks exceed 350 source characters (`_ARTICLE_SOURCE_THRESHOLD`), `translator.translate_article()` is used for structured JSON output, then published to Telegraph.

## Telegraph articles

Long-form content (>350 source chars) and merged text chunks are published as Telegraph articles via `articles.publish_to_telegraph()`.

- `bot.py:_format_telegraph_post()` produces the Telegram post layout: `✍ مقاله:` header, title, divider, summary, and `متن کامل مقاله: 👇👇👇` linked to the Telegraph URL
- `translator.translate_article()` uses `article_prompt.txt` for structured JSON output (`title`/`summary`/`body`), falling back to regular translation if JSON parsing fails
- Set `TELEGRAPH_ACCESS_TOKEN` env var to keep articles under a single Telegraph account; without it a new account is created on every restart

## Text chunk merging

Telegram splits long messages into chunks for non-premium accounts. `bot.py` buffers sent text messages from the same chat for 3 seconds (`_CHUNK_TIMEOUT`), merges them, then processes as a single message.

**Important**: Merged chunks ALWAYS go through `translate_article()` → Telegraph (no length threshold). The 350-char `_ARTICLE_SOURCE_THRESHOLD` only applies to single messages, not chunks.

## Rich formatting preservation

`_message_to_html()` converts Telegram message entities to HTML before translation. Currently handles:
- `MessageEntityBlockquote` → `<blockquote>`
- `MessageEntityTextUrl` → `<a href="...">`
- All other formatting (bold, italic, etc.) is stripped — unnecessary for the LLM

Post-processing: `_strip_quotes()` removes 11 Unicode quote variants, `_fix_unclosed_tags()` ensures blockquotes are properly closed.
`_strip_html_tags()` strips all HTML to measure raw text length for the article threshold.

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
