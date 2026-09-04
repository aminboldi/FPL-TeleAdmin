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
- `/fixtures` posts the gameweek that still has football in it, not the one the API calls current. The FPL API keeps a gameweek `is_current` until the *next* one's deadline, so between the final whistle and that deadline the command was advertising matches already played. `_fixtures_gameweek()` moves on as soon as the current gameweek has no unfinished fixture left; a fixture unfinished for more than `_FIXTURE_IN_PROGRESS_HOURS` counts as postponed rather than pending, so a rescheduled match cannot hold a gameweek open for weeks.
- `/a https://...` (or `a/https://...`) extracts an arbitrary article in reader mode, translates it, publishes it to Telegraph, and places the channel post in the normal half-hour review queue.
- Forwarding a burst of Telegram posts to the private admin bot (within a quiet window) merges their text into one Telegraph article, preserving each forwarded post as a separate source paragraph (see *Forwarded post batches*). A single forwarded post continues through the normal direct-translation path.
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
- `VideoMetadata.duration_seconds` comes from the `contentDetails` part that was already being requested. It feeds the transcript completeness check, which runs **before** translation so a partial transcript costs one small call rather than a full translation.
- Before translation, the transcript is passed through an AI correction pass using the full FPL player glossary (`first_name`, `second_name`, `web_name`, aliases, and club) to normalize likely ASR name errors to canonical English names. This is the one caller that still needs the whole roster in a prompt — speech recognition mangles names before any of them can be detected — and it reads that roster from `player_names.glossary()`. Everything after it is the shared *Player name consistency* path, which resolves only the names the text actually contains. Keep both stages: correction improves recognition, while the shared path guarantees the Persian spelling.
- Every transcript uses the structured article translator. It reconstructs raw captions into paragraphs, inferred topic headings, and genuine lists; short inline posts convert that structure into Telegram-safe bold headings, spacing, and bullets, while long posts retain Telegraph HTML. Whether a video becomes a post or an article is decided after translation (see *Inline post vs Telegraph article*).
- **Both YouTube post formats open with the title.** The old `▶️ ویدئوی جدید کانال <channel>` first line was dropped: the thumbnail already shows whose video it is, and the line cost caption room that the transcript needed. `_format_youtube_inline_post()` and `_format_youtube_telegraph_post()` therefore take no channel argument; the channel name still reaches the catalog through `source_tag`.
- **Video titles are cleaned, not just translated.** A YouTube title carries fragments that exist for search and self-promotion — `FPL 2025/26`, `3x Top 10k`, `#1 in the World`, channel names, emoji — and none of them mean anything in the channel. `translator.VIDEO_TITLE_INSTRUCTIONS` is appended to the shared translation prompt for this one call, so the model strips them while translating and still gets the same terminology dictionary and player glossary — no second call, no duplicated prompt file. `youtube_posts.clean_video_title()` is the deterministic net underneath, for the mechanical leftovers that survive translation because they are digits and Latin abbreviations. It only drops a *whole* separator-delimited segment, so `FPL` inside a sentence and a gameweek the video is actually about are left alone, and a title made entirely of tags keeps its own words rather than becoming empty.

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

For article translations, `article_prompt.txt` asks the LLM for structured JSON output (`title`, `summary`, `body`, `removed_images`, `complete`, `incomplete_reason`). `translator.translate_article()` parses the JSON with fallback to regular translation + auto-generated title/summary.

### Never use `str.format()` on a prompt

Prompts are filled by `translator._render()`, which does literal `{name}` replacement. `str.format()` must not be used: `article_prompt.txt` contains a literal JSON example, and `{ "title": ... }` is parsed as a replacement field, so `ARTICLE_PROMPT.format()` raised `KeyError` on **every** call. The structured article path therefore always failed and silently fell back to plain translation — which had a 4096-token ceiling and truncated long articles. Any new prompt containing braces breaks the same way under `.format()`.

### Output-token ceilings and truncation

Persian is token-heavy relative to English, so translated output is much longer than the English source suggests. The ceilings in `translator.py` (`_MAX_TOKENS_*`) are deliberately generous; the previous 4096/8192 values silently cut long articles and transcripts mid-sentence.

