from pathlib import Path
import json
import re

from openai import AsyncOpenAI

_prompt_path = Path(__file__).parent / "prompt.txt"
TRANSLATION_PROMPT = _prompt_path.read_text(encoding="utf-8")

_article_prompt_path = Path(__file__).parent / "article_prompt.txt"
ARTICLE_PROMPT = _article_prompt_path.read_text(encoding="utf-8")


class TranslationError(Exception):
    pass


class Translator:
    def __init__(self, api_key: str, model: str, fallback_model: str):
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            default_headers={
                "X-Title": "TeleAdmin",
            },
        )
        self.model = model
        self.fallback_model = fallback_model

    async def translate(self, text: str) -> str:
        try:
            return await self._call_model(self.model, text)
        except Exception:
            try:
                return await self._call_model(self.fallback_model, text)
            except Exception:
                raise TranslationError(
                    "Translation failed with both primary and fallback models"
                )

    async def translate_article(self, text: str) -> dict[str, str]:
        try:
            return await self._call_article_model(self.model, text)
        except Exception:
            try:
                return await self._call_article_model(self.fallback_model, text)
            except Exception:
                pass
        # Fallback: translate normally and auto-generate title/summary
        body = await self.translate(text)
        body = body.strip()
        lines = body.split("\n")
        title = lines[0].strip()[:100] if lines else ""
        plain = re.sub(r"<[^>]+>", "", body)
        summary = plain[:300].strip()
        return {"title": title, "summary": summary, "body": body}

    async def _call_model(self, model: str, text: str) -> str:
        response = await self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": TRANSLATION_PROMPT.format(text=text)}
            ],
            temperature=0.3,
            max_tokens=4096,
        )
        return response.choices[0].message.content.strip()

    async def _call_article_model(self, model: str, text: str) -> dict[str, str]:
        response = await self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": ARTICLE_PROMPT.format(text=text)}
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
