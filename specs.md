Project Specification: Telegram Channel Translation & Forwarding Bot
Project Overview
The objective is to create a lightweight, persistent Python background service that listens for new posts on a designated public Telegram source channel, translates the message content into natural, fluent Persian (Farsi), and publishes the translated text to a target Telegram channel owned by the user.

The service must run continuously (24/7) as an asynchronous event loop and utilize free-tier infrastructure.

Technical Stack & Constraints
Language: Python 3.10+

Core Framework: Telethon (MTProto Client API) for asynchronous event listening and posting.

Translation Gateway: OpenRouter API leveraging the meta-llama/llama-3.3-70b-instruct:free model (with a fallback path to google/gemma-4-31b-it:free).

Target Environment: Persistent Linux-based cloud worker platform (e.g., Northflank Developer Plan or Render Background Web Service paired with an uptime pinger).

State Management: Local session storage via Telethon (.session files).

Architectural Workflow
Event Capture: The Telethon client acts as a user instance, subscribing to the events.NewMessage event for the specified source channel username.

Validation: The incoming event is verified to ensure it contains textual content (event.message.text). Non-text updates or empty payloads are safely bypassed.

Translation Prompting: The raw text is wrapped in a rigid instruction prompt to enforce clean translation boundaries.

API Integration: The script sends an HTTP POST request to OpenRouter using an OpenAI-compatible client library or a lightweight standard HTTP client.

Dispatch: The resulting text is transmitted immediately to the target channel using the Telethon client instance.

Functional Requirements
1. Robust Telegram Listening (MTProto Client)
Use the Telethon framework rather than the standard HTTP Bot API to allow channel reading without requiring source admin privileges.

Implement an active connection session using local file persistence (translation_session.session).

2. High-Quality Persian Translation via OpenRouter
Connect to OpenRouter via an OpenAI-compatible SDK implementation or raw asynchronous HTTP requests.

Use meta-llama/llama-3.3-70b-instruct:free as the primary engine to leverage its deep parameter size for natural Persian phrasing.

Construct a clear prompt directing the model to output only the translated result.

The instruction prompt must explicitly tell the model to:

Translate text into natural, idiomatically correct Persian.

Maintain all original markdown links, structural paragraph breaks, formatting syntax, and emojis.

Omit any standard conversational filler, introductory headers, or diagnostic explanations in the output response.

3. Stability and Resiliency
Implement an enterprise-grade try/except safety block inside the message event handler to prevent a single translation failure, API rate limit, or upstream OpenRouter provider timeout from crashing the continuous execution loop.

Gracefully handle Telegram FloodWaitError exceptions by auto-sleeping for the specified duration requested by Telegram.

Log operational updates and errors transparently to stdout.

Ensure the script closes gracefully on termination signals (SIGINT, SIGTERM).

4. Security & Environment Configuration
Do not hardcode authentication credentials or specific channels. The system must ingest the following configuration elements strictly from the host environment or a local .env file:

TELEGRAM_API_ID (Integer)

TELEGRAM_API_HASH (String)

OPENROUTER_API_KEY (String)

SOURCE_CHANNEL_ID (String/Username)

TARGET_CHANNEL_ID (String/Username)