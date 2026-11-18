"""إعدادات البوت — تُقرأ من متغيرات البيئة / ملف .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _as_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _as_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _as_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """كل الإعدادات في مكان واحد، مع قيم افتراضية آمنة."""

    def __init__(self) -> None:
        # تليجرام
        self.telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_chat_ids: tuple[int, ...] = tuple(
            int(part.strip())
            for part in os.getenv("TELEGRAM_CHAT_IDS", "").split(",")
            if part.strip().lstrip("-").isdigit()
        )

        # منطقة المراقبة — صندوق إحداثيات الإمارات التقريبي
        self.lamin: float = _as_float("UAE_LAMIN", 22.5)
        self.lamax: float = _as_float("UAE_LAMAX", 26.2)
        self.lomin: float = _as_float("UAE_LOMIN", 51.5)
        self.lomax: float = _as_float("UAE_LOMAX", 56.5)
        self.center_lat: float = _as_float("UAE_CENTER_LAT", 24.0)
        self.center_lon: float = _as_float("UAE_CENTER_LON", 54.0)
        self.radius_nm: int = _as_int("UAE_RADIUS_NM", 220)  # أقصى قيمة مسموحة 250

        # التوقيت
        self.poll_interval_seconds: int = max(30, _as_int("POLL_INTERVAL_SECONDS", 120))
        self.report_interval_minutes: int = max(1, _as_int("REPORT_INTERVAL_MINUTES", 30))
        self.signal_loss_minutes: int = max(5, _as_int("SIGNAL_LOSS_MINUTES", 20))
        self.max_report_aircraft: int = max(1, _as_int("MAX_REPORT_AIRCRAFT", 20))

        # المصادر
        self.use_adsb_lol: bool = _as_bool("USE_ADSBLOL", True)
        self.use_opensky: bool = _as_bool("USE_OPENSKY", True)
        self.opensky_client_id: str = os.getenv("OPENSKY_CLIENT_ID", "").strip()
        self.opensky_client_secret: str = os.getenv("OPENSKY_CLIENT_SECRET", "").strip()

        # تحديد الدولة والمطارات
        self.use_geocoding: bool = _as_bool("USE_GEOCODING", True)
        self.nominatim_user_agent: str = os.getenv(
            "NOMINATIM_USER_AGENT", "uae-aircraft-monitor/1.0 (research)"
        )
        self.airports_cache_path: Path = Path(
            os.getenv("AIRPORTS_CACHE_PATH", "data/airports.csv")
        )

        # كشف الهبوط
        self.landing_max_km: float = _as_float("LANDING_MAX_KM", 25.0)
        self.landing_alt_ft: int = _as_int("LANDING_ALT_FT", 1200)
        self.landing_speed_kts: int = _as_int("LANDING_SPEED_KTS", 80)
        self.min_leg_minutes: int = _as_int("MIN_LEG_MINUTES", 30)

        # تتبّع عالمي لطائرات بعينها (قائمة مراقبة)
        self.use_watchlist: bool = _as_bool("USE_WATCHLIST", True)
        self.watchlist_interval_seconds: int = max(
            60, _as_int("WATCHLIST_INTERVAL_SECONDS", 300)
        )
        self.watchlist_max: int = max(1, _as_int("WATCHLIST_MAX", 20))
        self.watchlist_dynamic: bool = _as_bool("WATCHLIST_DYNAMIC", True)

        # طائرات إماراتية إضافية نتابعها صراحةً (ICAO24 مفصولة بفواصل)
        self.uae_watch_icao24: frozenset[str] = frozenset(
            value.strip().lower()
            for value in os.getenv("UAE_WATCH_ICAO24", "").split(",")
            if value.strip()
        )

        # التخزين
        self.db_path: Path = Path(os.getenv("DB_PATH", "data/aircraft.db"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.airports_cache_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """(lamin, lomin, lamax, lomax) — ترتيب معتمد في OpenSky."""
        return (self.lamin, self.lomin, self.lamax, self.lomax)

    @property
    def radius(self) -> int:
        return min(250, max(10, self.radius_nm))

    @property
    def has_opensky_credentials(self) -> bool:
        return bool(self.opensky_client_id and self.opensky_client_secret)


settings = Settings()
