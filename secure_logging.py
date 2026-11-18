"""تهيئة السجلات مع إخفاء الأسرار (التوكن) من المخرجات.

سبب الوجود: مكتبة ``httpx`` تسجّل عنوان كل طلب على مستوى INFO، وعنوان
واجهة تليجرام يحتوي على التوكن نفسه (``/bot<TOKEN>/getMe``). أي تسجيل أو
مشاركة للسجلات قد تكشف التوكن، لذلك نحجب الأنماط الحساسة ونخفض ضجيج
المكتبات التي تطبع العناوين.
"""
from __future__ import annotations

import logging
import re

# توكن تليجرام: أرقام ثم نقطتان ثم سلسلة طويلة (يسبقه غالباً "bot")
TELEGRAM_TOKEN_RE = re.compile(r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{20,}")
# أنماط عامة مثل token=... أو secret: ...
GENERIC_SECRET_RE = re.compile(
    r"(?i)\b(token|secret|password|api[_-]?key|client[_-]?secret)\b\s*[=:]\s*\S+"
)

REDACTED = "<محجوب>"


class RedactSecretsFilter(logging.Filter):
    """يحجب التوكن والأسرار قبل كتابة أي سجل."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - لا نُفشل التسجيل بسبب رسالة غريبة
            return True
        redacted = TELEGRAM_TOKEN_RE.sub(REDACTED, message)
        redacted = GENERIC_SECRET_RE.sub(r"\1=" + REDACTED, redacted)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def setup_logging(level: int = logging.INFO) -> None:
    """يهيّئ التسجيل ويقلّل ضجيج المكتبات التي تطبع عناوين الطلبات."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(RedactSecretsFilter())
    # مكتبات تطبع عنوان URL الكامل (ويحتوي التوكن) على مستوى INFO
    for noisy in ("httpx", "httpcore", "telegram", "telegram.ext", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
