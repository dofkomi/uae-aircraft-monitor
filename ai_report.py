"""تقرير تحليلي مكتوب بالعربية عبر نماذج ذكاء اصطناعي **مجانية**.

الفكرة: التقرير المفصّل الحالي بيانات وصفية (أرقام ومواقع). هذه الوحدة
تحوّل نفس البيانات إلى **دراسة مكتوبة** تجيب: ماذا يحدث؟ أين؟ ما الذي
يستحق المتابعة؟ وما حدود اليقين؟

المزوّدون المدعومون (كلهم بطبقة مجانية وبلا بطاقة دفع):
  * ``gemini``     — Google AI Studio (GEMINI_API_KEY)
  * ``groq``       — Groq Cloud (GROQ_API_KEY)
  * ``openrouter`` — OpenRouter (OPENROUTER_API_KEY، ومنها ``openrouter/free``)
  * ``custom``     — أي خدمة متوافقة مع OpenAI (AI_BASE_URL + AI_API_KEY)

الكشف تلقائي بحسب المفتاح الموجود، ويمكن فرض مزوّد بـ ``AI_PROVIDER``.
اسم النموذج يُكتشف تلقائياً من قائمة النماذج المتاحة (``/models``)، لذا لا
يتعطّل البوت عند تغيّر أسماء النماذج. عند غياب أي مفتاح يُستخدم **تحليل
محلي مكتوب** مبني على القواعد، حتى لا يبقى التقرير بيانات وصفية فقط.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiohttp

from classifier import assess, is_uae_related, is_uae_related_event
from config import settings
from country_shapes import default_shapes, flag_emoji
from models import AircraftState, LandingEvent, RouteInfo, SignalEvent

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
أنت محلّل بيانات طيران محترف يكتب بالعربية الفصحى المبسطة.
تتلقى لقطة بيانات مرصودة آلياً من شبكات ADS-B التطوعية العامة في منطقة
الإمارات، وتكتب دراسة تحليلية موجزة لقارئ مهتم بالطيران المدني والعسكري.

قواعد صارمة:
1. استخدم فقط الأرقام والأسماء الواردة في البيانات. لا تخترع تسجيلات أو
   مطارات أو أرقاماً غير موجودة.
2. لا تجزم بنوايا الطائرة أو سبب اختفاء الإشارة. البيانات لا تثبت إغلاق
   جهاز التعريف؛ غياب الرصد قد يكون خروجاً من تغطية المستقبلات.
3. ميّز بوضوح بين: حقيقة مرصودة (من البيانات) واستنتاج محتمل (بكلمة «قد»
   أو «يُرجّح»).
4. اهتم أساساً بالطائرات الإماراتية/المستأجرة إماراتياً: أين انقطعت
   إشارتها (أو الدولة القريبة إن كانت فوق المياه)، وهل ما تزال تبث
   عالمياً، وهل هبطت في الخارج ثم غيّرت وجهتها.
5. اذكر أرقاماً محددة عند الإشارة إلى ظاهرة (مثال: «3 طائرات»).
6. لا تتجاوز 380 كلمة.
7. عندما يُطلب منك تقدير الطابع العسكري، استند إلى مؤشرات مركّبة (النوع،
   المشغّل، النداء، التسجيل، غياب النداء، squawk، نمط الارتفاع/السرعة)
   وأعطِ درجة احتمالية بكلمة (مرجّح/محتمل/مستبعد) — لا تجزم.
8. رصد الوجهات غير المبرمجة: قارن الهبوط الفعلي بالمسار المجدول وبمطار
   جديد لم تُرصد فيه الطائرة سابقاً، واذكر السبب.

صيغة الإخراج (تُحوَّل آلياً إلى تنسيق تليجرام):
## عنوان قصير
- نقطة
- نقطة
جملة أو جملتان ختاميتان عن حدود التحليل.
لا تستخدم جداول ولا رموز Markdown أخرى غير «##» للعناوين و«-» للنقاط.
"""