`translator._response_text()` inspects `finish_reason` on every completion and raises `TruncatedResponseError` when it is `"length"`. This is the API's own report that output was cut off, so it is exact — prefer it over any heuristic. The summary call is the one deliberate exception: a clipped summary is cosmetic, so it tolerates the ceiling rather than failing the publish.

### Tone

All translation prompts ask for casual, conversational Persian — how an Iranian FPL fan talks to other fans — rather than formal written Persian. Tone never overrides the FPL terminology dictionary, player/club names, numbers, or prices. `transcript_format_prompt.txt` is explicitly told to preserve the casual register, because that pass runs after translation and would otherwise re-formalize the text.

## Player name consistency (`player_names.py`)

The database holds the Persian spelling the channel uses for each player. A model does not know those spellings — it transliterates from English every time, so the same player arrives as زولیس in one post and تزولیس in the next, and is sometimes left in English. `player_names.py` makes the stored spelling win everywhere: titles, summaries, translated posts, Telegraph articles, and catalog card summaries.

**Do not fix an individual player in a prompt.** A new name or a corrected transliteration is a database edit — `/players`, or the `alias` column — and takes effect on the next post. The `DCL`/`VVD`/`RDZ`/`MLS` lines still in `prompt.txt` predate this module; `RDZ` is a manager and has no player row, and the others can be retired once their alias is confirmed in the production database.

Three passes, in increasing order of cost:

1. **Prompt.** `prompt_glossary(source_text)` lists only the players that source actually names, with their Persian spelling, and is injected into `prompt.txt` and `article_prompt.txt` through the `{player_names}` placeholder. This is what stops the wrong transliteration from being generated at all, and it costs a few lines rather than a glossary of 570 players. It returns `""` when the text names nobody, so the prompt never carries an empty heading.
2. **Rewrite.** `enforce()` rewrites the result: an English name becomes its Persian spelling, and a recorded variant becomes the canonical one. It only ever touches visible text — HTML tags, attributes, and URLs are split out first.
3. **Learn.** When the source named a player but the translation contains no spelling of that name we know, a Persian word within a small edit distance is taken as a new variant, corrected, and written to that player's `alias` column, so pass 2 catches it from then on.

Guards that make pass 3 safe — weaken any one of these and it starts renaming ordinary words:

- Only players **detected in the source text** are considered, and only when no known spelling of the name is already present.
- `_variant_distance()` charges 1 for an inserted or dropped letter and for a swap **within** a confusable group (`زذضظ`, `چجشژ`, …) — the ones a transliterator actually confuses — and 2 for any other substitution, so an unrelated word cannot fit a budget of 1.
- Exactly one candidate word must match. Two equally close words means ambiguity, and nothing is changed.
- A word that is already some player's name is never re-pointed, and one misspelling cannot be claimed by two players in the same text.

Other invariants:

- A spelling that two players share is **dropped** when their Persian names differ (both `Davies`), and kept when they agree (both `Dasilva`). Never guess which player a bare surname means.
- An English match must start with a capital when the stored spelling does, so a lowercase `white` or `long` stays an ordinary English word.
- Comparison folds interchangeable Arabic-script letters (`ي`/`ی`, `ك`/`ک`, `آ`/`ا`) and the zero-width non-joiner, so one stored spelling matches every way it can be written.
- The index compiles a pattern over every stored spelling and takes ~0.4s, so it is cached for an hour and rebuilt through `player_names.reload()` **in a thread** — at startup, after an FPL refresh, and after a `/players` save. Never let it rebuild lazily on the event loop.
- Every entry point fails open. A database problem or a bad index costs name consistency, never the translation.

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
- **Every** translated output uses those player Farsi columns as the authoritative English-name → Persian-name mapping, not just YouTube transcripts (see *Player name consistency*). Missing values mean a player name cannot be deterministically translated, so keep these columns populated when refreshing the player database.
- Player community aliases in `players.alias` (populated by `generate_aliases.py`, by `/players`, and by learned Persian spellings). Comma-separated, and now holds two kinds of entry: English community abbreviations (`DCL`) and Persian misspellings the translator produced (`تزولیس`). Both are matched case- and script-insensitively, and feed the same replacement map.
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

Goal alerts are source-driven only. An API-polling live-goal watcher was tried
and removed: it could not reliably detect the scoring minute, so it failed to
match the source alert for deduplication and posted every goal twice, while not
being meaningfully faster than the source channel. Do not reintroduce it
without solving the minute/dedup problem first.

