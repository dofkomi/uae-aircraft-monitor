"""بوت تليجرام: أوامر تفاعلية + تقارير مجدولة كل ساعة.

كل تقرير = **دراسة تحليلية مكتوبة** (نموذج ذكاء اصطناعي مجاني أو محرّك
محلي) + التقرير المفصّل. البوت **خاص** افتراضياً: لا يستجيب إلا للمحادثات
المذكورة في ``TELEGRAM_CHAT_IDS`` أو التي أضافها المالك عبر ``/addchat``.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from telegram import BotCommand, Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

from config import settings
from reporter import build_messages, build_status
from report_docs import send_documents
from tracker import AircraftTracker

log = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("start", "🚀 معلومات البوت"),
    BotCommand("help", "📖 قائمة الأوامر"),
    BotCommand("report", "📄 تقرير فوري (دراسة + تفاصيل)"),
    BotCommand("analysis", "🧠 الدراسة التحليلية فقط"),
    BotCommand("status", "📊 حالة البوت والمصادر"),
    BotCommand("chats", "💬 المحادثات المسجّلة"),
    BotCommand("addchat", "➕ إضافة محادثة (للمالك فقط)"),
    BotCommand("removechat", "🗑️ إزالة المحادثة الحالية"),
    BotCommand("stopchat", "🔕 إيقاف التقارير لهذه المحادثة"),
]

MAX_MESSAGE_LENGTH = 3800
REPORT_COOLDOWN_SECONDS = 60


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
        self._last_report: dict[int, float] = {}

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
        """الاتحاد بين المحادثات المحددة في .env والمسجّلة عبر /addchat."""
        return set(settings.telegram_chat_ids) | self.dynamic_chats

    @property
    def owner_ids(self) -> set[int]:
        """مالك البوت: أول معرّف في TELEGRAM_CHAT_IDS (إن وُجد)."""
        return {settings.telegram_chat_ids[0]} if settings.telegram_chat_ids else set()

    def is_allowed(self, chat_id: int) -> bool:
        """قائمة سماح صارمة: أي محادثة غير مسجّلة تُرفض (fail-closed)."""
        return chat_id in self.recipients

    def _who(self, update: Update) -> str:
        user = update.effective_user
        if not user:
            return "مجهول"
        name = " ".join(filter(None, [user.first_name, user.last_name])) or "بلا اسم"
        username = f"@{user.username}" if user.username else "بلا معرّف"
        return f"{name} ({username})"

    async def _authorized(self, update: Update) -> bool:
        chat_id = update.effective_chat.id
        if self.is_allowed(chat_id):
            return True
        log.warning(
            "محاولة وصول غير مصرّح بها: chat=%s user=%s", chat_id, self._who(update)
        )
        if update.message:
            await update.message.reply_text(
                "⛔ <b>هذا البوت خاص.</b>\n\n"
                f"معرّف محادثتك: <code>{chat_id}</code>\n"
                "اطلب من مالك البوت إضافته إلى <code>TELEGRAM_CHAT_IDS</code> "
                "أو إضافتك بأمر <code>/addchat</code>.",
                parse_mode="HTML",
            )
        return False

    @staticmethod
    async def _send(update: Update, text: str) -> None:
        for part in split_message(text):
            await update.message.reply_text(part, parse_mode="HTML")

    @staticmethod
    async def _send_many(update: Update, messages: list[str]) -> None:
        for message in messages:
            await ReportBot._send(update, message)

    # ------------------------------------------------------------------ أوامر
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # التسجيل الذاتي ممنوع: المحادثة يجب أن تكون مصرّحاً لها مسبقاً.
        if not await self._authorized(update):
            return
        await update.message.reply_text(
            "✅ <b>البوت جاهز.</b>\n\n"
            "سأرسل تقريراً كل "
            f"<b>{settings.report_interval_minutes} دقيقة</b>: دراسة تحليلية مكتوبة "
            "للبيانات ثم التقرير المفصّل (حركة الطائرات في المجال الجوي للإمارات، "
            "ورصد فقدان إشارة ADS-B، وإثراء OSINT للمسارات).\n\n"
            "الأوامر:\n"
            "  /report — تقرير فوري (دراسة + تفاصيل)\n"
            "  /analysis — الدراسة التحليلية فقط\n"
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
        chat_id = update.effective_chat.id
        now = time.monotonic()
        wait = REPORT_COOLDOWN_SECONDS - (now - self._last_report.get(chat_id, 0.0))
        if wait > 0:
            await update.message.reply_text(
                f"⏳ انتظر {int(wait) + 1} ثانية قبل طلب تقرير جديد "
                "(حماية من استهلاك حصة المصادر)."
            )
            return
        self._last_report[chat_id] = now
        await update.message.reply_text("⏳ جارٍ تحديث البيانات وتحليلها...")
        await self.tracker.poll()
        await self._send_many(update, await build_messages(self.tracker))

    async def analysis_cmd(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """الدراسة التحليلية وحدها (بدون قسم البيانات المفصّل)."""
        if not await self._authorized(update):
            return
        from ai_report import build_analysis

        await update.message.reply_text("⏳ جارٍ تحديث البيانات وتحليلها...")
        await self.tracker.poll()
        result = await build_analysis(self.tracker)
        if not result:
            await update.message.reply_text(
                "ℹ️ التحليل معطّل. اضبط <code>AI_REPORT_MODE</code> على "
                "<code>both</code> أو <code>analysis</code>.",
                parse_mode="HTML",
            )
            return
        await self._send(update, result[0])

    async def chats_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        chats = sorted(self.recipients)
        text = "💬 <b>المحادثات المسجّلة</b>\n" + (
            "\n".join(f"  • <code>{chat}</code>" for chat in chats) if chats else "  • لا توجد محادثات بعد."
        )
        await self._send(update, text)

    async def addchat_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        if update.effective_chat.id not in self.owner_ids:
            await update.message.reply_text(
                "⛔ هذا الأمر مخصّص لمالك البوت فقط.", parse_mode="HTML"
            )
            return
        args = context.args or []
        if not args or not args[0].lstrip("-").isdigit():
            await update.message.reply_text("الاستخدام: <code>/addchat 123456789</code>", parse_mode="HTML")
            return
        new_id = int(args[0])
        if new_id in self.recipients:
            await update.message.reply_text(f"ℹ️ المحادثة {new_id} مسجّلة مسبقاً.")
            return
        self.dynamic_chats.add(new_id)
        self._save_chats()
        log.info("أُضيفت محادثة جديدة: %s", new_id)
        await update.message.reply_text(
            f"✅ أُضيفت المحادثة <code>{new_id}</code> وستصله التقارير.", parse_mode="HTML"
        )

    async def removechat_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._authorized(update):
            return
        chat_id = update.effective_chat.id
        self.dynamic_chats.discard(chat_id)
        self._save_chats()
        if chat_id in settings.telegram_chat_ids:
            await update.message.reply_text(
                "🗑️ أُزيلت من القائمة الديناميكية، لكنها ما تزال محددة في "
                "TELEGRAM_CHAT_IDS داخل .env — عدّل الملف لإزالتها نهائياً."
            )
            return
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
        try:
            messages = await build_messages(self.tracker)
        except Exception as exc:  # noqa: BLE001 - لا يوقف خطأ التحليل الجدولة
            log.exception("فشل بناء التقرير المجدول: %s", exc)
            return
        for chat_id in self.recipients:
            for message in messages:
                for part in split_message(message):
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id, text=part, parse_mode="HTML",
                            disable_web_page_preview=True,
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.warning("تعذّر الإرسال إلى %s: %s", chat_id, exc)
            try:
                await send_documents(
                    context.bot, chat_id, messages, settings.report_interval_minutes
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("تعذّر إرسال ملفات التقرير إلى %s: %s", chat_id, exc)

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
            ("analysis", self.analysis_cmd),
            ("status", self.status_cmd),
            ("chats", self.chats_cmd),
            ("addchat", self.addchat_cmd),
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
