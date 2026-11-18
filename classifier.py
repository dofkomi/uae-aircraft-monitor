"""تصنيف الطائرة إلى عسكرية / مدنية / غير معروفة.

يُبنى التصنيف على بيانات عامة فقط:
  1. رمز النوع ICAO (أقوى مؤشر — يأتي من adsb.lol أو adsbdb).
  2. اسم المشغّل/الشركة إن توفر.
  3. أنماط نداء الاستدعاء والتسجيل (مؤشر ضعيف، قابل للضبط أدناه).

تنبيه: الطائرات العسكرية كثيراً لا تبث ADS-B أصلاً، لذا غيابها من
البيانات لا يعني بالضرورة أنها "أغلقت" جهاز التعريف.
"""
from __future__ import annotations

from models import AircraftState

MILITARY = "military"
CIVILIAN = "civilian"
UNKNOWN = "unknown"

# رموز أنواع ICAO العسكرية المعروفة (مقاتلات، نقل، تدريب، مروحيات، استطلاع...)
MILITARY_TYPES = {
    # مقاتلات / هجومية
    "F16", "F15", "F18", "F35", "F14", "F22", "F4", "F5", "F8", "F84", "F86",
    "F100", "F104", "F105", "F111", "A10", "A4", "A7", "A37", "AV8", "HAR",
    "TORN", "RFAL", "TYPH", "M2000", "MIR2", "MIR3", "MIG21", "MIG23", "MIG25",
    "MIG29", "MIG31", "SU24", "SU25", "SU27", "SU30", "SU35", "JF17", "J10",
    "J11", "J20", "GRIP", "LCA", "HAWK", "MB339", "ALPH", "G115", "PC7",
    # نقل / تموين جوي
    "C130", "C17", "C5", "C295", "C27J", "A400", "C390", "KC135", "KC130",
    "KC46", "IL76", "IL78", "AN12", "AN26", "AN32", "AN70", "C212", "C23",
    "C26", "C47", "C160", "TRANSALL", "A330MRTT", "A400M",
    # مروحيات
    "AH64", "AH1", "AH6", "CH47", "CH53", "CH46", "UH60", "UH1", "S70",
    "EC725", "AS332", "SA330", "MI8", "MI17", "MI24", "MI26", "MI28",
    "KA50", "KA52", "W3", "W30", "AW101", "NH90", "TIGER", "APACH", "COBRA",
    # إنذار مبكر / استطلاع / مسيرات
    "E3", "E2", "E7", "E8", "A50", "P3", "P1", "IL38", "TU95", "TU142",
    "RC135", "U2", "SR71", "RQ4", "MQ9", "MQ1", "RPR", "PRED", "HERM",
    "HERMES", "WATCH", "SCAN", "EAGLE", "GLOBALHAWK",
}

# أنواع مدنية شائعة (خطوط رحلات / طيران عام) — تستخدم للتأكيد فقط
CIVILIAN_TYPES = {
    "A320", "A321", "A319", "A318", "A330", "A340", "A350", "A380", "B738",
    "B737", "B739", "B77W", "B772", "B773", "B788", "B789", "B744", "B748",
    "B763", "B764", "E190", "E195", "E170", "E175", "CRJ9", "CRJ7", "DH8D",
    "AT76", "A20N", "A21N", "B38M", "B39M", "BCS1", "BCS3", "A223", "A225",
    "C172", "C206", "PA28", "SR20", "DA40", "DA42", "P28A", "C150", "C152",
}

# كلمات في اسم المشغّل أو النموذج تدل على جهة عسكرية
MILITARY_KEYWORDS = (
    "air force", "military", "army", "navy", "defence", "defense",
    "قوات", "جيش", "دفاع", "سلاح الجو",
)

# نداءات خطوط الإمارات المدنية المعروفة (UAE = طيران الإمارات، وليس عسكرياً)
UAE_CIVIL_CALLSIGNS = ("UAE", "ETD", "FDB", "ABY", "THY", "QTR", "KAL", "CPA", "SIA", "UAE")

# أنماط تسجيل/نداء عسكرية محتملة — عدّلها حسب احتياجك
# لا تضف "AF" هنا لأنها تتطابق مع AFR (الخطوط الجوية الفرنسية) مثلاً
MILITARY_CALL_PATTERNS = ("RASHD", "FALCN", "MIL", "ARMY", "NAVY")


