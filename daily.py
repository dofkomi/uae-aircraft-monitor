"""تقرير 24 ساعة: تجميع الأحداث والهبوطات والوجهات ثم الاستنتاجات.

لا يُعرض هذا التقرير في تليجرام؛ يُضمَّن مع الدراسة التحليلية في ملفَّي
PDF و DOCX (انظر ``report_docs``). الهدف: قراءة يومية كاملة بلا تكرار،
تنتهي باستنتاجات مسندة إلى أرقام.
"""

from __future__ import annotations

import html
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from classifier import assess, is_uae_related
from config import settings
from country_shapes import flag_emoji
from models import LandingEvent, SignalEvent

log = logging.getLogger("daily")

REASON_LABELS = {
    "no_broadcast": "غياب بث داخل المنطقة",
    "left_coverage": "على الأرجح خرجت من نطاق الاستقبال",
    "unknown": "سبب غير محدد",
}

HOUR = timedelta(hours=1)


# ---------------------------------------------------------------------------
def _esc(value) -> str:
    return html.escape(str(value if value is not None else "—"))


def _flag(code: str | None) -> str:
    return flag_emoji(code) or "🌐"


def _country(code: str | None, name: str | None) -> str:
    if name:
        return f"{_flag(code)} {_esc(name)}"
    if code:
        return f"{_flag(code)} {_esc(code)}"
    return "🌐 غير محددة"


def _hour_label(moment: datetime) -> str:
    return moment.strftime("%H:00")


