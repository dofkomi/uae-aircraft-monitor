"""توليد تقارير تليجرام (صيغة HTML آمنة) — نسخة مفصّلة."""
from __future__ import annotations

import html
from collections import Counter
from datetime import datetime, timedelta, timezone

from airports import country_name, haversine_km
from classifier import assess, is_uae_related, is_uae_related_event, label
from config import settings
from country_shapes import default_shapes, flag_emoji
from models import AircraftState, LandingEvent, SignalEvent
from tracker import AircraftTracker

REASON_LABELS = {
    "no_broadcast": "غياب بث داخل المنطقة",
    "left_coverage": "على الأرجح خرجت من نطاق الاستقبال",
    "unknown": "سبب غير محدد",
}

EMERGENCY_SQUAWKS = {
    "7500": "اختطاف",
    "7600": "فقدان اتصال لاسلكي",
    "7700": "طوارئ عامة",
}

_COMPASS = (
    "شمال", "شمال-شرق", "شرق", "جنوب-شرق",
    "جنوب", "جنوب-غرب", "غرب", "شمال-غرب",
)


def _esc(value) -> str:
    return html.escape(str(value if value not in (None, "") else "—"))


def _fmt_time(dt: datetime) -> str:
    return dt.astimezone().strftime("%H:%M:%S")


