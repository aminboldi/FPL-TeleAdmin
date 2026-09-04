from pathlib import Path
import json
import logging
import re
import html as html_lib

from bs4 import BeautifulSoup
from openai import AsyncOpenAI

import player_names

logger = logging.getLogger(__name__)

_prompt_path = Path(__file__).parent / "prompt.txt"
TRANSLATION_PROMPT = _prompt_path.read_text(encoding="utf-8")

_article_prompt_path = Path(__file__).parent / "article_prompt.txt"
ARTICLE_PROMPT = _article_prompt_path.read_text(encoding="utf-8")
_transcript_correction_prompt_path = Path(__file__).parent / "transcript_correction_prompt.txt"
TRANSCRIPT_CORRECTION_PROMPT = _transcript_correction_prompt_path.read_text(encoding="utf-8")
_summary_prompt_path = Path(__file__).parent / "summary_prompt.txt"
SUMMARY_PROMPT = _summary_prompt_path.read_text(encoding="utf-8")
_transcript_format_prompt_path = Path(__file__).parent / "transcript_format_prompt.txt"
TRANSCRIPT_FORMAT_PROMPT = _transcript_format_prompt_path.read_text(encoding="utf-8")
_completeness_prompt_path = Path(__file__).parent / "completeness_prompt.txt"
COMPLETENESS_PROMPT = _completeness_prompt_path.read_text(encoding="utf-8")

# Output-token ceilings. Persian is token-heavy relative to English, so a long
# transcript or article needs far more room than the English source suggests.
# These were previously 4096/8192 and silently truncated long content: the
# article call returned invalid JSON, and the formatting pass cut the body
# mid-sentence. Every call now also inspects finish_reason, which is the
# authoritative signal that a response was cut off.
_MAX_TOKENS_TRANSLATE = 32768
_MAX_TOKENS_ARTICLE = 32768
_MAX_TOKENS_FORMAT = 32768
_MAX_TOKENS_CORRECTION = 32768
_MAX_TOKENS_SUMMARY = 800
_MAX_TOKENS_ASSESS = 600
VIDEO_TITLE_INSTRUCTIONS = """

Additional instructions for this input:
- The input is a YouTube video title from an FPL channel. Return the Persian title that belongs above the video in a Telegram post, not a literal translation of every word in it.
- Remove the parts that exist for YouTube search or for the creator's promotion rather than to say what the video is about: season tags ("FPL 2025/26", "2026/27", "Season 9"); a bare "FPL" or "Fantasy Premier League" used as a tag rather than as part of the sentence; channel, creator, and co-host names; rank claims ("3x Top 10k", "Top 1k Finisher", "1M OR", "#1 in the World"); sponsor, discount, and giveaway mentions; "subscribe", "must watch", "you NEED to see this" urging; hashtags; emoji; and bracketed tags that carry no information.
- Keep everything that says what the video is about: the gameweek, player and club names, chip names, transfers, deadlines, prices, and the question the video answers.
- Do not add a topic the title does not contain, do not make it longer, and do not turn a statement into a question. A title that is already clean is simply translated.
- Return one line of plain text: no quotation marks, no surrounding brackets, no trailing full stop, and no explanation.
"""

_MERGED_POSTS_INSTRUCTIONS = """

Additional instructions for this input:
- The input is several separate Telegram posts, published one after another and merged here in their original order. It is not one continuous article, and the first post is not an introduction to the others.
- The title must describe the whole set. Titling the article after the opening post is wrong. When the posts share a subject, title it after that subject; when they are separate items, use a title that covers them together.
- The summary must synthesize every post, not the first one.
- Keep every post's content and its order. Do not drop a post, and do not merge two posts into one statement.
- Give each post its own paragraph, and add an <h3> heading only where a post clearly begins a new topic.
"""

_TRANSCRIPT_FORMATTING_INSTRUCTIONS = """

Additional instructions for this raw YouTube transcript:
- The source may be one unstructured block with unreliable line breaks. Reconstruct it into a readable Persian article without omitting substantive points.
- Split it into short, logical <p> paragraphs even where the transcript provides no paragraph boundaries.
- Detect meaningful topic shifts. When there are two or more, add concise, descriptive <h3> headings that reflect the transcript; do not invent facts, claims, or topics.
- Turn clearly enumerated advice, options, comparisons, or steps into a <ul> or <ol>. Do not force ordinary prose into a list.
"""

