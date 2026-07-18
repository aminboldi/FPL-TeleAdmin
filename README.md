# TeleAdmin

Telegram channel translation and forwarding bot. Listens to source channels, translates messages from English to Persian (Farsi) via OpenRouter LLM API, and schedules translated posts to a target channel with a configurable delay for human review.

## Features

- **Multi-source listening** — supports up to 2 source channels
- **AI translation** — English to Persian via OpenRouter (Gemini Flash Lite, configurable)
- **Media forwarding** — photos, videos, and documents forwarded with translated captions
- **Scheduled posting** — posts land in the target channel's scheduled queue for admin review before publication
- **Admin notifications** — optional notification channel gets a preview of every scheduled post
- **Number formatting** — Persian/Arabic digits normalized to English and rendered in bold
- **Hashtag cleanup** — inline `#` stripped, hashtag-only lines removed
- **Link preservation** — URLs extracted from text, displayed as clickable "لینک" after translation
- **Custom signature** — configurable `@EPL_Fantasy` appended to every post
- **Editable prompt** — translation prompt lives in a standalone text file for easy tuning
- **Resilient** — handles FloodWaitError, translation failures, and connection drops gracefully

## Tech Stack

| Component       | Technology                          |
|-----------------|-------------------------------------|
| Language        | Python 3.12+                        |
| Telegram client | Telethon (MTProto, not Bot API)     |
| Translation     | OpenRouter API (OpenAI-compatible)  |
| Config          | python-dotenv + .env file           |
| Model           | google/gemini-2.5-flash-lite        |

## Project Structure

```
TeleAdmin/
├── .env                          # Environment variables (secrets, not committed)
├── .gitignore
├── specs.md                      # Original project specification
├── README.md
└── teleadmin_project/
    ├── bot.py                    # Main bot: event handlers, forwarding, notifications
    ├── config.py                 # Settings dataclass, env var loading
    ├── translator.py             # OpenRouter translation client
    ├── prompt.txt                # Editable LLM translation prompt
    └── requirements.txt          # Python dependencies
```

## Setup

### Prerequisites

- Python 3.12+
- Telegram API credentials (App api_id and api_hash from https://my.telegram.org)
- OpenRouter API key (https://openrouter.ai/keys)

### 1. Clone and install

```bash
git clone https://github.com/aminboldi/FPL-TeleAdmin.git
cd TeleAdmin
python3 -m venv teleadmin_project/.venv
source teleadmin_project/.venv/bin/activate
pip install -r teleadmin_project/requirements.txt
```

### 2. Configure environment

Copy and fill in `.env` at the project root:

```env
# Required
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=your_api_hash_here
OPEN_ROUTER_API_KEY=sk-or-v1-...
SOURCE_CHANNEL_ID=@sourcechannel
TARGET_CHANNEL_ID=@targetchannel

# Optional
SOURCE_CHANNEL2_ID=@second_source
NOTIF_CHANNEL_ID=@admin_notifications
OPEN_ROUTER_MODEL=google/gemini-2.5-flash-lite
```

### 3. First run (authenticates with Telegram)

```bash
cd teleadmin_project
python bot.py
```

On first run, you'll be prompted for your phone number and verification code. A `translation_session.session` file is created — subsequent runs skip login.

### 4. Keep it running

The bot is a long-lived process. Run it with a process manager (systemd, supervisor) or on a cloud worker (Render, Northflank).

## Configuration Reference

| Variable             | Required | Description                                    |
|----------------------|----------|------------------------------------------------|
| `TELEGRAM_API_ID`    | Yes      | Telegram app API ID (integer)                  |
| `TELEGRAM_API_HASH`  | Yes      | Telegram app API hash                          |
| `OPEN_ROUTER_API_KEY`| Yes      | OpenRouter API key                             |
| `SOURCE_CHANNEL_ID`  | Yes      | Primary source channel (e.g. `@channelname`)   |
| `TARGET_CHANNEL_ID`  | Yes      | Target channel for translated posts            |
| `SOURCE_CHANNEL2_ID` | No       | Secondary source channel                       |
| `NOTIF_CHANNEL_ID`   | No       | Channel for admin scheduling notifications     |
| `OPEN_ROUTER_MODEL`  | No       | LLM model (default: gemini-2.5-flash-lite)     |
| `TELEGRAPH_ACCESS_TOKEN`  | No   | Telegraph API token for unified article account |
| `PRICE_PREDICTIONS_ENABLED` | No  | Set to `false` to pause nightly price predictions |
| `LEAGUE_CODE`         | No       | FPL league code (default: 433b70)              |

## How It Works

```
Source Channel ──▶ [Telethon listener] ──▶ Strip hashtags
                                                │
                                      ┌─ URL found? ─▶ Extract link
                                      │                    │
                                      ▼                    ▼
                              Translate via LLM      Skip translation
                                      │                    │
                                      ▼                    ▼
                              Format numbers          Build link
                              Bold digits             caption
                                      │                    │
                                      └────────┬───────────┘
                                               ▼
                                     Build caption + signature
                                               │
                                    ┌──────────┼──────────┐
                                    ▼          ▼          ▼
                              Schedule post  Send media  Notify admin
                              (target ch)    with caption (notif ch)
```

1. Telethon user client listens for `NewMessage` events on source channels
2. Message text is cleaned (hashtags stripped, URLs extracted)
3. Text is sent to OpenRouter LLM for English→Persian translation
4. Numbers are normalized (Persian→English digits) and wrapped in `<b>` tags
5. Signature and optional link are appended
6. Post is scheduled to target channel (10 min delay by default)
7. If configured, a notification preview is sent to the notif channel

### Translation Prompt

Edit `teleadmin_project/prompt.txt` to tune translation quality. The `{text}` placeholder is replaced with the message to translate.

## Development

- **Parse mode**: Uses Telegram HTML (`parse_mode="html"`) for reliable formatting across all clients
- **Session persistence**: `.session` file stores Telegram auth — keep it out of git
- **Media handling**: Downloads to temp file with original extension, then re-uploads with `send_file()` to preserve media type detection
- **Flood control**: `FloodWaitError` is caught and respected with automatic sleep/retry
- **Translation fallback**: Primary model failure triggers automatic fallback to `google/gemini-2.5-flash-lite`

### Common Tasks

- **Change schedule delay**: Edit `SCHEDULE_DELAY_MINUTES` in `bot.py`
- **Change signature**: Edit `SIGNATURE` in `bot.py`
- **Change model**: Set `OPEN_ROUTER_MODEL` in `.env`
- **Tune translation**: Edit `prompt.txt`
- **Pause price predictions**: Set `PRICE_PREDICTIONS_ENABLED=false` in `.env`
