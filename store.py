"""تخزين محلي بسيط (SQLite) لبيانات الطائرات وأحداث فقدان الإشارة."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from models import AircraftState, LandingEvent, SignalEvent, utcnow

log = logging.getLogger(__name__)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init(self) -> None:
        with self._tx() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    icao24        TEXT PRIMARY KEY,
                    registration  TEXT,
                    aircraft_type TEXT,
                    model         TEXT,
                    manufacturer  TEXT,
                    operator      TEXT,
                    updated_at    INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sightings (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    icao24      TEXT NOT NULL,
                    seen_at     INTEGER NOT NULL,
                    callsign    TEXT,
                    lat         REAL,
                    lon         REAL,
                    altitude_ft INTEGER,
                    source      TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_sightings_icao ON sightings(icao24, seen_at);
                CREATE INDEX IF NOT EXISTS idx_sightings_time ON sightings(seen_at);
                CREATE TABLE IF NOT EXISTS signal_events (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    icao24          TEXT NOT NULL,
                    callsign        TEXT,
                    classification  TEXT,
                    last_seen       INTEGER NOT NULL,
                    lat             REAL,
                    lon             REAL,
                    reason          TEXT,
                    country         TEXT,
                    country_code    TEXT,
                    nearest_airport TEXT,
                    nearest_airport_name    TEXT,
                    nearest_airport_country TEXT,
                    nearest_airport_km      REAL,
                    last_alt_ft     INTEGER,
                    last_speed_kts  INTEGER,
                    last_track      REAL,
                    last_on_ground  INTEGER,
                    last_source     TEXT,
                    geo_source      TEXT,
                    created_at      INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_time ON signal_events(created_at);
                CREATE TABLE IF NOT EXISTS landings (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    icao24             TEXT NOT NULL,
                    callsign           TEXT,
                    registration       TEXT,
                    classification     TEXT,
                    uae_related        INTEGER,
                    airport_ident      TEXT,
                    airport_name       TEXT,
                    airport_country    TEXT,
                    at                 INTEGER NOT NULL,
                    lat                REAL,
                    lon                REAL,
                    from_airport       TEXT,
                    from_airport_name  TEXT,
                    from_country       TEXT,
                    from_at            INTEGER,
                    leg_minutes        INTEGER,
                    leg_distance_km    REAL,
                    divergence         INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_landings_time ON landings(at);
                CREATE INDEX IF NOT EXISTS idx_landings_icao ON landings(icao24, at);
                CREATE TABLE IF NOT EXISTS geo_cache (
                    key          TEXT PRIMARY KEY,
                    country      TEXT,
                    country_code TEXT,
                    source       TEXT,
                    fetched_at   INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS osint_cache (
                    key        TEXT PRIMARY KEY,
                    payload    TEXT,
                    fetched_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS poll_log (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    at      INTEGER NOT NULL,
                    source  TEXT,
                    count   INTEGER,
                    ok      INTEGER,
                    message TEXT
                );
                """
            )
            self._migrate(conn)

    _EXTRA_COLUMNS: dict[str, dict[str, str]] = {
        # كل الأعمدة غير الأساسية: يسمح بترقية أي قاعدة بيانات قديمة تلقائياً
        # (قواعد البيانات الأقدم كانت تفتقد country/nearest_airport وغيرها).
        "meta": {
            "registration": "TEXT",
            "aircraft_type": "TEXT",
            "model": "TEXT",
            "manufacturer": "TEXT",
            "operator": "TEXT",
            "updated_at": "INTEGER",
        },
        "sightings": {
            "callsign": "TEXT",
            "lat": "REAL",
            "lon": "REAL",
            "altitude_ft": "INTEGER",
            "source": "TEXT",
        },
        "signal_events": {
            "callsign": "TEXT",
            "registration": "TEXT",
            "classification": "TEXT",
            "lat": "REAL",
            "lon": "REAL",
            "reason": "TEXT",
            "country": "TEXT",
            "country_code": "TEXT",
            "nearest_airport": "TEXT",
            "nearest_airport_name": "TEXT",
            "nearest_airport_country": "TEXT",
            "nearest_airport_km": "REAL",
            "last_alt_ft": "INTEGER",
            "last_speed_kts": "INTEGER",
            "last_track": "REAL",
            "last_on_ground": "INTEGER",
            "last_source": "TEXT",
            "geo_source": "TEXT",
            "route_json": "TEXT",
            "still_broadcasting": "INTEGER",
            "global_country": "TEXT",
            "global_country_code": "TEXT",
            "global_last_seen": "INTEGER",
        },
        "landings": {
            "callsign": "TEXT",
            "registration": "TEXT",
            "classification": "TEXT",
            "uae_related": "INTEGER",
            "airport_ident": "TEXT",
            "airport_name": "TEXT",
            "airport_country": "TEXT",
            "lat": "REAL",
            "lon": "REAL",
            "from_airport": "TEXT",
            "from_airport_name": "TEXT",
            "from_country": "TEXT",
            "from_at": "INTEGER",
            "leg_minutes": "INTEGER",
            "leg_distance_km": "REAL",
            "divergence": "INTEGER",
            "route_json": "TEXT",
            "new_airport": "INTEGER",
            "unplanned": "INTEGER",
        },
    }

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """يضيف الأعمدة الناقصة لقواعد البيانات القديمة دون فقدان البيانات."""
        for table, columns in self._EXTRA_COLUMNS.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, sqltype in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sqltype}")
                    log.info("ترحيل: أُضيف العمود %s.%s", table, name)

    # ---------- بيانات تعريف الطائرة ----------
    def get_meta(self, icao24: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM meta WHERE icao24 = ?", (icao24,)).fetchone()
            return dict(row) if row else None

    def save_meta(self, icao24: str, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in {
            "registration", "aircraft_type", "model", "manufacturer", "operator"
        }}
        if not fields:
            return
        fields["updated_at"] = int(time.time())
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        updates = ", ".join(f"{key}=excluded.{key}" for key in fields if key != "updated_at")
        with self._tx() as conn:
            conn.execute(
                f"INSERT INTO meta (icao24, {columns}) VALUES (?, {placeholders}) "
                f"ON CONFLICT(icao24) DO UPDATE SET {updates}",
                (icao24, *fields.values()),
            )

    # ---------- الرصد ----------
    def add_sighting(self, state: AircraftState) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO sightings (icao24, seen_at, callsign, lat, lon, altitude_ft, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    state.icao24,
                    state.last_contact or int(time.time()),
                    state.callsign,
                    state.latitude,
                    state.longitude,
                    state.altitude_ft,
                    state.source,
                ),
            )

    def add_event(self, event: SignalEvent) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO signal_events "
                "(icao24, callsign, registration, classification, last_seen, lat, lon, reason, "
                "country, country_code, nearest_airport, nearest_airport_name, "
                "nearest_airport_country, nearest_airport_km, last_alt_ft, "
                "last_speed_kts, last_track, last_on_ground, last_source, "
                "geo_source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.icao24,
                    event.callsign,
                    event.registration,
                    event.classification,
                    int(event.last_seen.timestamp()),
                    event.last_lat,
                    event.last_lon,
                    event.reason,
                    event.country,
                    event.country_code,
                    event.nearest_airport,
                    event.nearest_airport_name,
                    event.nearest_airport_country,
                    event.nearest_airport_km,
                    event.last_alt_ft,
                    event.last_speed_kts,
                    event.last_track,
                    int(event.last_on_ground),
                    event.last_source,
                    event.geo_source,
                    int(event.created_at.timestamp()),
                ),
            )

    def update_event_geo(self, event: SignalEvent) -> None:
        """يضيف اسم الدولة/المطار إلى أحدث صف لهذا الحدث بعد الجلب الجغرافي."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE signal_events SET country = ?, country_code = ?, "
                "nearest_airport = ?, nearest_airport_name = ?, "
                "nearest_airport_country = ?, nearest_airport_km = ?, geo_source = ? "
                "WHERE id = (SELECT id FROM signal_events WHERE icao24 = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1)",
                (
                    event.country,
                    event.country_code,
                    event.nearest_airport,
                    event.nearest_airport_name,
                    event.nearest_airport_country,
                    event.nearest_airport_km,
                    event.geo_source,
                    event.icao24,
                ),
            )

    def add_landing(self, landing: LandingEvent) -> None:
        from osint import route_to_json

        with self._tx() as conn:
            conn.execute(
                "INSERT INTO landings "
                "(icao24, callsign, registration, classification, uae_related, "
                "airport_ident, airport_name, airport_country, at, lat, lon, "
                "from_airport, from_airport_name, from_country, from_at, "
                "leg_minutes, leg_distance_km, divergence, route_json, "
                "new_airport, unplanned) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    landing.icao24,
                    landing.callsign,
                    landing.registration,
                    landing.classification,
                    int(landing.uae_related),
                    landing.airport_ident,
                    landing.airport_name,
                    landing.airport_country_code,
                    int(landing.at.timestamp()),
                    landing.latitude,
                    landing.longitude,
                    landing.from_airport,
                    landing.from_airport_name,
                    landing.from_country_code,
                    int(landing.from_at.timestamp()) if landing.from_at else None,
                    landing.leg_minutes,
                    landing.leg_distance_km,
                    int(landing.divergence),
                    route_to_json(landing.route),
                    int(landing.new_airport),
                    int(landing.unplanned),
                ),
            )

    def airport_visit_count(self, icao24: str, airport_ident: str) -> int:
        """كم مرة هبطت هذه الطائرة في هذا المطار سابقاً (كل التاريخ المخزّن)."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM landings WHERE icao24 = ? AND airport_ident = ?",
                (icao24, airport_ident),
            ).fetchone()
            return int(row["n"]) if row else 0

    def airports_seen(self, icao24: str, limit: int = 30) -> list[str]:
        """قائمة المطارات التي هبطت فيها الطائرة سابقاً (لرصد الوجهات الجديدة)."""
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT airport_ident, MAX(at) AS last FROM landings "
                "WHERE icao24 = ? AND airport_ident IS NOT NULL "
                "GROUP BY airport_ident ORDER BY last DESC LIMIT ?",
                (icao24, limit),
            ).fetchall()
            return [row["airport_ident"] for row in rows]

    def was_airborne_recently(
        self, icao24: str, within_minutes: int = 45, min_alt_ft: int = 1200
    ) -> bool:
        """هل شوهدت الطائرة محلّقة (فوق ارتفاع الهبوط) خلال الفترة الأخيرة؟

        يُستخدم لمنع اعتبار الطائرات المتوقفة على الأرض «هبوطاً» عند أول
        تشغيل، خصوصاً في وضع الجدولة السحابية حيث تُفقد الحالة من الذاكرة.
        """
        cutoff = int(time.time()) - within_minutes * 60
        with self._tx() as conn:
            row = conn.execute(
                "SELECT 1 FROM sightings WHERE icao24 = ? AND seen_at >= ? "
                "AND altitude_ft IS NOT NULL AND altitude_ft > ? LIMIT 1",
                (icao24, cutoff, min_alt_ft),
            ).fetchone()
            return bool(row)

    def has_recent_landing(
        self, icao24: str, airport_ident: str, within_minutes: int = 180
    ) -> bool:
        """يمنع تسجيل نفس الهبوط أكثر من مرة بين تشغيلات متتالية."""
        cutoff = int(time.time()) - within_minutes * 60
        with self._tx() as conn:
            row = conn.execute(
                "SELECT 1 FROM landings WHERE icao24 = ? AND airport_ident = ? "
                "AND at >= ? LIMIT 1",
                (icao24, airport_ident, cutoff),
            ).fetchone()
            return bool(row)

    def landings_since(self, since_ts: int, limit: int = 100) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM landings WHERE at >= ? ORDER BY at DESC LIMIT ?",
                (since_ts, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def sightings_for(self, icao24: str, since_ts: int, limit: int = 500) -> list[dict]:
        """مسار طائرة مرتّب زمنياً — يُستخدم لتحليل هندسة المسار (انحراف/قفزات)."""
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT seen_at, lat, lon, altitude_ft FROM sightings "
                "WHERE icao24 = ? AND seen_at >= ? AND lat IS NOT NULL AND lon IS NOT NULL "
                "ORDER BY seen_at ASC LIMIT ?",
                (icao24, since_ts, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_geo(self, key: str) -> dict | None:
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM geo_cache WHERE key = ?", (key,)).fetchone()
            return dict(row) if row else None

    def save_geo(self, key: str, country: str, country_code: str | None, source: str) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO geo_cache (key, country, country_code, source, fetched_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET country=excluded.country, "
                "country_code=excluded.country_code, source=excluded.source, "
                "fetched_at=excluded.fetched_at",
                (key, country, country_code, source, int(time.time())),
            )

    def get_osint(
        self, key: str, max_age: "timedelta | None" = None
    ) -> dict | None:
        """يقرأ عنصر OSINT مخزّناً (المسار/الحالة العالمية) إن لم ينتهِ أجله."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM osint_cache WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        if max_age is not None:
            age = time.time() - row["fetched_at"]
            if age > max_age.total_seconds():
                return None
        try:
            payload = json.loads(row["payload"]) if row["payload"] else None
        except (TypeError, ValueError):
            payload = None
        return {"payload": payload, "fetched_at": row["fetched_at"]}

    def save_osint(self, key: str, payload: dict | None, ttl: "timedelta") -> None:
        """يخزّن نتيجة OSINT (أو وسم «لا يوجد» عند ``payload=None``)."""
        raw = json.dumps(payload, ensure_ascii=False) if payload is not None else None
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO osint_cache (key, payload, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
                "fetched_at=excluded.fetched_at",
                (key, raw, int(time.time())),
            )

    def update_event_osint(self, event: SignalEvent) -> None:
        """يحفظ نتائج OSINT الخاصة بحدث انقطاع الإشارة."""
        from osint import route_to_json

        with self._tx() as conn:
            conn.execute(
                "UPDATE signal_events SET route_json = ?, still_broadcasting = ?, "
                "global_country = ?, global_country_code = ?, global_last_seen = ? "
                "WHERE icao24 = ? AND last_seen = ?",
                (
                    route_to_json(event.route),
                    int(event.still_broadcasting),
                    event.global_country,
                    event.global_country_code,
                    int(event.global_last_seen.timestamp()) if event.global_last_seen else None,
                    event.icao24,
                    int(event.last_seen.timestamp()),
                ),
            )

    def events_since(self, since_ts: int, limit: int = 100) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM signal_events WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                (since_ts, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def log_poll(self, source: str, count: int, ok: bool, message: str = "") -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO poll_log (at, source, count, ok, message) VALUES (?, ?, ?, ?, ?)",
                (int(time.time()), source, count, int(ok), message[:300]),
            )

    def prune(self, keep_days: int = 7) -> None:
        cutoff = int(time.time()) - keep_days * 86400
        with self._tx() as conn:
            for table, column in (
                ("sightings", "seen_at"),
                ("signal_events", "created_at"),
                ("landings", "at"),
                ("poll_log", "at"),
            ):
                conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))

    def stats(self) -> dict:
        with self._tx() as conn:
            return {
                "known_aircraft": conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0],
                "sightings": conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0],
                "signal_events": conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0],
                "landings": conn.execute("SELECT COUNT(*) FROM landings").fetchone()[0],
            }


# ----------------------------------------------------------------------
# محوّلات الصف → كائن
# ----------------------------------------------------------------------
def _dt(value) -> "datetime | None":
    """يحوّل طابعاً زمنياً يونكس (ثوان) إلى datetime بتوقيت UTC."""
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def event_from_row(row: dict) -> SignalEvent:
    """يبني ``SignalEvent`` من صف جدول ``signal_events``.

    ضروري في التشغيل السحابي: كل دورة تبدأ بذاكرة فارغة، فقراءة الأحداث من
    القاعدة هي الطريقة الوحيدة لبناء تقرير الـ24 ساعة ومصفوفة الساحل.
    """
    from osint import route_from_json

    return SignalEvent(
        icao24=row.get("icao24") or "",
        callsign=row.get("callsign"),
        classification=row.get("classification") or "unknown",
        last_seen=_dt(row.get("last_seen")) or utcnow(),
        last_lat=row.get("lat"),
        last_lon=row.get("lon"),
        registration=row.get("registration"),
        reason=row.get("reason") or "unknown",
        country=row.get("country"),
        country_code=row.get("country_code"),
        nearest_airport=row.get("nearest_airport"),
        nearest_airport_name=row.get("nearest_airport_name"),
        nearest_airport_country=row.get("nearest_airport_country"),
        nearest_airport_km=row.get("nearest_airport_km"),
        last_alt_ft=row.get("last_alt_ft"),
        last_speed_kts=row.get("last_speed_kts"),
        last_track=row.get("last_track"),
        last_on_ground=bool(row.get("last_on_ground")),
        last_source=row.get("last_source") or "",
        geo_source=row.get("geo_source") or "",
        route=route_from_json(row.get("route_json")),
        still_broadcasting=bool(row.get("still_broadcasting")),
        global_country=row.get("global_country"),
        global_country_code=row.get("global_country_code"),
        global_last_seen=_dt(row.get("global_last_seen")),
        created_at=_dt(row.get("created_at")) or utcnow(),
    )


def landing_from_row(row: dict) -> LandingEvent:
    """يبني ``LandingEvent`` من صف جدول ``landings`` (نفس سبب ``event_from_row``)."""
    from osint import route_from_json

    return LandingEvent(
        icao24=row.get("icao24") or "",
        callsign=row.get("callsign"),
        registration=row.get("registration"),
        classification=row.get("classification") or "unknown",
        uae_related=bool(row.get("uae_related")),
        airport_ident=row.get("airport_ident"),
        airport_name=row.get("airport_name"),
        airport_country_code=row.get("airport_country"),
        at=_dt(row.get("at")) or utcnow(),
        latitude=row.get("lat"),
        longitude=row.get("lon"),
        from_airport=row.get("from_airport"),
        from_airport_name=row.get("from_airport_name"),
        from_country_code=row.get("from_country"),
        from_at=_dt(row.get("from_at")),
        leg_minutes=row.get("leg_minutes"),
        leg_distance_km=row.get("leg_distance_km"),
        divergence=bool(row.get("divergence")),
        route=route_from_json(row.get("route_json")),
        new_airport=bool(row.get("new_airport")),
        unplanned=bool(row.get("unplanned")),
    )
