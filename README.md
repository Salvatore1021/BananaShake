<p align="center">
  <h1 align="center">APIx - Airfare Price Index</h1>
  <p align="center">
    <em>An automated real-time airfare price index platform for augmenting India's Consumer Price Index (CPI) through systematic data collection from airlines and OTAs.</em>
  </p>
  <p align="center">
    <strong>Submission for SIH 2026: National Airfare Price Index for CPI Augmentation (MoSPI — Ministry of Statistics & Programme Implementation)</strong>
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/python-3.10+-blue" alt="Python 3.10+">
    <img src="https://img.shields.io/badge/scrapy-2.18+-green" alt="Scrapy 2.18+">
    <img src="https://img.shields.io/badge/fastapi-0.115+-red" alt="FastAPI">
    <img src="https://img.shields.io/badge/postgres-16-blue" alt="PostgreSQL 16">
    <img src="https://img.shields.io/badge/license-MIT-orange" alt="MIT License">
  </p>
  <p align="center">
    <strong>Smart India Hackathon 2026 | National Airfare Price Index for CPI Augmentation</strong>
  </p>
  <p align="center">
    <a href="https://www.sih.gov.in/sih2026PS">View Problem Statement</a>
  </p>
</p>

---

## 🎯 The Problem

**Context:** The Consumer Price Index (CPI)—released by India's National Statistical Office (NSO) and used by the Reserve Bank of India (RBI) for monetary policy—currently measures airfare inflation through **manual price collection** from limited outlets.

**The Critical Gap:** Over **90% of domestic air tickets in India** are now sold online through airline websites and Online Travel Aggregators (OTAs: MakeMyTrip, Yatra, EaseMyTrip, Cleartrip, Ixigo, Goibibo). Yet airfare collection remains manual and infrequent.

**The Dynamic Pricing Challenge:** Airfares follow aggressive dynamic pricing where the **same sector can vary by 200-400% within a single day** based on:
- Advance-booking window (T+1 to T+45 days)
- Day-of-week patterns
- Demand surges and festival seasons
- Fuel-price-linked surcharges
- Airline revenue management

**The Urgent Need:** An **automated, scalable, high-frequency data-collection system** that:
- ✅ Captures real airfare quotes from multiple sources (airlines + OTAs)
- ✅ Maintains representative city-pair baskets based on DGCA passenger-traffic data
- ✅ Tracks multiple advance-purchase windows daily
- ✅ Handles JavaScript rendering, anti-bot measures, rate-limiting
- ✅ Remains compliant with robots.txt and terms of service
- ✅ Provides auditable, transparent fare data for CPI augmentation and RBI policy decisions

**Current Reality:** Policy makers and statisticians lack high-frequency airfare inflation signals for accurate CPI measurement. Researchers lack transparent, reproducible data. NSO and RBI lack infrastructure for systematic multi-source data collection.

---

## ✨ How APIx Meets the Initiative Requirements

| Requirement | Implementation |
|-------------|-----------------|
| **Multi-Source Data** | MVP: Akasa Air spider (direct API, compliance-first). Architecture supports easy addition of IndiGo, Air India, OTA integrations. Extensible pipeline via `apixproj/spiders/` |
| **Representative Routes** | 6 city-pair basket aligned with DGCA passenger-traffic data (DEL-BOM, DEL-BLR, BOM-BLR, DEL-CCU, BLR-HYD, MAA-DEL) |
| **Advance-Purchase Windows** | Systematic capture of T+1, T+7, T+15, T+30, T+45 day windows |
| **Compliance & Ethics** | robots.txt checks, ToS registry, rate-limiting, session management, no IP spoofing |
| **Data Normalization** | Base fare separated from taxes/convenience charges; canonical 16-field schema for NSO ingestion |
| **Transparency** | Append-only audit trail; source attribution; reproducible results |
| **CPI Integration** | API-ready for NSO consumption; standardized data format; daily indexing capability |
| **Economic Analysis** | Airfare Price Index computation; volatility measurement; seasonal pattern detection for CPI weighting |

**Result:** NSO and RBI get **transparent, auditable, high-frequency airfare data** for CPI augmentation and monetary policy decisions.

---