Player resolution for every match event is constrained to the two clubs in the
fixture (`alerts._resolve_player()` takes `strict_team_code`/
`allowed_team_codes`), preventing same-name players at unrelated clubs from
being selected.

## Price-change alerts

Source-channel price-change posts are ignored. The scheduler curates the report directly from the official FPL bootstrap payload, including confirmed changes and next-update projections.

`price_changes.is_price_change()` gates that exclusion and must stay tolerant of the source channel's formatting. It has posted both `Price Fallers! 📉 (3) #FPL` with `🔴 J.Timber #ARS £6.1m` rows and, later, `Price Fallers! 📉 #FPL` with `⬇ Madueke £6.3m` rows — no count, no team code. The old regex required the count, so the newer posts fell through to the LLM and were published as translated articles. Detection now accepts a leading header on its own, and elsewhere in a message requires at least two `£x.xm` rows so prose about risers and fallers is still translated normally. `parse_price_change()` is unused; only detection matters.

The official report lists every confirmed rise, and only confirmed/predicted falls for players above 1% ownership. Each riser/faller list is sorted by ownership descending, and rows are wrapped in `<blockquote>`.

### Confirmed changes are detected, not scheduled

Confirmed changes are found by **diffing official prices against a saved baseline**, not by posting at a fixed clock time. FPL applies price changes at roughly 01:30 UTC and the exact moment drifts, so a fixed-time post either fires before the change lands or misses it. Do not reintroduce a clock-triggered confirmed-price post.

- `livefpl.fetch_price_payload()` fetches the bootstrap **once**; the diff, the report, and the new baseline all come from that single payload. Re-fetching between those steps loses any change that lands in between.
- The baseline lives in `last_updated` under `price_prediction_snapshot` and advances **only after Telegram accepts the post**, so a failed send retries on the next pass and a change is never reported twice.
- `livefpl.load_price_snapshot()` returns `None` (never seeded) versus `{}` (seeded but empty) deliberately. With no baseline the scheduler seeds it silently and posts nothing — `cost_change_event` counts the whole gameweek, not the last day, so it is not a valid stand-in for "today's changes".
- Because it is a diff, changes that happen while the bot is down are reported on the next successful pass.
- The bootstrap is ~1.7MB, so the check runs on its own `_PRICE_POLL_INTERVAL` (5 min) rather than every 30s scheduler tick.
- `PRICE_PREDICTIONS_ENABLED` gates **only** the 23:30 prediction watchlist. Confirmed changes are always reported.

### Scheduler jobs are individually isolated

Every job in `run_scheduler`'s loop has its own `try`/`except`. They previously shared one block, so a failure in the lineup check silently suppressed every price report for the life of the process. Keep them isolated when adding jobs.

## Lineups

Source channels post lineups in English format (`LINE-UPS | #TOTEVE`). The bot parses and resolves each player to Farsi name + price/position, grouped by team with a separator.

Detection: `alerts.is_lineup()` checks for `LINE-UPS | #TEAMA_TEAMB` header.
Formatting: `alerts.format_lineup()` includes kickoff time (converted to Iran time, UTC+3:30). Each player row is wrapped in `<blockquote>`.

## Deadline automation

An event-driven loop in `deadlines.py` that posts a deadline-passed message at each gameweek's deadline time.

- **Deadline-passed post**: At deadline time, posts `deadline.jpg` with caption announcing the deadline passed, including the league invite link.

The FPL league code is stored in `LEAGUE_CODE` env var (default `433b70`). The full link is `https://fantasy.premierleague.com/leagues/auto-join/{code}`.

## LiveFPL API integration (`livefpl.py`)

The bot fetches post-match points/EO data from `livefpl.us` and price data from the official FPL API — **no Playwright needed**. The endpoints are:

- `https://livefpl.us/api/games.json` — per-game player points, EO%, stats, events. Each player entry: `[web_name, eo%, ?, points, [[stat_name, value, points], ...], element_id, name, pos_code]`. The `minutes` stat in `p[4]` determines who started.
- `https://fantasy.premierleague.com/api/bootstrap-static/` — official player prices, confirmed gameweek changes, and `price_change_percent` / `price_change_projections` predictions.

