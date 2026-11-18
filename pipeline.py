"""خطّ تحليل ثلاثي المراحل: Groq (تصفية) → Gemini (استدلال) → OpenRouter (تدقيق).

يُغذّى من ``ai_report.build_snapshot`` (ساعي) أو ``daily.build_daily_snapshot``
(24 ساعة). المخرجات نص Markdown عربي بنفس صيغة ``ai_report.format_ai_text``،
مع خريطة تدقيق ``hex → model`` تُغذّي عمود ``Audited_By_Model`` في المصفوفة.

ضمانات صارمة:

* **صفر هلوسة**: كل مرحلة تتلقّى بيانات مرصودة فقط، وأي رد لا يطابق المخطط
  يُرفض. مفاتيح السجلات القادمة من النموذج تُقاطع مع المفاتيح المعروفة، وأي
  مفتاح مجهول يُسقَط فوراً.
* **صفر تكرار**: الإزالة الحتمية في بايثون هي المرجع، ومخرجات Groq لا تزيد
  السجلات ولا تُنشئها — يمكنها فقط تأكيد/إسقاط سجل قائم.
* **تدقيق إلزامي**: لا يُعتمد أي تحليل من Gemini قبل مراجعة نموذج ثانٍ
  (OpenRouter، ثم Groq بديلاً). إن تعذّر التدقيق يُرجع ``None`` كاملاً
  فتتراجع الطبقة العليا إلى المحرّك المحلي بدل نشر تحليل غير مُدقَّق.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from classifier import is_uae_related_event
from config import settings
from ledger import dedup_key
from llm_router import LLMRouter

log = logging.getLogger("pipeline")

MAX_RECORDS = 40
MAX_POINTS_PER_SECTION = 8
MAX_SECTIONS = 10

# حقول المركّب من ADS-B لا تحمل MSN؛ نعلن ذلك بدل تخمين رقم تسلسلي.
MSN_UNAVAILABLE = "غير متوفر — ADS-B العام لا يحمل MSN"

VERDICTS = {"upheld", "revised", "rejected"}
VERDICT_AR = {"upheld": "مؤكّد", "revised": "معدّل", "rejected": "مسقَط"}

_SPACE_RE = re.compile(r"\s+")
_HEX_RE = re.compile(r"^[0-9a-f]{6}$")


@dataclass
class PipelineResult:
    text: str
    source: str
    audit: dict[str, str] = field(default_factory=dict)
    verdicts: dict[str, str] = field(default_factory=dict)
    stages: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 1. بناء السجلات وإزالة التكرار (حتمي — لا يعتمد على أي نموذج)
# ---------------------------------------------------------------------------
def _clean(value, limit: int = 400) -> str:
    text = _SPACE_RE.sub(" ", str(value or "")).strip()
    return text[:limit]


def _entry_stamp(minutes_ago) -> datetime:
    try:
        return datetime.now(timezone.utc) - timedelta(minutes=float(minutes_ago or 0))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def records_from_snapshot(snapshot: dict) -> list[dict]:
    """يستخرج سجلات الطائرات الإماراتية فقط، بمفتاح إزالة تكرار ثابت."""
    from classifier import _uae_match

    records: dict[str, dict] = {}

    def add(record: dict) -> None:
        key = record["dedup_key"]
        existing = records.get(key)
        if existing is None or record.get("score", 0) > existing.get("score", 0):
            records[key] = record

    matrix = (snapshot.get("sahel_osint") or snapshot.get("sahel") or {}).get("matrix") or []
    for row in matrix:
        if not isinstance(row, dict):
            continue
        hex_code = _clean(row.get("Flight_Hex"), 12).lower()
        if not _HEX_RE.match(hex_code):
            continue
        if not _uae_match(hex_code, None, row.get("Registration")):
            continue  # البوت متخصّص في الطائرات الإماراتية وحدها
        observed = row.get("Dedup_Key") or dedup_key(
            hex_code, datetime.now(timezone.utc), "MAT"
        )
        add(
            {
                "hex": hex_code,
                "registration": _clean(row.get("Registration"), 24),
                "msn": _clean(row.get("MSN") or MSN_UNAVAILABLE, 60),
                "operator": _clean(row.get("Declared_Operator"), 80),
                "origin": _clean(row.get("Origin"), 80),
                "entry_point": _clean(row.get("Entry_Point_Sahel"), 200),
                "blackout": _clean(row.get("Blackout_LatLong"), 40),
                "landing": _clean(row.get("Landing_Airfield"), 200),
                "zone": _clean(row.get("Zone"), 40),
                "score": int(row.get("Anomaly_Score") or 0),
                "confidence": _clean(row.get("Confidence_Level"), 24),
                "indicators": [_clean(item, 160) for item in (row.get("Anomaly_Indicators") or "").split("؛") if _clean(item, 160)],
                "dedup_key": str(observed),
            }
        )

    for item in snapshot.get("signal_loss_uae") or snapshot.get("uae_events") or []:
        if not isinstance(item, dict):
            continue
        hex_code = ""
        callsign = _clean(item.get("callsign"), 24)
        if _HEX_RE.match(callsign.lower()):
            hex_code = callsign.lower()
        if not hex_code:
            # النداء ليس رمزاً؛ نستخدمه مفتاحاً ظاهراً بدل اختلاق ICAO24.
            hex_code = callsign.lower() or "unknown"
        if not _uae_match(hex_code, callsign, item.get("registration")):
            continue  # البوت متخصّص في الطائرات الإماراتية وحدها
        stamp = _entry_stamp(item.get("minutes_since_last_seen", item.get("minutes_ago", 0)))
        key = dedup_key(hex_code, stamp, "SIG")
        add(
            {
                "hex": hex_code,
                "registration": _clean(item.get("registration"), 24),
                "msn": MSN_UNAVAILABLE,
                "operator": _clean(item.get("global_country") or "", 80),
                "origin": _clean(item.get("route"), 80),
                "entry_point": _clean(item.get("country"), 120),
                "blackout": "",
                "landing": "",
                "zone": "",
                "score": 0,
                "confidence": "",
                "indicators": [
                    _clean(item.get("reason"), 120),
                ] if item.get("reason") else [],
                "dedup_key": key,
            }
        )

    return sorted(records.values(), key=lambda row: row.get("score", 0), reverse=True)[
        :MAX_RECORDS
    ]


def dedupe_records(records: list[dict]) -> list[dict]:
    """إزالة تكرار حتمية بمفتاح ``[hex|date|entry_ts]`` مع إبقاء الأعلى درجة."""
    best: dict[str, dict] = {}
    for record in records:
        key = record.get("dedup_key") or ""
        if key not in best or record.get("score", 0) > best[key].get("score", 0):
            best[key] = record
    return list(best.values())


# ---------------------------------------------------------------------------
# 2. المطالبات (JSON صارم، عربية، بلا هلوسة)
# ---------------------------------------------------------------------------
FILTER_SYSTEM = """\
أنت طبقة استقبال وتصفية سريعة في خطّ OSINT جوي. مهمّتك الوحيدة: قراءة سجلات
تليمتري مرصودة، وتأكيد مفاتيح إزالة التكرار الصحيحة، ورفع رايات الشذوذ.
لا تُنشئ سجلاً جديداً. لا تُغيّر رقماً. لا تخمّن ما ليس في البيانات.
أعد JSON فقط، بلا أي نص خارج الكائن.
"""

REASON_SYSTEM = """\
أنت محلّل استخبارات جوية مفتوحة المصدر (Tier-1 Aviation OSINT) تكتب بالعربية.
تتلقّى سجلات ADS-B مرصودة لطائرات إماراتية (مدنية/حكومية/مستأجرة) في نطاق
الساحل الأفريقي ودول العبور والممرات البحرية.