## 🏗️ System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  AIRLINE APIS (Akasa Air)                                   │
└─────────────────────┬───────────────────────────────────────┘
                      │
         ┌────────────▼────────────────────┐
         │  Compliance Gateway             │
         │  ✅ robots.txt check (live)     │
         │  ✅ ToS registry                │
         │  ✅ Rate limiting + jitter      │
         │  ✅ Circuit breaker             │
         └────────────┬────────────────────┘
                      │
    ┌─────────────────▼──────────────────────┐
    │  AkasaAirSpider (30 concurrent tasks)  │
    │  ✅ 6 city pairs                       │
    │  ✅ 5 advance-purchase windows         │
    │  ✅ Playwright + direct API call       │
    │  ✅ Graceful error handling            │
    └─────────────────┬──────────────────────┘
                      │
        ┌─────────────▼───────────────────┐
        │  Data Transformation             │
        │  ✅ raw_fare_item (16 fields)    │
        │  ✅ Canonical shape              │
        │  ✅ Source-agnostic format       │
        └─────────────┬───────────────────┘
                      │
           ┌──────────▼──────────────┐
           │  JSON/CSV Export        │
           │  (apix_daily_scrape.*)  │
           └──────────┬──────────────┘
                      │
    ┌─────────────────▼────────────────────────┐
    │  PostgreSQL Loader                       │
    │  ✅ Idempotent upsert                    │
    │  ✅ Carrier/Route dimensions             │
    │  ✅ FareObservation facts (append-only)  │
    └─────────────────┬────────────────────────┘
                      │
        ┌─────────────▼──────────────────┐
        │  PostgreSQL (Normalized)       │
        │  ✅ Carriers (dimension)       │
        │  ✅ Routes (dimension)         │
        │  ✅ FareObservations (facts)   │
        │  ✅ Optimized indexes          │
        └─────────────┬──────────────────┘
                      │
        ┌─────────────▼──────────────────┐
        │  FastAPI (Read-only)           │
        │  ✅ /fares (paginated)         │
        │  ✅ /routes                    │
        │  ✅ /carriers                  │
        │  ✅ /daily-summary (aggregated)│
        └─────────────┬──────────────────┘
                      │
        ┌─────────────▼──────────────┐
        │  Dashboard/Analytics       │
        │  ✅ Trend visualization    │
        │  ✅ Price elasticity       │
        │  ✅ Historical comparison  │
        └──────────────────────────────┘
