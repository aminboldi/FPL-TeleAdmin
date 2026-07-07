# AGENTS.md — TeleAdmin

## Run command

```bash
cd teleadmin_project && python bot.py
```

Must run from `teleadmin_project/` (not repo root) because the Telethon session file path is relative.

## Parse mode: HTML only

Telethon supports `"md"`, `"markdown"`, `"html"`, `"htm"` — but NOT `"md2"` or `"markdownv2"`. The repo uses `parse_mode="html"` everywhere (`bot.py:159,168,139`).

Telethon's legacy markdown parser does NOT consume `\` as an escape character; it passes backslashes through literally. Do not reintroduce markdown escaping or markdown parse mode.

HTML formatting conventions used in the code:
- Bold: `<b>text</b>`
- Links: `<a href="url">text</a>`
- Text content is escaped via `_escape_html()` (handles `&`, `<`, `>`)

## Media must preserve file extension

When downloading and re-uploading media, the temp file must include the original extension from `event.message.file.ext` (`bot.py:104-106`). Without it, Telethon falls back to sending as a generic document attachment instead of an inline photo/video.

## Config and session layout

- `.env` lives at repo root. `config.py` loads it from `Path(__file__).parent.parent / ".env"`.
- Telethon session is resolved at `bot.py:30-34`: if `TELETHON_SESSION_STRING` env var is set, a `StringSession` is used (cloud deployment). Otherwise it falls back to the local file `teleadmin_project/translation_session.session`.
- First run locally prompts for phone number + verification code. After login, run `python export_session.py` to export the session as a string for cloud deployment.
- Keep `.env` out of git. The session file is committed as a convenience for Render, but `TELETHON_SESSION_STRING` takes priority.

## Render deployment

- **Procfile at root**: `web: cd teleadmin_project && python bot.py` — must say `web:` (not `worker:`) because Render's free tier only includes Web Services. Background Workers start at $7/month.
- **Health server**: `bot.py:216-227` runs a minimal async HTTP server on `PORT` env var. This satisfies Render's web service requirement and enables uptime monitoring.
- **Free tier sleep**: Web Services spin down after 15 min of inactivity. Use an external uptime pinger (e.g. UptimeRobot free tier) to hit the `onrender.com` URL every 5 min.
- **StringSession**: Export with `python export_session.py`, add as `TELETHON_SESSION_STRING` env var on Render. The committed `.session` file won't work reliably across deployment machines.
- **Dual `requirements.txt`**: `requirements.txt` at root points to `teleadmin_project/requirements.txt` so Render's Python buildpack detects the project. Render installs via `pip install -r requirements.txt` (default build command).

## URL extraction from messages

URLs come from two sources (`bot.py:79-88`):
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
