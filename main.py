"""نقطة الدخول: بوت تليجرام لمراقبة الطائرات في المجال الجوي الإماراتي."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from adsb_sources import build_sources
from aircraft_db import AircraftDB
from airports import AirportDB
from bot import ReportBot
from config import settings
from country_shapes import CountryShapes
from geo import GeoResolver
from osint import OsintClient
from reporter import build_messages
from secure_logging import setup_logging
from store import Store
from tracker import AircraftTracker

setup_logging()
log = logging.getLogger("uae-aircraft-monitor")

# ── حماية من التكرار في التشغيل السحابي ──
# المُشغِّل الخارجي (cron-job.org) يرسل مرة كل ساعة، وقد يعود جدول GitHub
# يوماً ما (موعدان احتياطيان) أو يُعاد تشغيل الدورة يدوياً. القاعدة:
# تقرير واحد على الأكثر لكل ساعة UTC، مع نافذة قصيرة ضد إعادة المحاولة.
# نافذة الـ40 دقيقة القديمة كانت تُسقط تشغيل الساعة التالي إذا جرّب المستخدم
# إرسالاً يدوياً قبل موعده بـ22 دقيقة؛ أصبحت النافذة 10 دقائق + قيد الساعة.
SEND_GUARD_MINUTES = 10
SEND_MARKER = Path("data/last-send.txt")
# إيقاع منفصل للتقرير التفصيلي: كل 24 ساعة مرة واحدة، لا مع كل تقرير ساعي.
DAILY_MARKER = Path("data/last-daily.txt")


def _last_send_stamp() -> float | None:
    try:
        return float(SEND_MARKER.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _same_hour_slot(stamp: float, now_ts: float | None = None) -> bool:
    """هل آخر إرسال والآن داخل نفس ساعة UTC؟"""
    now = datetime.fromtimestamp(
        time.time() if now_ts is None else now_ts, timezone.utc
    )
    then = datetime.fromtimestamp(stamp, timezone.utc)
    return (now.year, now.month, now.day, now.hour) == (
        then.year, then.month, then.day, then.hour
    )


def _send_guard_reason(now_ts: float | None = None) -> str | None:
    """سبب تخطّي هذه الدورة، أو ``None`` إن حان تقرير جديد."""
    stamp = _last_send_stamp()
    if stamp is None:
        return None
    now_ts = time.time() if now_ts is None else now_ts
    age = (now_ts - stamp) / 60.0
    if age < SEND_GUARD_MINUTES:
        return f"آخر تقرير أُرسل قبل {age:.0f} دقيقة"
    if _same_hour_slot(stamp, now_ts):
        return "أُرسل تقرير هذه الساعة بالفعل"
    return None


def _mark_sent() -> None:
    try:
        SEND_MARKER.parent.mkdir(parents=True, exist_ok=True)
        SEND_MARKER.write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - لا نُفشل التقرير بسبب ملف علامة
        log.warning("تعذّر حفظ علامة الإرسال: %s", exc)


def _hours_since_last_daily() -> float | None:
    try:
        stamp = float(DAILY_MARKER.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return (time.time() - stamp) / 3600.0


def _mark_daily_sent() -> None:
    try:
        DAILY_MARKER.parent.mkdir(parents=True, exist_ok=True)
        DAILY_MARKER.write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        log.warning("تعذّر حفظ علامة التقرير اليومي: %s", exc)


def _should_include_daily(force: bool = False) -> bool:
    """هل حان موعد التقرير التفصيلي (24 ساعة)؟"""
    if force:
        return True
    age = _hours_since_last_daily()
    return age is None or age >= settings.daily_guard_hours



def build_tracker() -> AircraftTracker:
    sources = build_sources()
    if not sources:
        raise SystemExit(
            "❌ لا توجد مصادر بيانات مُفعّلة. فعّل USE_ADSBLOL أو USE_ADSB_FI أو USE_OPENSKY."
        )
    store = Store(settings.db_path)
    aircraft_db = AircraftDB(store)
    airports = AirportDB(settings.airports_cache_path)
    shapes = CountryShapes(
        cache_path=settings.countries_path,
        enabled=settings.use_offline_country,
    )
    geo = GeoResolver(
        store=store,
        airports=airports,
        user_agent=settings.nominatim_user_agent,
        enabled=settings.use_geocoding,
        shapes=shapes,
        nearest_max_km=settings.nearest_country_max_km,
    )
    osint = OsintClient(
        store=store,
        enabled=settings.osint_enabled,
        user_agent=settings.nominatim_user_agent,
        max_routes_per_poll=settings.osint_max_routes_per_poll,
    )
    return AircraftTracker(
        sources, aircraft_db, store, geo=geo, airports=airports, osint=osint
    )


async def _once() -> None:
    tracker = build_tracker()
    await tracker.poll()
    messages = await build_messages(tracker)
    print(("\n\n" + "─" * 60 + "\n\n").join(messages))


async def _send_once(force: bool = False) -> None:
    """جلب واحد + إرسال التقرير إلى كل المحادثات المسجّلة (للنشر السحابي بالجدولة)."""
    from telegram import Bot

    from bot import ReportBot, split_message
    from report_docs import send_documents

    # حماية من التكرار: تقرير واحد على الأكثر لكل ساعة UTC. يمنع تكرار
    # المحاولات المتقاربة (إعادة تشغيل يدوية أو موعدان احتياطيان في GitHub)
    # دون أن يُسقط تشغيل الساعة التالي بسبب اختبار يدوي قبل موعده بدقائق.
    if not force:
        reason = _send_guard_reason()
        if reason is not None:
            log.info("%s — تخطّي هذه الدورة (حماية من التكرار)", reason)
            return

    tracker = build_tracker()
    await tracker.poll()
    recipients = ReportBot(tracker).recipients
    if not recipients:
        raise SystemExit(
            "❌ لا توجد محادثات مسجّلة. أرسل /start للبوت أو حدّد TELEGRAM_CHAT_IDS."
        )
    include_daily = _should_include_daily(force)
    messages = await build_messages(tracker)
    # مهلة أوسع: رفع PDF/DOCX يحتاج وقتاً أطول من رسالة نصية، وشبكة السحابة
    # قد تتأخر في إنشاء الاتصال. نعيد المحاولة فقط إن لم يُرسَل شيء بعد
    # (حتى لا يصل التقرير مرتين).
    from telegram.request import HTTPXRequest

    request = HTTPXRequest(connect_timeout=30, read_timeout=120, write_timeout=240)
    last_error: Exception | None = None
    for attempt in range(1, 4):
        sent = 0
        try:
            async with Bot(settings.telegram_token, request=request) as tg:
                for chat_id in recipients:
                    for message in messages:
                        for part in split_message(message):
                            await tg.send_message(
                                chat_id=chat_id,
                                text=part,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                            )
                            sent += 1
                    # نسختان قابلتان للاستغلال: PDF للطباعة وDOCX للتعديل
                    # (الدراسة + التفاصيل + تقرير 24 ساعة)، والدراسة في تليجرام.
                    await send_documents(
                        tg,
                        chat_id,
                        tracker,
                        messages[0] if messages else None,
                        settings.report_interval_minutes,
                        include_daily=include_daily,
                    )
            log.info("تم إرسال %d رسالة إلى %d محادثة", sent, len(recipients))
            _mark_sent()
            if include_daily:
                _mark_daily_sent()
            return
        except Exception as exc:  # noqa: BLE001 - فشل الشبكة لا يفسد الجلب
            last_error = exc
            log.warning("فشل الإرسال (محاولة %d/3): %s", attempt, exc)
            if sent:
                log.error("أُرسل جزء من التقرير قبل الفشل — لا نعيد المحاولة تفادياً للتكرار")
                break
            await asyncio.sleep(5 * attempt)
    raise SystemExit(f"❌ تعذّر الإرسال إلى تليجرام: {last_error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="بوت مراقبة الطائرات — الإمارات")
    parser.add_argument(
        "--once",
        action="store_true",
        help="جلب بيانات واحدة وطباعة التقرير بدون تشغيل البوت (للاختبار)",
    )
    parser.add_argument(
        "--send-once",
        action="store_true",
        help="جلب بيانات واحدة وإرسال التقرير فوراً (للاستخدام مع جدولة سحابية)",
    )
    parser.add_argument(
        "--force-send",
        action="store_true",
        help="إرسال فوري حتى لو أُرسل تقرير هذه الساعة (للتجربة)",
    )
    args = parser.parse_args()

    if args.once:
        asyncio.run(_once())
        return
    if args.send_once:
        asyncio.run(_send_once(force=args.force_send))
        return

    if not settings.telegram_token:
        raise SystemExit(
            "❌ متغير TELEGRAM_BOT_TOKEN مطلوب.\n"
            "انسخ .env.example إلى .env وأدخل توكن البوت من @BotFather."
        )

    tracker = build_tracker()
    bot = ReportBot(tracker)
    application = bot.build_application()

    log.info(
        "🚀 البوت يعمل | المنطقة: %.2f–%.2fN %.2f–%.2fE | تقرير كل %d دقيقة",
        settings.lamin, settings.lamax, settings.lomin, settings.lomax,
        settings.report_interval_minutes,
    )
    application.run_polling()


if __name__ == "__main__":
    main()
