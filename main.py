"""نقطة الدخول: بوت تليجرام لمراقبة الطائرات في المجال الجوي الإماراتي."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from adsb_sources import build_sources
from aircraft_db import AircraftDB
from airports import AirportDB
from bot import ReportBot
from config import settings
from geo import GeoResolver
from reporter import build_report
from secure_logging import setup_logging
from store import Store
from tracker import AircraftTracker

setup_logging()
log = logging.getLogger("uae-aircraft-monitor")


def build_tracker() -> AircraftTracker:
    sources = build_sources()
    if not sources:
        raise SystemExit("❌ لا توجد مصادر بيانات مُفعّلة. فعّل USE_ADSBLOL أو USE_OPENSKY.")
    store = Store(settings.db_path)
    aircraft_db = AircraftDB(store)
    airports = AirportDB(settings.airports_cache_path)
    geo = GeoResolver(
        store=store,
        airports=airports,
        user_agent=settings.nominatim_user_agent,
        enabled=settings.use_geocoding,
    )
    return AircraftTracker(sources, aircraft_db, store, geo=geo, airports=airports)


async def _once() -> None:
    tracker = build_tracker()
    await tracker.poll()
    print(build_report(tracker))


async def _send_once() -> None:
    """جلب واحد + إرسال التقرير إلى كل المحادثات المسجّلة (للنشر السحابي بالجدولة)."""
    from telegram import Bot

    from bot import ReportBot, split_message

    tracker = build_tracker()
    await tracker.poll()
    recipients = ReportBot(tracker).recipients
    if not recipients:
        raise SystemExit(
            "❌ لا توجد محادثات مسجّلة. أرسل /start للبوت أو حدّد TELEGRAM_CHAT_IDS."
        )
    text = build_report(tracker)
    async with Bot(settings.telegram_token) as tg:
        for chat_id in recipients:
            for part in split_message(text):
                await tg.send_message(
                    chat_id=chat_id,
                    text=part,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
    log.info("تم إرسال التقرير إلى %d محادثة", len(recipients))


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
    args = parser.parse_args()

    if args.once:
        asyncio.run(_once())
        return
    if args.send_once:
        asyncio.run(_send_once())
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