# ---------------------------------------------------------------------------
def build_daily_snapshot(tracker, hours: int = 24) -> dict:
    """يجمع كل ما تحتاجه كتابة تقرير 24 ساعة في قاموس واحد."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    events: list[SignalEvent] = tracker.events_since(since)
    landings: list[LandingEvent] = tracker.landings_since(since)
    states = list(tracker.current.values())

    uae_events = [e for e in events if _is_uae_event(e)]
    uae_landings = [l for l in landings if l.uae_related]
    # البوت متخصّص في الطائرات الإماراتية وحدها؛ غيرها إحصاء سياقي.
    uae_states = [s for s in states if is_uae_related(s)]
    unplanned = [l for l in uae_landings if l.unplanned]
    diverged = [l for l in uae_landings if l.divergence]
    other_events = [e for e in events if not _is_uae_event(e)]
    other_landings = [l for l in landings if not l.uae_related]

    by_country = Counter()
    by_country_uae = Counter()
    by_reason = Counter()
    by_hour = Counter()
    for event in uae_events:
        label = event.country or event.country_code or "غير محددة"
        by_country[label] += 1
        by_country_uae[label] += 1
        by_reason[event.reason or "unknown"] += 1
        by_hour[event.last_seen.replace(minute=0, second=0, microsecond=0)] += 1

    land_by_airport = Counter()
    land_by_country = Counter()
    for landing in uae_landings:
        land_by_airport[
            (landing.airport_ident, landing.airport_name, landing.airport_country_code)
        ] += 1
        land_by_country[landing.airport_country_code or "غير محددة"] += 1

    global_seen = sum(1 for e in uae_events if e.still_broadcasting)
    suspects = []
    for state in uae_states:
        if tracker.classifications.get(state.icao24) == "military":
            continue
        result = assess(state)
        if result["score"] >= 0.40:
            suspects.append(result)
    suspects.sort(key=lambda item: item["score"], reverse=True)

    sahel = {}
    try:
        from sahel import build_matrix, sahel_scope_text, summarise

        matrix = build_matrix(tracker, hours)
        scope = summarise(tracker, hours, rows=matrix)
        sahel = {
            "scope": sahel_scope_text(),
            "events_by_zone": scope["events_by_zone"],
            "landings_by_zone": scope["landings_by_zone"],
            "flagged": scope["flagged"],
            "coverage_exits": scope.get("coverage_exits", 0),
            "top": scope["top"],
            "matrix": [row.as_dict() for row in matrix],
        }
    except Exception as exc:  # noqa: BLE001 - التقرير اليومي يكفي بدونه
        log.warning("تعذّر بناء ملخّص الساحل اليومي: %s", exc)

    return {
        "hours": hours,
        "sahel": sahel,
        "window": {
            "from": since.strftime("%Y-%m-%d %H:%M"),
            "to": now.strftime("%Y-%m-%d %H:%M"),
        },
        "totals": {
            "aircraft_now": len(uae_states),
            "airborne_now": sum(1 for s in uae_states if not s.on_ground),
            "military_now": sum(
                1 for s in uae_states
                if tracker.classifications.get(s.icao24) == "military"
            ),
            "events": len(uae_events),
            "events_uae": len(uae_events),
            "events_other": len(other_events),
            "events_still_global": global_seen,
            "landings": len(uae_landings),
            "landings_uae": len(uae_landings),
            "landings_other": len(other_landings),
            "unplanned": len(unplanned),
            "diverged": len(diverged),
            "unique_aircraft": len(
                {e.icao24 for e in uae_events} | {l.icao24 for l in uae_landings}
            ),
            "context_aircraft_now": len(states),
            "context_events": len(events),
            "context_landings": len(landings),
        },
        "events_by_country": [
            {"country": name, "count": count} for name, count in by_country.most_common(8)
        ],
        "events_by_country_uae": [
            {"country": name, "count": count}
            for name, count in by_country_uae.most_common(8)
        ],
        "events_by_reason": [
            {"reason": REASON_LABELS.get(key, key), "count": count}
            for key, count in by_reason.most_common()
        ],
        "activity_by_hour": [
            {"hour": moment.strftime("%H:00"), "events": count}
            for moment, count in sorted(by_hour.items())
        ],
        "landings_by_airport": [
            {
                "airport": ident or "غير معروف",
                "name": name,
                "country": code,
                "count": count,
            }
            for (ident, name, code), count in land_by_airport.most_common(10)
        ],
        "landings_by_country": [
            {"country": code, "count": count}
            for code, count in land_by_country.most_common(8)
        ],
        "uae_events": [_event_brief(e) for e in _recent(uae_events)[:12]],
        "unplanned": [_landing_brief(l) for l in _recent(landings=unplanned)[:10]],
        "diverged": [_landing_brief(l) for l in _recent(landings=diverged)[:8]],
        "military_suspects": [
            {
                "aircraft": item["label"],
                "score": item["score"],
                "verdict": item["verdict_text"],
                "reasons": item["reasons"],
            }
            for item in suspects[:8]
        ],
        "sources": [
            {"name": source.name, "ok": source.health.ok}
            for source in tracker.sources
        ],
    }


def _recent(
    events: list[SignalEvent] | None = None,
    landings: list[LandingEvent] | None = None,
) -> list:
    if events is not None:
        return sorted(events, key=lambda e: -e.last_seen.timestamp())
    if landings is not None:
        return sorted(landings, key=lambda l: -l.at.timestamp())
    return []


def _is_uae_event(event: SignalEvent) -> bool:
    from classifier import is_uae_related_event

    return is_uae_related_event(event)


def _event_brief(event: SignalEvent) -> dict:
    return {
        "callsign": event.callsign or event.icao24,
        "country": event.country,
        "country_code": event.country_code,
        "reason": REASON_LABELS.get(event.reason, event.reason),
        "minutes_ago": int(
            (datetime.now(timezone.utc) - event.last_seen).total_seconds() // 60
        ),
        "still_global": event.still_broadcasting,
        "global_country": event.global_country,
        "route": _route_text(event.route),
    }


def _landing_brief(landing: LandingEvent) -> dict:
    return {
        "callsign": landing.callsign or landing.icao24,
        "registration": landing.registration,
        "uae_related": landing.uae_related,
        "airport": landing.airport_ident,
        "airport_name": landing.airport_name,
        "country": landing.airport_country_code,
        "from": landing.from_airport,
        "at": landing.at.strftime("%H:%M"),
        "unplanned": landing.unplanned,
        "reason": landing.unplanned_reason,
    }


def _route_text(route) -> str | None:
    if not route:
        return None
    origin = getattr(route, "origin_icao", None) or getattr(route, "origin", None)
    dest = getattr(route, "dest_icao", None) or getattr(route, "destination", None)
    if origin and dest:
        return f"{origin} → {dest}"
    return None


# ---------------------------------------------------------------------------
def build_daily_report(tracker, hours: int = 24) -> str:
    """تقرير 24 ساعة مفصّل بصيغة HTML آمنة (للـ PDF/DOCX فقط)."""
    snap = build_daily_snapshot(tracker, hours)
    totals = snap["totals"]
    lines: list[str] = []

    lines.append(f"🗓️ <b>تقرير {hours} ساعة</b>")
    lines.append(
        f"  من {snap['window']['from']} إلى {snap['window']['to']} UTC · "
        f"{totals['unique_aircraft']} طائرة مختلفة ظهرت في الأحداث أو الهبوطات"
    )
    lines.append("")

    lines.append("📊 <b>الحصيلة الرقمية</b>")
    lines.append(f"  • أحداث فقدان إشارة: <b>{totals['events']}</b> "
                 f"(إماراتية <b>{totals['events_uae']}</b> · أخرى {totals['events_other']})")
    lines.append(f"  • ما تزال تبث عالمياً بعد الغياب المحلي: <b>{totals['events_still_global']}</b>")
    lines.append(f"  • هبوطات مرصودة: <b>{totals['landings']}</b> "
                 f"(إماراتية <b>{totals['landings_uae']}</b>)")
    lines.append(f"  • وجهات غير مبرمجة: <b>{totals['unplanned']}</b> · "
                 f"هبوط خارجي بوجهة غير إماراتية: <b>{totals['diverged']}</b>")
    lines.append("")

    lines.append("🇦🇪 <b>أين انقطعت إشارة الطائرات الإماراتية</b>")
    if snap["events_by_country_uae"]:
        for item in snap["events_by_country_uae"]:
            lines.append(f"  • {_country(None, item['country'])}: <b>{item['count']}</b>")
    else:
        lines.append("  • لا أحداث لطائرات إماراتية.")
    lines.append("")

    lines.append("⏱️ <b>توزّع الأحداث على ساعات اليوم</b>")
    if snap["activity_by_hour"]:
        peak = max(snap["activity_by_hour"], key=lambda item: item["events"])
        lines.append(
            "  • " + " · ".join(
                f"{item['hour']}={item['events']}" for item in snap["activity_by_hour"]
            )
        )
        lines.append(f"  • أعلى نشاط: <b>{peak['hour']}</b> بـ {peak['events']} حدثاً.")
    else:
        lines.append("  • لا بيانات ساعية.")
    lines.append("")

    lines.append("🧾 <b>أسباب الغياب</b>")
    if snap["events_by_reason"]:
        for item in snap["events_by_reason"]:
            lines.append(f"  • {_esc(item['reason'])}: <b>{item['count']}</b>")
    else:
        lines.append("  • لا أسباب مسجّلة.")
    lines.append("")

    lines.append("🛬 <b>أكثر المطارات استقبالاً للهبوطات</b>")
    if snap["landings_by_airport"]:
        for item in snap["landings_by_airport"]:
            name = item["name"] or item["airport"]
            lines.append(
                f"  • {_country(item['country'], None)} · <b>{_esc(name)}</b> "
                f"({_esc(item['airport'])}): <b>{item['count']}</b>"
            )
    else:
        lines.append("  • لا هبوطات مرصودة.")
    lines.append("")

    lines.append("✈️ <b>أحداث الطائرات الإماراتية (الأحدث)</b>")
    if snap["uae_events"]:
        for item in snap["uae_events"]:
            state = (
                f"ما تزال تبث عالمياً في {_esc(item['global_country'])}"
                if item["still_global"]
                else "لا رصد عالمي مطابق"
            )
            route = f" · مسار {_esc(item['route'])}" if item["route"] else ""
            lines.append(
                f"  • <b>{_esc(item['callsign'])}</b> — "
                f"{_country(item['country_code'], item['country'])} · "
                f"قبل {item['minutes_ago']} دقيقة · {state}{route}"
            )
    else:
        lines.append("  • لا أحداث لطائرات إماراتية.")
    lines.append("")

    lines.append("🆕 <b>وجهات غير مبرمجة (الأحدث)</b>")
    if snap["unplanned"]:
        for item in snap["unplanned"]:
            lines.append(
                f"  • <b>{_esc(item['callsign'])}</b> — "
                f"{_esc(item['airport_name'] or item['airport'])} "
                f"({_country(item['country'], None)}) · {_esc(item['at'])} · "
                f"{_esc(item['reason'] or 'سبب غير محدد')}"
            )
    else:
        lines.append("  • لا توقّفات في مطارات جديدة أو مخالفة للمسار.")
    lines.append("")

    lines.append("🔎 <b>مؤشرات عسكرية مرصودة خلال اليوم</b>")
    if snap["military_suspects"]:
        for item in snap["military_suspects"]:
            reasons = "، ".join(item["reasons"]) or "مؤشرات محدودة"
            lines.append(
                f"  • <b>{_esc(item['aircraft'])}</b> — {_esc(item['verdict'])} "
                f"({int(item['score'] * 100)}%) · {_esc(reasons)}"
            )
    else:
        lines.append("  • لا مؤشرات كافية في اللحظة الحالية.")
    lines.append("")

    sahel = snap.get("sahel") or {}
    lines.append("🌍 <b>نطاق الساحل الأفريقي والمسارات غير الطبيعية</b>")
    if sahel:
        lines.append(f"  <i>نطاق التركيز: {_esc(sahel['scope'])}.</i>")
        if sahel.get("events_by_zone"):
            lines.append(
                "  • انقطاعات ضمن نطاق المتابعة: "
                + " · ".join(
                    f"{_esc(item['zone'])} ({item['count']})"
                    for item in sahel["events_by_zone"]
                )
            )
        if sahel.get("landings_by_zone"):
            lines.append(
                "  • هبوطات ضمن نطاق المتابعة: "
                + " · ".join(
                    f"{_esc(item['zone'])} ({item['count']})"
                    for item in sahel["landings_by_zone"]
                )
            )
        lines.append(f"  • طائرات تجاوزت عتبة الشذوذ: <b>{sahel.get('flagged', 0)}</b>")
        if sahel.get("coverage_exits"):
            lines.append(
                f"  • خروج من نطاق التغطية (ليس شذوذاً): {sahel['coverage_exits']}"
            )
        for item in (sahel.get("top") or [])[:5]:
            reasons = "؛ ".join(item.get("indicators") or []) or "مؤشرات محدودة"
            lines.append(
                f"  • <b>{_esc(item['aircraft'])}</b> ({_esc(item.get('registration'))}) — "
                f"{item['score']}% · ثقة {_esc(item['confidence'])} · "
                f"{_esc(item['zone'])} · {_esc(reasons)}"
            )
        lines.append("")
        lines.append("🧾 <b>مصفوفة الاستخبارات (Part A) — الطائرات الإماراتية</b>")
        matrix_rows = sahel.get("matrix") or []
        if matrix_rows:
            for index, row in enumerate(matrix_rows, start=1):
                lines.append(
                    f"  <b>{index}) {_esc(row.get('Flight_Hex'))} · "
                    f"{_esc(row.get('Registration'))}</b> — درجة الشذوذ "
                    f"<b>{row.get('Anomaly_Score', 0)}%</b> · ثقة "
                    f"{_esc(row.get('Confidence_Level'))} · {_esc(row.get('Zone'))}"
                )
                lines.append(f"      • MSN: {_esc(row.get('MSN'))}")
                lines.append(f"      • الأصل/الانطلاق: {_esc(row.get('Origin'))}")
                lines.append(
                    f"      • نقطة الدخول إلى الساحل: {_esc(row.get('Entry_Point_Sahel'))}"
                )
                lines.append(
                    f"      • إحداثيات الانقطاع: {_esc(row.get('Blackout_LatLong'))}"
                )
                lines.append(f"      • مهبط الهبوط: {_esc(row.get('Landing_Airfield'))}")
                lines.append(
                    f"      • تدقيق النموذج الثاني: {_esc(row.get('Audited_By_Model'))}"
                )
                lines.append(
                    "      • المؤشرات: " + _esc(row.get("Anomaly_Indicators") or "—")
                )
                if row.get("Sentinel_EO_Browser"):
                    lines.append(
                        f"      • Sentinel-2: {_esc(row.get('Sentinel_EO_Browser'))}"
                    )
                lines.append("")
        else:
            lines.append("  • لا صفوف إماراتية في المصفوفة لهذه الفترة.")
        lines.append("")
    else:
        lines.append("  • تعذّر بناء تحليل الساحل لهذه الفترة.")
    lines.append("")

    # ── Part B: سجل نقاط إخفاء التعريف والمهابط المتكرّرة ──
    from ledger import build_hotspots, sync

    try:
        sync(tracker, hours)
        hotspots = build_hotspots(tracker, hours)
    except Exception as exc:  # noqa: BLE001 - لا يُفشل التقرير
        log.warning("تعذّر بناء سجل الساحل اليومي (Part B): %s", exc)
        hotspots = []
    lines.append("🗂️ <b>سجل نقاط إخفاء التعريف والمهابط (Part B)</b>")
    if hotspots:
        for index, row in enumerate(hotspots, start=1):
            lines.append(
                f"  <b>{index}) {_esc(row.zone_id)}</b> · {_esc(row.country_region)} — "
                f"تكرار <b>{row.frequency}</b> ({_esc(row.breakdown)})"
            )
            lines.append(f"      • المهبط/الإحداثيات: {_esc(row.airfield)}")
            lines.append(
                "      • الرموز المرتبطة: "
                + ("، ".join(_esc(code) for code in row.hex_codes) or "—")
            )
            lines.append(f"      • حالة التحقق البصري: {_esc(row.imint_status)}")
            lines.append(f"      • مستوى الثقة: {_esc(row.confidence)}")
            lines.append("")
        lines.append(
            "  <i>التكرار يعني «رصد متكرّر» لا «تأكيد هبوط»؛ التحقق البصري معلّق.</i>"
        )
    else:
        lines.append("  • لا نقاط ساخنة في السجل بعد.")
    lines.append("")

    down = [item["name"] for item in snap["sources"] if not item["ok"]]
    lines.append("🛰️ <b>جودة المصادر خلال الفترة</b>")
    if down:
        # adsb.lol وadsb.fi متكافئان: سقوط أحدهما لا يعني ثغرة ما دام الآخر يعمل.
        point_ok = any(
            item["ok"] and item["name"] in ("adsb.lol", "adsb.fi")
            for item in snap["sources"]
        )
        note = (
            "المصدر البديل يعمل، فالمواقع مكتملة."
            if point_ok
            else "ثغرة تغطية محتملة."
        )
        lines.append(f"  • ⚠️ مصادر متعطّلة: {_esc('، '.join(down))} — {note}")
    else:
        lines.append(f"  • كل المصادر ({len(snap['sources'])}) تعمل.")
    lines.append("")

    lines.append(
        "<i>حدود التحليل: بيانات ADS-B عامة؛ «الدولة» هي دولة آخر موقع مُستقبَل، "
        "والوجهة مُستنتَجة من الهبوط المرصود. لا يثبت أي رقم هنا إغلاق جهاز "
        "التعريف قصداً أو نيّة الطائرة.</i>"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
def build_daily_local_analysis(snapshot: dict) -> str:
    """استنتاجات 24 ساعة بالقواعد — بديل مضمون عند غياب النموذج."""
    totals = snapshot["totals"]
    hours = snapshot["hours"]
    lines = ["## حصيلة اليوم"]
    lines.append(
        f"- {totals['events']} حدث فقدان إشارة، منها {totals['events_uae']} لطائرات "
        f"إماراتية، و{totals['landings']} هبوطاً مرصوداً في {hours} ساعة."
    )
    if totals["events_still_global"]:
        lines.append(
            f"- {totals['events_still_global']} من أحداث الطائرات الإماراتية كانت "
            "طائراتها ما تزال تبث عالمياً: الغياب محلي لا توقف فعلي."
        )

    lines.append("## الأنماط المتكررة")
    patterns = 0
    if snapshot["events_by_country"]:
        top = snapshot["events_by_country"][0]
        if top["count"] >= 2:
            lines.append(
                f"- {top['country']} تتكرر في {top['count']} أحداث — نمط لا حدث منفرد."
            )
            patterns += 1
    if snapshot["landings_by_airport"]:
        top = snapshot["landings_by_airport"][0]
        if top["count"] >= 2:
            lines.append(
                f"- {top['name'] or top['airport']} استقبل {top['count']} هبوطات — "
                "وجهة متكررة لا شذوذ."
            )
            patterns += 1
    if not patterns:
        lines.append("- لا نمط متكرر واضح؛ الأحداث موزّعة على دول ومطارات مختلفة.")

    lines.append("## الشذوذ")
    anomalies = 0
    if totals["unplanned"]:
        lines.append(
            f"- {totals['unplanned']} توقّف في مطار جديد أو مخالف للمسار — "
            "يستحق مراجعة سبب التحويل."
        )
        anomalies += 1
    if totals["diverged"]:
        lines.append(
            f"- {totals['diverged']} حالة هبوط خارجي تلته وجهة غير إماراتية."
        )
        anomalies += 1
    if not anomalies:
        lines.append("- لا شذوذ في الوجهات: كل الهبوطات ضمن المطارات المعتادة.")

    lines.append("## الطائرات والوجهات")
    if snapshot["uae_events"]:
        item = snapshot["uae_events"][0]
        lines.append(
            f"- أحدث حدث إماراتي: {item['callsign']} فوق "
            f"{item['country'] or 'موقع غير محدد'} قبل {item['minutes_ago']} دقيقة."
        )
    if snapshot["military_suspects"]:
        item = snapshot["military_suspects"][0]
        lines.append(
            f"- أعلى مؤشر عسكري: {item['aircraft']} — {item['verdict']} "
            f"({int(item['score'] * 100)}%)."
        )
    if not snapshot["uae_events"] and not snapshot["military_suspects"]:
        lines.append("- لا أحداث إماراتية ولا مؤشرات عسكرية بارزة في هذه الفترة.")

    lines.append("## الساحل والمسارات غير الطبيعية")
    sahel = snapshot.get("sahel") or {}
    if sahel and sahel.get("flagged"):
        top = sahel.get("top") or []
        lines.append(f"- {sahel['flagged']} طائرة تجاوزت عتبة الشذوذ داخل النطاق.")
        if top:
            best = top[0]
            lines.append(
                f"- أعلى شذوذ: {best['aircraft']} ({best.get('registration')}) "
                f"{best['score']}% بثقة {best['confidence']} — "
                + ("؛ ".join(best.get("indicators") or []) or "مؤشرات محدودة")
                + "."
            )
        lines.append(
            "- الدرجة تجميع مؤشرات مستقلة؛ لا تُثبت نيّة ولا إغلاقاً متعمداً، "
            "و«طبقة التأجير» تبقى غير محددة بلا سجلات عقود."
        )
    elif sahel:
        lines.append("- لا طائرة تجاوزت عتبة الشذوذ داخل نطاق الساحل اليوم.")
        if sahel.get("coverage_exits"):
            lines.append(
                f"- {sahel['coverage_exits']} طائرة غادرت نطاق التغطية فقط "
                "(ليس شذوذاً ولا دليل إيقاف بث)."
            )
    else:
        lines.append("- تعذّر بناء تحليل الساحل لهذه الفترة.")

    lines.append("## الاستنتاجات")
    lines.append(
        f"- الحصيلة الكلية ({totals['events']} حدثاً و{totals['landings']} هبوطاً) "
        "ضمن النطاق المعتاد للرصد التطوعي، ولا تشير إلى تغيّر مفاجئ في الحركة."
    )
    if totals["events_still_global"]:
        lines.append(
            "- الفرضية «أُغلقت أجهزة التعريف» **مستبعدة** جزئياً: جزء من الطائرات "
            "ظل يبث عالمياً، فالمشكلة في تغطية المستقبلات المحلية."
        )
    if totals["unplanned"]:
        lines.append(
            "- تحويلات المسار مؤكدة رقمياً لكن سببها غير معروف؛ تحتاج متابعة "
            "في الدورة القادمة قبل استخلاص نمط."
        )
    lines.append(
        "- ما يبقى غير مفسّر: سبب انقطاع الطائرات التي لم تُرصد عالمياً، "
        "وهو ما لا تستطيع بيانات ADS-B العامة حسمه."
    )
    lines.append(
        "التحليل مبني على بيانات ADS-B العامة فقط؛ غياب الرصد قد يكون بسبب "
        "تغطية المستقبلات لا إغلاق جهاز التعريف."
    )
    return "\n".join(lines)


async def build_daily_analysis(tracker, analyzer=None, hours: int = 24) -> tuple[str, str]:
    """نسخة HTML من استنتاجات 24 ساعة + وسم المصدر (ai أو local)."""
    from ai_report import (
        AIAnalyzer,
        AI_REPORT_TITLE,
        DAILY_SYSTEM_PROMPT,
        build_daily_prompt,
        format_ai_text,
    )

    snapshot = build_daily_snapshot(tracker, hours)
    title = f"🧠 <b>استنتاجات {hours} ساعة</b>"

    # 1) خطّ النماذج الثلاثي (تصفية → استدلال → تدقيق) قبل المزوّد الواحد.
    if settings.llm_pipeline_enabled and settings.ai_report_mode != "off":
        try:
            from pipeline import run_analysis

            result = await run_analysis(snapshot, tracker=tracker, kind="daily")
        except Exception as exc:  # noqa: BLE001
            log.warning("تعذّر خطّ النماذج اليومي: %s", exc)
            result = None
        if result is not None:
            text = format_ai_text(result.text)
            if text:
                return (
                    f"{title}\n<i>المصدر: {html.escape(result.source)}</i>\n\n{text}",
                    result.source,
                )

    if settings.ai_enabled and settings.ai_report_mode != "off":
        analyzer = analyzer or AIAnalyzer()
        if analyzer.available:
            raw = await analyzer.analyze(
                snapshot,
                prompt=build_daily_prompt(snapshot),
                system=DAILY_SYSTEM_PROMPT,
            )
            text = format_ai_text(raw or "") if raw else ""
            if text:
                return (
                    f"{title}\n<i>المصدر: {html.escape(analyzer.describe())}</i>\n\n{text}",
                    analyzer.describe(),
                )
            log.info("تعذّر تحليل 24 ساعة بالنموذج: %s", analyzer.last_error)

    return f"{title}\n\n{build_daily_local_analysis(snapshot)}", "local"