Key functions:
- `build_game_text(fixture)` — per-game player points with blockquote formatting, color circles, and starter/sub split
- `build_eo_text()` — global EO leaderboard (players with ≥10% EO, sorted descending)
- `build_price_changes_text()` — confirmed and predicted price risers/fallers from official FPL data
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
| Price predictions | 23:30 Iran time nightly | official FPL bootstrap via `livefpl.build_price_changes_text()` |
| Actual price changes | Whenever official prices differ from the saved baseline (checked every 5 min) | official FPL bootstrap diffed against `price_prediction_snapshot` |
| EO leaderboard | 75 minutes after each deadline | `livefpl.build_eo_text()` |
| Game points | When game status becomes "Done" in API (polled every 30s) | `livefpl.build_game_text()` |
| Deadline-passed | At deadline time | `deadlines.py` (unchanged) |

Deduplication uses the `last_updated` DB table (same as deadline posts).

Price predictions can be paused by setting `PRICE_PREDICTIONS_ENABLED=false` in `.env`. The scheduler loop still runs and confirmed price changes are still reported; only the 23:30 prediction watchlist is skipped.

## Translated post queue

All messages that go through LLM translation (forwarded source posts, articles, X imports, and YouTube imports) use the shared half-hour publishing queue in `post_queue.py`:

- Publishing slots run every 30 minutes from 08:30 through 00:30 Iran time.
- The next upcoming slot is treated as the current slot and skipped. For example, a post received at 13:12 starts at 14:00, then later posts take 14:30, 15:00, and so on.
- During the 00:30–08:00 blackout, queued posts start at 08:30.
- Existing Telegram scheduled messages are read before each allocation, so occupied slots remain respected after a bot restart.
- Slot allocation and sending share an async lock. The process also remembers the last reserved slot per target, ensuring successive queued posts advance by 30 minutes even if Telegram has not yet returned a just-created scheduled message; Telegram's scheduled list remains the restart-safe baseline.

Exceptions (sent immediately, no delay):
- Game-action alerts (`alerts.py`)
- Official price-change report (`livefpl.py`)
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
- Source-specific cleanup is now **structural only**: it selects the article container, extracts the feature image, and drops related-article cards and tag buttons. It must not try to find the end of the article.
- Promotional *text* removal is the translator's job (`article_prompt.txt`). The old scripted end-of-article detection — FFFix's promo-banner filename and offer wording, FFScout's final `wp-block-separator` and `READ MORE:` paragraphs, AllAboutFPL's `Further reads`/FFHUB blocks — was removed. Each keyed on exact site wording or separator structure, so when a site changed either one the heuristic silently truncated real content. Do not reintroduce it.
- Promotional *images* are the one thing the model cannot handle — it can delete the offer text around a promo-code banner but the picture stays — so they are removed before translation by `article_images.py` (see *Recurring promotional images*).
- A feature image is selected from the source social/header image, or the first article image, and sent with the Telegram post. It is removed from Telegraph so users do not see it twice. Remaining inline images are replaced with positional `[[TELEADMIN_IMAGE_N]]` markers and restored at those positions afterward. Whether the result is published as a Telegraph article or an ordinary post is decided after translation (see *Inline post vs Telegraph article*).
- **Deleted promo images must be reported.** `restore_images_in_place()` re-appends any marker the model dropped, so a promotional banner inside a block the translator deleted would reappear at the end of the article. The prompt therefore requires every deliberately-deleted placeholder to be listed in `removed_images`, and that set is passed to `restore_images_in_place(..., removed_images=...)`. Keep those two in sync when changing either side.
- Source hyperlinks are removed while retaining visible text, **except** cross-links that can be rerouted to our own translation of the linked article (see *Internal-link rerouting*). The original article URL is appended only at the end of the Telegraph article, never in the Telegram caption.

### 2. Long-text / merged-chunk articles (>940 chars)

When a single text message exceeds 940 source characters (`_ARTICLE_SOURCE_THRESHOLD`), `translator.translate_article()` is used for structured JSON output, then published to Telegraph.

Plain-text pasted articles have their line breaks converted to explicit `<br>`
tags before translation. The article prompt asks the model to preserve those
breaks, and the final Telegraph normalizer also restores newlines when a model
returns otherwise unstructured prose.

## Telegraph articles