المنهجية الإلزامية (طبّقها ولا تتجاوزها):
1. تثبيت الهوية: ICAO24 ثم التسجيل. MSN غير متوفر في ADS-B العام — اكتب ذلك.
2. التليمتري الخام: الارتفاع والسرعة والمسار ونقطة الانقطاع كما وردت.
3. كشف الشذوذ الحركي/المساري: التفاف، محاذاة حدود FIR، انحراف عن الدائرة
   العظمى، تعارض ارتفاع/سرعة.
4. تحقيق جنائي للانقطاع: إحداثيات الانقطاع، ودوائر المدى ومتجهات السرعة
   لتضييق نطاق الهبوط المحتمل.
5. التحقق البصري: لا تدّعِ تحقّقاً؛ اذكر أن Sentinel-2 بانتظار المراجعة.
6. تحيّز التغطية: «خروج من نطاق التغطية» ليس شذوذاً ولا دليل إغلاق بث.

قواعد صارمة:
- استخدم فقط الأرقام والأسماء الواردة. صفر اختلاق.
- آخر موقع ≠ إثبات إغلاق متعمّد. الوجهة مُستنتَجة لا مؤكّدة.
- «طبقة التأجير الفعلية» غير محددة بلا سجلات عقود.
- لا تكرّر طائرة أو رقماً في أكثر من قسم.
- كل استنتاج يُسند إلى رقم أو حقل من البيانات.
- لا تذكر أسماء حقول JSON أو قيمها الإنجليزية.
- لا تتجاوز 650 كلمة.