# Translation providers occasionally return Persian or Arabic-Indic digits. All
# bot output uses English digits, so normalize the provider response once here
# instead of relying on every individual post formatter to remember it.
_ENGLISH_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# Models occasionally preserve a fixture/team code despite the prompt.  Apply
# this deterministic final pass to visible text only; HTML tags and attributes
# (in particular URLs) must remain byte-for-byte intact.
_TEAM_ABBREVIATIONS = {
    "BOU": "بورنموث", "ARS": "آرسنال", "AVL": "استون ویلا", "BRE": "برنتفورد",
    "BHA": "برایتون", "BUR": "برنلی", "CHE": "چلسی", "CRY": "کریستال پالاس",
    "EVE": "اورتون", "FUL": "فولام", "LEE": "لیدز", "LIV": "لیورپول",
    "MCI": "منچستر سیتی", "MUN": "منچستر یونایتد", "NEW": "نیوکاسل",
    "NFO": "ناتینگهام فارست", "SUN": "ساندرلند", "TOT": "تاتنهام",
    "WHU": "وست هم", "WOL": "ولوز", "BIR": "بیرمنگام", "BBR": "بلکبرن",
    "BRC": "بریستول", "CHA": "چارلتون", "COV": "کاونتری", "DER": "داربی",
    "HUL": "هال سیتی", "IPS": "ایپسویچ", "LEI": "لستر", "MID": "میدلزبرو",
    "MIL": "میلوال", "NOR": "نوریچ", "OXF": "آکسفورد", "POR": "پورتموث",
    "PNE": "پرستون", "QPR": "کیو پی آر", "SHU": "شفیلد یونایتد",
    "SHW": "شفیلد ونزدی", "SOU": "ساوتهمپتون", "STO": "استوک", "SWA": "سوانزی",
    "WAT": "واتفورد", "WBA": "وست بروم", "WXH": "رکسهم",
}
_TEAM_ABBREVIATION_RE = re.compile(
    r"\b(?:" + "|".join(_TEAM_ABBREVIATIONS) + r")\b"
)


def _normalize_digits(text: str) -> str:
    return text.translate(_ENGLISH_DIGITS)


def _translate_team_abbreviations(text: str) -> str:
    """Replace remaining all-caps team codes without touching HTML markup."""
    parts = re.split(r"(<[^>]*>)", text)
    for index in range(0, len(parts), 2):
        parts[index] = _TEAM_ABBREVIATION_RE.sub(
            lambda match: _TEAM_ABBREVIATIONS[match.group(0)], parts[index]
        )
    return "".join(parts)


class TranslationError(Exception):
    pass


class TruncatedResponseError(TranslationError):
    """The model stopped because it hit the output-token ceiling.

    This is reported by the API itself (``finish_reason == "length"``), so it
    is an exact signal rather than a heuristic. Treating it as an error stops
    a half-translated article from being published as though it were whole.
    """


class ContentIncompleteError(Exception):
    """The *source* content is partial — a truncated feed or transcript.

    Distinct from TruncatedResponseError: nothing went wrong with our model
    call, the material we were given simply does not cover the whole article
    or video, so it must not be published.
    """

    def __init__(self, reason: str = ""):
        super().__init__(reason or "Content appears incomplete")
        self.reason = reason


def _render(prompt: str, **fields: str) -> str:
    """Fill a prompt template by literal placeholder replacement.

    ``str.format`` cannot be used here: these prompts contain literal JSON
    examples, and ``{ "title": ... }`` is parsed as a replacement field.
    ARTICLE_PROMPT.format() therefore raised KeyError on every call, so the
    structured article path always failed and silently fell back to plain
    translation. Literal replacement is immune to braces in prompt text.
    """
    for name, value in fields.items():
        prompt = prompt.replace("{" + name + "}", value)
    return prompt