def _fmt_date(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d")


def _ago(dt: datetime, now: datetime) -> str:
    seconds = max(0, int((now - dt).total_seconds()))
    if seconds < 60:
        return f"منذ {seconds} ثانية"
    minutes = seconds // 60
    if minutes < 60:
        return f"منذ {minutes} دقيقة"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"منذ {hours}س {minutes}د"
    days, hours = divmod(hours, 24)
    return f"منذ {days} يوم {hours}س"


def _compass(deg: float | None) -> str:
    if deg is None:
        return "—"
    return _COMPASS[int((deg % 360) / 45 + 0.5) % 8]


def _fmt_alt(ft: int | None) -> str:
    if ft is None:
        return "—"
    return f"{ft:,} قدم ({int(ft * 0.3048):,} م)"


def _fmt_speed(kts: int | None) -> str:
    if kts is None:
        return "—"
    return f"{kts:,} عقدة ({int(kts * 1.852):,} كم/س)"


def _fmt_vrate(vr_ms: float | None) -> str:
    if vr_ms is None:
        return "—"
    fpm = int(vr_ms * 196.85)
    if fpm > 100:
        return f"صعود {fpm:,} قدم/د"
    if fpm < -100:
        return f"هبوط {abs(fpm):,} قدم/د"
    return "مستقر أفقياً"


def _fmt_dist(km: float | None) -> str:
    if km is None:
        return "—"
    if km < 10:
        return f"{km:.1f} كم"
    return f"{int(km):,} كم"


def _fmt_leg(minutes: int | None) -> str:
    if minutes is None:
        return "—"
    hours, mins = divmod(max(0, minutes), 60)
    if hours:
        return f"{hours}س {mins}د"
    return f"{mins} دقيقة"


def _fmt_pos(lat: float | None, lon: float | None) -> str:
    if lat is None or lon is None:
        return "—"
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{abs(lat):.4f}°{ns} {abs(lon):.4f}°{ew}"


def _country_text(lat: float | None, lon: float | None) -> str:
    """اسم الدولة (مع العلم) من حدود محلية — بديل عرض الإحداثيات وحدها."""
    try:
        return default_shapes().country_label(lat, lon)
    except Exception:  # noqa: BLE001 - لا نُفشل التقرير بسبب خريطة تالفة
        return "—"


def _squawk_tag(squawk: str | None) -> str:
    if not squawk:
        return ""
    if squawk in EMERGENCY_SQUAWKS:
        return f" 🚨 <b>طوارئ {squawk}</b> ({EMERGENCY_SQUAWKS[squawk]})"
    return f" · 🔢 {squawk}"


def _route_line(route) -> str:
    """سطر المسار المجدول من OSINT: «🛫 دبي DXB → لندن LHR · Emirates»."""
    if route is None:
        return ""
    origin = route.origin_label
    dest = route.dest_label
    text = f"🛫 {_esc(origin)} → {_esc(dest)}"
    if route.airline:
        text += f" · {_esc(route.airline)}"
    text += " <i>(مسار مجدول — adsbdb)</i>"
    return text


def _aircraft_detail(
    state: AircraftState,
    classification: str,
    now: datetime,
    center: tuple[float, float],
) -> str:
    """سطر تفصيلي متعدد الأسطر لطائرة حالية."""
    name = _esc(state.display_name)
    flags = ""
    if is_uae_related(state):
        flags += " 🇦🇪"
    if state.squawk in EMERGENCY_SQUAWKS:
        flags += " 🚨"

    identity: list[str] = []
    if state.registration:
        identity.append(_esc(state.registration))
    if state.aircraft_type:
        identity.append(_esc(state.aircraft_type))
    if state.model and state.model != state.aircraft_type:
        identity.append(_esc(state.model))
    ident_text = " · ".join(identity)

    position = _fmt_pos(state.latitude, state.longitude)
    if state.latitude is not None and state.longitude is not None:
        dist = haversine_km(
            state.latitude, state.longitude, center[0], center[1]
        )
        position += f" · يبعد {_fmt_dist(dist)} عن مركز المراقبة"

    source = state.source or "—"
    contact = (
        _ago(datetime.fromtimestamp(state.last_contact, tz=timezone.utc), now)
        if state.last_contact else "—"
    )
    alt_text = "🛬 على الأرض" if state.on_ground else _fmt_alt(state.altitude_ft)
    vrate_text = "—" if state.on_ground else _fmt_vrate(state.vertical_rate)

    lines = [
        f"  • <b>{name}</b> [{_esc(label(classification))}]{flags}"
        + (f" · {ident_text}" if ident_text else ""),
        f"    🌍 {_esc(_country_text(state.latitude, state.longitude))}"
        f" · 📍 {position} · 🧭 {_compass(state.true_track)}",
        f"    ⛰️ {alt_text} · ⚡ {_fmt_speed(state.speed_kts)} "
        f"· ↕️ {vrate_text}",
        f"    📡 {_esc(source)} · 🕐 آخر إشارة {contact}{_squawk_tag(state.squawk)}",
    ]
    route_line = _route_line(state.route)
    if route_line:
        lines.append(f"    {route_line}")
    return "\n".join(lines)


def _event_country_label(event: SignalEvent) -> str:
    """دولة الانقطاع مع العلم، وتوضيح إن كانت «الدولة القريبة» لا دولة الموقع."""
    country = event.country
    if not country:
        country = _country_text(event.last_lat, event.last_lon)
        if country in ("—", ""):
            country = "دولة غير محددة"
    flag = flag_emoji(event.country_code) if event.country_code else ""
    text = f"{flag} {_esc(country)}".strip()
    if event.geo_source == "nearest_country":
        text += " <i>(الدولة القريبة)</i>"
    return text


def _event_detail(event: SignalEvent, now: datetime) -> str:
    name = event.callsign or event.icao24
    registration = f" / {event.registration}" if event.registration else ""
    lines = [
        f"  • <b>{_esc(name)}</b>{_esc(registration)} "
        f"[{_esc(label(event.classification))}] — <b>{_event_country_label(event)}</b>",
        f"    🕐 آخر ظهور {_fmt_time(event.last_seen)} ({_ago(event.last_seen, now)}) "
        f"· 📍 {_fmt_pos(event.last_lat, event.last_lon)}",
        f"    ⛰️ {_fmt_alt(event.last_alt_ft)} · ⚡ {_fmt_speed(event.last_speed_kts)} "
        f"· {'🛬 على الأرض' if event.last_on_ground else '🛫 في الجو'}",
    ]
    if event.nearest_airport:
        airport = event.nearest_airport
        if event.nearest_airport_name:
            airport += f" ({event.nearest_airport_name})"
        country_text = country_name(event.nearest_airport_country)
        distance = f" — {_fmt_dist(event.nearest_airport_km)}" if event.nearest_airport_km else ""
        lines.append(
            f"    🛬 أقرب مطار: {_esc(airport)} · {_esc(country_text)}{distance}"
        )
    reason = REASON_LABELS.get(event.reason, event.reason)
    source = f" · 📡 {_esc(event.last_source)}" if event.last_source else ""
    lines.append(f"    📶 السبب التقديري: {_esc(reason)}{source}")
    route_line = _route_line(event.route)
    if route_line:
        lines.append(f"    {route_line}")
    if event.still_broadcasting:
        where = event.global_country or _country_text(event.global_lat, event.global_lon)
        seen = ""
        if event.global_last_seen:
            seen = f" · آخر بث {_fmt_time(event.global_last_seen)}"
        alt = f" · {_fmt_alt(event.global_alt_ft)}" if event.global_alt_ft else ""
        lines.append(
            f"    🔎 <b>OSINT (adsb.lol):</b> الإشارة لم تتوقف — الطائرة تُرصد "
            f"الآن في {_esc(where)}{alt}{seen}. الأرجح خروجها من نطاق مستقبلاتنا."
        )
    return "\n".join(lines)


def _landing_detail(item: LandingEvent) -> str:
    tag = " 🇦🇪 مرتبطة بالإمارات" if item.uae_related else ""
    name = item.callsign or item.icao24
    if item.registration:
        name += f" / {item.registration}"
    to = f"{item.airport_ident or '—'} ({country_name(item.airport_country_code)})"
    if item.airport_name:
        to = f"{item.airport_ident or '—'} — {item.airport_name} ({country_name(item.airport_country_code)})"

    if item.from_airport:
        frm = f"{item.from_airport}"
        if item.from_airport_name:
            frm += f" — {item.from_airport_name}"
        frm += f" ({country_name(item.from_country_code)})"
        route = f"{_esc(frm)} → <b>{_esc(to)}</b>"
    else:
        route = f"الهبوط في <b>{_esc(to)}</b>"

    leg = []
    if item.leg_minutes is not None:
        leg.append(f"⏱️ مدة الرحلة {_fmt_leg(item.leg_minutes)}")
    if item.leg_distance_km is not None:
        leg.append(f"📏 مسافة مباشرة {_fmt_dist(item.leg_distance_km)}")
    leg_text = " · ".join(leg)

    lines = [
        f"  • <b>{_esc(name)}</b> [{_esc(label(item.classification))}]{tag}",
        f"    {route}",
        f"    🕐 {_fmt_time(item.at)} · 📍 {_fmt_pos(item.latitude, item.longitude)}"
        + (f" · {leg_text}" if leg_text else ""),
    ]
    if item.divergence:
        lines.append("    ⚠️ <b>هبوط خارجي تلته وجهة غير الإمارات</b>")
    route_line = _route_line(item.route)
    if route_line:
        lines.append(f"    {route_line}")
    if item.route_mismatch:
        lines.append(
            f"    ⚠️ <b>مغادرة عن المسار المجدول:</b> الوجهة المعلنة "
            f"{_esc(item.route.dest_icao)} والهبوط الفعلي {_esc(item.airport_ident)}"
        )
    return "\n".join(lines)


def _percent(count: int, total: int) -> str:
    if total <= 0:
        return "0%"
    return f"{count / total * 100:.0f}%"


def build_report(tracker: AircraftTracker, period_minutes: int | None = None) -> str:
    period = period_minutes or settings.report_interval_minutes
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=period)
    counts = tracker.counts()
    events = tracker.events_since(since)
    landings = tracker.landings_since(since)
    states = list(tracker.current.values())

    airborne = [s for s in states if not s.on_ground]
    on_ground = [s for s in states if s.on_ground]
    uae_related = [s for s in states if is_uae_related(s)]
    climbing = sum(1 for s in airborne if s.vertical_rate and s.vertical_rate > 0.5)
    descending = sum(1 for s in airborne if s.vertical_rate and s.vertical_rate < -0.5)
    level = len(airborne) - climbing - descending
    emergency = [s for s in states if s.squawk in EMERGENCY_SQUAWKS]

    altitudes = [s.altitude_ft for s in airborne if s.altitude_ft is not None]
    speeds = [s.speed_kts for s in states if s.speed_kts is not None]
    avg_alt = int(sum(altitudes) / len(altitudes)) if altitudes else None
    avg_speed = int(sum(speeds) / len(speeds)) if speeds else None
    max_alt_state = max(
        (s for s in airborne if s.altitude_ft is not None),
        key=lambda s: s.altitude_ft or 0,
        default=None,
    )
    max_speed_state = max(
        (s for s in states if s.speed_kts is not None),
        key=lambda s: s.speed_kts or 0,
        default=None,
    )

    diverged = [item for item in landings if item.divergence]
    uae_diverged = [item for item in diverged if item.uae_related]
    other_diverged = [item for item in diverged if not item.uae_related]
    unplanned = [item for item in landings if item.unplanned]
    def _event_country(event: SignalEvent) -> str:
        """دولة آخر موقع: المخزّنة، أو تُحدَّد محلياً، أو «فوق المياه»."""
        if event.country:
            return event.country
        text = _country_text(event.last_lat, event.last_lon)
        return "غير محدد" if text in ("—", "") else text

    # انقطاع الإشارة: التركيز على الطائرات الإماراتية/المستأجرة إماراتياً فقط
    uae_events = [event for event in events if is_uae_related_event(event)]
    other_events = [event for event in events if not is_uae_related_event(event)]
    by_country: Counter[str] = Counter(_event_country(event) for event in uae_events)

    center = (settings.center_lat, settings.center_lon)

    lines: list[str] = []
    lines.append("🛩️ <b>تقرير مراقبة الطائرات — الإمارات</b>")
    lines.append(
        f"🗓️ {_fmt_date(now)} · 🕐 {_fmt_time(since)} → {_fmt_time(now)} "
        f"(آخر {period} دقيقة)"
    )
    lines.append(
        f"📍 نطاق المراقبة: {settings.lamin:.2f}–{settings.lamax:.2f}N · "
        f"{settings.lomin:.2f}–{settings.lomax:.2f}E\n"
    )

    # ── الملخص التنفيذي ──
    lines.append("📊 <b>الملخص التنفيذي</b>")
    lines.append(f"  • إجمالي الطائرات المرصودة الآن: <b>{counts['current']}</b>")
    lines.append(
        f"  • التصنيف: عسكرية <b>{counts['military']}</b> · مدنية "
        f"<b>{counts['civilian']}</b> · غير مصنّفة <b>{counts['unknown']}</b>"
    )
    lines.append(
        f"  • الحالة: في الجو <b>{len(airborne)}</b> · على الأرض <b>{len(on_ground)}</b> "
        f"· 🇦🇪 مرتبطة بالإمارات <b>{len(uae_related)}</b>"
    )
    lines.append(
        f"  • الحركة: صعود {climbing} · هبوط {descending} · مستوى ثابت {level}"
    )
    if avg_alt is not None or avg_speed is not None:
        lines.append(
            f"  • المتوسط: ارتفاع {_fmt_alt(avg_alt)} · سرعة {_fmt_speed(avg_speed)}"
        )
    lines.append(
        f"  • انقطاع إشارة (إماراتية): <b>{len(uae_events)}</b> طائرة في "
        f"<b>{len(by_country)}</b> دولة · طائرات أخرى: <b>{len(other_events)}</b>"
    )
    lines.append(
        f"  • هبوطات: <b>{len(landings)}</b> · وجهات غير الإمارات: <b>{len(diverged)}</b> "
        f"· غير مبرمجة سابقاً: <b>{len(unplanned)}</b>"
    )
    if emergency:
        codes = ", ".join(sorted({s.squawk or "" for s in emergency}))
        lines.append(f"  • 🚨 أكواد طوارئ مرصودة: <b>{_esc(codes)}</b>")
    lines.append(f"  • عدد الجلبات: {tracker.poll_count}\n")

    # ── أبرز الملاحظات ──
    highlights: list[str] = []
    if by_country:
        country, count = by_country.most_common(1)[0]
        highlights.append(
            f"أكثر دولة انقطع فوقها الإشارة (طائرات إماراتية): <b>{_esc(country)}</b> "
            f"({count} طائرة · {_percent(count, len(uae_events))})"
        )
    if uae_diverged:
        highlights.append(
            f"<b>{len(uae_diverged)}</b> طائرة إماراتية/مستأجرة هبطت بالخارج "
            "ثم واصلت إلى وجهة غير الإمارات"
        )
    if emergency:
        names = ", ".join(s.display_name for s in emergency[:5])
        highlights.append(f"طائرات بأكواد طوارئ: <b>{_esc(names)}</b>")
    if max_alt_state is not None:
        highlights.append(
            f"أعلى طائرة: <b>{_esc(max_alt_state.display_name)}</b> على "
            f"{_fmt_alt(max_alt_state.altitude_ft)}"
        )
    if max_speed_state is not None and max_speed_state.speed_kts:
        highlights.append(
            f"أسرع طائرة: <b>{_esc(max_speed_state.display_name)}</b> بسرعة "
            f"{_fmt_speed(max_speed_state.speed_kts)}"
        )
    if highlights:
        lines.append("⭐ <b>أبرز الملاحظات</b>")
        for item in highlights:
            lines.append(f"  • {item}")
        lines.append("")

    # ── توزيع الطائرات الحالية حسب الدولة (تحديد محلي بلا استهلاك حصة) ──
    state_countries: Counter[str] = Counter()
    for state in states:
        if state.latitude is None or state.longitude is None:
            state_countries["📍 بدون موقع معروف"] += 1
            continue
        label_text = _country_text(state.latitude, state.longitude)
        if label_text == "—":
            label_text = "📍 دولة غير محددة"
        state_countries[label_text] += 1

    # ── التركيز الأساسي: الدولة التي انقطع فيها الإشارة ──
    lines.append("🗺️ <b>توزيع الطائرات الحالية حسب الدولة</b>")
    if state_countries:
        for country, count in state_countries.most_common(12):
            lines.append(
                f"  • {_esc(country)}: <b>{count}</b> طائرة "
                f"({_percent(count, len(states))})"
            )
        if len(state_countries) > 12:
            lines.append(f"  … و {len(state_countries) - 12} دولة أخرى")
    else:
        lines.append("  • لا توجد طائرات مرصودة حالياً.")
    lines.append("")

    # ── التركيز الأساسي: الدولة التي انقطع فيها الإشارة (طائرات إماراتية فقط) ──
    lines.append(
        "🌍 <b>الدول التي انقطع فيها الإشارة — الطائرات الإماراتية</b> "
        "<i>(التركيز الرئيسي)</i>"
    )
    if by_country:
        for country, count in by_country.most_common():
            lines.append(
                f"  • <b>{_esc(country)}</b>: {count} طائرة "
                f"({_percent(count, len(uae_events))})"
            )
    else:
        lines.append("  • لا توجد حالات انقطاع لطائرات إماراتية خلال هذه الفترة.")
    lines.append("")

    # ── تفاصيل فقدان الإشارة (طائرات إماراتية فقط) ──
    lines.append(
        "📡 <b>تفاصيل فقدان الإشارة ADS-B — الطائرات الإماراتية</b>"
    )
    if uae_events:
        ordered = sorted(
            uae_events, key=lambda e: (e.country or "zzz", -e.last_seen.timestamp())
        )
        for event in ordered[: settings.max_report_aircraft]:
            lines.append(_event_detail(event, now))
        if len(uae_events) > settings.max_report_aircraft:
            lines.append(f"  … و {len(uae_events) - settings.max_report_aircraft} حدث آخر")
    else:
        lines.append("  • لا توجد أحداث فقدان إشارة لطائرات إماراتية خلال هذه الفترة.")
    if other_events:
        lines.append(
            f"  <i>طائرات أخرى: {len(other_events)} حدث — تُسجَّل في قاعدة البيانات "
            "ولا تُفصَّل هنا (خارج نطاق التركيز).</i>"
        )
    lines.append("")

    # ── الهبوطات المرصودة ──
    lines.append("🛬 <b>الهبوطات المرصودة خلال الفترة</b>")
    if landings:
        for item in landings[: settings.max_report_aircraft]:
            lines.append(_landing_detail(item))
        if len(landings) > settings.max_report_aircraft:
            lines.append(f"  … و {len(landings) - settings.max_report_aircraft} هبوط آخر")
    else:
        lines.append("  • لا توجد هبوطات مرصودة خلال هذه الفترة.")
    lines.append("")

    # ── الطائرات التي هبطت بالخارج ثم غيّرت وجهتها ──
    lines.append("✈️ <b>هبطت في الخارج ثم تغيّرت وجهتها إلى غير الإمارات</b>")
    if uae_diverged:
        lines.append("  <u>طائرات إماراتية أو مستأجرة من قِبل جهات إماراتية:</u>")
        for item in uae_diverged[: settings.max_report_aircraft]:
            lines.append(_landing_detail(item))
    if other_diverged:
        lines.append("  <u>طائرات أخرى:</u>")
        for item in other_diverged[: settings.max_report_aircraft]:
            lines.append(_landing_detail(item))
    if not diverged:
        lines.append("  • لا توجد حالات هبوط في الخارج تلتها وجهة غير الإمارات.")
    lines.append("")

    # ── وجهات/رحلات غير مبرمجة سابقاً ──
    uae_unplanned = [item for item in unplanned if item.uae_related]
    lines.append("🆕 <b>وجهات غير مبرمجة سابقاً (توقّف مفاجئ خارج النمط)</b>")
    if unplanned:
        for item in uae_unplanned[: settings.max_report_aircraft]:
            reason = item.unplanned_reason or "غير معروف"
            lines.append(
                f"  • 🇦🇪 <b>{_esc(item.registration or item.callsign or item.icao24)}</b> — "
                f"{_esc(item.airport_name or item.airport_ident or 'مطار غير معروف')} "
                f"({_esc(item.airport_country_code or '—')}) · {_esc(reason)}"
            )
        others = [item for item in unplanned if not item.uae_related]
        if others:
            lines.append(
                f"  <i>طائرات أخرى بوجهات جديدة: {len(others)} — تُسجَّل في قاعدة البيانات.</i>"
            )
    else:
        lines.append("  • لا توجد هبوطات في مطارات جديدة أو مخالفة للمسار خلال الفترة.")
    lines.append("")

    # ── تنبؤ الطابع العسكري ──
    lines.append("🔎 <b>تنبؤ الطابع العسكري (حركة + بيانات تعريف)</b>")
    predictions = []
    for state in states:
        if tracker.classifications.get(state.icao24) == "military":
            continue  # المصنّفة عسكرياً تُعرض في قسمها أدناه
        result = assess(state)
        if result["score"] >= 0.40:
            predictions.append(result)
    predictions.sort(key=lambda item: item["score"], reverse=True)
    if predictions:
        lines.append(
            "  <i>تقدير احتمالي آلي من نمط الطيران والنداء والتسجيل والنوع — "
            "ليس تأكيداً.</i>"
        )
        for result in predictions[:10]:
            reasons = " · ".join(result["reasons"][:3]) or "لا مؤشرات قوية"
            lines.append(
                f"  • <b>{_esc(result['label'])}</b> — {_esc(result['verdict_text'])} "
                f"({int(result['score'] * 100)}%) · {_esc(reasons)}"
            )
    else:
        lines.append("  • لا توجد مؤشرات عسكرية كافية على الطائرات غير المصنّفة.")
    lines.append("")

    # ── الطائرات المرتبطة بالإمارات ──
    lines.append("🇦🇪 <b>الطائرات المرتبطة بالإمارات (حالياً)</b>")
    if uae_related:
        uae_related.sort(key=lambda s: (s.on_ground, s.display_name))
        for state in uae_related[: settings.max_report_aircraft]:
            lines.append(
                _aircraft_detail(
                    state, tracker.classifications.get(state.icao24, "unknown"), now, center
                )
            )
        if len(uae_related) > settings.max_report_aircraft:
            lines.append(
                f"  … و {len(uae_related) - settings.max_report_aircraft} طائرة أخرى"
            )
    else:
        lines.append("  • لا توجد طائرات مرتبطة بالإمارات مرصودة حالياً.")
    lines.append("")

    # ── الطائرات العسكرية ──
    military = [
        state for state in states
        if tracker.classifications.get(state.icao24) == "military"
    ]
    military.sort(key=lambda s: s.display_name)
    lines.append("🛡️ <b>الطائرات العسكرية المرصودة</b>")
    if military:
        for state in military[: settings.max_report_aircraft]:
            lines.append(
                _aircraft_detail(
                    state, tracker.classifications.get(state.icao24, "unknown"), now, center
                )
            )
        if len(military) > settings.max_report_aircraft:
            lines.append(f"  … و {len(military) - settings.max_report_aircraft} طائرة أخرى")
    else:
        lines.append("  • لا توجد طائرات عسكرية مرصودة في هذه اللحظة.")
    lines.append("")

    # ── أسرع/أعلى الطائرات ──
    top_speed = sorted(
        (s for s in states if s.speed_kts is not None),
        key=lambda s: s.speed_kts or 0,
        reverse=True,
    )[:5]
    top_alt = sorted(
        (s for s in airborne if s.altitude_ft is not None),
        key=lambda s: s.altitude_ft or 0,
        reverse=True,
    )[:5]
    if top_speed or top_alt:
        lines.append("🚀 <b>أبرز الطائرات</b>")
        if top_speed:
            lines.append(
                "  <u>الأسرع:</u> "
                + " · ".join(
                    f"{_esc(s.display_name)} ({_fmt_speed(s.speed_kts)})" for s in top_speed
                )
            )
        if top_alt:
            lines.append(
                "  <u>الأعلى:</u> "
                + " · ".join(
                    f"{_esc(s.display_name)} ({_fmt_alt(s.altitude_ft)})" for s in top_alt
                )
            )
        lines.append("")

    # ── توزيع دول التسجيل ──
    origins = Counter(
        s.origin_country for s in states if s.origin_country
    )
    if origins:
        top = " · ".join(
            f"{_esc(name)} ({count})" for name, count in origins.most_common(8)
        )
        lines.append(f"🏳️ <b>دول التسجيل المرصودة</b>: {top}\n")

    # ── أكثر نداءات الاستدعاء ──
    prefixes = Counter(
        (state.callsign or "")[:3]
        for state in states
        if state.callsign
    )
    if prefixes:
        top = " · ".join(
            f"{_esc(key)} ({count})" for key, count in prefixes.most_common(8)
        )
        lines.append(f"🔤 <b>أكثر نداءات الاستدعاء</b>: {top}\n")

    # ── نطاق الساحل الأفريقي والمسارات غير الطبيعية ──
    from sahel import build_matrix, sahel_scope_text, summarise

    matrix = build_matrix(tracker, 24)
    scope = summarise(tracker, 24, rows=matrix)
    lines.append("🛰️ <b>نطاق الساحل الأفريقي والمسارات غير الطبيعية</b>")
    lines.append(f"  <i>نطاق التركيز: {_esc(sahel_scope_text())}.</i>")
    if scope["events_by_zone"]:
        lines.append(
            "  <u>انقطاعات ضمن نطاق المتابعة (الساحل ودول العبور والممرات):</u> "
            + " · ".join(
                f"{_esc(item['zone'])} ({item['count']})"
                for item in scope["events_by_zone"]
            )
        )
    else:
        lines.append("  • لا انقطاعات مرصودة داخل نطاق المتابعة خلال 24 ساعة.")
    if scope["landings_by_zone"]:
        lines.append(
            "  <u>هبوطات ضمن نطاق المتابعة:</u> "
            + " · ".join(
                f"{_esc(item['zone'])} ({item['count']})"
                for item in scope["landings_by_zone"]
            )
        )
    lines.append(
        f"  <u>طائرات تجاوزت عتبة الشذوذ:</u> <b>{scope['flagged']}</b> "
        "من أصل ما رُصد داخل النطاق أو المرتبط بالإمارات."
    )
    if scope.get("coverage_exits"):
        lines.append(
            f"  <u>خروج من نطاق التغطية (ليس شذوذاً):</u> {scope['coverage_exits']} "
            "— طائرات غادرت مجال المستقبلات، لا دليل على إيقاف البث."
        )
    lines.append("")

    lines.append("🧾 <b>مصفوفة الاستخبارات — الحقول التسعة</b>")
    if matrix:
        for index, row in enumerate(matrix, start=1):
            lines.append(
                f"  <b>{index}) {_esc(row.icao24.upper())} · "
                f"{_esc(row.registration)}</b> — درجة الشذوذ "
                f"<b>{row.score}%</b> · ثقة {_esc(row.confidence)} · {_esc(row.zone)}"
            )
            lines.append(f"      • المشغّل المعلن: {_esc(row.operator)}")
            lines.append(f"      • طبقة التأجير الفعلية: {_esc(row.leasing_layer)}")
            lines.append(f"      • نقطة الدخول إلى الساحل: {_esc(row.entry_point)}")
            lines.append(f"      • إحداثيات الانقطاع: {_esc(row.blackout)}")
            lines.append(f"      • مهبط الهبوط: {_esc(row.landing)}")
            lines.append(
                "      • المؤشرات: "
                + ("؛ ".join(_esc(item) for item in row.indicators) or "—")
            )
            if row.sentinel_url:
                lines.append(f"      • تحقّق بصري (Sentinel-2): {_esc(row.sentinel_url)}")
            if row.earth_url:
                lines.append(f"      • Google Earth: {_esc(row.earth_url)}")
            lines.append("")
        lines.append(
            "  <i>مستوى الثقة يُشتق من عدد المؤشرات المستقلة لا من حجم الدرجة. "
            "«طبقة التأجير الفعلية» لا تكشفها بيانات ADS-B العامة؛ تبقى غير محددة "
            "حتى تُقابل بسجلات الطيران والعقود.</i>"
        )
    else:
        lines.append(
            "  • لا صفوف في المصفوفة: لم تُرصد أي رحلة داخل نطاق الساحل، ولم "
            "تتجاوز أي طائرة مرتبطة بالإمارات عتبة الشذوذ (20%) خلال 24 ساعة."
        )
    lines.append("")

    # ── حالة المصادر ──
    source_parts = []
    for source in tracker.sources:
        state = "✅" if source.health.ok else "⚠️"
        source_parts.append(f"{state} {_esc(source.name)} ({source.health.aircraft_count})")
    lines.append("🔌 <b>المصادر</b>: " + " · ".join(source_parts))
    lines.append("")
    lines.append(
        "<i>تنبيه: البيانات من شبكات ADS-B التطوعية العامة. «الدولة التي انقطع "
        "فوقها الإشارة» هي دولة آخر موقع مُستقبَل، ولا تعني بالضرورة أن جهاز "
        "التعريف أُغلق قصداً؛ قد يكون السبب خروجاً من نطاق الاستقبال أو فجوة "
        "في المستقبلات. الوجهة اللاحقة ومدة الرحلة مُستنتَجة من مكان الهبوط "
        "التالي، وليست من خطة طيران رسمية. المسافة المذكورة خط مباشر بين "
        "نقطتي الهبوط وليست مسار الرحلة الفعلي. كما أن كثيراً من الطائرات "
        "العسكرية لا تبث ADS-B أصلاً.</i>"
    )
    lines.append(
        "<i>إثراء OSINT مجاني: adsbdb (المسار المجدول من نداء الاستدعاء) و"
        "adsb.lol (البث العالمي للطائرة). المسار المجدول قد يكون قديماً أو "
        "غير محدّث، والوجهة الفعلية تُعرف من الهبوط المرصود لا من الجدول.</i>"
    )
    return "\n".join(lines)