Long-form content (>940 source chars) and merged text chunks are published as Telegraph articles via `articles.publish_to_telegraph()`.

- `bot.py:_format_telegraph_post()` produces the Telegram post layout: `✍ مقاله جدید <source>` header, title, AI-generated summary, and `👈👈متن کامل فارسی مقاله👉👉` linked to the Telegraph URL. URL article posts include their feature image as Telegram media.
- `translator.translate_article()` uses `article_prompt.txt` for structured JSON output (`title`/`summary`/`body`), falling back to regular translation if JSON parsing fails
- Set `TELEGRAPH_ACCESS_TOKEN` env var to keep articles under a single Telegraph account; without it a new account is created on every restart

### Instant View depends on the page structure

Telegram renders a `telegra.ph` link through its built-in Instant View template, which is written against the markup Telegraph's own editor produces. A page that deviates from it is opened in the in-app browser instead: slow to load, in Telegraph's light theme, ignoring the reader's font. Both deviations below were live on published pages — six of twelve had the first, two of twelve the second — and `articles.normalize_telegraph_structure()` now removes them at the one boundary every page crosses (`publish_to_telegraph()` and `edit_telegraph_page()` both call it).

- **No `<br>` as a direct child of the article.** Telegram text arrives with its line breaks converted to `<br>` by `_prepare_plain_article_layout()`; the paragraph-wrapping loop then turns the text between the breaks into separate `<p>` elements but leaves the `<br>` tags themselves at the root. They carry nothing at that point and are dropped. A `<br>` *inside* a paragraph is a real line break and is kept.
- **Every `<img>` is wrapped in `<figure>`.** `restore_images_in_place()` inserts bare `<img>` elements; Telegraph's editor never does. Images already inside a figure are left alone.

When changing the published HTML, check a real page rather than the stored content: `getPage` returns what we sent, while the rendered page at `https://telegra.ph/<path>` is what Telegram parses. Comparing the root-level children of `<article>` against a page written in Telegraph's editor is what surfaced both of these.

- `articles.publish_to_telegraph()` indexes each new page with its AI summary, source tag, original source URL, and feature-image URL. The feature image is intentionally removed from the Telegraph body when it is sent separately with the Telegram post; the catalog must use the stored URL rather than expecting an image inside Telegraph HTML.
- YouTube article pages use the fetched YouTube thumbnail URL. URL-imported articles use the selected source/header image URL. No local image copy is required for catalog cards; images are loaded from their public HTTPS URLs. Articles without a recoverable image render as text-only cards.
- `_enrich_article_catalog()` backfills older indexed pages: it generates an AI summary from the full Telegraph content, recovers the original source link when present, derives YouTube thumbnails, and attempts to refetch source-article images. It runs once per start and works through the **whole** backlog in batches (`_CATALOG_ENRICHMENT_BATCH`, capped by `_CATALOG_ENRICHMENT_LIMIT`), not just the first page of it.
- Recovery is impossible when an old page contains neither an image nor an original source link, so `pages_needing_enrichment()` takes never-attempted pages first and then the oldest attempt (`ORDER BY enriched_at, published_at DESC`), and `_enrich_catalog_page()` stamps `enriched_at` in a `finally` whether or not anything was recovered. That is what makes the sweep terminate and lets the next start continue past the unfixable remainder. `enriched_at` is a separate column on purpose: `sync_from_telegraph()` rewrites `updated_at` for every page it imports, which would keep resetting the backlog position.
- `article_catalog.first_source_url()` recovers that source URL from an already-published page. It must never return the catalogue footer link that every page ends with, nor a `telegra.ph`/`graph.org` link — since internal-link rerouting, those can be our own articles. It prefers the anchor labelled `منبع اصلی`. Values stored by the earlier version, which returned the footer, are cleared once by the `catalog_footer_source_cleared` migration.
- `article_monitor.py` polls Premier League and Fantasy Football Fix listing pages plus the Fantasy Football Scout and AllAboutFPL RSS feeds every 15 minutes. It seeds the current backlog on first startup, then sends only newly discovered URLs through `_publish_article_from_url()` and the normal half-hour review queue. Seen URLs and retry state live in `runtime_config.db`; `/set ARTICLE_MONITOR_ENABLED false` pauses polling.

## Internal-link rerouting

