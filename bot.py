"""بوت تليجرام: أوامر تفاعلية + تقارير مجدولة كل نصف ساعة."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from telegram import BotCommand, Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

from config import settings
from reporter import build_report, build_status
from tracker import AircraftTracker

log = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("start", "🚀 تشغيل البوت وتسجيل هذه المحادثة"),
    BotCommand("help", "📖 قائمة الأوامر"),
    BotCommand("report", "📄 إرسال تقرير فوري"),
    BotCommand("status", "📊 حالة البوت والمصادر"),
    BotCommand("chats", "💬 المحادثات المسجّلة"),
    BotCommand("removechat", "🗑️ إزالة المحادثة الحالية"),
    BotCommand("stopchat", "🔕 إيقاف التقارير لهذه المحادثة"),
]

MAX_MESSAGE_LENGTH = 3800


def split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """تقسيم الرسالة على حدود الأسطر لتفادي حد تليجرام (4096 حرفاً)."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        line_size = len(line) + 1
        if current and size + line_size > limit:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += line_size
    if current:
        parts.append("\n".join(current))
    return parts


class ReportBot:
    def __init__(self, tracker: AircraftTracker) -> None:
        self.tracker = tracker
        self.chats_file: Path = settings.db_path.parent / "chats.json"
        self.dynamic_chats: set[int] = self._load_chats()

    # ------------------------------------------------------------------
    def _load_chats(self) -> set[int]:
        try:
            data = json.loads(self.chats_file.read_text(encoding="utf-8"))
            return {int(value) for value in data}
        except (OSError, ValueError, TypeError):
            return set()

    def _save_chats(self) -> None:
        try:
            self.chats_file.write_text(
                json.dumps(sorted(self.dynamic_chats)), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("تعذّر حفظ قائمة المحادثات: %s", exc)

    @property
    def recipients(self) -> set[int]:
        """الاتحاد بين المحادثات المحددة في .env والمسجّلة عبر /start."""
        return set(settings.telegram_chat_ids) | self.dynamic_chats

    def is_allowed(self, chat_id: int) -> bool:
        return not self.recipients or chat_id in self.recipients

    async def _authorized(self, update: Update) -> bool:
        if self.is_allowed(update.effective_chat.id):
            return True
        if update.message:
            await update.message.reply_text("⛔ غير مصرح لك باستخدام هذا البوت.")
        return False

    @staticmethod
    async def _send(update: Update, text: str) -> None:
        for part in split_message(text):
            await update.message.reply_text(part, parse_mode="HTML")

    # ------------------------------------------------------------------ أوامر
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        self.dynamic_chats.add(chat_id)
        self._save_chats()
        await update.message.reply_text(
            "✅ <b>تم تسجيل هذه المحادثة.</b>\n\n"
            "سأرسل تقريراً كل "
            f"<b>{settings.report_interval_minutes} دقيقة</b> يتضمّن حركة الطائرات "
            "في المجال الجوي للإمارات ورصد فقدان إشارة ADS-B.\n\n"
            "الأوامر:\n"
            "  /report — تقرير فوري\n"
            "  /status — حالة البوت والمصادر\n"
            "  /help — المساعدة\n"
            "  /stopchat — إيقاف التقارير هنا",
            parse_mode="HTML",
        )

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        await self.start(update, context)

    async def status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        await self._send(update, build_status(self.tracker))

    async def report_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        await update.message.reply_text("⏳ جارٍ تحديث البيانات...")
        await self.tracker.poll()
        await self._send(update, build_report(self.tracker))

    async def chats_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        chats = sorted(self.recipients)
        text = "💬 <b>المحادثات المسجّلة</b>\n" + (
            "\n".join(f"  • <code>{chat}</code>" for chat in chats) if chats else "  • لا توجد محادثات بعد."
        )
        await self._send(update, text)

    async def removechat_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        chat_id = update.effective_chat.id
        self.dynamic_chats.discard(chat_id)
        self._save_chats()
        await update.message.reply_text("🗑️ تمت إزالة المحادثة من القائمة الديناميكية.")

    async def stopchat_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.removechat_cmd(update, context)

    # ------------------------------------------------------------ مهام مجدولة
    async def scheduled_poll(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await self.tracker.poll()
        except Exception as exc:  # noqa: BLE001 - لا يجب أن يوقف خطأً الجدولة
            log.exception("فشل الجلب المجدول: %s", exc)

    async def scheduled_report(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = build_report(self.tracker)
        for chat_id in self.recipients:
            for part in split_message(text):
                try:
                    await context.bot.send_message(
                        chat_id=chat_id, text=part, parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("تعذّر الإرسال إلى %s: %s", chat_id, exc)

    # ------------------------------------------------------------------ إعداد
    async def _post_init(self, application: Application) -> None:
        try:
            await application.bot.set_my_commands(BOT_COMMANDS)
            log.info("تم تسجيل قائمة الأوامر في تليجرام")
        except Exception as exc:  # noqa: BLE001
            log.warning("تعذّر تسجيل قائمة الأوامر: %s", exc)

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("خطأ في البوت: %s", context.error, exc_info=context.error)

    def build_application(self) -> Application:
        application = (
            ApplicationBuilder()
            .token(settings.telegram_token)
            .post_init(self._post_init)
            .build()
        )
        for name, handler in (
            ("start", self.start),
            ("help", self.help_cmd),
            ("report", self.report_cmd),
            ("status", self.status_cmd),
            ("chats", self.chats_cmd),
            ("removechat", self.removechat_cmd),
            ("stopchat", self.stopchat_cmd),
        ):
            application.add_handler(CommandHandler(name, handler))

        application.add_error_handler(self._on_error)

        queue = application.job_queue
        if queue:
            queue.run_repeating(
                self.scheduled_poll,
                interval=settings.poll_interval_seconds,
                first=5,
            )
            queue.run_repeating(
                self.scheduled_report,
                interval=settings.report_interval_minutes * 60,
                first=settings.report_interval_minutes * 60,
            )
        return application
