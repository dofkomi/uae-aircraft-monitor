"""محرك التتبّع: جلب، دمج، تصنيف، فقدان الإشارة، وتتبّع الهبوط والوجهات."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from adsb_sources import BaseSource, WatchlistSource
from aircraft_db import AircraftDB
from airports import AirportDB, country_name
from classifier import classify, is_uae_related
from config import settings
from geo import GeoResolver
from models import AircraftState, LandingEvent, SignalEvent
from store import Store

log = logging.getLogger(__name__)

KNOWN_RETENTION = timedelta(hours=6)
MAX_RECENT_EVENTS = 200
MAX_GEOCODE_PER_POLL = 8
MAX_LEG_AGE = timedelta(hours=24)


class AircraftTracker:
    def __init__(
        self,
        sources: list[BaseSource],
        aircraft_db: AircraftDB,
        store: Store,
        geo: GeoResolver | None = None,
        airports: AirportDB | None = None,
    ) -> None:
        self.sources = sources
        self.db = aircraft_db
        self.store = store
        self.geo = geo
        self.airports = airports

        self.current: dict[str, AircraftState] = {}
        self.known: dict[str, tuple[AircraftState, datetime]] = {}
        self.classifications: dict[str, str] = {}
        self.missed: dict[str, int] = {}
        self.reported_missing: set[str] = set()
        self.recent_events: list[SignalEvent] = []
        self.recent_landings: list[LandingEvent] = []

        # حالة الهبوط/الوجهة
        self.on_ground_now: set[str] = set()
        self.last_landing: dict[str, LandingEvent] = {}

        self.last_poll: datetime | None = None
        self.poll_count = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    async def poll(self) -> dict[str, AircraftState]:
        async with self._lock:
            results = await asyncio.gather(
                *(source.fetch() for source in self.sources),
                return_exceptions=True,
            )

            merged: dict[str, AircraftState] = {}
            for source, result in zip(self.sources, results):
                if isinstance(result, BaseException):
                    log.warning("[%s] استثناء أثناء الجلب: %s", source.name, result)
                    source.health.ok = False
                    source.health.message = str(result)[:300]
                    self.store.log_poll(source.name, 0, False, str(result))
                    continue

                for state in result:
                    previous = merged.get(state.icao24)
                    if previous:
                        previous.merge_missing(state)
                    else:
                        merged[state.icao24] = state
                self.store.log_poll(source.name, len(result), source.health.ok, source.health.message)

            # إثراء الطائرات الجديدة أو التي تفتقر لنوعها (حدّ أقصى لحماية المصادر المجانية)
            enrich_targets = [
                state for state in merged.values()
                if state.icao24 not in self.known or not state.aircraft_type
            ][:40]
            if enrich_targets:
                await asyncio.gather(
                    *(self.db.enrich(state) for state in enrich_targets),
                    return_exceptions=True,
                )

            now = datetime.now(timezone.utc)
            seen_ids = set(merged)

            # تحديث الطائرات المرصودة الآن
            for icao, state in merged.items():
                self.known[icao] = (state, now)
                self.classifications[icao] = classify(state)
                self.missed[icao] = 0
                self.reported_missing.discard(icao)
                self.store.add_sighting(state)

            # إضافة الطائرات المرتبطة بالإمارات إلى القائمة لتتبّعها عالمياً
            if settings.watchlist_dynamic:
                watch = next(
                    (item for item in self.sources if isinstance(item, WatchlistSource)), None
                )
                if watch:
                    for icao, state in merged.items():
                        if is_uae_related(state):
                            watch.add(icao)

            # كشف الغياب وتحديد الدولة التي انقطع فوقها الإشارة
            threshold = settings.signal_loss_minutes * 60
            cutoff_known = now - KNOWN_RETENTION
            new_events: list[SignalEvent] = []
            for icao, (state, observed_at) in list(self.known.items()):
                if icao in seen_ids:
                    continue
                if observed_at < cutoff_known:
                    self.known.pop(icao, None)
                    self.classifications.pop(icao, None)
                    self.missed.pop(icao, None)
                    self.reported_missing.discard(icao)
                    continue

                self.missed[icao] = self.missed.get(icao, 0) + 1
                last_contact = state.last_contact
                last_seen = (
                    datetime.fromtimestamp(last_contact, tz=timezone.utc)
                    if last_contact else observed_at
                )
                age = (now - last_seen).total_seconds()
                if age >= threshold and icao not in self.reported_missing:
                    event = SignalEvent(
                        icao24=icao,
                        callsign=state.callsign,
                        classification=self.classifications.get(icao, "unknown"),
                        last_seen=last_seen,
                        last_lat=state.latitude,
                        last_lon=state.longitude,
                        reason=self._guess_reason(state),
                    )
                    new_events.append(event)
                    self.recent_events.append(event)
                    self.recent_events = self.recent_events[-MAX_RECENT_EVENTS:]
                    self.store.add_event(event)
                    self.reported_missing.add(icao)
                    log.info("حدث فقدان إشارة: %s (%s)", icao, event.reason)

            await self._enrich_events(new_events)

            # كشف الهبوط وتغيّر الوجهة
            self._detect_landings(merged, now)

            self.current = merged
            self.last_poll = now
            self.poll_count += 1
            return merged

    # ------------------------------------------------------------------
    async def _enrich_events(self, events: list[SignalEvent]) -> None:
        """يضيف اسم الدولة وأقرب مطار لكل حدث فقدان إشارة جديد."""
        if not self.geo or not events:
            return
        for event in events[:MAX_GEOCODE_PER_POLL]:
            try:
                country, code, source = await self.geo.lookup(event.last_lat, event.last_lon)
            except Exception as exc:  # noqa: BLE001 - لا يوقف التتبع بسبب فشل جغرافي
                log.debug("تعذّر تحديد دولة الحدث %s: %s", event.icao24, exc)
                continue
            event.country = country
            event.country_code = code
            event.geo_source = source
            if self.airports and event.last_lat is not None and event.last_lon is not None:
                airport = self.airports.nearest(event.last_lat, event.last_lon, 50)
                if airport:
                    event.nearest_airport = airport.ident
            # تحديث الصف المُسجَّل مسبقاً بالبيانات الجغرافية دون تكراره
            self.store.update_event_geo(event)

    def _detect_landings(self, merged: dict[str, AircraftState], now: datetime) -> None:
        """يرصد هبوط الطائرة في مطار، ويقارن بآخر هبوط لاكتشاف وجهة غير الإمارات."""
        if not self.airports:
            return

        for icao, state in merged.items():
            altitude = state.altitude_ft
            speed = state.speed_kts
            landed = state.on_ground or (
                altitude is not None and altitude <= settings.landing_alt_ft
                and speed is not None and speed <= settings.landing_speed_kts
            )

            if not landed:
                self.on_ground_now.discard(icao)
                continue
            if icao in self.on_ground_now:
                continue  # نفس الهبوط في دورات متتالية

            if state.latitude is None or state.longitude is None:
                continue
            airport = self.airports.nearest(
                state.latitude, state.longitude, settings.landing_max_km
            )
            if not airport:
                continue

            self.on_ground_now.add(icao)
            previous = self.last_landing.get(icao)
            from_airport: str | None = None
            from_country: str | None = None
            divergence = False

            if previous and now - previous.at <= MAX_LEG_AGE:
                from_airport = previous.airport_ident
                from_country = previous.airport_country_code
                if (
                    previous.airport_ident != airport.ident
                    and from_country != "AE"
                    and airport.country_code != "AE"
                ):
                    divergence = True

            uae_related = is_uae_related(state)
            landing = LandingEvent(
                icao24=icao,
                callsign=state.callsign,
                registration=state.registration,
                classification=self.classifications.get(icao, "unknown"),
                uae_related=uae_related,
                airport_ident=airport.ident,
                airport_name=airport.name,
                airport_country_code=airport.country_code,
                at=now,
                latitude=state.latitude,
                longitude=state.longitude,
                from_airport=from_airport,
                from_country_code=from_country,
                divergence=divergence,
            )
            self.recent_landings.append(landing)
            self.recent_landings = self.recent_landings[-MAX_RECENT_EVENTS:]
            self.store.add_landing(landing)
            self.last_landing[icao] = landing

            if divergence:
                log.info(
                    "وجهة غير الإمارات: %s من %s إلى %s",
                    icao, from_airport, airport.ident,
                )

    # ------------------------------------------------------------------
    @staticmethod
    def _guess_reason(state: AircraftState) -> str:
        """تخمين سبب الغياب من آخر موقع معروف (تقديري فقط).

        ملاحظة مهمة: بيانات ADS-B العامة لا تستطيع الجزم بأن جهاز التعريف
        أُغلق قصداً؛ الغياب قد يكون بسبب خروج الطائرة من نطاق الاستقبال
        أو فجوة في أجهزة الاستقبال التطوعية.
        """
        lat, lon = state.latitude, state.longitude
        if lat is None or lon is None:
            return "unknown"
        lamin, lomin, lamax, lomax = settings.bbox
        margin = 0.6  # درجة تقريباً ~60 كم
        near_edge = (
            lat <= lamin + margin or lat >= lamax - margin
            or lon <= lomin + margin or lon >= lomax - margin
        )
        outside = not (lamin <= lat <= lamax and lomin <= lon <= lomax)
        if outside or near_edge:
            return "left_coverage"
        return "no_broadcast"

    def events_since(self, since: datetime) -> list[SignalEvent]:
        return [event for event in self.recent_events if event.created_at >= since]

    def landings_since(self, since: datetime) -> list[LandingEvent]:
        return [item for item in self.recent_landings if item.at >= since]

    def counts(self) -> dict[str, int]:
        counts = {"military": 0, "civilian": 0, "unknown": 0}
        for icao in self.current:  # نحسب الطائرات المرصودة حالياً فقط
            classification = self.classifications.get(icao, "unknown")
            counts[classification] = counts.get(classification, 0) + 1
        return {
            "current": len(self.current),
            "known": len(self.known),
            **counts,
        }