def _response_text(response) -> str:
    """Return a completion's text, refusing silently truncated output."""
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise TruncatedResponseError(
            "Model output hit the token ceiling and was cut off"
        )
    return (choice.message.content or "").strip()


class Translator:
    def __init__(
        self,
        api_key: str,
        model: str,
        fallback_model: str,
        google_api_key: str | None = None,
        google_model: str = "gemini-3.1-flash-lite",
    ):
        self.openrouter_client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            default_headers={
                "X-Title": "TeleAdmin",
            },
        )
        # Keep the constructor's model argument for compatibility with existing
        # callers; the two-tier pipeline deliberately uses the configured
        # fallback_model for OpenRouter.
        self.openrouter_model = model
        self.fallback_model = fallback_model
        self.google_client = (
            AsyncOpenAI(
                api_key=google_api_key,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            )
            if google_api_key else None
        )
        self.google_model = google_model

    async def translate(self, text: str, *, instructions: str = "") -> str:
        """Translate one piece of text, optionally under extra instructions.

        ``instructions`` are appended to the shared translation prompt so a
        caller with a specific job — a video title that needs its search tags
        removed — gets the same terminology dictionary and player glossary
        without a second model call or a duplicated prompt file.
        """
        try:
            if not self.google_client:
                raise TranslationError("Google AI Studio is not configured")
            translated = await self._call_model(
                self.google_client, self.google_model, text, instructions
            )
            return self._finish_text(translated, text)
        except Exception:
            try:
                translated = await self._call_model(
                    self.openrouter_client, self.fallback_model, text, instructions
                )
                return self._finish_text(translated, text)
            except Exception:
                raise TranslationError(
                    "Translation failed with both primary and fallback models"
                )

    @staticmethod
    def _finish_text(translated: str, source_text: str) -> str:
        """Normalize a model response into the channel's own spellings."""
        return player_names.enforce(
            _translate_team_abbreviations(_normalize_digits(translated)),
            source_text=source_text,
        )

    async def correct_transcript(
        self, text: str, player_glossary: list[dict[str, str]]
    ) -> str:
        """Normalize likely ASR player-name errors before translation.

        Correction is best-effort: if either model cannot complete the pass,
        the original transcript is returned so importing the video still works.
        """
        source = str(text or "").strip()
        if not source or not player_glossary:
            return source

        glossary = self._format_player_glossary(player_glossary)
        prompt = _render(
            TRANSCRIPT_CORRECTION_PROMPT,
            player_glossary=glossary,
            text=source,
        )
        for client, model in (
            (self.google_client, self.google_model),
            (self.openrouter_client, self.fallback_model),
        ):
            if client is None:
                continue
            try:
                corrected = await self._call_transcript_correction(
                    client, model, prompt
                )
                if corrected:
                    return corrected
            except Exception as exc:
                logger.warning(
                    "Transcript player-name correction failed with %s: %s",
                    model,
                    exc,
                )
        return source

    @staticmethod
    def _format_player_glossary(players: list[dict[str, str]]) -> str:
        lines = []
        for player in players:
            canonical = str(player.get("canonical") or "").strip()
            if not canonical:
                continue
            display = str(player.get("display") or "").strip()
            aliases = str(player.get("aliases") or "").strip()
            team = str(player.get("team") or "").strip()
            details = [f"canonical: {canonical}"]
            if display and display.casefold() != canonical.casefold():
                details.append(f"display: {display}")
            if aliases:
                details.append(f"aliases: {aliases}")
            if team:
                details.append(f"club: {team}")
            lines.append("- " + " | ".join(details))
        return "\n".join(lines)

    async def translate_article(
        self, text: str, *, transcript: bool = False, merged_posts: bool = False,
    ) -> dict[str, str]:
        formatting_instructions = ""
        if transcript:
            formatting_instructions = _TRANSCRIPT_FORMATTING_INSTRUCTIONS
        elif merged_posts:
            formatting_instructions = _MERGED_POSTS_INSTRUCTIONS
        for client, model in (
            (self.google_client, self.google_model),
            (self.openrouter_client, self.fallback_model),
        ):
            if client is None:
                continue
            try:
                article = self._normalize_article(
                    await self._call_article_model(
                        client, model, text, formatting_instructions
                    )
                )
            except Exception:
                continue
            return await self._finish_article(article, text, transcript=transcript)

        # Fallback: translate normally. Summary generation is deliberately
        # handled separately, so a translation/API failure can never turn the
        # first 300 characters of the article into a fake summary.
        body = (await self.translate(text)).strip()
        lines = body.split("\n")
        return await self._finish_article(
            {
                "title": lines[0].strip()[:100] if lines else "",
                "summary": "",
                "body": body,
                "removed_images": set(),
                "complete": True,
                "incomplete_reason": "",
            },
            text,
            transcript=transcript,
        )

    async def _finish_article(
        self, article: dict, source_text: str, *, transcript: bool,
    ) -> dict:
        """Format, summarize, and apply the channel's player spellings.

        The body is corrected before it is summarized: the summary is written
        from the translated body, so correcting the body first is what stops a
        wrong player spelling from being copied into the Telegram post.
        """
        if transcript:
            article["body"] = await self._format_transcript_body(
                article.get("body", "")
            )
        article["body"] = player_names.enforce(
            article.get("body", ""), source_text=source_text
        )
        article["summary"] = (
            await self.summarize_article(article["body"])
            or article.get("summary", "")
        )
        for field in ("title", "summary"):
            article[field] = player_names.enforce(
                article.get(field, ""), source_text=source_text
            )
        return article

    # Talk-heavy FPL videos. Used only to give the model the arithmetic, since
    # models are unreliable at computing a ratio themselves.
    _CHARS_PER_MINUTE_MIN = 750
    _CHARS_PER_MINUTE_MAX = 1100

    async def assess_completeness(
        self,
        text: str,
        *,
        kind: str = "article",
        duration_seconds: int | None = None,
    ) -> dict:
        """Ask the model whether the SOURCE material was captured in full.

        Returns ``{"complete": bool, "confidence": str, "reason": str}``. A
        failed or unparseable assessment returns complete=True: this gate must
        never become a new reason for content to silently disappear.
        """
        source = str(text or "").strip()
        if not source:
            return {"complete": False, "confidence": "high", "reason": "empty content"}

        length_facts = f"Captured length: {len(source)} characters."
        if duration_seconds and duration_seconds > 0:
            minutes = duration_seconds / 60
            low = int(minutes * self._CHARS_PER_MINUTE_MIN)
            high = int(minutes * self._CHARS_PER_MINUTE_MAX)
            ratio = len(source) / max(1, low)
            length_facts += (
                f"\nVideo duration: {int(duration_seconds)} seconds"
                f" ({minutes:.1f} minutes)."
                f"\nExpected transcript length for this duration:"
                f" roughly {low} to {high} characters."
                f"\nCaptured / minimum-expected ratio: {ratio:.2f}"
                f" (1.00 or above means the length is plausible)."
            )

        prompt = _render(
            COMPLETENESS_PROMPT,
            kind=kind,
            length_facts=length_facts,
            text=source,
        )
        for client, model in (
            (self.google_client, self.google_model),
            (self.openrouter_client, self.fallback_model),
        ):
            if client is None:
                continue
            try:
                value = await self._call_assessment_model(client, model, prompt)
            except Exception as exc:
                logger.warning("Completeness check failed on %s: %s", model, exc)
                continue
            return {
                "complete": bool(value.get("complete", True)),
                "confidence": str(value.get("confidence") or "medium"),
                "reason": str(value.get("reason") or ""),
            }
        # Never let an unavailable checker block publishing.
        return {"complete": True, "confidence": "low", "reason": "assessment unavailable"}

    async def summarize_article(self, translated_html: str) -> str:
        """Create a concise Telegram preview from the complete translated article."""
        source = self._summary_source_text(translated_html)
        if not source:
            return ""

        try:
            if not self.google_client:
                raise TranslationError("Google AI Studio is not configured")
            return self._clean_summary(
                await self._call_summary_model(
                    self.google_client, self.google_model, source
                )
            )
        except Exception:
            try:
                return self._clean_summary(
                    await self._call_summary_model(
                        self.openrouter_client, self.fallback_model, source
                    )
                )
            except Exception:
                # The long article itself is still publishable if this optional
                # preview call fails. Callers can use the structured summary as
                # a secondary fallback.
                return ""

    @staticmethod
    def _summary_source_text(text: str) -> str:
        """Make translated HTML readable to the plain-text summary prompt."""
        text = re.sub(r"<img\b[^>]*>", "", text, flags=re.IGNORECASE)
        text = re.sub(
            r"</(?:p|h[1-6]|li|blockquote|figure|br|hr)>",
            "\n",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"<[^>]+>", "", text)
        text = html_lib.unescape(text)
        text = re.sub(r"\[\[TELEADMIN_IMAGE_\d+\]\]", "", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _clean_summary(text: str) -> str:
        """Normalize a model response and enforce a caption-sized summary."""
        raw = text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
        raw = raw.strip()

        # Accept a JSON response despite asking for plain text, but never pass
        # JSON or a markdown label into the Telegram caption.
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end > start:
            try:
                value = json.loads(raw[start:end + 1]).get("summary", "")
                if isinstance(value, str):
                    raw = value
            except (json.JSONDecodeError, AttributeError):
                pass
        raw = re.sub(r"^(?:summary|خلاصه)\s*:\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"^(?:[-*•]\s+)", "", raw)
        raw = re.sub(r"<[^>]+>", "", raw)
        raw = html_lib.unescape(re.sub(r"\s+", " ", raw)).strip(" \"'")
        raw = _translate_team_abbreviations(_normalize_digits(raw))
        # The summary is written from an already-corrected body, but it is also
        # generated on its own for catalog cards, so it is normalized here too.
        raw = player_names.enforce(raw)
        if len(raw) <= 400:
            return raw

        # Prefer a complete sentence over a hard character cut. Persian text
        # may use either Persian or Latin punctuation.
        candidate = raw[:400]
        boundary = max(candidate.rfind("."), candidate.rfind("؟"), candidate.rfind("!"), candidate.rfind("؛"))
        if boundary >= 160:
            return candidate[:boundary + 1].strip()
        return candidate.rstrip() + "…"

    @staticmethod
    def _normalize_article(article: dict) -> dict:
        normalized = {
            key: _translate_team_abbreviations(_normalize_digits(value))
            if isinstance(value, str) else value
            for key, value in article.items()
        }
        # The model reports which image placeholders it deliberately dropped as
        # promotional. Without this the restore step re-appends those banners
        # at the end of the article.
        removed = normalized.get("removed_images")
        indexes: set[int] = set()
        if isinstance(removed, (list, tuple, set)):
            for value in removed:
                try:
                    indexes.add(int(value))
                except (TypeError, ValueError):
                    continue
        normalized["removed_images"] = indexes
        normalized["complete"] = bool(normalized.get("complete", True))
        normalized["incomplete_reason"] = str(
            normalized.get("incomplete_reason") or ""
        )
        return normalized

    async def _format_transcript_body(self, body: str) -> str:
        """Enforce readable structure after transcript translation.

        Formatting instructions in the translation prompt are advisory. This
        second pass is deliberately separate so a successful translation can
        still be reformatted, and a failed formatter falls back to deterministic
        paragraph splitting instead of publishing one giant block.
        """
        body = str(body or "").strip()
        if not body:
            return body

        for client, model in (
            (self.google_client, self.google_model),
            (self.openrouter_client, self.fallback_model),
        ):
            if client is None:
                continue
            try:
                formatted = await self._call_format_model(client, model, body)
                formatted = self._clean_formatted_html(formatted)
                if self._has_readable_structure(formatted):
                    return formatted
            except Exception:
                continue
        return self._deterministic_transcript_format(body)

    @staticmethod
    def _clean_formatted_html(value: str) -> str:
        raw = str(value or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
        soup = BeautifulSoup(raw.strip(), "html.parser")
        root = soup.body or soup
        return "".join(str(child) for child in root.contents).strip()

    @staticmethod
    def _has_readable_structure(value: str) -> bool:
        soup = BeautifulSoup(value, "html.parser")
        text = soup.get_text(" ", strip=True)
        if not text:
            return False
        blocks = soup.find_all(["p", "h3", "h4", "ul", "ol", "blockquote"])
        if len(text) <= 900:
            return bool(blocks)
        return len(blocks) >= 2 or len(soup.find_all("br")) >= 1

    @staticmethod
    def _deterministic_transcript_format(value: str) -> str:
        """Split an unformatted fallback into readable Telegraph paragraphs."""
        soup = BeautifulSoup(value, "html.parser")
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
        if not text:
            return value.strip()

        sentences = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?؟؛])\s+", text)
            if sentence.strip()
        ]
        chunks: list[str] = []
        current = ""
        sentence_count = 0
        for sentence in sentences:
            if current and (
                sentence_count >= 3
                or len(current) + len(sentence) + 1 > 550
            ):
                chunks.append(current)
                current = ""
                sentence_count = 0
            current = f"{current} {sentence}".strip()
            sentence_count += 1
        if current:
            chunks.append(current)

        if len(chunks) == 1 and len(chunks[0]) > 700:
            words = chunks[0].split()
            chunks = []
            current_words: list[str] = []
            current_length = 0
            for word in words:
                if current_words and current_length + len(word) + 1 > 500:
                    chunks.append(" ".join(current_words))
                    current_words = []
                    current_length = 0
                current_words.append(word)
                current_length += len(word) + 1
            if current_words:
                chunks.append(" ".join(current_words))

        return "\n".join(
            f"<p>{html_lib.escape(chunk, quote=False)}</p>"
            for chunk in chunks
        )

    async def _call_model(
        self, client: AsyncOpenAI, model: str, text: str, instructions: str = "",
    ) -> str:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": _render(
                        TRANSLATION_PROMPT,
                        player_names=player_names.prompt_glossary(text),
                        text=text,
                    ) + instructions,
                }
            ],
            temperature=0.3,
            max_tokens=_MAX_TOKENS_TRANSLATE,
        )
        return _response_text(response)

    async def _call_transcript_correction(
        self, client: AsyncOpenAI, model: str, prompt: str,
    ) -> str:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=_MAX_TOKENS_CORRECTION,
        )
        raw = _response_text(response)
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
        return raw.strip()

    async def _call_article_model(
        self, client: AsyncOpenAI, model: str, text: str, formatting_instructions: str = ""
    ) -> dict[str, str]:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": _render(
                        ARTICLE_PROMPT,
                        player_names=player_names.prompt_glossary(text),
                        text=text,
                    ) + formatting_instructions,
                }
            ],
            temperature=0.3,
            max_tokens=_MAX_TOKENS_ARTICLE,
        )
        raw = _response_text(response)

        # Strip markdown code fences
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
        raw = raw.strip()

        # Try to find JSON object boundaries
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start:end + 1]

        return json.loads(raw)

    async def _call_format_model(
        self, client: AsyncOpenAI, model: str, text: str,
    ) -> str:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": _render(TRANSCRIPT_FORMAT_PROMPT, text=text)}
            ],
            temperature=0.2,
            max_tokens=_MAX_TOKENS_FORMAT,
        )
        return _response_text(response)

    async def _call_summary_model(
        self, client: AsyncOpenAI, model: str, text: str,
    ) -> str:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": _render(SUMMARY_PROMPT, text=text)}
            ],
            temperature=0.2,
            max_tokens=_MAX_TOKENS_SUMMARY,
        )
        # A clipped summary is cosmetic, not a lost article, so this call
        # tolerates the ceiling instead of failing the whole publish.
        return (response.choices[0].message.content or "").strip()

    async def _call_assessment_model(
        self, client: AsyncOpenAI, model: str, prompt: str,
    ) -> dict:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=_MAX_TOKENS_ASSESS,
        )
        raw = _response_text(response)
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
        raw = raw.strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start:end + 1]
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Assessment response was not a JSON object")
        return value
