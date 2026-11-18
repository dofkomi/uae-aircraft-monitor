"""هياكل البيانات المستخدمة في البوت."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RouteInfo:
    """مسار رحلة مُستنتَج من نداء الاستدعاء (مصدر OSINT مجاني: adsbdb)."""

    callsign: str | None = None
    airline: str | None = None
    airline_country: str | None = None
    origin_icao: str | None = None
    origin_iata: str | None = None
    origin_name: str | None = None
    origin_country: str | None = None
    dest_icao: str | None = None
    dest_iata: str | None = None
    dest_name: str | None = None
    dest_country: str | None = None
    source: str = "adsbdb"

    @property
    def route_label(self) -> str:
        """«OMDB → EGLL» — رموز المطارين."""
        origin = self.origin_icao or self.origin_iata or "؟"
        dest = self.dest_icao or self.dest_iata or "؟"
        return f"{origin} → {dest}"

    def _airport_label(self, code: str | None, city: str | None) -> str:
        parts = [part for part in (city, code) if part]
        return " ".join(parts) if parts else "؟"

    @property
    def origin_label(self) -> str:
        return self._airport_label(self.origin_iata or self.origin_icao, self.origin_name)

    @property
    def dest_label(self) -> str:
        return self._airport_label(self.dest_iata or self.dest_icao, self.dest_name)

    def to_dict(self) -> dict:
        return {
            "callsign": self.callsign,
            "airline": self.airline,
            "airline_country": self.airline_country,
            "origin_icao": self.origin_icao,
            "origin_iata": self.origin_iata,
            "origin_name": self.origin_name,
            "origin_country": self.origin_country,
            "dest_icao": self.dest_icao,
            "dest_iata": self.dest_iata,
            "dest_name": self.dest_name,
            "dest_country": self.dest_country,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "RouteInfo | None":
        if not isinstance(data, dict):
            return None
        fields = {
            key: data.get(key)
            for key in (
                "callsign", "airline", "airline_country", "origin_icao",
                "origin_iata", "origin_name", "origin_country", "dest_icao",
                "dest_iata", "dest_name", "dest_country",
            )
        }
        if not any(fields.values()):
            return None
        return cls(source=data.get("source") or "adsbdb", **fields)


@dataclass
class AircraftState:
    """حالة طائرة في لحظة معينة، موحّدة من كل المصادر."""

    icao24: str
    callsign: str | None = None
    origin_country: str | None = None
    longitude: float | None = None
    latitude: float | None = None
    baro_altitude: float | None = None      # متر
    geo_altitude: float | None = None       # متر
    on_ground: bool = False
    velocity: float | None = None           # م/ث
    true_track: float | None = None         # درجة
    vertical_rate: float | None = None      # م/ث
    squawk: str | None = None
    last_contact: int | None = None         # unix seconds
    source: str = ""
    registration: str | None = None
    aircraft_type: str | None = None        # ICAO type designator
    model: str | None = None
    manufacturer: str | None = None
    route: "RouteInfo | None" = None        # إثراء OSINT (مسار الرحلة)

    @property
    def altitude_ft(self) -> int | None:
        value = self.geo_altitude if self.geo_altitude is not None else self.baro_altitude
        return None if value is None else int(value * 3.28084)

    @property
    def speed_kts(self) -> int | None:
        return None if self.velocity is None else int(self.velocity * 1.94384)

    @property
    def display_name(self) -> str:
        return (self.callsign or self.registration or self.icao24).strip()

    def merge_missing(self, other: "AircraftState") -> None:
        """يكمل الحقول الناقصة من مصدر آخر بدون الكتابة على قيمة موجودة."""
        for attr in (
            "callsign", "origin_country", "longitude", "latitude", "baro_altitude",
            "geo_altitude", "velocity", "true_track", "vertical_rate", "squawk",
            "registration", "aircraft_type", "model", "manufacturer",
        ):
            if getattr(self, attr) in (None, "") and getattr(other, attr) not in (None, ""):
                setattr(self, attr, getattr(other, attr))
        if other.last_contact and (not self.last_contact or other.last_contact > self.last_contact):
            self.last_contact = other.last_contact
        if other.source and other.source not in self.source:
            self.source = f"{self.source}+{other.source}" if self.source else other.source


@dataclass
class SignalEvent:
    """حدث فقدان إشارة ADS-B لطائرة كانت تظهر في المنطقة."""

    icao24: str
    callsign: str | None
    classification: str
    last_seen: datetime
    last_lat: float | None
    last_lon: float | None
    registration: str | None = None   # لتحديد إن كانت الطائرة إماراتية
    reason: str = "unknown"   # no_broadcast | left_coverage | unknown
    country: str | None = None        # الدولة التي انقطع فوقها الإشارة
    country_code: str | None = None
    nearest_airport: str | None = None
    nearest_airport_name: str | None = None
    nearest_airport_country: str | None = None
    nearest_airport_km: float | None = None
    last_alt_ft: int | None = None
    last_speed_kts: int | None = None
    last_track: float | None = None
    last_on_ground: bool = False
    last_source: str = ""
    geo_source: str = ""
    # ── إثراء OSINT (اختياري) ──
    route: "RouteInfo | None" = None
    still_broadcasting: bool = False          # هل ما تزال تبث عالمياً بعد غيابها عنّا؟
    global_lat: float | None = None
    global_lon: float | None = None
    global_alt_ft: int | None = None
    global_country: str | None = None
    global_country_code: str | None = None
    global_last_seen: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass
class LandingEvent:
    """هبوط طائرة في مطار، مع الوجهة التي جاء منها واللاحقة إن عُرفت."""

    icao24: str
    callsign: str | None
    registration: str | None
    classification: str
    uae_related: bool
    airport_ident: str | None
    airport_name: str | None
    airport_country_code: str | None
    at: datetime
    latitude: float | None = None
    longitude: float | None = None
    from_airport: str | None = None
    from_airport_name: str | None = None
    from_country_code: str | None = None
    from_at: datetime | None = None
    leg_minutes: int | None = None
    leg_distance_km: float | None = None
    divergence: bool = False   # هبطت بالخارج ثم واصلت إلى وجهة غير الإمارات
    route: "RouteInfo | None" = None   # المسار المجدول (OSINT) إن عُرف

    @property
    def route_mismatch(self) -> bool:
        """هل هبطت في مطار مختلف عن وجهتها المجدولة؟"""
        if not self.route or not self.route.dest_icao or not self.airport_ident:
            return False
        return self.route.dest_icao.strip().upper() != self.airport_ident.strip().upper()


@dataclass
class SourceHealth:
    name: str
    ok: bool
    last_success: datetime | None = None
    message: str = ""
    aircraft_count: int = 0
