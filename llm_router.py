"""توجيه المهام بين ثلاثة مزوّدين مجانيين: Groq ثم Gemini ثم OpenRouter.

كل مزوّد له دور محدّد في خطّ المعالجة (انظر ``pipeline``):

  * ``groq``       — استقبال وتصفية وإزالة تكرار سريعة (نموذج صغير وسريع).
  * ``gemini``     — استدلال عميق، ربط جغرافي، ونمذجة حركية.
  * ``openrouter`` — مراجعة عدائية وإعادة نمذجة ودمج التقرير النهائي.

قواعد صارمة:
  * مخرجات JSON فقط، تُتحقّق مقابل مفاتيح إلزامية قبل القبول.
  * حدّ أدنى للفاصل بين الطلبات + تبريد عند 429/401/403/5xx.
  * ميزانية يومية محفوظة في SQLite (``llm_calls``) حتى لا تُستنزف الطبقة المجانية.
  * أي فشل يُرجع ``None`` بهدوء؛ الطبقة الأعلى تتراجع إلى المحرّك المحلي.

لا يُطبع أي مفتاح في السجل، ولا يُرسل أي سرّ إلى نموذج.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field

import aiohttp

from config import settings

log = logging.getLogger("llm_router")

USER_AGENT = (
    "uae-aircraft-monitor/2.0 (OSINT research; "
    "+https://github.com/dofkomi/uae-aircraft-monitor)"
)

# كل دور له سلسلة مزوّدين: الأول أساسي، والثاني بديل عند سقوط الأول.
ROLE_CHAIN: dict[str, tuple[str, ...]] = {
    "filter": ("groq",),
    "reason": ("gemini",),
    "audit": ("openrouter", "groq"),
}

ROLE_LABEL = {
    "filter": "تصفية وإزالة تكرار",
    "reason": "استدلال عميق",
    "audit": "تدقيق عدائي",
}

_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


@dataclass
class LLMProvider:
    name: str
    kind: str  # "openai" | "gemini"
    base_url: str
    api_key: str
    model: str
    headers: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return bool(self.api_key and self.model)


@dataclass
class LLMReply:
    """رد ناجح: البيانات المهيكلة + المزوّد والنموذج الذي أنتجها."""

    data: dict
    provider: str
    model: str


def extract_json(text: str) -> dict | None:
    """يستخرج أول كائن JSON من رد نصّي (يتحمّل أسوار ``` وسطوراً زائدة)."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        payload = json.loads(cleaned)
        return payload if isinstance(payload, dict) else None
    except (TypeError, ValueError):
        pass
    match = _JSON_OBJECT.search(cleaned)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def validate(payload: dict | None, required: tuple[str, ...]) -> dict | None:
    """يرفض أي رد لا يحمل المفاتيح الإلزامية (صفر افتراض غير متحقَّق منه)."""
    if not isinstance(payload, dict):
        return None
    for key in required:
        if key not in payload:
            return None
    return payload


class LLMRouter:
    """عميل موحّد للمزوّدين الثلاثة، مع حدود معدل وميزانية يومية."""

    def __init__(self, store=None) -> None:
        self.store = store
        self.providers: dict[str, LLMProvider] = self._build_providers()
        self._cooldown_until: dict[str, float] = {}
        self._last_call: dict[str, float] = {}
        self.last_error = ""

    # ------------------------------------------------------------------
    def _build_providers(self) -> dict[str, LLMProvider]:
        providers: dict[str, LLMProvider] = {}
        if settings.groq_api_key:
            # Groq يرفض الطلبات بلا User-Agent مناسب (Cloudflare 1010).
            providers["groq"] = LLMProvider(
                name="groq",
                kind="openai",
                base_url="https://api.groq.com/openai/v1",
                api_key=settings.groq_api_key,
                model=settings.groq_model,
                headers={"User-Agent": USER_AGENT},
            )
        if settings.gemini_api_key:
            providers["gemini"] = LLMProvider(
                name="gemini",
                kind="gemini",
                base_url="https://generativelanguage.googleapis.com/v1beta",
                api_key=settings.gemini_api_key,
                model=settings.gemini_model,
                headers={"User-Agent": USER_AGENT},
            )
        if settings.openrouter_api_key:
            providers["openrouter"] = LLMProvider(
                name="openrouter",
                kind="openai",
                base_url="https://openrouter.ai/api/v1",
                api_key=settings.openrouter_api_key,
                model=settings.openrouter_model,
                headers={
                    "User-Agent": USER_AGENT,
                    "HTTP-Referer": "https://github.com/dofkomi/uae-aircraft-monitor",
                    "X-Title": "UAE Aircraft Monitor",
                },
            )
        return providers

    @property
    def available(self) -> bool:
        return any(provider.ready for provider in self.providers.values())

    def describe(self) -> str:
        parts: list[str] = []
        for role, chain in ROLE_CHAIN.items():
            for name in chain:
                provider = self.providers.get(name)
                if provider and provider.ready:
                    parts.append(f"{ROLE_LABEL[role]}: {provider.model}")
                    break
        return " → ".join(parts) if parts else "معطّل (لا مفاتيح)"

    def roles_available(self) -> list[str]:
        return [role for role in ROLE_CHAIN if self._chain(role)]

    # ------------------------------------------------------------------
    def _chain(self, role: str) -> list[LLMProvider]:
        chain: list[LLMProvider] = []
        for name in ROLE_CHAIN.get(role, ()):
            provider = self.providers.get(name)
            if not provider or not provider.ready:
                continue
            if time.time() < self._cooldown_until.get(name, 0):
                continue
            chain.append(provider)
        return chain

    def _budget_left(self, provider: LLMProvider) -> bool:
        if self.store is None:
            return True
        try:
            return self.store.llm_calls_today(provider.name) < settings.llm_daily_calls
        except Exception:  # noqa: BLE001 - القاعدة ليست شرطاً للتشغيل
            return True

    def _record(self, provider: LLMProvider, role: str, ok: bool, note: str = "") -> None:
        if self.store is None:
            return
        try:
            self.store.record_llm_call(provider.name, role, ok, note)
        except Exception:  # noqa: BLE001
            pass

    async def _throttle(self, name: str) -> None:
        gap = settings.llm_min_interval
        if gap <= 0:
            return
        last = self._last_call.get(name, 0.0)
        wait = last + gap - time.time()
        if wait > 0:
            await asyncio.sleep(min(wait, 5.0))
        self._last_call[name] = time.time()

    # ------------------------------------------------------------------
    async def call(
        self,
        role: str,
        system: str,
        prompt: str,
        required: tuple[str, ...] = (),
        max_tokens: int | None = None,
        temperature: float = 0.0,
    ) -> LLMReply | None:
        """ينفّذ طلب JSON لدور محدّد، ويجرّب سلسلة المزوّدين بالترتيب."""
        tokens = max_tokens or settings.llm_max_tokens
        for provider in self._chain(role):
            if not self._budget_left(provider):
                log.info(
                    "تخطّي %s: استُنفدت ميزانية اليوم (%d طلباً)",
                    provider.name, settings.llm_daily_calls,
                )
                continue
            await self._throttle(provider.name)
            data = await self._request(provider, role, system, prompt, tokens, temperature)
            if data is not None:
                data = validate(data, required)
            if data is not None:
                self._record(provider, role, True)
                return LLMReply(data=data, provider=provider.name, model=provider.model)
            self._record(provider, role, False, self.last_error)
        return None

    # ------------------------------------------------------------------
    async def _request(
        self,
        provider: LLMProvider,
        role: str,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> dict | None:
        if provider.kind == "gemini":
            return await self._gemini(provider, system, prompt, max_tokens, temperature)
        return await self._openai(provider, system, prompt, max_tokens, temperature)

    async def _openai(
        self,
        provider: LLMProvider,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool = True,
    ) -> dict | None:
        url = f"{provider.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {provider.api_key}",
            **provider.headers,
        }
        body: dict = {
            "model": provider.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        status, payload, detail = await self._post(url, headers, body)
        if status == 400 and json_mode and "response_format" in str(detail).lower():
            # بعض النماذج المجانية لا تدعم response_format؛ نعيد بلا القيد.
            body.pop("response_format", None)
            status, payload, detail = await self._post(url, headers, body)
        if status != 200 or not isinstance(payload, dict):
            self._fail(provider, status, detail)
            return None

        choices = payload.get("choices") or []
        if not choices:
            self.last_error = f"{provider.name}: رد بلا choices"
            return None
        choice = choices[0] or {}
        message = choice.get("message") or {}
        content = message.get("content") or ""
        # OpenRouter قد يضع JSON في reasoning_content عند نماذج التفكير.
        if not content.strip():
            content = message.get("reasoning_content") or message.get("reasoning") or ""
        data = extract_json(str(content))
        if data is None and choice.get("finish_reason") == "length":
            self.last_error = f"{provider.name}: انقطع الرد (length)"
            log.info("رد %s انقطع قبل اكتمال JSON — نعيد بميزانية أكبر", provider.name)
            body["max_tokens"] = min(max_tokens * 2, 8000)
            status, payload, detail = await self._post(url, headers, body)
            if status == 200 and isinstance(payload, dict):
                choices = payload.get("choices") or []
                if choices:
                    message = (choices[0] or {}).get("message") or {}
                    data = extract_json(str(message.get("content") or ""))
        if data is None:
            self.last_error = self.last_error or f"{provider.name}: رد ليس JSON صالحاً"
            log.warning("%s: تعذّر تحليل JSON", provider.name)
        return data

    async def _gemini(
        self,
        provider: LLMProvider,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> dict | None:
        url = (
            f"{provider.base_url}/models/{provider.model}:generateContent"
            f"?key={provider.api_key}"
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **provider.headers,
        }
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
                "responseMimeType": "application/json",
            },
        }
        status, payload, detail = await self._post(url, headers, body)
        if status != 200 or not isinstance(payload, dict):
            self._fail(provider, status, detail)
            return None
        candidates = payload.get("candidates") or []
        if not candidates:
            self.last_error = "gemini: رد بلا candidates"
            return None
        parts = ((candidates[0] or {}).get("content") or {}).get("parts") or []
        # أجزاء «التفكير» ليست مخرجات؛ نقرأ النص الظاهر فقط.
        text = "\n".join(
            str(part.get("text") or "")
            for part in parts
            if isinstance(part, dict) and not part.get("thought")
        ).strip()
        data = extract_json(text)
        if data is None:
            self.last_error = "gemini: رد ليس JSON صالحاً"
            log.warning("gemini: تعذّر تحليل JSON (%s)", text[:120])
        return data

    async def _post(
        self, url: str, headers: dict, body: dict
    ) -> tuple[int, dict | None, str]:
        timeout = aiohttp.ClientTimeout(total=settings.llm_stage_timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=body) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        return resp.status, None, text[:400]
                    try:
                        payload = json.loads(text)
                    except ValueError:
                        return resp.status, None, "رد غير JSON"
                    return resp.status, payload, ""
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return 0, None, f"{type(exc).__name__}: {exc}"[:300]

    def _fail(self, provider: LLMProvider, status: int, detail: str) -> None:
        self.last_error = f"{provider.name}: HTTP {status} {detail}"[:300]
        log.warning("LLM %s → HTTP %s: %s", provider.name, status, detail[:200])
        # 429 = تجاوز حد؛ 401/403 = مفتاح أو حظر؛ 5xx = عطل مؤقت. كلها تستحق تبريداً.
        if status in (0, 401, 403, 429) or status >= 500:
            cooldown = settings.llm_cooldown_seconds
            if status == 429:
                cooldown *= 2  # احترام أشد لحدّ المعدل
            self._cooldown_until[provider.name] = time.time() + cooldown