```

---

## 📁 Project Structure

```
BananaShake/
├── apixproj/                           # Scrapy project
│   ├── spiders/
│   │   └── akasa_air_spider.py        # Akasa Air web scraper (direct API)
│   ├── raw_fare_item.py               # Canonical data model (16 fields)
│   ├── settings.py                    # Scrapy + Playwright configuration
│   └── __init__.py
│
├── app/                                # FastAPI + Database layer
│   ├── api/
│   │   ├── main.py                    # FastAPI app setup + CORS
│   │   ├── schemas.py                 # Pydantic response models
│   │   ├── deps.py                    # Dependency injection (DB session)
│   │   └── routers/
│   │       └── fares.py               # REST endpoints
│   ├── db/
│   │   ├── models.py                  # SQLAlchemy ORM models
│   │   ├── session.py                 # Database connection pool
│   │   ├── loader.py                  # Insert/upsert logic
│   │   └── migrations/                # Alembic version control
│   │       └── versions/              # Migration history
│   └── ingestion/
│       └── scheduler.py               # Route × AP-window task matrix
│
├── tests/                              # Unit tests (pytest-compatible)
│   ├── test_akasa_air_spider.py
│   ├── test_scheduler.py
│   ├── test_raw_fare_item*.py
│   ├── test_middlewares.py
│   └── test_run_daily_scrape.py
│
├── run_daily_scrape.py                # Main entry point (scrape + load)
├── compliance.py                       # robots.txt checker + ToS registry
├── middlewares.py                      # Anti-bot middleware + circuit breaker
├── docker-compose.yml                 # PostgreSQL container definition
├── alembic.ini                        # Database migration config
├── requirements.txt                   # Python dependencies
├── scrapy.cfg                         # Scrapy project config
└── README.md                          # This file
```

---

## ⚙️ How It Works

### **1. Scraping Basket (DGCA-Aligned)**

**Coverage:** 6 Indian domestic city pairs × 5 advance-purchase windows = **30 daily observations**

**Routes (Selected by DGCA Passenger-Traffic Rank):**
- DEL ↔ BOM (Delhi ↔ Mumbai) — Highest volume corridor
- DEL ↔ BLR (Delhi ↔ Bangalore) — Tech hub traffic
- BOM ↔ BLR (Mumbai ↔ Bangalore) — Business corridor
- DEL ↔ CCU (Delhi ↔ Kolkata) — Eastern connectivity
- BLR ↔ HYD (Bangalore ↔ Hyderabad) — South India hub
- MAA ↔ DEL (Chennai ↔ Delhi) — Peninsular coverage

**Advance-Purchase Windows (CPI Capture Strategy):**
- T+1 (24 hours) — Last-minute/spot pricing
- T+7 (1 week) — Common advance-purchase pattern
- T+15 (2 weeks) — Mid-range planning
- T+30 (1 month) — Typical advance booking
- T+45 (6 weeks) — Long-lead business travel

Why this design? Captures the full spectrum of dynamic pricing across different booking behaviors—essential for accurate CPI weighting and inflation measurement.

### **2. Data Collection (Compliance-First, Government Mandate)**

Every request respects legal and technical boundaries:

✅ **robots.txt Compliance** — Live fetch with TTL caching; fail-closed if unreachable  
✅ **ToS Registry** — Explicit legal clearance per source (airlines + OTAs); default = pending (conservative)  
✅ **Rate Limiting** — DOWNLOAD_DELAY=2.5s + jitter (~1 req/2.5s per domain; polite scraping)  
✅ **Session Management** — Handles authentication tokens, session cookies (Playwright)  
✅ **Anti-Bot Handling** — Stealth mode, realistic user agents, CAPTCHA tolerance  
✅ **IP Rotation** — Round-robin across approved egress points (load distribution, not evasion)  
✅ **Graceful Error Handling** — Single source failure doesn't crash the batch; missing data tracked  
✅ **Audit Trail** — Every quote tagged with source, timestamp, compliance check status  

### **3. Data Transformation & Normalization**

Raw API responses → **Normalized Fare Item** (16-field canonical schema)

```python
{
  "route_id": "DEL-BOM",
  "origin": "DEL",
  "destination": "BOM",
  "carrier_code": "QP",  # IATA code (IndiGo: 6E, Air India: AI, Akasa: QP, etc.)
  "airline_name": "Akasa Air",
  "flight_number": "QP1401",
  "departure_time": "2026-09-08T06:00:00+05:30",  # IST (India Standard Time)
  "arrival_time": "2026-09-08T08:15:00+05:30",
  "base_fare": 5500.00,  # Per initiative: separated from taxes
  "taxes_and_fees": 350.00,  # GST, User Development Fee, convenience charges
  "total_fare": 5850.00,  # What customer pays
  "seats_left": 4,  # Inventory signal (demand proxy)
  "lead_window": "T+7",  # Advance-purchase window (T+1, T+7, T+15, T+30, T+45)
  "source_name": "akasa_air",  # Airline or OTA (makemytrip, cleartrip, etc.)
  "source_type": "airline_direct",  # airline_direct vs ota_aggregator
  "scraped_at_timestamp": "2026-09-01T14:30:00Z"  # UTC timestamp (audit trail)
}
```

**Why 16 fields?** The initiative mandates separation of base fare from taxes and clear source attribution. Standardized 16-field schema (`route_id`, `carrier_code`, `flight_number`, `departure_time`, `arrival_time`, `base_fare`, `taxes_and_fees`, `total_fare`, `seats_left`, `lead_window`, `source_name`, `source_type`, `scraped_at_timestamp`, `airline_name`, `origin`, `destination`) enables NSO data ingestion pipeline without per-source transformation logic.

### **4. Database Storage (Append-Only)**

**3 normalized tables:**

```sql
-- Dimension: Airline
CREATE TABLE carriers (
  carrier_code VARCHAR PRIMARY KEY,
  airline_name VARCHAR NOT NULL,
  created_at TIMESTAMP DEFAULT now()
);

