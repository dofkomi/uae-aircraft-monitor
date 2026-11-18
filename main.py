"""نقطة الدخول: بوت تليجرام لمراقبة الطائرات في المجال الجوي الإماراتي."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
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
# GitHub قد يؤجّل موعداً مجدولاً أو يُهمله، لذا نضع موعدين في الساعة. هذا
# الملف يحفظ وقت آخر إرسال ناجح، فتتخطّى المحاولة الثانية الإرسال بدل أن
# يصل التقرير مرتين.
SEND_GUARD_MINUTES = 40
SEND_MARKER = Path("data/last-send.txt")


def _minutes_since_last_send() -> float | None:
    try:
        stamp = float(SEND_MARKER.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return (time.time() - stamp) / 60.0


def _mark_sent() -> None:
    try:
        SEND_MARKER.parent.mkdir(parents=True, exist_ok=True)
        SEND_MARKER.write_text(str(time.time()), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 - لا نُفشل التقرير بسبب ملف علامة
        log.warning("تعذّر حفظ علامة الإرسال: %s", exc)



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

    # حماية من التكرار: الجدولة السحابية تُحاول أكثر من مرة في الساعة (موعدان
    # احتياطيان) لأن GitHub قد يؤجّل أحد الموعدين أو يُهمله. إن أُرسل تقرير
    # قبل قليل، نتخطّى هذه المحاولة بهدوء.
    if not force:
        age = _minutes_since_last_send()
        if age is not None and age < SEND_GUARD_MINUTES:
            log.info(
                "آخر تقرير أُرسل قبل %.0f دقيقة — تخطّي هذه الدورة (حماية من التكرار)",
                age,
            )
            return

    tracker = build_tracker()
    await tracker.poll()
    recipients = ReportBot(tracker).recipients
    if not recipients:
        raise SystemExit(
            "❌ لا توجد محادثات مسجّلة. أرسل /start للبوت أو حدّد TELEGRAM_CHAT_IDS."
        )
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
                    )
            log.info("تم إرسال %d رسالة إلى %d محادثة", sent, len(recipients))
            _mark_sent()
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
        help="إرسال فوري حتى لو أُرسل تقرير قبل أقل من 40 دقيقة (للتجربة)",
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