async def build_messages(
    tracker: AircraftTracker,
    period_minutes: int | None = None,
    analyzer=None,
) -> list[str]:
    """رسالة تليجرام الوحيدة: الدراسة التحليلية المكتوبة.

    البيانات التفصيلية لم تعد تُعرض في المحادثة؛ تُولَّد في ملفَّي PDF و DOCX
    (انظر ``report_docs.send_documents``) مع تقرير 24 ساعة والاستنتاجات.
    """
    from ai_report import build_analysis

    # رسالة واحدة فقط: الدراسة التحليلية. البيانات التفصيلية لم تعد تُعرض
    # في تليجرام، بل تُحفظ في ملفَّي PDF و DOCX المرفقين مع كل تقرير.
    result = await build_analysis(tracker, period_minutes, analyzer)
    if result:
        return [result[0]]
    return [
        "🧠 <b>الدراسة التحليلية غير مفعّلة</b>\n"
        "AI_REPORT_MODE=off — التفاصيل الكاملة في ملفَّي PDF و DOCX المرفقين."
    ]


def build_status(tracker: AircraftTracker) -> str:
    counts = tracker.counts()
    last = tracker.last_poll.strftime("%H:%M:%S") if tracker.last_poll else "لم يحدث بعد"
    db_stats = tracker.store.stats()
    lines = [
        "📊 <b>حالة البوت</b>",
        f"  • آخر جلب: {last}",
        f"  • عدد الجلبات: {tracker.poll_count}",
        f"  • حالياً: {counts['current']} · معروف: {counts['known']}",
        f"  • عسكرية: {counts['military']} · مدنية: {counts['civilian']}",
        f"  • قاعدة البيانات: {db_stats['sightings']} رصد، "
        f"{db_stats['signal_events']} انقطاع، {db_stats['landings']} هبوط",
        f"  • تحديد الدولة: {'مفعّل' if (tracker.geo and tracker.geo.enabled) else 'معطّل'}",
        f"  • حدود الدول المحلية: "
        f"{'جاهزة' if (tracker.geo and tracker.geo.shapes and tracker.geo.shapes.ready) else 'غير متاحة'}",
        f"  • إثراء OSINT (مسار/بث عالمي): "
        f"{'مفعّل' if (tracker.osint and tracker.osint.enabled) else 'معطّل'}",
    ]
    try:
        from ai_report import AIAnalyzer

        analyzer = AIAnalyzer()
        ai_state = analyzer.describe() if analyzer.available else "معطّل (لا يوجد مفتاح)"
        lines.append(
            f"  • تقرير الذكاء الاصطناعي: {settings.ai_report_mode} · {_esc(ai_state)}"
        )
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  • تقرير الذكاء الاصطناعي: تعذّر الفحص ({_esc(exc)})")
    lines.extend([
        "",
        "🔌 <b>المصادر</b>",
    ])
    for source in tracker.sources:
        state = "✅ يعمل" if source.health.ok else "⚠️ متوقف"
        detail = source.health.message if source.health.message else "—"
        lines.append(f"  • {_esc(source.name)}: {state} ({source.health.aircraft_count} طائرة)")
        if detail:
            lines.append(f"    <code>{_esc(detail)}</code>")
    return "\n".join(lines)