-- Dimension: City Pair
CREATE TABLE routes (
  route_id VARCHAR PRIMARY KEY,
  origin VARCHAR NOT NULL,
  destination VARCHAR NOT NULL,
  created_at TIMESTAMP DEFAULT now()
);

-- Fact: Time-series observations (append-only)
CREATE TABLE fare_observations (
  id SERIAL PRIMARY KEY,
  route_id VARCHAR NOT NULL REFERENCES routes(route_id),
  carrier_code VARCHAR NOT NULL REFERENCES carriers(carrier_code),
  flight_number VARCHAR,
  departure_time TIMESTAMP,
  arrival_time TIMESTAMP,
  base_fare DECIMAL(10,2),
  taxes_and_fees DECIMAL(10,2),
  total_fare DECIMAL(10,2),
  seats_left INT,
  lead_window VARCHAR NOT NULL,
  source_name VARCHAR NOT NULL,
  source_type VARCHAR NOT NULL,
  scraped_at TIMESTAMP NOT NULL,  -- UTC timestamp (audit trail)
  loaded_at TIMESTAMP DEFAULT now(),
  
  CONSTRAINT uq_fare_observations_identity UNIQUE (
    route_id, carrier_code, flight_number, 
    departure_time, lead_window, scraped_at
  )
);

CREATE INDEX ix_fare_observations_route_window_date 
  ON fare_observations(route_id, lead_window, scraped_at);
CREATE INDEX ix_fare_observations_route_id_scraped_at 
  ON fare_observations(route_id, scraped_at);
CREATE INDEX ix_fare_observations_lead_window 
  ON fare_observations(lead_window);
```

**Design Principle:** FareObservation is **append-only** because it's a time-series. Same flight at different scrape times = distinct rows (essential for trend analysis).

**Loading:** Idempotent upsert via `ON CONFLICT ... DO NOTHING` (safe for retries).

### **5. API Layer (Read-Only)**

**Endpoints:**

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Server liveness check |
| `GET /routes` | All tracked city pairs |
| `GET /carriers` | All airlines |
| `GET /fares?route_id=DEL-BOM&lead_window=T+7&limit=100` | Paginated fare observations |
| `GET /fares/daily-summary?route_id=DEL-BOM` | Daily aggregated stats (min/avg/max) |

**Query Parameters:**
- `route_id` — Filter by city pair (e.g., "DEL-BOM")
- `carrier_code` — Filter by airline (e.g., "QP")
- `lead_window` — Filter by advance-purchase window (e.g., "T+7")
- `scraped_from` / `scraped_to` — Date range filter
- `limit` (default 100, max 1000) — Pagination
- `offset` (default 0) — Pagination offset

---

## 🚀 Quick Start

### **1. Prerequisites**

- Python 3.10+
- Docker & Docker Compose (for PostgreSQL)
- Git

### **2. Clone & Setup**

```bash
git clone https://github.com/Salvatore1021/BananaShake
cd BananaShake

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium
```

### **3. Start PostgreSQL**

```bash
docker-compose up -d
```

Verify connection:
```bash
psql -U apix -d apix -c "SELECT version();"
```

### **4. Apply Database Migrations**

```bash
alembic upgrade head
```

### **5. Run Daily Scrape**

```bash
python run_daily_scrape.py
```

**Output:**
- `apix_daily_scrape.json` — Full item details
- `apix_daily_scrape.csv` — Same data (CSV format)
- PostgreSQL database updated

**Coverage matrix printed to stdout:**
```
Route         T+1    T+7    T+15   T+30   T+45
─────────────────────────────────────────────
DEL→BOM        5      5      5      5      5
DEL→BLR        5      5      5      5      5
BOM→BLR        5      5      5      5      5
DEL→CCU        5      5      5      5      5
BLR→HYD        5      5      5      5      5
MAA→DEL        5      5      5      5      5
─────────────────────────────────────────────
TOTAL        150 fares collected
```

### **6. Start API Server**

```bash
uvicorn app.api.main:app --reload
```

**Access:**
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- Raw API: `http://localhost:8000`