DEFAULT_MODELS = {
    "gemini": (
        "gemini-3.1-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-3-flash-preview",
        "gemini-3.8-flash",
        "gemini-3.5-flash",
        "gemini-2.5-flash",
    ),
    "groq": (
        "llama-3.3-70b-versatile",
        "gpt-oss-120b",
        "llama-4-scout-17b-16e-instruct",
        "qwen3.8-27b",
        "llama-3.1-8b-instant",
    ),
    "openrouter": (
        "openrouter/free",
        "google/gemma-4-31b-it:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
    ),
}

BASE_URLS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}

PROVIDER_KEYS = {
    "gemini": "gemini_api_key",
    "groq": "groq_api_key",
    "openrouter": "openrouter_api_key",
}

AI_REPORT_TITLE = "🧠 <b>الدراسة التحليلية للبيانات</b>"
LOCAL_REPORT_TITLE = "🧠 <b>الدراسة التحليلية (محرّك محلي — بدون نموذج ذكاء اصطناعي)</b>"


@dataclass
class Provider:
    name: str
    kind: str          # gemini | openai
    api_key: str
    base_url: str
    model: str = ""
    prefer: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
def detect_provider() -> Provider | None:
    """يختار المزوّد المتاح (أو المزوّد المطلوب صراحةً)."""
    explicit = settings.ai_provider
    if explicit in {"gemini", "groq", "openrouter"}:
        key = getattr(settings, PROVIDER_KEYS[explicit], "") or settings.ai_api_key
        if not key:
            log.info("AI_PROVIDER=%s لكن المفتاح غير موجود — تعطيل التحليل", explicit)
            return None
        return Provider(
            name=explicit,
            kind="gemini" if explicit == "gemini" else "openai",
            api_key=key,
            base_url=BASE_URLS[explicit],
            prefer=DEFAULT_MODELS[explicit],
        )
    if explicit == "custom":
        if not (settings.ai_base_url and settings.ai_api_key):
            return None
        return Provider(
            name="custom", kind="openai", api_key=settings.ai_api_key,
            base_url=settings.ai_base_url.rstrip("/"),
        )

    for name in ("gemini", "groq", "openrouter"):
        key = getattr(settings, PROVIDER_KEYS[name], "")
        if key:
            return Provider(
                name=name,
                kind="gemini" if name == "gemini" else "openai",
                api_key=key,
                base_url=BASE_URLS[name],
                prefer=DEFAULT_MODELS[name],
            )
    if settings.ai_base_url and settings.ai_api_key:
        return Provider(
            name="custom", kind="openai", api_key=settings.ai_api_key,
            base_url=settings.ai_base_url.rstrip("/"),
        )
    return None