def _normalize_type(value: str | None) -> str:
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def classify(state: AircraftState) -> str:
    """يرجع military / civilian / unknown."""
    type_code = _normalize_type(state.aircraft_type)
    haystack = " ".join(
        str(value or "").lower()
        for value in (state.model, state.manufacturer, state.origin_country)
    )
    callsign = (state.callsign or "").upper()
    registration = (state.registration or "").upper()

    # 1) رمز النوع — أقوى مؤشر
    if type_code in MILITARY_TYPES:
        return MILITARY
    # متغيّرات مثل F16C أو C130J: نستخدم startswith لتجنّب تطابق EC130 مع C130
    military_prefixes = ("F16", "F15", "F18", "F35", "C130", "KC135", "AH64", "MQ9")
    if any(
        type_code.startswith(prefix) and len(type_code) <= len(prefix) + 2
        for prefix in military_prefixes
    ):
        return MILITARY

    # 2) كلمات المشغّل/النموذج
    if any(word in haystack for word in MILITARY_KEYWORDS):
        return MILITARY

    # 3) أنماط النداء العسكرية (نتجنب UAE لأنها طيران الإمارات)
    if any(callsign.startswith(prefix) for prefix in MILITARY_CALL_PATTERNS):
        return MILITARY
    if registration.startswith(("MIL", "AF-")):
        return MILITARY

    # 4) مؤشرات مدنية
    if type_code in CIVILIAN_TYPES:
        return CIVILIAN
    if any(callsign.startswith(prefix) for prefix in UAE_CIVIL_CALLSIGNS):
        return CIVILIAN
    if registration.startswith("A6-"):
        return CIVILIAN

    return UNKNOWN


# ---------------------------------------------------------------------------
# تنبؤ احتمالي بالطابع العسكري (لا يعتمد على نوع الطائرة وحده)
# ---------------------------------------------------------------------------
VERDICTS = (
    ("military", "عسكرية (مؤكد)", 0.80),
    ("likely_military", "عسكرية مرجّحة", 0.60),
    ("uncertain", "غير حاسمة", 0.40),
    ("likely_civilian", "مدنية مرجّحة", 0.20),
    ("civilian", "مدنية (مؤكد)", 0.00),
)

# نطاقات أكواد IFF/Squawk المستخدمة عسكرياً في كثير من الدول (مؤشر ضعيف)
MILITARY_SQUAWKS = ("4000", "4001", "4100", "4200", "4300", "4400", "4500", "4600", "4700")


def _verdict(score: float) -> tuple[str, str]:
    for key, text, threshold in VERDICTS:
        if score >= threshold:
            return key, text
    return "civilian", "مدنية (مؤكد)"