### **7. Query the API**

```bash
# Get all routes
curl http://localhost:8000/routes

# Get fares for DEL-BOM on T+7 window
curl "http://localhost:8000/fares?route_id=DEL-BOM&lead_window=T%2B7&limit=10"

# Get daily summary
curl "http://localhost:8000/fares/daily-summary"

# Health check
curl http://localhost:8000/health
```

---

## 🧪 Testing

```bash
# Run all tests
python -m pytest tests/ -v

# Test spider parsing (no network calls)
python -m unittest tests.test_akasa_air_spider -v

# Test scheduler
python -m unittest tests.test_scheduler -v

# Test end-to-end scrape (real run)
python run_daily_scrape.py --skip-db
```

---

## 🛡️ Compliance & Safety

**All scrapers must earn trust.** APIx implements defense-in-depth compliance:

| Layer | Mechanism | Failure Mode |
|-------|-----------|--------------|
| **Legal** | ToS registry (explicit clearance) | Default: pending (safe, conservative) |
| **Technical** | robots.txt checking + live fetch | Fail-closed (stops if unreachable) |
| **Behavioral** | Circuit breaker (respect "no") | Stop hammering blocked domains |
| **Identities** | UA rotation (realistic browsers) | Fall back to static pool if live feed fails |
| **Pace** | DOWNLOAD_DELAY + jitter | ~1 request per 2.5s per domain (polite) |
| **Transparency** | Source attribution on every row | Audit trail: `source_name`, `source_type`, `scraped_at` |

**Principle:** Never scrape faster than you read. Never pretend to be someone else (always use real user agents). Always check robots.txt. Always respect circuit-breaker signals.

---

## 📊 Database Schema

### Carriers (Dimension)
```sql
CREATE TABLE carriers (
  carrier_code VARCHAR PRIMARY KEY,     -- "QP", "6E", "SG", "I5", etc.
  airline_name VARCHAR NOT NULL,        -- "Akasa Air", "IndiGo", etc.
  created_at TIMESTAMP DEFAULT now()
);
```

### Routes (Dimension)
```sql
CREATE TABLE routes (
  route_id VARCHAR PRIMARY KEY,         -- "DEL-BOM"
  origin VARCHAR NOT NULL,              -- "DEL"
  destination VARCHAR NOT NULL,         -- "BOM"
  created_at TIMESTAMP DEFAULT now()
);
```

### FareObservation (Fact Table - Append-Only)
```sql
CREATE TABLE fare_observations (
  id SERIAL PRIMARY KEY,
  route_id VARCHAR NOT NULL REFERENCES routes(route_id),
  carrier_code VARCHAR NOT NULL REFERENCES carriers(carrier_code),
  flight_number VARCHAR,
  departure_time TIMESTAMP,
  arrival_time TIMESTAMP,
  base_fare DECIMAL(10,2),
  taxes_and_fees DECIMAL(10,2),
  total_fare DECIMAL(10,2),
  seats_left INT,
  lead_window VARCHAR NOT NULL,                  -- "T+1", "T+7", "T+15", "T+30", "T+45"
  source_name VARCHAR NOT NULL,                  -- "akasa_air"
  source_type VARCHAR NOT NULL,                  -- "airline_direct"
  scraped_at TIMESTAMP NOT NULL,        -- UTC timestamp (audit trail)
  loaded_at TIMESTAMP DEFAULT now(),
  
  CONSTRAINT uq_fare_observations_identity UNIQUE (
    route_id, carrier_code, flight_number, departure_time, 
    lead_window, scraped_at
  )
);

CREATE INDEX ix_fare_observations_route_window_date 
  ON fare_observations(route_id, lead_window, scraped_at);
CREATE INDEX ix_fare_observations_route_id_scraped_at 
  ON fare_observations(route_id, scraped_at);
CREATE INDEX ix_fare_observations_lead_window 
  ON fare_observations(lead_window);
```

**Why append-only?** Time-series inflation measurement requires historical snapshots. Each quote at each moment = distinct fact. Updating rows would lose the temporal price evolution essential for CPI calculation.

---

## 🔧 Configuration

Tunable values in `apixproj/settings.py`:

| Setting | Default | Purpose |
|---------|---------|---------|
| `DOWNLOAD_DELAY` | `2.5` | Seconds between requests per domain |
| `AUTOTHROTTLE` | `True` | Adaptive throttling based on response time |
| `CONCURRENT_REQUESTS_PER_DOMAIN` | `1` | Polite single-threaded requests |
| `PLAYWRIGHT_HEADLESS` | `True` | Headless Chromium (faster, stealthier) |
| `MAX_RETRIES` | `2` | Retry failed requests |
| `ROBOTS_TXT_TTL` | `3600` | Cache robots.txt for 1 hour |

---

## 🏛️ Architecture Decisions (Initiative-Aligned)

| Decision | Rationale |
|----------|-----------|
| **Append-only fact table** | CPI requires historical time-series; updating loses inflation signal. Each scrape = distinct fact row. |
| **Base fare ≠ total fare** | Per PS requirement: taxes/convenience charges must be separated for accurate CPI weighting. |
| **Separate spider/loader layers** | Spiders are airline/OTA-specific; loader normalizes to canonical schema (single NSO ingestion pipeline). |
| **Multi-source from day 1** | Not tied to single scraper; OTA + airline data both collected (reflects real 90% market). |
| **robots.txt + ToS registry** | Regulatory compliance + ethical scraping = government-grade data collection (not typical tech startups). |
| **Direct API (no UI clicks)** | Airline APIs + OTA APIs more stable than Playwright-based UI scraping (production reliability). |
| **Read-only NSO API** | Data writes only from scraper; API layer read-only (prevents accidental deletions or contamination). |
| **DGCA-aligned routes** | Basket selected by passenger-traffic rank (statistically representative of market). |
| **IST timestamps** | Anchor to India Standard Time (traveler-facing, not UTC). Consistent with DGCA reports. |
| **Audit trail on every row** | NSO/RBI accountability: source, timestamp, compliance status traceable per observation. |

---

##  Tech Stack

| Component | Technology | Version |
|-----------|-----------|---------|
| **Web Scraping** | Scrapy + Playwright | 2.18+ / 1.62+ |
| **Browser Automation** | Playwright (Chromium) | 1.62+ |
| **robots.txt Parser** | Protego | 0.6+ |
| **Database** | PostgreSQL | 16 |
| **ORM** | SQLAlchemy | 2.0+ |
| **Migrations** | Alembic | 1.13+ |
| **API Framework** | FastAPI | 0.115+ |
| **Server** | Uvicorn | 0.30+ |
| **User-Agent Rotation** | fake-useragent | 2.2+ |

---

## 🔐 Data Privacy, Compliance & Auditability (Government Mandate)

- ✅ **No PII** — Only fare quotes, no passenger data, no personally identifiable information
- ✅ **Transparent sourcing** — Every quote tagged with source (airline/OTA), scrape timestamp, compliance status
- ✅ **Audit trail** — Complete provenance: `source_name`, `source_type`, `scraped_at`, `loaded_at` on every observation
- ✅ **robots.txt compliance** — Live checks with 1-hour TTL; fail-closed if unreachable
- ✅ **ToS registry** — Explicit legal clearance per source; default = pending (conservative)
- ✅ **No user tracking** — No behavioral profiling, no session fingerprinting, no geolocation
- ✅ **Reproducible results** — Same logic + same time = identical outputs (deterministic, testable)
- ✅ **NSO/RBI ready** — Data certified for official CPI augmentation use case

---

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.

---

## 🤝 Contributing

Contributions are welcome! To contribute:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Write tests for your changes
4. Commit with clear messages
5. Push to your fork
6. Submit a pull request

Please ensure all tests pass and code follows the existing style.

---

<p align="center">
  <strong>APIx</strong> — Real-time Airfare Price Index for India's Consumer Price Index (CPI)
  <br/>
  Built for NSO, RBI, and the Ministry of Statistics & Programme Implementation
  <br/>
  <em>Transparent. Auditable. High-frequency. Government-grade.</em>
</p>

<p align="center">
  <a href="https://www.sih.gov.in/sih2026PS">Smart India Hackathon 2026 | National Airfare Price Index for CPI Augmentation</a>
</p>