أعد JSON فقط بهذا الشكل:
{"sections":[{"heading":"عنوان عربي قصير","points":["نقطة عربية"]}],
 "audit":[{"hex":"<icao24>","verdict":"upheld|revised|rejected","note":"سبب موجز"}],
 "unknowns":["ما لا تفسّره البيانات"]}
"""

AUDIT_SYSTEM = """\
أنت مدقّق عدائي (Adversarial Auditor) في خطّ OSINT جوي. تتلقّى تحليلاً كتبه
نموذج آخر مع البيانات الأصلية. مهمّتك:

1. ارفض أي ادّعاء لا يسنده حقل في البيانات (هلوسة أو استنتاج بلا رقم).
2. أعد نمذجة عتبات الشذوذ إن لزم، وأسقط أي صف مكرّر (نفس ICAO24 والتاريخ
   ونقطة الدخول).
3. لا ترفع درجة الثقة من عندك، ولا تحوّل «احتمالاً» إلى «تأكيد».
4. أبقِ التحذيرات العلمية: آخر موقع ≠ إغلاق متعمّد، والوجهة مُستنتَجة،
   وكثير من العسكري لا يبث ADS-B.
5. صحّح الصياغة العربية وأعد الدمج النهائي بلا تكرار.

أعد JSON فقط بهذا الشكل:
{"sections":[{"heading":"عنوان عربي قصير","points":["نقطة عربية"]}],
 "audit":[{"hex":"<icao24>","verdict":"upheld|revised|rejected","note":"سبب موجز"}],
 "conclusions":["استنتاج مسند إلى رقم"]}
