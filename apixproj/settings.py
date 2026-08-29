from compliance import USER_AGENT_POOL, get_random_user_agent, get_random_proxy

BOT_NAME = "apixproj"
SPIDER_MODULES = ["apixproj.spiders"]
NEWSPIDER_MODULE = "apixproj.spiders"

ROBOTSTXT_OBEY = False
DOWNLOAD_HANDLERS = {
    "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
}
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
PLAYWRIGHT_LAUNCH_OPTIONS = {
    "headless": True,
    "args": [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
    ],
}
PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT = 60000
PLAYWRIGHT_BROWSER_TYPE = "chromium"
PLAYWRIGHT_CONTEXT_ARGS = {
    "viewport": {"width": 1440, "height": 1100},
    "user_agent": get_random_user_agent(),
    "java_script_enabled": True,
    "ignore_https_errors": True,
}

DEFAULT_REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

USER_AGENT = get_random_user_agent()
USER_AGENT_LIST = USER_AGENT_POOL
PROXY_LIST = [p for p in [get_random_proxy()] if p]

DOWNLOAD_DELAY = 2.5
RANDOMIZE_DOWNLOAD_DELAY = True
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 2
AUTOTHROTTLE_MAX_DELAY = 10
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0
RETRY_ENABLED = True
RETRY_TIMES = 2
CONCURRENT_REQUESTS_PER_DOMAIN = 1
COOKIES_ENABLED = True  # required for SessionCookieMiddleware's per-source cookiejars to persist anything

LOG_LEVEL = "INFO"

# Priority numbers: lower = closer to the engine, higher = closer to the
# downloader. process_request runs low->high; process_response runs
# high->low (reverse) — see middlewares.py's docstring for why
# ExponentialBackoffMiddleware (550) must sit downloader-side of
# AntiBotBackoffMiddleware (543), and SessionCookieMiddleware (100) must
# sit engine-side of Scrapy's own CookiesMiddleware (700, from
# DOWNLOADER_MIDDLEWARES_BASE).
DOWNLOADER_MIDDLEWARES = {
    "middlewares.SessionCookieMiddleware": 100,
    "middlewares.AntiBotBackoffMiddleware": 543,
    "middlewares.ExponentialBackoffMiddleware": 550,
}

APIX_DATABASE_URL = "sqlite:///apix_demo.db"
ITEM_PIPELINES = {
    "pipelines.JsonExportPipeline": 300,
    "pipelines.RawFareQuotePipeline": 310,
}
OTA_TARGET_URL = "https://flight.yatra.com/air-service/dom2/price?searchId=614b8271-37a1-4edc-9102-38fb5ee2429a&msid=614b8271-37a1-4edc-9102-38fb5ee2429a&mode=Background&bpc=true&isSR=false&unique=1787948125668&variation=0&specialFareFlag=undefined&flightIdCSV=DELBOMIX1605EP20260830_AIRASIAAPI&flightPrice=6900&sc=AIRASIAAPI&dfc=false"
FEEDS = {
    "ota_flights.json": {"format": "json", "overwrite": True},
}