# ---------------------------------------------------------------------------
def format_ai_text(raw: str) -> str:
    """يحوّل نص النموذج إلى HTML آمن لتليجرام."""
    text = (raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        heading = re.match(r"^#{1,6}\s+(.*)$", stripped)
        if heading:
            lines.append(f"<b>{html.escape(heading.group(1).strip())}</b>")
            continue
        bullet = re.match(r"^[-*•]\s+(.*)$", stripped)
        if bullet:
            lines.append("• " + html.escape(bullet.group(1).replace("**", "").strip()))
            continue
        lines.append(html.escape(stripped.replace("**", "")))
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _country_info(lat: float | None, lon: float | None) -> tuple[str, bool]:
    """(اسم الدولة مع العلم، هل هي «دولة قريبة» لنقطة فوق المياه؟)."""
    try:
        shapes = default_shapes()
    except Exception:  # noqa: BLE001
        return "غير محدد", False
    name, code, _ = shapes.lookup(lat, lon)
    if name:
        return f"{flag_emoji(code)} {name}".strip(), False
    near = shapes.nearest(lat, lon, settings.nearest_country_max_km)
    if near:
        return f"{flag_emoji(near[1])} {near[0]}".strip(), True
    return "فوق المياه/خارج الحدود", False


def _country(lat: float | None, lon: float | None) -> str:
    """اسم دولة النقطة (بلا تكرار لاحقة «قريبة» في التجميع)."""
    return _country_info(lat, lon)[0]


def _route_text(route: RouteInfo | None) -> str | None:
    if route is None:
        return None
    parts = [route.route_label]
    if route.airline:
        parts.append(route.airline)
    return " · ".join(parts)


def _aircraft_brief(state: AircraftState, classification: str) -> dict:
    country, near = _country_info(state.latitude, state.longitude)
    altitude = state.altitude_ft
    if altitude is not None and altitude < 0:
        altitude = 0
    return {
        "callsign": state.callsign or state.icao24,
        "registration": state.registration,
        "type": state.aircraft_type,
        "class": classification,
        "on_ground": state.on_ground,
        "alt_ft": altitude,
        "speed_kts": state.speed_kts,
        "track_deg": int(state.true_track) if state.true_track is not None else None,
        "vertical_fpm": int(state.vertical_rate * 196.85) if state.vertical_rate else None,
        "country": country,
        "near_water": near,
        "route": _route_text(state.route),
    }


def _event_brief(event: SignalEvent, now: datetime) -> dict:
    minutes = max(0, int((now - event.last_seen).total_seconds() // 60))
    country, near = _country_info(event.last_lat, event.last_lon)
    return {
        "callsign": event.callsign or event.icao24,
        "registration": event.registration,
        "class": event.classification,
        "country_at_loss": event.country or country,
        "near_country_used": event.geo_source == "nearest_country" or near,
        "reason": event.reason,
        "minutes_since_last_seen": minutes,
        "last_alt_ft": event.last_alt_ft,
        "last_speed_kts": event.last_speed_kts,
        "nearest_airport": event.nearest_airport,
        "route": _route_text(event.route),
        "still_broadcasting_globally": event.still_broadcasting,
        "current_country_global": event.global_country,
    }


def _landing_brief(item: LandingEvent) -> dict:
    return {
        "callsign": item.callsign or item.icao24,
        "registration": item.registration,
        "uae_related": item.uae_related,
        "landed_at": item.airport_ident,
        "landed_country": item.airport_country_code,
        "from": item.from_airport,
        "from_country": item.from_country_code,
        "leg_minutes": item.leg_minutes,
        "divergence": item.divergence,
        "scheduled_route": _route_text(item.route),
        "route_mismatch": item.route_mismatch,
        "new_airport": item.new_airport,
        "unplanned": item.unplanned,
        "unplanned_reason": item.unplanned_reason,
    }


def _military_suspects(tracker, states: list[AircraftState]) -> list[dict]:
    """مرشّحون للطابع العسكري من غير المصنّفين، مرتّبون بالاحتمال المحلي.

    النموذج اللغوي يرى هذه المؤشرات ثم يصدر تقديره النهائي بنفسه.
    """
    out: list[dict] = []
    for state in states:
        if tracker.classifications.get(state.icao24) == "military":
            continue
        result = assess(state)
        if result["score"] < 0.30:
            continue
        out.append(
            {
                "aircraft": state.display_name,
                "type": state.aircraft_type,
                "callsign": state.callsign,
                "registration": state.registration,
                "operator": state.operator,
                "alt_ft": state.altitude_ft,
                "speed_kts": state.speed_kts,
                "on_ground": state.on_ground,
                "squawk": state.squawk,
                "local_score": result["score"],
                "local_verdict": result["verdict_text"],
                "reasons": result["reasons"],
            }
        )
    out.sort(key=lambda item: item["local_score"], reverse=True)
    return out[:12]


def build_snapshot(tracker, period_minutes: int | None = None) -> dict:
    """لقطة مضغوطة (JSON) تُغذّى للنموذج — بلا HTML وبلا إحداثيات خام."""
    period = period_minutes or settings.report_interval_minutes
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=period)

    states = list(tracker.current.values())
    events = tracker.events_since(since)
    landings = tracker.landings_since(since)
    counts = tracker.counts()

    airborne = [s for s in states if not s.on_ground]
    uae_states = [s for s in states if is_uae_related(s)]
    uae_events = [e for e in events if is_uae_related_event(e)]
    other_events = [e for e in events if not is_uae_related_event(e)]
    military = [
        s for s in states
        if tracker.classifications.get(s.icao24) == "military"
    ]
    emergencies = [s for s in states if s.squawk in {"7500", "7600", "7700"}]

    altitudes = [s.altitude_ft for s in airborne if s.altitude_ft is not None]
    speeds = [s.speed_kts for s in states if s.speed_kts is not None]

    countries = Counter(_country(s.latitude, s.longitude) for s in states)
    near_counts = Counter(
        _country(s.latitude, s.longitude)
        for s in states
        if _country_info(s.latitude, s.longitude)[1]
    )

    return {
        "generated_at": now.isoformat(timespec="minutes"),
        "window_minutes": period,
        "watch_area": {
            "lat": [settings.lamin, settings.lamax],
            "lon": [settings.lomin, settings.lomax],
        },
        "totals": {
            "aircraft_now": len(states),
            "airborne": len(airborne),
            "on_ground": len(states) - len(airborne),
            "military": counts["military"],
            "civilian": counts["civilian"],
            "unknown": counts["unknown"],
            "uae_related": len(uae_states),
            "avg_alt_ft": int(sum(altitudes) / len(altitudes)) if altitudes else None,
            "avg_speed_kts": int(sum(speeds) / len(speeds)) if speeds else None,
        },
        "countries_now": [
            {
                "country": country,
                "count": count,
                "over_water_near": near_counts.get(country, 0),
            }
            for country, count in countries.most_common(10)
        ],
        "uae_aircraft_now": [
            _aircraft_brief(s, tracker.classifications.get(s.icao24, "unknown"))
            for s in sorted(uae_states, key=lambda s: s.display_name)[:12]
        ],
        "military_now": [
            _aircraft_brief(s, "military")
            for s in sorted(military, key=lambda s: s.display_name)[:10]
        ],
        "military_suspects": _military_suspects(tracker, states),
        "signal_loss_uae": [
            _event_brief(e, now)
            for e in sorted(uae_events, key=lambda e: -e.last_seen.timestamp())[:10]
        ],
        "signal_loss_other_count": len(other_events),
        "landings": [
            _landing_brief(item)
            for item in sorted(landings, key=lambda i: -i.at.timestamp())[:10]
        ],
        "emergencies": [s.display_name for s in emergencies][:5],
        "sources": [
            {"name": source.name, "ok": source.health.ok,
             "count": source.health.aircraft_count}
            for source in tracker.sources
        ],
    }


def build_user_prompt(snapshot: dict) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    return (
        "هذه لقطة بيانات مرصودة الآن. اكتب دراسة تحليلية عربية موجزة.\n"
        "ابدأ بـ «## الخلاصة» ثم «## الطائرات الإماراتية» ثم "
        "«## انقطاع الإشارة» ثم «## الهبوطات والوجهات» ثم "
        "«## تقدير الطابع العسكري» ثم «## وجهات غير مبرمجة» ثم "
        "«## ما يستحق المتابعة».\n"
        "في «تقدير الطابع العسكري»: استخدم القائمة military_suspects "
        "(ودرجة local_score كمرجع) وقدّم تقديرك أنت لكل طائرة مع سبب موجز، "
        "واذكر أن كثيراً من العسكري لا يبث ADS-B أصلاً.\n"
        "في «وجهات غير مبرمجة»: اذكر الطائرات التي وردت بـ unplanned=true "
        "مع سبب كل واحدة (مخالفة المسار أو مطار جديد).\n"
        "إن كان قسم بلا بيانات فاكتب سطراً واحداً يوضّح ذلك.\n\n"
        f"البيانات:\n{payload}"
    )


_NON_TEXT_HINTS = (
    "tts", "embed", "guard", "rerank", "image", "transcribe", "lyria",
    "robotics", "computer-use", "deep-research", "antigravity", "omni",
    "banana", "clip", "audio", "veo", "imagen", "whisper",
)


def _is_text_model(name: str) -> bool:
    """يستبعد نماذج الصور/الصوت/البحث العميق التي لا تصلح لكتابة تقرير."""
    lowered = name.lower()
    return not any(hint in lowered for hint in _NON_TEXT_HINTS)


# ---------------------------------------------------------------------------
class AIAnalyzer:
    """عميل رفيع لأي مزوّد مجاني، مع كشف النموذج وتخزينه مؤقتاً."""

    def __init__(self, provider: Provider | None = None) -> None:
        self.provider = provider if provider is not None else detect_provider()
        self.last_error = ""
        self._model: str | None = None
        self._models: list[str] | None = None
        self._bad: set[str] = set()

    @property
    def available(self) -> bool:
        return self.provider is not None

    def describe(self) -> str:
        if not self.provider:
            return "معطّل (لا يوجد مفتاح)"
        model = self._model or settings.ai_model or (
            self.provider.prefer[0] if self.provider.prefer else "—"
        )
        return f"{self.provider.name} · {model}"

    async def analyze(self, snapshot: dict) -> str | None:
        """يجرّب النماذج المرشّحة بالترتيب حتى ينجح أحدها.

        بعض النماذج الجديدة تُرجع استجابة فارغة أو 404/503 رغم ظهورها في
        قائمة النماذج، لذا لا نعتمد على اسم واحد بل ننتقل تلقائياً للبديل.
        """
        if not self.provider:
            return None
        prompt = build_user_prompt(snapshot)
        for model in await self._candidates():
            try:
                raw = await self._call(model, prompt)
            except Exception as exc:  # noqa: BLE001 - لا نُفشل التقرير بسبب النموذج
                self.last_error = str(exc)[:300]
                log.warning("فشل استدعاء النموذج %s: %s", model, exc)
                self._bad.add(model)
                continue
            text = (raw or "").strip()
            if text:
                self._model = model
                return text
            log.info("النموذج %s لم يُرجع نصاً — تجربة بديل", model)
            self._bad.add(model)
        self.last_error = self.last_error or "لم يُرجع أي نموذج نصاً قابلاً للاستخدام"
        return None

    # ------------------------------------------------------------------
    async def _candidates(self) -> list[str]:
        """قائمة النماذج المفضّلة القابلة للتجربة، بالترتيب."""
        if settings.ai_model:
            return [settings.ai_model]
        if self._model and self._model not in self._bad:
            return [self._model]
        if self._models is None:
            self._models = await self._list_models()
        names = self._models or []
        ordered: list[str] = []
        for wanted in self.provider.prefer:
            if not names or wanted in names:
                ordered.append(wanted)
        for name in names:
            if name in ordered or not _is_text_model(name):
                continue
            ordered.append(name)
        if not ordered:
            ordered = list(self.provider.prefer)
        usable = [name for name in ordered if name not in self._bad]
        return usable[:6] or usable

    async def _list_models(self) -> list[str]:
        provider = self.provider
        headers = {"Accept": "application/json"}
        if provider.kind == "gemini":
            url = f"{provider.base_url}/models?key={provider.api_key}"
        else:
            url = f"{provider.base_url}/models"
            headers["Authorization"] = f"Bearer {provider.api_key}"
        payload = await self._request("GET", url, headers)
        if not isinstance(payload, dict):
            return []
        items = payload.get("data") or payload.get("models") or []
        names: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("id") or item.get("name") or ""
            if provider.kind == "gemini":
                name = name.replace("models/", "")
                methods = item.get("supportedGenerationMethods") or []
                if methods and "generateContent" not in methods:
                    continue
            if name:
                names.append(name)
        return names

    async def _call(self, model: str, prompt: str) -> str | None:
        provider = self.provider
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if provider.kind == "gemini":
            url = f"{provider.base_url}/models/{model}:generateContent?key={provider.api_key}"
            body = {
                "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": settings.ai_temperature,
                    "maxOutputTokens": settings.ai_max_tokens,
                },
            }
        else:
            url = f"{provider.base_url}/chat/completions"
            headers["Authorization"] = f"Bearer {provider.api_key}"
            if provider.name == "openrouter":
                headers["HTTP-Referer"] = "https://github.com/uae-aircraft-monitor"
                headers["X-Title"] = "UAE Aircraft Monitor"
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": settings.ai_temperature,
                "max_tokens": settings.ai_max_tokens,
            }
        payload = await self._request("POST", url, headers, body)
        if not isinstance(payload, dict):
            return None
        if provider.kind == "gemini":
            candidates = payload.get("candidates") or []
            if not candidates:
                return None
            parts = (candidates[0].get("content") or {}).get("parts") or []
            # نتجاهل أجزاء «التفكير» (thought) ونبقي النص الظاهر فقط
            texts = [
                str(part.get("text", ""))
                for part in parts
                if isinstance(part, dict) and not part.get("thought")
            ]
            return "\n".join(texts).strip() or None
        choices = payload.get("choices") or []
        if not choices:
            return None
        return ((choices[0].get("message") or {}).get("content") or "").strip() or None

    async def _request(
        self, method: str, url: str, headers: dict, body: dict | None = None
    ) -> dict | None:
        timeout = aiohttp.ClientTimeout(total=settings.ai_timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, headers=headers, json=body) as resp:
                    if resp.status >= 400:
                        detail = (await resp.text())[:300]
                        self.last_error = f"HTTP {resp.status}: {detail}"
                        log.warning("AI %s → HTTP %s: %s", url.split("?")[0], resp.status, detail)
                        return None
                    return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            self.last_error = str(exc)[:300]
            return None


