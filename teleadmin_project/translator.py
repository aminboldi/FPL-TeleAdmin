from pathlib import Path
import json
import re
import html as html_lib

from openai import AsyncOpenAI

_prompt_path = Path(__file__).parent / "prompt.txt"
TRANSLATION_PROMPT = _prompt_path.read_text(encoding="utf-8")

_article_prompt_path = Path(__file__).parent / "article_prompt.txt"
ARTICLE_PROMPT = _article_prompt_path.read_text(encoding="utf-8")
_summary_prompt_path = Path(__file__).parent / "summary_prompt.txt"
SUMMARY_PROMPT = _summary_prompt_path.read_text(encoding="utf-8")
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

    async def translate(self, text: str) -> str:
        try:
            if not self.google_client:
                raise TranslationError("Google AI Studio is not configured")
            return _translate_team_abbreviations(_normalize_digits(
                await self._call_model(self.google_client, self.google_model, text)
            ))
        except Exception:
            try:
                return _translate_team_abbreviations(_normalize_digits(
                    await self._call_model(
                        self.openrouter_client, self.fallback_model, text
                    )
                ))
            except Exception:
                raise TranslationError(
                    "Translation failed with both primary and fallback models"
                )

    async def translate_article(self, text: str, *, transcript: bool = False) -> dict[str, str]:
        formatting_instructions = _TRANSCRIPT_FORMATTING_INSTRUCTIONS if transcript else ""
        try:
            if not self.google_client:
                raise TranslationError("Google AI Studio is not configured")
            article = self._normalize_article(
                await self._call_article_model(
                    self.google_client, self.google_model, text, formatting_instructions
                )
            )
            article["summary"] = (
                await self.summarize_article(article.get("body", ""))
                or article.get("summary", "")
            )
            return article
        except Exception:
            try:
                article = self._normalize_article(
                    await self._call_article_model(
                        self.openrouter_client, self.fallback_model, text, formatting_instructions
                    )
                )
                article["summary"] = (
                    await self.summarize_article(article.get("body", ""))
                    or article.get("summary", "")
                )
                return article
            except Exception:
                pass
        # Fallback: translate normally. Summary generation is deliberately
        # handled separately, so a translation/API failure can never turn the
        # first 300 characters of the article into a fake summary.
        body = await self.translate(text)
        body = body.strip()
        lines = body.split("\n")
        title = lines[0].strip()[:100] if lines else ""
        summary = await self.summarize_article(body)
        return {"title": title, "summary": summary, "body": body}

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
    def _normalize_article(article: dict[str, str]) -> dict[str, str]:
        return {
            key: _translate_team_abbreviations(_normalize_digits(value))
            if isinstance(value, str) else value
            for key, value in article.items()
        }

    async def _call_model(self, client: AsyncOpenAI, model: str, text: str) -> str:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": TRANSLATION_PROMPT.format(text=text)}
            ],
            temperature=0.3,
            max_tokens=4096,
        )
        return response.choices[0].message.content.strip()

    async def _call_article_model(
        self, client: AsyncOpenAI, model: str, text: str, formatting_instructions: str = ""
    ) -> dict[str, str]:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": ARTICLE_PROMPT.format(text=text) + formatting_instructions,
                }
            ],
            temperature=0.3,
            max_tokens=8192,
        )
        raw = response.choices[0].message.content.strip()

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

    async def _call_summary_model(
        self, client: AsyncOpenAI, model: str, text: str,
    ) -> str:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": SUMMARY_PROMPT.format(text=text)}
            ],
            temperature=0.2,
            max_tokens=600,
        )
        return response.choices[0].message.content.strip()
