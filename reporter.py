"""توليد تقارير تليجرام (صيغة HTML آمنة)."""
from __future__ import annotations

import html
from collections import Counter
from datetime import datetime, timedelta, timezone

from airports import country_name
from classifier import label
from config import settings
from models import AircraftState, LandingEvent
from tracker import AircraftTracker

REASON_LABELS = {
    "no_broadcast": "غياب بث داخل المنطقة",
    "left_coverage": "على الأرجح خرجت من نطاق الاستقبال",
    "unknown": "سبب غير محدد",
}


def _esc(value) -> str:
    return html.escape(str(value if value not in (None, "") else "—"))


def _fmt_time(dt: datetime) -> str:
    return dt.astimezone().strftime("%H:%M:%S")


def _aircraft_line(state: AircraftState, classification: str) -> str:
    alt = f"{state.altitude_ft:,} قدم" if state.altitude_ft is not None else "—"
    spd = f"{state.speed_kts} عقدة" if state.speed_kts is not None else "—"
    pos = (
        f"{state.latitude:.2f}°N {state.longitude:.2f}°E"
        if state.latitude is not None and state.longitude is not None else "—"
    )
    extra = ""
    if state.registration:
        extra += f" · {_esc(state.registration)}"
    if state.aircraft_type:
        extra += f" · {_esc(state.aircraft_type)}"
    return (
        f"  • <b>{_esc(state.display_name)}</b> [{_esc(label(classification))}]{extra}\n"
        f"    {pos} · {alt} · {spd}\n"
    )


def _landing_line(item: LandingEvent) -> str:
    tag = " 🇦🇪 مرتبطة بالإمارات" if item.uae_related else ""
    name = item.callsign or item.icao24
    if item.registration:
        name += f" / {item.registration}"
    frm = f"{item.from_airport or '—'} ({country_name(item.from_country_code)})"
    to = f"{item.airport_ident or '—'} ({country_name(item.airport_country_code)})"
    return (
        f"  • <b>{_esc(name)}</b> [{_esc(label(item.classification))}]{_esc(tag)}\n"
        f"    {_esc(frm)} → <b>{_esc(to)}</b> · {_fmt_time(item.at)}"
    )