Source articles link mostly to their own earlier articles, and a large share of those already have a published Persian page. Rather than dropping such a hyperlink with the rest, its destination is swapped for our own Telegraph article: the reference survives and the reader stays in Persian, inside the channel's catalogue. A link with no published translation is still removed, visible text intact.

- `article_catalog.source_key()` is the matching identity. It ignores `www`, trailing slashes, tracking parameters and AMP suffixes, keys Premier League URLs on the numeric article ID (the slug is editable after publication), and keys YouTube URLs on the video ID. A URL that identifies no specific article — a homepage, a section index — returns an empty key and can never match anything.
- `article_catalog.resolve_source_links()` only offers pages with `hidden=0`. A hidden row is a draft, an import that was never posted, or an article still waiting in the review queue, and linking to any of those would announce it early. This is also why `source_url` matters enough to be part of `pages_needing_enrichment()`: a page that does not know which article it translated can never be linked to.
- `articles.reroute_internal_links()` rewrites matching anchors in place and returns the Telegraph URLs it produced. Anchors it did not rewrite are dropped by the caller, exactly as before. It fails open — a catalog error means no rerouting, never a failed publish.
- **Rerouted links survive translation, and nothing else does.** The allowlist is captured before the model runs (`articles.article_link_targets()`, or the second return value of `articles.reroute_html_links()`) and enforced afterwards by `articles.sanitize_article_links()`, which unwraps every anchor whose href is not in it. A model that mangles, moves, invents or copies a URL therefore costs us a link; it can never publish a wrong one. Every path that publishes model output passes its allowlist — the YouTube transcript paths pass none, because a transcript has no links to keep.
- `article_prompt.txt` tells the model to preserve `<a>` tags and their hrefs exactly. That instruction and the allowlist are two halves of one mechanism: relaxing the prompt without enforcing an allowlist would publish raw source links.
- Reader-mode extraction runs with `include_links=True` purely so `_telegraph_safe_article_html()` can see the anchors; that function still drops every one that was not rerouted.

## Inline post vs Telegraph article

Not everything needs a Telegraph page. A translation short enough to fit a caption, with no inline image to place, is published as an ordinary Telegram post carrying its feature image.