# ---------------------------------------------------------------------------
def build_local_analysis(snapshot: dict) -> str:
    """تحليل مكتوب بالقواعد — بديل مضمون عند غياب مفتاح الذكاء الاصطناعي."""
    totals = snapshot.get("totals") or {}
    countries = snapshot.get("countries_now") or []
    uae_states = snapshot.get("uae_aircraft_now") or []
    uae_events = snapshot.get("signal_loss_uae") or []
    landings = snapshot.get("landings") or []
    other_events = snapshot.get("signal_loss_other_count") or 0
    emergencies = snapshot.get("emergencies") or []

    lines = ["## الخلاصة"]
    lines.append(
        f"- رُصدت {totals.get('aircraft_now', 0)} طائرة الآن داخل نطاق المراقبة، "
        f"منها {totals.get('airborne', 0)} في الجو و{totals.get('on_ground', 0)} على الأرض."
    )
    lines.append(
        f"- التصنيف: عسكرية {totals.get('military', 0)} · مدنية "
        f"{totals.get('civilian', 0)} · غير مصنّفة {totals.get('unknown', 0)}."
    )
    if countries:
        top = "، ".join(
            f"{item['country']} ({item['count']}"
            + (f"، منها {item['over_water_near']} فوق المياه" if item.get("over_water_near") else "")
            + ")"
            for item in countries[:4]
        )
        lines.append(f"- أبرز دول المواقع الحالية: {top}.")
    if emergencies:
        lines.append(f"- 🚨 أكواد طوارئ مرصودة: {', '.join(emergencies)}.")

    lines.append("## الطائرات الإماراتية")
    if uae_states:
        for item in uae_states[:6]:
            route = f" — مسار {item['route']}" if item.get("route") else ""
            alt = f" · {item['alt_ft']:,} قدم" if item.get("alt_ft") else ""
            water = " (فوق المياه قرب الساحل)" if item.get("near_water") else ""
            lines.append(
                f"- {item['callsign']} ({item.get('type') or 'نوع غير معروف'}) "
                f"في {item['country']}{water}{alt}{route}."
            )
    else:
        lines.append("- لا توجد طائرات إماراتية مرصودة في هذه اللحظة.")

    lines.append("## انقطاع الإشارة")
    if uae_events:
        for item in uae_events[:6]:
            near = " (دولة قريبة)" if item.get("near_country_used") else ""
            state = (
                "وما تزال تبث عالمياً في " + str(item.get("current_country_global"))
                if item.get("still_broadcasting_globally")
                else "ولا رصد عالمي مطابق لها في الشبكة"
            )
            lines.append(
                f"- {item['callsign']}: آخر رصد فوق {item['country_at_loss']}{near} "
                f"قبل {item['minutes_since_last_seen']} دقيقة، {state}."
            )
    else:
        lines.append("- لا توجد أحداث انقطاع لطائرات إماراتية خلال الفترة.")
    if other_events:
        lines.append(f"- {other_events} حدث انقطاع لطائرات غير إماراتية (خارج نطاق التركيز).")

    lines.append("## الهبوطات والوجهات")
    if landings:
        for item in landings[:6]:
            flag = "طائرة إماراتية" if item.get("uae_related") else "طائرة أخرى"
            extra = " ⚠️ هبوط خارجي بوجهة مختلفة عن المسار المجدول" if item.get("route_mismatch") else ""
            lines.append(
                f"- {item['callsign']} ({flag}) هبطت في {item.get('landed_at')} "
                f"قادمة من {item.get('from') or 'غير معروف'}{extra}."
            )
    else:
        lines.append("- لا توجد هبوطات مرصودة خلال الفترة.")

    lines.append("## تقدير الطابع العسكري")
    suspects = snapshot.get("military_suspects") or []
    if suspects:
        for item in suspects[:6]:
            reasons = "، ".join(item.get("reasons") or []) or "مؤشرات محدودة"
            lines.append(
                f"- {item['aircraft']}: تقدير محلي {item.get('local_verdict')} "
                f"({int((item.get('local_score') or 0) * 100)}%) — {reasons}."
            )
        lines.append(
            "- تقدير احتمالي من نمط الحركة وبيانات التعريف، وليس تأكيداً؛ "
            "وكثير من الطائرات العسكرية لا تبث ADS-B أصلاً."
        )
    else:
        lines.append("- لا مؤشرات كافية على طابع عسكري لغير المصنّفة في هذه اللحظة.")

    lines.append("## وجهات غير مبرمجة")
    unplanned = [item for item in landings if item.get("unplanned")]
    if unplanned:
        for item in unplanned[:6]:
            flag = "إماراتية" if item.get("uae_related") else "أخرى"
            lines.append(
                f"- {item['callsign']} ({flag}) توقّفت في {item.get('landed_at')}: "
                f"{item.get('unplanned_reason') or 'سبب غير محدد'}."
            )
    else:
        lines.append("- لا توقّفات في مطارات جديدة أو مخالفة للمسار خلال الفترة.")

    lines.append("## ما يستحق المتابعة")
    followups = 0
    if uae_events:
        lines.append("- مراقبة الطائرات التي انقطعت إشارتها محلياً وتحقّق البث العالمي لاحقاً.")
        followups += 1
    if any(item.get("divergence") for item in landings):
        lines.append("- هبوطات خارجية تلتها وجهات غير إماراتية تستحق مراجعة المسار المجدول.")
        followups += 1
    if any(item.get("still_broadcasting_globally") for item in uae_events):
        lines.append(
            "- طائرات ما تزال تبث عالمياً: غيابها محلياً سببه تغطية المستقبلات لا إغلاق البث."
        )
        followups += 1
    if any(item.get("unplanned") for item in landings):
        lines.append("- توقّفات في مطارات جديدة أو مخالفة للمسار: تُستحق مراجعة سبب التحويل.")
        followups += 1
    if not followups:
        lines.append(
            "- لا مؤشرات غير معتادة في هذه الفترة؛ يُنصح بمقارنة الدورات القادمة "
            "لرصد أي تغيّر في نمط المسارات أو انقطاع الإشارة."
        )
    lines.append(
        "التحليل مبني على بيانات ADS-B العامة فقط، ولا يثبت إغلاق جهاز التعريف "
        "أو نيّة الطائرة؛ غياب الرصد قد يكون بسبب تغطية المستقبلات."
    )
    return "\n".join(lines)


async def build_analysis(
    tracker, period_minutes: int | None = None, analyzer: AIAnalyzer | None = None
) -> tuple[str, str] | None:
    """يرجع ``(نص HTML جاهز، وسم المصدر)`` أو ``None`` إذا كان الوضع off."""
    if settings.ai_report_mode == "off":
        return None
    snapshot = build_snapshot(tracker, period_minutes)

    if settings.ai_enabled:
        analyzer = analyzer or AIAnalyzer()
        if analyzer.available:
            raw = await analyzer.analyze(snapshot)
            text = format_ai_text(raw or "") if raw else ""
            if text:
                return (
                    f"{AI_REPORT_TITLE}\n<i>المصدر: {html.escape(analyzer.describe())}</i>\n\n{text}",
                    analyzer.describe(),
                )
            log.info("تعذّر التحليل بالنموذج (%s) — سيُستخدم المحرّك المحلي", analyzer.last_error)

    return (
        f"{LOCAL_REPORT_TITLE}\n\n{build_local_analysis(snapshot)}",
        "local",
    )
