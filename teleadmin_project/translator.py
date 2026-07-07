from pathlib import Path

from openai import AsyncOpenAI

_prompt_path = Path(__file__).parent / "prompt.txt"
TRANSLATION_PROMPT = _prompt_path.read_text(encoding="utf-8")


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