def assess(state: AircraftState) -> dict:
    """تقييم احتمالي (0..1) لكون الطائرة عسكرية، مع أسباب مقروءة.

    يعتمد على دراسة الحركة وبيانات التعريف معاً:
    نوع الطائرة، المشغّل/النموذج، النداء، التسجيل، وجود نداء أصلاً،
    وأكواد Squawk العسكرية، ونمط الطيران (ارتفاع منخفض جداً/دوران).
    """
    type_code = _normalize_type(state.aircraft_type)
    haystack = " ".join(
        str(value or "").lower()
        for value in (state.model, state.manufacturer, state.origin_country)
    )
    callsign = (state.callsign or "").upper().strip()
    registration = (state.registration or "").upper().strip()
    score = 0.0
    reasons: list[str] = []

    # 1) نوع الطائرة
    if type_code in MILITARY_TYPES:
        score += 0.85
        reasons.append(f"نوع عسكري معروف ({state.aircraft_type})")
    elif type_code in CIVILIAN_TYPES:
        score -= 0.70
        reasons.append(f"نوع مدني شائع ({state.aircraft_type})")

    # 2) المشغّل/النموذج
    if any(word in haystack for word in MILITARY_KEYWORDS):
        score += 0.55
        reasons.append("اسم المشغّل/النموذج يشير لجهة عسكرية")

    # 3) النداء
    if callsign:
        if any(callsign.startswith(prefix) for prefix in MILITARY_CALL_PATTERNS):
            score += 0.50
            reasons.append(f"نداء عسكري ({callsign})")
        elif any(callsign.startswith(prefix) for prefix in UAE_CIVIL_CALLSIGNS):
            score -= 0.55
            reasons.append(f"نداء خطوط مدنية ({callsign})")
        elif callsign.replace("-", "").isalnum() and len(callsign) <= 4 and not callsign[:3].isalpha():
            score += 0.15
            reasons.append("نداء غير قياسي")
    else:
        # طائرات كثيرة عسكرية تبث بلا نداء، لكن هذا وحده لا يكفي
        score += 0.20
        reasons.append("لا تبث نداءً (شائع لدى العسكري)")

    # 4) التسجيل
    if registration:
        if registration.startswith(("MIL", "AF-")):
            score += 0.50
            reasons.append(f"تسجيل عسكري ({registration})")
        elif registration.startswith("A6-"):
            score -= 0.55
            reasons.append("تسجيل مدني إماراتي (A6-)")
    elif callsign:
        score += 0.10
        reasons.append("لا تسجيل معلن")

    # 5) أكواد Squawk
    if (state.squawk or "") in MILITARY_SQUAWKS:
        score += 0.25
        reasons.append(f"squawk عسكري ({state.squawk})")

    # 6) نمط الحركة
    alt = state.altitude_ft
    speed = state.speed_kts
    if alt is not None and not state.on_ground:
        if alt < 500 and (speed or 0) > 200:
            score += 0.20
            reasons.append("طيران منخفض جداً بسرعة عالية (نمط تدريب/اعتراض)")
        elif alt > 45000:
            score += 0.15
            reasons.append("ارتفاع تشغيلي عالٍ غير معتاد للمدني")

    score = max(0.0, min(1.0, round(score, 2)))
    key, text = _verdict(score)
    return {
        "icao24": state.icao24,
        "label": state.display_name,
        "score": score,
        "verdict": key,
        "verdict_text": text,
        "reasons": reasons[:4],
    }


def predict_military(state: AircraftState) -> tuple[bool, float, list[str]]:
    """نسخة مختصرة: (هل عسكرية؟، الثقة، الأسباب)."""
    result = assess(state)
    return result["verdict"] in ("military", "likely_military"), result["score"], result["reasons"]


def label(classification: str) -> str:
    return {MILITARY: "عسكرية", CIVILIAN: "مدنية", UNKNOWN: "غير مصنّفة"}.get(
        classification, "غير مصنّفة"
    )


def _uae_match(
    icao24: str | None,
    callsign: str | None,
    registration: str | None,
    model: str | None = None,
    manufacturer: str | None = None,
) -> bool:
    """المنطق المشترك لتمييز الطائرات الإماراتية/المستأجرة إماراتياً."""
    from config import settings  # استيراد مؤخر لتجنّب الدورة

    registration_text = (registration or "").upper().strip()
    callsign_text = (callsign or "").upper().strip()
    text = f"{model or ''} {manufacturer or ''}".lower()

    if (icao24 or "").lower() in settings.uae_watch_icao24:
        return True
    if registration_text.startswith("A6-"):
        return True
    if callsign_text.startswith(("UAE", "ETD", "FDB", "ABY")):
        return True
    if any(word in text for word in ("emirates", "etihad", "flydubai", "air arabia")):
        return True
    return False


def is_uae_related(state: AircraftState) -> bool:
    """هل الطائرة إماراتية أو مرتبطة بالإمارات (مستأجرة/مشغّلة من قِبل جهة إماراتية)؟

    المعايير كلها من بيانات عامة: التسجيل A6-، نداءات الخطوط الإماراتية،
    أو قائمة صريحة يحددها المستخدم في .env (UAE_WATCH_ICAO24).
    """
    return _uae_match(
        state.icao24, state.callsign, state.registration, state.model, state.manufacturer
    )


def is_uae_related_event(event) -> bool:
    """نفس المعايير لأحداث فقدان الإشارة (لا تحتوي كل حقول الحالة)."""
    return _uae_match(
        getattr(event, "icao24", None),
        getattr(event, "callsign", None),
        getattr(event, "registration", None),
        getattr(event, "model", None),
        getattr(event, "manufacturer", None),
    )
