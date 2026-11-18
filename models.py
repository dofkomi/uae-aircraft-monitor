"""هياكل البيانات المستخدمة في البوت."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    reason: str = "unknown"   # no_broadcast | left_coverage | unknown
    country: str | None = None        # الدولة التي انقطع فوقها الإشارة
    country_code: str | None = None
    nearest_airport: str | None = None
    geo_source: str = ""
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
    from_country_code: str | None = None
    divergence: bool = False   # هبطت بالخارج ثم واصلت إلى وجهة غير الإمارات


@dataclass
class SourceHealth:
    name: str
    ok: bool
    last_success: datetime | None = None
    message: str = ""
    aircraft_count: int = 0