def build_report(tracker: AircraftTracker, period_minutes: int | None = None) -> str:
    period = period_minutes or settings.report_interval_minutes
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=period)
    counts = tracker.counts()
    events = tracker.events_since(since)
    landings = tracker.landings_since(since)

    lines: list[str] = []
    lines.append("🛩️ <b>تقرير مراقبة الطائرات — الإمارات</b>")
    lines.append(f"🕐 {_fmt_time(since)} → {_fmt_time(now)} (آخر {period} دقيقة)\n")

    lines.append("📊 <b>الملخص</b>")
    lines.append(f"  • المرصودة حالياً: <b>{counts['current']}</b> طائرة")
    lines.append(
        f"  • عسكرية: <b>{counts['military']}</b> · مدنية: <b>{counts['civilian']}</b> "
        f"· غير مصنّفة: <b>{counts['unknown']}</b>"
    )
    lines.append(
        f"  • انقطاع إشارة: <b>{len(events)}</b> · هبوطات: {len(landings)} "
        f"· وجهات غير الإمارات: <b>{sum(1 for item in landings if item.divergence)}</b>"
    )
    lines.append(f"  • عدد الجلبات: {tracker.poll_count}\n")

    # ── التركيز الأساسي: الدولة التي انقطع فيها الإشارة ──
    by_country: Counter[str] = Counter(event.country or "غير محدد" for event in events)
    lines.append("🌍 <b>الدول التي انقطع فيها الإشارة (التركيز الرئيسي)</b>")
    if by_country:
        for country, count in by_country.most_common():
            lines.append(f"  • <b>{_esc(country)}</b>: {count} طائرة")
    else:
        lines.append("  • لا توجد حالات انقطاع خلال هذه الفترة.")
    lines.append("")

    # تفاصيل حالات الانقطاع — الدولة أولاً
    lines.append("📡 <b>تفاصيل فقدان الإشارة ADS-B</b>")
    if events:
        for event in events[: settings.max_report_aircraft]:
            when = _fmt_time(event.last_seen)
            country = event.country or "دولة غير محددة"
            position = (
                f"{event.last_lat:.2f}°N {event.last_lon:.2f}°E"
                if event.last_lat is not None and event.last_lon is not None else "—"
            )
            airport = f" · أقرب مطار: {event.nearest_airport}" if event.nearest_airport else ""
            lines.append(
                f"  • <b>{_esc(event.callsign or event.icao24)}</b> "
                f"[{_esc(label(event.classification))}] — <b>{_esc(country)}</b>\n"
                f"    آخر ظهور {when} · {position}{_esc(airport)} · "
                f"{_esc(REASON_LABELS.get(event.reason, event.reason))}"
            )
        if len(events) > settings.max_report_aircraft:
            lines.append(f"  … و {len(events) - settings.max_report_aircraft} حدث آخر")
    else:
        lines.append("  • لا توجد أحداث فقدان إشارة خلال هذه الفترة.")
    lines.append("")

    # ── الطائرات التي هبطت في الخارج ثم أخذت وجهة غير الإمارات ──
    diverged = [item for item in landings if item.divergence]
    uae_diverged = [item for item in diverged if item.uae_related]
    lines.append("✈️ <b>هبطت في الخارج ثم تغيّرت وجهتها إلى غير الإمارات</b>")
    if uae_diverged:
        lines.append("  <u>طائرات إماراتية أو مستأجرة من قِبل جهات إماراتية:</u>")
        for item in uae_diverged[: settings.max_report_aircraft]:
            lines.append(_landing_line(item))
    other_diverged = [item for item in diverged if not item.uae_related]
    if other_diverged:
        lines.append("  <u>طائرات أخرى:</u>")
        for item in other_diverged[: settings.max_report_aircraft]:
            lines.append(_landing_line(item))
    if not diverged:
        lines.append("  • لا توجد حالات هبوط في الخارج تلتها وجهة غير الإمارات.")
    lines.append("")

    # الطائرات العسكرية المرصودة
    military = [
        (state, tracker.classifications.get(icao, "unknown"))
        for icao, state in tracker.current.items()
        if tracker.classifications.get(icao) == "military"
    ]
    military.sort(key=lambda item: item[0].display_name)
    lines.append("🛡️ <b>الطائرات العسكرية المرصودة</b>")
    if military:
        for state, classification in military[: settings.max_report_aircraft]:
            lines.append(_aircraft_line(state, classification).rstrip())
        if len(military) > settings.max_report_aircraft:
            lines.append(f"  … و {len(military) - settings.max_report_aircraft} طائرة أخرى")
    else:
        lines.append("  • لا توجد طائرات عسكرية مرصودة في هذه اللحظة.")
    lines.append("")

    # أكثر نداءات الاستدعاء تكراراً
    prefixes = Counter(
        (state.callsign or "")[:3]
        for state in tracker.current.values()
        if state.callsign
    )
    if prefixes:
        top = " · ".join(f"{_esc(key)} ({count})" for key, count in prefixes.most_common(6))
        lines.append(f"🔤 <b>أكثر نداءات الاستدعاء</b>: {top}\n")

    # حالة المصادر
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
        "في المستقبلات. الوجهة اللاحقة مُستنتَجة من مكان الهبوط التالي، وليست "
        "من خطة طيران رسمية. كما أن كثيراً من الطائرات العسكرية لا تبث "
        "ADS-B أصلاً.</i>"
    )
    return "\n".join(lines)


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
        "",
        "🔌 <b>المصادر</b>",
    ]
    for source in tracker.sources:
        state = "✅ يعمل" if source.health.ok else "⚠️ متوقف"
        detail = source.health.message if source.health.message else "—"
        lines.append(f"  • {_esc(source.name)}: {state} ({source.health.aircraft_count} طائرة)")
        if detail:
            lines.append(f"    <code>{_esc(detail)}</code>")
    return "\n".join(lines)
