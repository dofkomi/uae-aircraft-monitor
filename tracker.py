"""محرك التتبّع: جلب، دمج، تصنيف، فقدان الإشارة، وتتبّع الهبوط والوجهات."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from adsb_sources import BaseSource, WatchlistSource
from aircraft_db import AircraftDB
from airports import AirportDB, country_name, haversine_km
from classifier import classify, is_uae_related, is_uae_related_event
from config import settings
from geo import GeoResolver
from models import AircraftState, LandingEvent, RouteInfo, SignalEvent
from osint import OsintClient
from store import Store

log = logging.getLogger(__name__)

KNOWN_RETENTION = timedelta(hours=6)
MAX_RECENT_EVENTS = 200
# سقف قراءة النافذة الزمنية من القاعدة (24 ساعة قد تحوي مئات الأحداث)
DB_WINDOW_LIMIT = 1000
MAX_GEOCODE_PER_POLL = 8
MAX_ROUTES_PER_POLL = 12
MAX_OSINT_EVENTS_PER_POLL = 8
MAX_LEG_AGE = timedelta(hours=24)


class AircraftTracker:
    def __init__(
        self,
        sources: list[BaseSource],
        aircraft_db: AircraftDB,
        store: Store,
        geo: GeoResolver | None = None,
        airports: AirportDB | None = None,
        osint: OsintClient | None = None,
    ) -> None:
        self.sources = sources
        self.db = aircraft_db
        self.store = store
        self.geo = geo
        self.airports = airports
        self.osint = osint

        self.current: dict[str, AircraftState] = {}
        self.known: dict[str, tuple[AircraftState, datetime]] = {}
        self.classifications: dict[str, str] = {}
        self.missed: dict[str, int] = {}
        self.reported_missing: set[str] = set()
        self.recent_events: list[SignalEvent] = []
        self.recent_landings: list[LandingEvent] = []

        # حالة الهبوط/الوجهة
        self.on_ground_now: set[str] = set()
        self.airborne_seen: set[str] = set()
        self.last_landing: dict[str, LandingEvent] = {}

        self.last_poll: datetime | None = None
        self.poll_count = 0
        self._lock = asyncio.Lock()

        self._restore_watchlist()

    def _restore_watchlist(self) -> None:
        """يستعيد قائمة التتبّع العالمي من القاعدة.

        كل تشغيل سحابي يبدأ بذاكرة فارغة؛ بدون الاستعادة تُنسى الطائرات
        الإماراتية التي رُصدت في الدورات السابقة، فيتوقّف التتبّع العالمي
        (ونطاق الساحل) عند حدود مستقبلات الإمارات.
        """
        watch = next(
            (item for item in self.sources if isinstance(item, WatchlistSource)), None
        )
        if watch is None:
            return
        try:
            saved = self.store.watchlist()
        except Exception as exc:  # noqa: BLE001 - لا نُسقط التشغيل بسبب القاعدة
            log.warning("تعذّرت استعادة قائمة التتبّع: %s", exc)
            return
        for icao in saved:
            watch.add(icao)
        if saved:
            log.info("استُعيدت %d طائرة إلى قائمة التتبّع العالمي", len(saved))

    # ------------------------------------------------------------------
    async def poll(self) -> dict[str, AircraftState]:
        async with self._lock:
            if self.osint:
                self.osint.reset_poll_budget()
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

            # إثراء OSINT: مسار الرحلة للطائرات الإماراتية (adsbdb، مجاني)
            await self._enrich_routes(merged)

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
                        if is_uae_related(state) and icao not in watch.extra_icaos:
                            watch.add(icao)
                            # الحفظ في القاعدة ضروري: كل تشغيل سحابي ذاكرة جديدة.
                            self.store.add_watch(icao)

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
                    self.airborne_seen.discard(icao)
                    self.on_ground_now.discard(icao)
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
                        registration=state.registration,
                        reason=self._guess_reason(state),
                        last_alt_ft=state.altitude_ft,
                        last_speed_kts=state.speed_kts,
                        last_track=state.true_track,
                        last_on_ground=state.on_ground,
                        last_source=state.source,
                    )
                    new_events.append(event)
                    self.recent_events.append(event)
                    self.recent_events = self.recent_events[-MAX_RECENT_EVENTS:]
                    self.store.add_event(event)
                    self.reported_missing.add(icao)
                    log.info("حدث فقدان إشارة: %s (%s)", icao, event.reason)

            await self._enrich_events(new_events)

            # كشف الهبوط وتغيّر الوجهة
            new_landings = self._detect_landings(merged, now)
            await self._enrich_landings(new_landings)
            for landing in new_landings:
                # يُحسب بعد جلب المسار المجدول: مخالفة الوجهة أو مطار جديد
                landing.unplanned = landing.route_mismatch or landing.new_airport
                self.store.add_landing(landing)
                if landing.unplanned:
                    log.info(
                        "وجهة غير مبرمجة: %s → %s (%s)",
                        landing.icao24,
                        landing.airport_ident,
                        landing.unplanned_reason,
                    )

            self.current = merged
            self.last_poll = now
            self.poll_count += 1
            return merged

    # ------------------------------------------------------------------
    async def _enrich_events(self, events: list[SignalEvent]) -> None:
        """يضيف الدولة (أو الدولة القريبة) وأقرب مطار لأحداث فقدان الإشارة.

        التركيز على الطائرات الإماراتية/المستأجرة إماراتياً: لها تُحدَّد الدولة
        التي انقطع فوقها البث، وإن كانت النقطة فوق المياه تُذكر **الدولة
        القريبة**. أما الطائرات الأخرى فيُحدَّد موقعها محلياً فقط بلا شبكة
        وبلا استهلاك حصة Nominatim.
        """
        if not events:
            return

        uae_events: list[SignalEvent] = []
        other_events: list[SignalEvent] = []
        for event in events:
            (uae_events if is_uae_related_event(event) else other_events).append(event)

        shapes = getattr(self.geo, "shapes", None) if self.geo else None
        if self.geo is not None:
            if shapes is None:
                # لا حدود محلية: السلوك القديم (Nominatim/أقرب مطار) للجميع
                for event in events[:MAX_GEOCODE_PER_POLL]:
                    await self._enrich_event(event)
            else:
                # غير الإماراتية: تحديد محلي سريع فقط، دون تخمين دولة قريبة
                for event in other_events:
                    try:
                        name, code, source = shapes.lookup(event.last_lat, event.last_lon)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("تعذّر تحديد موقع الحدث %s: %s", event.icao24, exc)
                        continue
                    self._apply_event_geo(event, name, code, source if name else "unknown")

                # الإماراتية: محلي → الدولة القريبة → Nominatim عند الحاجة
                for event in uae_events[:MAX_GEOCODE_PER_POLL]:
                    await self._enrich_event(event)

        # إثراء OSINT للطائرات الإماراتية فقط: المسار + البث العالمي
        for event in uae_events[:MAX_OSINT_EVENTS_PER_POLL]:
            await self._enrich_event_osint(event)

    async def _enrich_event(self, event: SignalEvent) -> None:
        try:
            country, code, source = await self.geo.lookup(event.last_lat, event.last_lon)
        except Exception as exc:  # noqa: BLE001 - لا يوقف التتبع بسبب فشل جغرافي
            log.debug("تعذّر تحديد دولة الحدث %s: %s", event.icao24, exc)
            return
        self._apply_event_geo(event, country, code, source)

    def _apply_event_geo(
        self,
        event: SignalEvent,
        country: str | None,
        code: str | None,
        source: str,
    ) -> None:
        event.country = country
        event.country_code = code
        event.geo_source = source
        if self.airports and event.last_lat is not None and event.last_lon is not None:
            airport = self.airports.nearest(event.last_lat, event.last_lon, 50)
            if airport:
                event.nearest_airport = airport.ident
                event.nearest_airport_name = airport.name
                event.nearest_airport_country = airport.country_code
                event.nearest_airport_km = haversine_km(
                    event.last_lat, event.last_lon, airport.lat, airport.lon
                )
        # تحديث الصف المُسجَّل مسبقاً بالبيانات الجغرافية دون تكراره
        self.store.update_event_geo(event)

    # ------------------------------------------------------------------ OSINT
    async def _enrich_routes(self, merged: dict[str, AircraftState]) -> None:
        """يجلب مسار الرحلة (adsbdb) للطائرات الإماراتية الحالية.

        المسار يوضّح «من أين وإلى أين» بدل رموز المطارات وحدها، ويُستخدم
        لاحقاً لمقارنة الوجهة المجدولة بالهبوط الفعلي.
        """
        if not self.osint:
            return
        targets = [
            state for state in merged.values()
            if state.route is None and state.callsign and is_uae_related(state)
        ][:MAX_ROUTES_PER_POLL]
        if not targets:
            return
        routes = await asyncio.gather(
            *(self.osint.route(state.callsign) for state in targets),
            return_exceptions=True,
        )
        for state, route in zip(targets, routes):
            if isinstance(route, BaseException):
                log.debug("تعذّر جلب مسار %s: %s", state.callsign, route)
                continue
            state.route = route

    async def _enrich_landings(self, landings: list[LandingEvent]) -> None:
        """يضيف المسار المجدول لهبوطات الطائرات الإماراتية (لمقارنة الوجهة)."""
        if not self.osint or not landings:
            return
        targets = [item for item in landings if item.uae_related][:6]
        if not targets:
            return
        routes = await asyncio.gather(
            *(self.osint.route(item.callsign) for item in targets),
            return_exceptions=True,
        )
        for landing, route in zip(targets, routes):
            if isinstance(route, BaseException):
                continue
            landing.route = route

    async def _enrich_event_osint(self, event: SignalEvent) -> None:
        """مسار الرحلة + هل ما تزال تبث عالمياً بعد انقطاعها عنّا؟

        سؤال adsb.lol عن الطائرة عالمياً يفصل بين حالتين مختلفتين تماماً:
          * ما تزال تبث في مكان آخر → الإشارة **لم تنقطع**، بل خرجت من
            نطاق مستقبلاتنا (أو من تغطيتنا الإقليمية).
          * لا رصد عالمي → احتمال إغلاق البث أو وجودها خارج تغطية الشبكة.
        """
        if not self.osint:
            return
        if event.route is None:
            event.route = await self.osint.route(event.callsign)

        state = await self.osint.global_state(event.icao24)
        if not state:
            return
        event.still_broadcasting = True
        event.global_lat = state.get("lat")
        event.global_lon = state.get("lon")
        event.global_alt_ft = state.get("alt_ft")
        seen_at = state.get("seen_at")
        if seen_at:
            try:
                event.global_last_seen = datetime.fromtimestamp(
                    int(seen_at) / 1000, tz=timezone.utc
                )
            except (ValueError, OSError, TypeError):
                event.global_last_seen = None
        country, code = self._country_for(event.global_lat, event.global_lon)
        event.global_country = country
        event.global_country_code = code
        try:
            self.store.update_event_osint(event)
        except Exception as exc:  # noqa: BLE001 - التخزين لا يوقف التقرير
            log.debug("تعذّر حفظ OSINT للحدث %s: %s", event.icao24, exc)

    def _country_for(
        self, lat: float | None, lon: float | None
    ) -> tuple[str | None, str | None]:
        """دولة نقطة (مع الدولة القريبة عند الوقوع فوق المياه)."""
        shapes = getattr(self.geo, "shapes", None) if self.geo else None
        if shapes is None or lat is None or lon is None:
            return None, None
        try:
            name, code, _ = shapes.lookup(lat, lon)
            if name:
                return name, code
            near = shapes.nearest(lat, lon, settings.nearest_country_max_km)
            if near:
                return near[0], near[1]
        except Exception as exc:  # noqa: BLE001
            log.debug("تعذّر تحديد دولة النقطة العالمية: %s", exc)
        return None, None

    def _detect_landings(
        self, merged: dict[str, AircraftState], now: datetime
    ) -> list[LandingEvent]:
        """يرصد هبوط الطائرة في مطار، ويقارن بآخر هبوط لاكتشاف وجهة غير الإمارات."""
        if not self.airports:
            return []
        new_landings: list[LandingEvent] = []

        for icao, state in merged.items():
            altitude = state.altitude_ft
            speed = state.speed_kts
            landed = state.on_ground or (
                altitude is not None and altitude <= settings.landing_alt_ft
                and speed is not None and speed <= settings.landing_speed_kts
            )

            if not landed:
                self.on_ground_now.discard(icao)
                self.airborne_seen.add(icao)
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

            # لا نسجّل «هبوطاً» إلا إذا شوهدت الطائرة محلّقة قبله (في هذه
            # العملية أو في الرصد المحفوظ)، حتى لا تُعدّ الطائرات المتوقفة
            # على الأرض هبوطاً عند أول تشغيل أو في وضع الجدولة السحابية.
            was_airborne = icao in self.airborne_seen or self.store.was_airborne_recently(
                icao, min_alt_ft=settings.landing_alt_ft
            )
            if not was_airborne:
                continue
            if self.store.has_recent_landing(icao, airport.ident):
                self.on_ground_now.add(icao)
                self.airborne_seen.discard(icao)
                continue

            self.on_ground_now.add(icao)
            self.airborne_seen.discard(icao)
            previous = self.last_landing.get(icao)
            from_airport: str | None = None
            from_airport_name: str | None = None
            from_country: str | None = None
            from_at: datetime | None = None
            leg_minutes: int | None = None
            leg_distance_km: float | None = None
            divergence = False

            if previous and now - previous.at <= MAX_LEG_AGE:
                from_airport = previous.airport_ident
                from_airport_name = previous.airport_name
                from_country = previous.airport_country_code
                from_at = previous.at
                leg_minutes = int((now - previous.at).total_seconds() // 60)
                if previous.latitude is not None and previous.longitude is not None:
                    leg_distance_km = haversine_km(
                        previous.latitude, previous.longitude,
                        state.latitude, state.longitude,
                    )
                if (
                    previous.airport_ident != airport.ident
                    and from_country != "AE"
                    and airport.country_code != "AE"
                ):
                    divergence = True

            uae_related = is_uae_related(state)
            # «وجهة غير مبرمجة سابقاً»: مطار لم تُرصد فيه هذه الطائرة قبل الآن.
            # نشترط وجود تاريخ سابق للطائرة حتى لا تُعدّ أول رحلة مرصودة شذوذاً.
            prior_visits = self.store.airport_visit_count(icao, airport.ident)
            new_airport = prior_visits == 0 and bool(self.store.airports_seen(icao, limit=1))
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
                from_airport_name=from_airport_name,
                from_country_code=from_country,
                from_at=from_at,
                leg_minutes=leg_minutes,
                leg_distance_km=leg_distance_km,
                divergence=divergence,
                new_airport=new_airport,
            )
            self.recent_landings.append(landing)
            self.recent_landings = self.recent_landings[-MAX_RECENT_EVENTS:]
            new_landings.append(landing)
            self.last_landing[icao] = landing

            if divergence:
                log.info(
                    "وجهة غير الإمارات: %s من %s إلى %s",
                    icao, from_airport, airport.ident,
                )
        return new_landings

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
        """أحداث فقدان الإشارة داخل النافذة: الذاكرة + قاعدة البيانات.

        في التشغيل السحابي تبدأ الذاكرة فارغة في كل دورة، فقراءة القاعدة
        ضرورية وإلا بقي تقرير الـ24 ساعة ومصفوفة الساحل فارغين دائماً.
        """
        from store import event_from_row

        merged: dict[tuple[str, int], SignalEvent] = {}
        for event in self.recent_events:
            if event.created_at >= since:
                merged[(event.icao24, int(event.last_seen.timestamp()))] = event
        try:
            rows = self.store.events_since(
                int(since.timestamp()), limit=DB_WINDOW_LIMIT
            )
        except Exception as exc:  # noqa: BLE001 - لا نُسقط التقرير بسبب القاعدة
            log.warning("تعذّرت قراءة أحداث فقدان الإشارة من القاعدة: %s", exc)
            rows = []
        for row in rows:
            event = event_from_row(row)
            merged.setdefault((event.icao24, int(event.last_seen.timestamp())), event)
        return sorted(merged.values(), key=lambda item: item.created_at)

    def landings_since(self, since: datetime) -> list[LandingEvent]:
        """هبوطات النافذة: الذاكرة + قاعدة البيانات (نفس سبب ``events_since``)."""
        from store import landing_from_row

        merged: dict[tuple[str, int, str], LandingEvent] = {}
        for item in self.recent_landings:
            if item.at >= since:
                merged[(item.icao24, int(item.at.timestamp()), item.airport_ident or "")] = item
        try:
            rows = self.store.landings_since(
                int(since.timestamp()), limit=DB_WINDOW_LIMIT
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("تعذّرت قراءة الهبوطات من القاعدة: %s", exc)
            rows = []
        for row in rows:
            item = landing_from_row(row)
            merged.setdefault(
                (item.icao24, int(item.at.timestamp()), item.airport_ident or ""), item
            )
        return sorted(merged.values(), key=lambda item: item.at)

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