"""


def _payload_for(snapshot: dict, kind: str) -> dict:
    """لقطة مضغوطة (بلا حقول فارغة) تصلح لمطالبة نموذج بحدّ توكنات معقول."""
    if kind == "daily":
        keys = (
            "hours", "window", "totals", "events_by_country_uae", "events_by_reason",
            "activity_by_hour", "landings_by_airport", "uae_events", "unplanned",
            "diverged", "military_suspects", "sahel", "sources",
        )
    else:
        keys = (
            "generated_at", "window_minutes", "totals", "countries_now",
            "uae_aircraft_now", "military_suspects", "signal_loss_uae", "landings",
            "emergencies", "sahel_osint", "sources",
        )
    payload: dict = {"kind": kind}
    for key in keys:
        value = snapshot.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            value = value[:12]
        if key in ("sahel", "sahel_osint") and isinstance(value, dict):
            # المصفوفة تُمرّر كسجلات مستقلة؛ لا نكرّرها داخل اللقطة.
            value = {k: v for k, v in value.items() if k != "matrix"}
        payload[key] = value
    return payload


def _json_block(data) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 3. المراحل
# ---------------------------------------------------------------------------
async def _stage_filter(router: LLMRouter, records: list[dict]) -> tuple[list[dict], str]:
    """Groq: يؤكّد مفاتيح الإزالة ويرفع الرايات. لا يضيف سجلات أبداً."""
    known = {record["dedup_key"] for record in records}
    prompt = (
        "هذه سجلات مرصودة (JSON). أعد تأكيد مفاتيح إزالة التكرار التي يجب "
        "إبقاؤها، وارفع رايات الشذوذ الموجزة. لا تُنشئ مفاتيح جديدة.\n"
        'المخطط: {"deduped_keys":["<dedup_key>"],"flags":["راية عربية"],'
        '"counts":{"records":0,"duplicates_removed":0}}\n'
        f"السجلات:\n{_json_block(records)}"
    )
    reply = await router.call("filter", FILTER_SYSTEM, prompt, required=("deduped_keys",))
    if reply is None:
        return records, "تخطّي (Groq غير متاح) — الإزالة الحتمية فقط"

    raw_keys = reply.data.get("deduped_keys") or []
    kept = [record for record in records if record["dedup_key"] in set(raw_keys)]
    unknown = [key for key in raw_keys if key not in known]
    if unknown:
        log.warning("Groq أعاد %d مفتاحاً مجهولاً — أُسقطت", len(unknown))
    # حماية من الإفراط: إن أسقط النموذج أكثر من نصف السجلات نُبقي المرجع الحتمي.
    if not kept or len(kept) < len(records) / 2:
        return records, "مقبول جزئياً (تجاوز حد الإسقاط) — المرجع الحتمي"
    removed = len(records) - len(kept)
    return kept, f"نجح — أبقي {len(kept)} وأُسقط {removed}"


async def _stage_reason(
    router: LLMRouter, payload: dict, records: list[dict], filter_note: str
) -> tuple[dict, str] | None:
    prompt = (
        "اكتب الدراسة التحليلية من البيانات التالية فقط. الطائرات الإماراتية "
        "وحدها هي موضوع التحليل؛ ما عداها إحصاء سياقي لا يُفصَّل.\n"
        f"نتيجة التصفية: {filter_note}\n"
        f"السجلات المعتمدة:\n{_json_block(records)}\n"
        f"اللقطة السياقية:\n{_json_block(payload)}"
    )
    reply = await router.call(
        "reason", REASON_SYSTEM, prompt, required=("sections",), temperature=0.2
    )
    if reply is None:
        return None
    sections = _valid_sections(reply.data.get("sections"))
    if not sections:
        log.warning("Gemini أعاد أقساماً غير صالحة — رفض المرحلة")
        return None
    return (
        {
            "sections": sections,
            "audit": _valid_audit(reply.data.get("audit")),
            "unknowns": [
                _clean(item, 240) for item in (reply.data.get("unknowns") or [])[:6]
            ],
        },
        reply.model,
    )


async def _stage_audit(
    router: LLMRouter, payload: dict, records: list[dict], draft: dict
) -> tuple[dict, str] | None:
    prompt = (
        "دقّق التحليل التالي عدائياً مقابل البيانات الأصلية، وأعد الدمج النهائي "
        "بلا تكرار. أسقط أي ادّعاء غير مسند، وأبقِ التحذيرات العلمية.\n"
        f"السجلات الأصلية:\n{_json_block(records)}\n"
        f"اللقطة السياقية:\n{_json_block(payload)}\n"
        f"مسوّدة التحليل:\n{_json_block(draft)}"
    )
    reply = await router.call(
        "audit", AUDIT_SYSTEM, prompt, required=("sections",), temperature=0.1
    )
    if reply is None:
        return None
    sections = _valid_sections(reply.data.get("sections"))
    if not sections:
        log.warning("المدقّق أعاد أقساماً غير صالحة — رفض المرحلة")
        return None
    conclusions = [
        _clean(item, 260) for item in (reply.data.get("conclusions") or [])[:5]
    ]
    return (
        {
            "sections": sections,
            "audit": _valid_audit(reply.data.get("audit")),
            "conclusions": conclusions,
        },
        reply.model,
    )


def _valid_sections(raw) -> list[dict]:
    """يتحقّق من بنية الأقسام ويرفض أي شكل غير متوقّع."""
    if not isinstance(raw, list):
        return []
    sections: list[dict] = []
    for item in raw[:MAX_SECTIONS]:
        if not isinstance(item, dict):
            continue
        heading = _clean(item.get("heading"), 80)
        points = item.get("points")
        if not heading or not isinstance(points, list):
            continue
        clean_points = [
            _clean(point, 400) for point in points[:MAX_POINTS_PER_SECTION] if _clean(point, 400)
        ]
        if clean_points:
            sections.append({"heading": heading, "points": clean_points})
    return sections


def _valid_audit(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw[:MAX_RECORDS]:
        if not isinstance(item, dict):
            continue
        hex_code = _clean(item.get("hex"), 12).lower()
        verdict = _clean(item.get("verdict"), 20).lower()
        if not hex_code or verdict not in VERDICTS:
            continue
        out.append(
            {"hex": hex_code, "verdict": verdict, "note": _clean(item.get("note"), 200)}
        )
    return out


# ---------------------------------------------------------------------------
# 4. التنسيق النهائي
# ---------------------------------------------------------------------------
def _render(sections: list[dict], audit: list[dict], auditor: str) -> str:
    lines: list[str] = []
    for section in sections:
        lines.append(f"## {section['heading']}")
        for point in section["points"]:
            lines.append(f"- {point}")
        lines.append("")
    if audit:
        counts = {"upheld": 0, "revised": 0, "rejected": 0}
        for item in audit:
            counts[item["verdict"]] += 1
        lines.append("## التدقيق المتقاطع")
        lines.append(
            f"- روجعت {len(audit)} طائرة بواسطة نموذج ثانٍ ({auditor}): "
            f"{counts['upheld']} مؤكّد، {counts['revised']} معدّل، "
            f"{counts['rejected']} مسقَط."
        )
        rejected = [item for item in audit if item["verdict"] == "rejected"]
        if rejected:
            codes = "، ".join(item["hex"].upper() for item in rejected[:5])
            lines.append(f"- أُسقطت ادّعاءات غير مسندة عن: {codes}.")
        lines.append(
            "- التدقيق يتحقّق من الإسناد إلى البيانات، لا من نيّة الطائرة؛ "
            "آخر موقع لا يثبت إغلاق جهاز التعريف قصداً."
        )
        lines.append("")
    return "\n".join(lines).strip()


def _persist_audit(tracker, audit: list[dict], model: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    store = getattr(tracker, "store", None) if tracker is not None else None
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for item in audit:
        mapping[item["hex"]] = model
        if store is None:
            continue
        try:
            store.record_audit(item["hex"], day, model, item["verdict"], item["note"])
        except Exception as exc:  # noqa: BLE001 - التدقيق لا يُفشل التقرير
            log.warning("تعذّر حفظ تدقيق %s: %s", item["hex"], exc)
    return mapping


# ---------------------------------------------------------------------------
# 5. الواجهة
# ---------------------------------------------------------------------------
async def run_analysis(
    snapshot: dict,
    tracker=None,
    kind: str = "hourly",
    router: LLMRouter | None = None,
) -> PipelineResult | None:
    """ينفّذ المراحل الثلاث ويعيد نتيجة مُدقَّقة، أو ``None`` للتراجع المحلي."""
    router = router or LLMRouter(store=getattr(tracker, "store", None))
    if not router.available:
        return None

    records = dedupe_records(records_from_snapshot(snapshot))
    if not records:
        log.info("لا سجلات إماراتية في اللقطة — لا حاجة إلى خطّ النماذج")
        return None
    payload = _payload_for(snapshot, kind)

    stages: dict[str, str] = {}
    records, filter_note = await _stage_filter(router, records)
    stages["filter"] = filter_note

    reasoned = await _stage_reason(router, payload, records, filter_note)
    if reasoned is None:
        stages["reason"] = "فشل — تراجع إلى المحرّك المحلي"
        return None
    draft, reason_model = reasoned
    stages["reason"] = f"نجح ({reason_model})"

    audited = await _stage_audit(router, payload, records, draft)
    if audited is None:
        stages["audit"] = "فشل — لا تحليل غير مُدقَّق"
        return None
    final, audit_model = audited
    stages["audit"] = f"نجح ({audit_model})"

    text = _render(final["sections"], final["audit"], audit_model)
    if not text:
        return None

    audit_map = _persist_audit(tracker, final["audit"], audit_model)
    verdicts = {item["hex"]: item["verdict"] for item in final["audit"]}
    source = f"{reason_model} → تدقيق {audit_model}"
    log.info(
        "خطّ النماذج نجح: %d سجل، %d قسم، %d تدقيق",
        len(records), len(final["sections"]), len(final["audit"]),
    )
    return PipelineResult(
        text=text,
        source=source,
        audit=audit_map,
        verdicts=verdicts,
        stages=stages,
    )
