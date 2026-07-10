# AGENTS.md — TeleAdmin

## Run command

```bash
cd teleadmin_project && python bot.py
```

Must run from `teleadmin_project/` (not repo root) because the Telethon session file path is relative.

## Parse mode: HTML only

Telethon supports `"md"`, `"markdown"`, `"html"`, `"htm"` — but NOT `"md2"` or `"markdownv2"`. The repo uses `parse_mode="html"` everywhere (`bot.py:142,161,182`).

Telethon's legacy markdown parser does NOT consume `\` as an escape character; it passes backslashes through literally. Do not reintroduce markdown escaping or markdown parse mode.

HTML formatting conventions used in the code:
- Bold: `<b>text</b>`
- Links: `<a href="url">text</a>`
- Text content is escaped via `_escape_html()` (handles `&`, `<`, `>`)

## Media must preserve file extension

When downloading and re-uploading media, the temp file must include the original extension from `event.message.file.ext` (`bot.py:106-109`). Without it, Telethon falls back to sending as a generic document attachment instead of an inline photo/video.

## Config and session layout

- `.env` lives at repo root. `config.py` loads it from `Path(__file__).parent.parent / ".env"`.
- Telethon session is resolved at `bot.py:31-36`: if `TELETHON_SESSION_STRING` env var is set, a `StringSession` is used (cloud deployment). Otherwise it falls back to the local file `teleadmin_project/translation_session.session`.
- First run locally prompts for phone number + verification code. After login, run `python export_session.py` to export the session as a string for cloud deployment.
- Keep `.env` out of git. The session file is committed as a convenience, but `TELETHON_SESSION_STRING` takes priority.
- The env var is `OPEN_ROUTER_API_KEY` (with underscore between OPEN and ROUTER). The old specs.md uses `OPENROUTER_API_KEY` (no underscore) — that's wrong.

## Deployment

- Deployed on a Coolify-managed VPS. The Procfile at root (`web: cd teleadmin_project && python bot.py`) is used as the start command.
- **Health server**: `bot.py:248-261` runs a minimal async HTTP server on `PORT` env var. This enables uptime monitoring on any platform.
- **StringSession**: Export with `python export_session.py`, add as `TELETHON_SESSION_STRING` env var. The committed `.session` file won't work reliably across deployment machines.
- **Root `requirements.txt`**: points to `teleadmin_project/requirements.txt`. Required for buildpack-based deployment that runs `pip install -r requirements.txt`.

## URL extraction from messages

URLs come from two sources (`bot.py:81-90`):
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

## Git push

Git remote is HTTPS (`https://github.com/aminboldi/TeleAdmin.git`). SSH was tested and failed — do not switch to SSH URLs. `gh auth` is configured and uses HTTPS.

## No test/lint/typecheck infrastructure

There are no tests, no CI, no pre-commit hooks, and no typechecker config. Verification is manual (run bot, check Telegram channels).

## FPL database (`fpl.db`)

SQLite database at `teleadmin_project/fpl.db`. Schema and query helpers in `database.py`. Populated from FPL API at `https://fantasy.premierleague.com/api/`.

- `python database.py` rebuilds from FPL API JSON (expects files at `/tmp/fpl_bootstrap.json` and `/tmp/fpl_fixtures.json`)
- Player name quirks: `search_name` column stores ASCII-normalized `second_name` (strips diacritics). Use this for lookups, not raw `second_name`.
- Team Farsi names live in `teams.name_fa` / `teams.short_name_fa`
- Player Farsi names in `players.first_name_fa`, `second_name_fa`, `web_name_fa` (populated by `translate_names.py`)
- Player community aliases in `players.alias` (populated by `generate_aliases.py`)

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
Formatting: `price_changes.format_price_changes_farsi()` outputs Farsi format with day-of-week header, risers section, fallers section, and `@EPL_Fantasy` signature.

## Lineups

Source channels post lineups in English format (`LINE-UPS | #TOTEVE`). The bot parses and resolves each player to Farsi name + price/position, grouped by team with a separator.

Detection: `alerts.is_lineup()` checks for `LINE-UPS | #TEAMA_TEAMB` header.
Formatting: `alerts.format_lineup()` includes kickoff time (converted to Iran time, UTC+3:30).

## Deadline automation

Two automated operations in `deadlines.py`, running as a background loop every 60s:

1. **Schedule reminder**: 2 hours after the last game of the previous GW finishes, schedules a Telegram message for the upcoming GW's deadline saying `شنبه ساعت 14:30، دللاین هفته 39`.

2. **Deadline-passed post**: At deadline time, posts `deadline.jpg` with caption announcing the deadline passed, including the league invite link.

The FPL league code is stored in `LEAGUE_CODE` env var (default `433b70`). The full link is `https://fantasy.premierleague.com/leagues/auto-join/{code}`.

### Number formatting (all automated posts)

All numbers in automated posts use English digits and are wrapped in `<b>` tags. Prices show one decimal + position letter (e.g., `<b>6.5M</b>`).

### Iran timezone

UTC+3:30 year-round (Iran does not observe DST). All times from the FPL API (GMT/UTC) are converted to Iran time.