- **The decision is made on the finished Persian text**, in `_publish_article_from_url()` and `_import_youtube_transcript()`. It used to be made on the English source (`_SHORT_ARTICLE_SOURCE_LIMIT = 700` chars of article, 800 of transcript), which is why it almost never fired: sources are far longer than that, and the source length predicts neither the translated length nor the caption's. The YouTube path also translated twice when its guess was wrong.
- **The caption is the only limit.** There is no separate cap on the body: the assembled caption either fits `_MEDIA_CAPTION_LIMIT` (1024, Telegram's own) or the article goes to Telegraph. Nothing is ever shortened to make it fit — not the title, not the body — and a short post carries no summary. With a typical title the caption's fixed parts (title, source link, AI signature) cost ~89 units, so the body ceiling lands around 935 characters; a longer title lowers it.
- `_caption_length()` measures in **UTF-16 code units**, which is how Telegram counts. Every caption ends with an emoji-bearing signature, so counting characters would under-report and let a caption through that Telegram then rejects.
- **Only when there are no inline images.** A Telegram post can carry one picture, so an article with images in its body belongs on Telegraph. Images the translator deliberately deleted (`removed_images`) do not count — a promo banner it removed must not force a page.
- `articles.strip_image_markers()` clears any `[[TELEADMIN_IMAGE_N]]` the model left behind. The Telegraph path resolves markers in `restore_images_in_place()`; the caption path has no equivalent, and a marker in a caption would be shown to readers.

## Recurring promotional images

Sources repeat the same promo-code banner or house advert on every post. Matching them by filename or wording is the approach that already failed (see above). `article_images.py` separates them by **how often an image is reused**: one belonging to an article appears in that article and almost nowhere else, while a banner appears in article after article.

- Every image URL a source hands us is recorded against the article it came from, in `article_image_sightings` in `runtime_config.db`. Seen in `_MIN_ARTICLES` (3) **distinct articles**, it stops being treated as content.
- Distinct *articles*, not sightings: the article key is `article_catalog.source_key()`, so a retry or a re-import cannot convict an article's own images. Recording stops at `_MAX_SIGHTINGS` (25), once the verdict can no longer change.
- `article_images.image_key()` drops the query string, because the same banner is served with different resize and cache-busting parameters from one article to the next.
- `articles._drop_recurring_images()` applies the verdict in `fetch_article()` — the single choke point for both extractors — removing them from `images`, from the PL `parts`, and from the article HTML before translation.
- **A recurring feature image is only swapped when there is something to swap to.** Publishing requires a feature image; a banner is better than a failed article. A site that serves one default `og:image` for every post therefore keeps working.
- The first two articles carrying a new banner still show it. That is inherent to a frequency test and self-corrects; do not add a wording or filename rule to cover the gap.

## Forwarded post batches

An admin forwards a run of Telegram posts to the private bot and they become one Telegraph article. `_queue_forwarded_submission()` collects them, `_forwarded_batch_posts()` turns the raw messages into posts, and `_translate_forwarded_article()` publishes.

- **A quiet window is the only thing holding the batch together.** Every arrival pushes a shared deadline (`_forward_batch_deadlines`) rather than cancelling and recreating the timer. The old cancel-and-recreate could kill a task that had already started publishing, which lost the article *and* left the admin's dashboard call waiting on a future nobody would ever resolve. The task is also removed from `_forward_batch_tasks` before publishing, so a post arriving mid-publish opens a new batch instead of joining one already taken.
- The window is `_FORWARDED_BATCH_TIMEOUT`, or `_FORWARDED_BATCH_MEDIA_TIMEOUT` once any message in the batch carries media. Both are longer than the original 3s: a post arriving behind its siblings — in practice one carrying a photo — used to miss the batch entirely and be published on its own.
- **An album is one post, not several.** Telegram sends one message per album item with the caption on whichever item carries it, so `_forwarded_batch_posts()` collapses a `grouped_id` group into a single `_ForwardedPost`, taking the caption from whichever member has it and the first image as that post's image.
- **The first picture in the chain becomes the article's feature image; the rest are dropped.** Every post's text counts either way — a post is a member of the batch whether or not it came with a picture. `_is_photo_message()` deliberately accepts photos and image documents but not video, so a forwarded clip contributes its caption without becoming the feature image.
- The image is sent as the Telegram post's media only; the Telegraph body stays text. If the caption would exceed `_MEDIA_CAPTION_LIMIT`, the post goes out without the image rather than failing — Telegram rejects an over-long caption on a media message.
- `translate_article(..., merged_posts=True)` is passed when the batch has more than one text post. Without it the model reads the concatenation as one article and titles and summarizes it after the opening post; the instructions tell it the input is a series of separate posts and that the title and summary must cover all of them. `summary_prompt.txt` carries the matching rule, because `_finish_article()` regenerates the summary from the translated body and would otherwise discard the article prompt's version.

## Incomplete-content gate

Partial content is never published. Two independent checks exist:

- **Our output was cut off** — `TruncatedResponseError`, from `finish_reason == "length"`. Exact, not a guess.
- **The source itself was partial** — `ContentIncompleteError`, from `translator.assess_completeness()` or the `complete` field of the article JSON. Used for a feed that served a truncated article, or a transcript provider that returned only the opening minutes.

`assess_completeness()` is given the character count and, for videos, the duration plus the pre-computed expected range (roughly 750–1100 chars of speech per minute for these talk-heavy channels) — models are unreliable at doing that arithmetic themselves. A clear sign-off outweighs the length heuristic, since it proves the ending was captured. The check fails **open**: an unavailable or unparseable assessment returns `complete: True`, so this gate can never become a new reason for content to vanish silently.

Retry then give up, reporting once:

- Articles: `runtime_config.finish_article_monitor_candidate(..., max_attempts=4)` returns `True` when the attempt budget is spent and moves the URL to status `abandoned`, which `claim_article_monitor_candidate()` never re-claims. `bot._notify_article_abandoned()` then sends one private admin message.
- YouTube: `bot._notify_youtube_monitor_failure()` counts attempts in memory, retries across polls, then marks the video `skipped` and notifies admins once.

Only the automated feeds are gated (`_publish_article_from_url` and the YouTube transcript path). Source-channel Telegram posts and admin submissions are not — a forwarded post is complete by definition, and gating it would block legitimate content.

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
