import json
import os
import random
import re
from datetime import datetime, timezone

import scrapy
from scrapy.http import Response

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.2478.80",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]


class YatraDemoSpider(scrapy.Spider):
    name = "yatra_demo"
    allowed_domains = ["www.yatra.com", "flight.yatra.com", "secure.yatra.com", "127.0.0.1"]
    custom_settings = {
        "DOWNLOAD_TIMEOUT": 120,
        "PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT": 60000,
        "PLAYWRIGHT_LAUNCH_OPTIONS": {
            "headless": True,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        },
        "PLAYWRIGHT_CONTEXT_ARGS": {
            "viewport": {"width": 1440, "height": 1100},
            "java_script_enabled": True,
            "ignore_https_errors": True,
        },
    }

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        spider.target_url = (
            os.getenv("OTA_TARGET_URL")
            or crawler.settings.get("OTA_TARGET_URL")
            or "https://flight.yatra.com/air-service/dom2/price?searchId=614b8271-37a1-4edc-9102-38fb5ee2429a&msid=614b8271-37a1-4edc-9102-38fb5ee2429a&mode=Background&bpc=true&isSR=false&unique=1787948125668&variation=0&specialFareFlag=undefined&flightIdCSV=DELBOMIX1605EP20260830_AIRASIAAPI&flightPrice=6900&sc=AIRASIAAPI&dfc=false"
        )
        spider.landing_url = (
            os.getenv("OTA_LANDING_URL")
            or crawler.settings.get("OTA_LANDING_URL")
            or spider.target_url
        )
        return spider

    def start_requests(self):
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": "https://www.yatra.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Origin": "https://flight.yatra.com",
            "X-Requested-With": "XMLHttpRequest",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        yield scrapy.Request(
            self.landing_url,
            callback=self.parse_browser_session,
            headers=headers,
            errback=self.handle_failure,
            meta={
                "playwright": True,
                "playwright_include_page": True,
                "download_timeout": 120000,
            },
        )

    async def start(self):
        for request in self.start_requests():
            yield request

    def handle_failure(self, failure):
        self.logger.warning("Request failed for %s: %s", self.target_url, failure.value)
        return []

    async def parse_browser_session(self, response: Response):
        page = response.meta.get("playwright_page")
        if page is None:
            self.logger.warning("Yatra landing page did not provide a Playwright page")
            return

        api_response = None
        try:
            content_type = response.headers.get("Content-Type", b"").decode().lower()
            if "json" in content_type:
                payload = json.loads(response.text)
                flights = self._extract_api_flights(payload)
                if flights:
                    for flight in flights:
                        yield self._to_output_item(flight)
                    return

            await page.wait_for_load_state("domcontentloaded")
            api_response = await page.goto(
                self.target_url,
                wait_until="domcontentloaded",
                timeout=120000,
            )
            if api_response is None:
                self.logger.warning("Yatra price request returned no browser response")
                return

            body = await api_response.text()
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self.logger.warning(
                    "Yatra price request returned non-JSON content (status=%s)",
                    api_response.status,
                )
                return

            flights = self._extract_api_flights(payload)
            if not flights:
                self.logger.warning(
                    "Yatra API returned no flights (status=%s, url=%s)",
                    api_response.status,
                    self.target_url,
                )
                return

            for flight in flights:
                yield self._to_output_item(flight)
        except Exception as exc:  # noqa: BLE001 - browser failures are logged per run
            self.logger.warning("Browser session failed for %s: %s", self.target_url, exc)
        finally:
            await page.close()

    def _parse_price(self, value: str) -> float | None:
        if value is None:
            return None
        m = re.search(r"\d[\d,\.]*", value.replace("₹", "").replace("INR", ""))
        if not m:
            return None
        return float(m.group(0).replace(",", ""))

    def _extract_json_ld(self, response: Response):
        for sel in response.xpath("//script[@type='application/ld+json']/text()").getall():
            try:
                parsed = json.loads(sel)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                yield parsed
            elif isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        yield item

    def _extract_flights(self, response: Response):
        try:
            payload = response.json()
        except Exception:
            payload = None

        if payload:
            route_flights = self._extract_api_flights(payload)
            if route_flights:
                return route_flights

        flights = []

        for data in self._extract_json_ld(response):
            if isinstance(data, dict) and data.get("@type") in {"FlightOffer", "FlightReservation", "ItemList"}:
                flights.append(data)

        if flights:
            return flights

        cards = response.css("article, div.flight-card, li.flight-item, div.result-card")
        for card in cards:
            airline = card.css(".airline-name::text, .carrier-name::text, .airline::text").get() or "unknown"
            departure = card.css(".dep-time::text, .departure-time::text, .time-depart::text").get() or "00:00"
            arrival = card.css(".arr-time::text, .arrival-time::text, .time-arrive::text").get() or "00:00"
            duration = card.css(".duration::text, .flight-duration::text, .duration-text::text").get() or "N/A"
            price = card.css(".price::text, .fare::text, .amount::text, .final-price::text").get() or "0"
            flights.append({
                "airline": airline.strip(),
                "departure_time": departure.strip(),
                "arrival_time": arrival.strip(),
                "flight_duration": duration.strip(),
                "price_inr": self._parse_price(price) or 0,
            })

        if not flights:
            for block in response.css("script::text").getall():
                if "price" in block.lower() and "flight" in block.lower():
                    try:
                        root = json.loads(block)
                        if isinstance(root, dict):
                            flights.append(root)
                    except Exception:
                        continue

        return flights

    def parse(self, response: Response, **kwargs):
        extracted = self._extract_flights(response)
        if not extracted:
            self.logger.warning("No flight data found for %s; page may be blocked or the page structure changed.", response.url)
            return

        for item in extracted:
            if not isinstance(item, dict):
                continue

            yield self._to_output_item(item)

    def _to_output_item(self, item):
        if not isinstance(item, dict):
            return None

        airline = item.get("airline") or item.get("carrierName") or item.get("airlineName") or "unknown"
        departure = item.get("departure_time") or item.get("departureTime") or "00:00"
        arrival = item.get("arrival_time") or item.get("arrivalTime") or "00:00"
        duration = item.get("flight_duration") or item.get("flightDuration") or item.get("duration") or "N/A"
        price = item.get("price_inr") or item.get("price") or item.get("fare") or item.get("totalPrice") or 0

        return {
            "source": "yatra",
            "route": item.get("route") or "DEL-BOM",
            "airline": str(airline).strip(),
            "departure_time": str(departure).strip(),
            "arrival_time": str(arrival).strip(),
            "flight_duration": str(duration).strip(),
            "price_inr": float(self._parse_price(str(price)) if isinstance(price, str) else price or 0),
            "currency": "INR",
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }

    def _extract_api_flights(self, payload: dict):
        if not isinstance(payload, dict):
            return []

        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            return []

        schedule = data.get("fltSchedule") or {}
        fare_details = data.get("fareDetails") or {}
        airline_names = data.get("airlineNames") or {}
        origin = data.get("org") or "DEL"
        destination = data.get("dest") or "BOM"

        flights = []
        for route_key, route_items in schedule.items():
            for route_item in route_items or []:
                route_id = route_item.get("ID") or route_key
                for od in route_item.get("OD") or []:
                    for segment in od.get("FS") or []:
                        flight_code = segment.get("fid") or route_id
                        route_main = fare_details.get(route_key, {}).get(flight_code, {}) if isinstance(fare_details.get(route_key), dict) else {}
                        fare_obj = route_main.get("O", {}).get("ADT", {}) if isinstance(route_main, dict) else {}

                        price_raw = (
                            fare_obj.get("ftf")
                            or fare_obj.get("bf")
                            or fare_obj.get("tf")
                            or route_item.get("fare")
                            or od.get("fare")
                            or 0
                        )
                        airline_code = segment.get("ac") or segment.get("vac") or "unknown"
                        flights.append({
                            "route": f"{origin}-{destination}",
                            "airline": airline_names.get(airline_code, segment.get("acn") or airline_code),
                            "departure_time": segment.get("dd") or od.get("tdu") or "00:00",
                            "arrival_time": segment.get("ad") or "00:00",
                            "flight_duration": self._normalize_duration(segment.get("du") or segment.get("dum") or od.get("tlot") or "N/A"),
                            "price_inr": self._to_float(price_raw),
                        })

        return flights

    @staticmethod
    def _to_float(value):
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = value.replace(",", "").replace("₹", "").replace("INR", "").strip()
            try:
                return float(cleaned)
            except ValueError:
                return 0.0
        return 0.0

    @staticmethod
    def _normalize_duration(value):
        if value is None:
            return "N/A"
        val = str(value).strip()
        if not val:
            return "N/A"
        if re.fullmatch(r"\d{3,4}", val):
            numeric = int(val)
            if len(val) >= 3 and len(val) <= 4:
                hours = numeric // 100 if len(val) == 4 else numeric // 100
                minutes = numeric % 100
                if hours and minutes:
                    return f"{hours}h {minutes}m"
                if hours:
                    return f"{hours}h"
                return f"{minutes}m"
        return val
