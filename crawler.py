#!/usr/bin/env python3
"""Crawler SchweizMobil — Scrapy + Playwright (JS rendu)."""

from __future__ import annotations

import json
import logging
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy.exceptions import IgnoreRequest
from scrapy_playwright.page import PageMethod

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "crawler.log"
HIKING_DIR = ROOT / "hiking"
CHROMIUM_DIR = ROOT / "chromium"
CHROMIUM_USER_DATA_DIR = CHROMIUM_DIR / "user-data"
CHROMIUM_CACHE_DIR = CHROMIUM_DIR / "cache"
START_URLS = [
    f"https://schweizmobil.ch/fr/suisse-a-pied/itineraire-{n}"
    for n in (*range(1, 8), *range(22, 100))
]
LOCAL_ROUTES_URL = "https://schweizmobil.ch/api/4/routes/hike/local?lang=fr"
TIMEOUT_SECS = 15
STAGE_URL_RE = re.compile(
    r"^https://schweizmobil\.ch/fr/suisse-a-pied/itineraire-(\d+)/etape-(\d+)/?$"
)

IMAGE_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".avif",
    ".bmp",
)

LOG_SETTINGS = {
    "LOG_ENABLED": True,
    "LOG_LEVEL": "INFO",
    "LOG_FORMAT": "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    "LOG_FILE": str(LOG_PATH),
    "LOG_FILE_APPEND": False,
    "LOG_ENCODING": "utf-8",
}


def chrome_executable() -> str:
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return str(Path(path).resolve())
    raise RuntimeError("Aucun binaire Chrome/Chromium trouve dans PATH")


def playwright_meta(*, wait_route_title: bool = False) -> dict:
    timeout_ms = TIMEOUT_SECS * 1000
    methods = [
        PageMethod("wait_for_load_state", "domcontentloaded", timeout=timeout_ms),
        PageMethod("wait_for_selector", "element-header", timeout=timeout_ms),
    ]
    if wait_route_title:
        methods.append(
            PageMethod("wait_for_selector", 'p[data-cy="route-title"]', timeout=timeout_ms)
        )
    return {"playwright": True, "playwright_page_methods": methods}


def stage_ids(url: str) -> tuple[str, str] | None:
    match = STAGE_URL_RE.match(urlparse(url)._replace(query="", fragment="").geturl())
    if not match:
        return None
    return match.group(1), match.group(2)


def clean_locality(name: str) -> str:
    name = name or ""
    name = re.sub(r"\([^)]*\)", "", name)
    name = name.replace("\xa0", " ").replace("\u202f", " ")
    name = re.split(r"[,/]", name, maxsplit=1)[0]
    return re.sub(r"\s+", " ", name).strip()


def clean_stage_text(text: str, d2: str = "") -> str:
    text = text or ""
    text = re.sub(r"\([^)]*\)", "", text)
    text = text.replace("\xa0", " ").replace("\u202f", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if d2:
        text = re.sub(rf"^Étape\s+{re.escape(str(d2))}:\s*", "", text).strip()
    parts = [p.strip() for p in re.split(r"\s+[–—−-]\s+", text) if p.strip()]
    if len(parts) >= 2:
        return f"{clean_locality(parts[0])} - {clean_locality(parts[-1])}"
    return clean_locality(text)


def invert_stage_text(text: str) -> str:
    parts = re.split(r"\s+-\s+", text.strip(), maxsplit=1)
    if len(parts) != 2:
        return text
    return f"{parts[1]} - {parts[0]}"


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, i
        shift += 7


def _read_svarint(buf: bytes, i: int) -> tuple[int, int]:
    value, i = _read_varint(buf, i)
    return (value >> 1) ^ -(value & 1), i


def geobuf_points(data: bytes) -> list[tuple[float, float, float]]:
    dims = 2
    precision = 6
    packed: list[int] = []

    def walk_geometry(start: int, end: int) -> None:
        i = start
        while i < end:
            key, i = _read_varint(data, i)
            field, wire = key >> 3, key & 7
            if wire == 0:
                _, i = _read_varint(data, i)
            elif wire == 2:
                length, i = _read_varint(data, i)
                sub_end = i + length
                if field == 3:
                    j = i
                    while j < sub_end:
                        value, j = _read_svarint(data, j)
                        packed.append(value)
                elif field == 4:
                    walk_geometry(i, sub_end)
                i = sub_end
            else:
                raise ValueError(f"unsupported geobuf wire type {wire}")

    def walk_feature(start: int, end: int) -> None:
        i = start
        while i < end:
            key, i = _read_varint(data, i)
            field, wire = key >> 3, key & 7
            if wire == 0:
                _, i = _read_varint(data, i)
            elif wire == 2:
                length, i = _read_varint(data, i)
                sub_end = i + length
                if field == 1:
                    walk_geometry(i, sub_end)
                i = sub_end
            else:
                raise ValueError(f"unsupported geobuf wire type {wire}")

    i = 0
    while i < len(data):
        key, i = _read_varint(data, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _read_varint(data, i)
            if field == 2:
                dims = value
            elif field == 3:
                precision = value
        elif wire == 2:
            length, i = _read_varint(data, i)
            sub_end = i + length
            if field == 4:
                j = i
                while j < sub_end:
                    fkey, j = _read_varint(data, j)
                    ffield, fwire = fkey >> 3, fkey & 7
                    if fwire == 0:
                        _, j = _read_varint(data, j)
                    elif fwire == 2:
                        flen, j = _read_varint(data, j)
                        fend = j + flen
                        if ffield == 1:
                            walk_feature(j, fend)
                        j = fend
                    else:
                        raise ValueError(f"unsupported geobuf wire type {fwire}")
            i = sub_end
        else:
            raise ValueError(f"unsupported geobuf wire type {wire}")

    scale = 10**precision
    east = north = ele = 0.0
    points: list[tuple[float, float, float]] = []
    for k in range(0, len(packed), dims):
        east += packed[k]
        north += packed[k + 1]
        z = 0.0
        if dims >= 3:
            ele += packed[k + 2]
            z = ele / scale
        points.append((east / scale, north / scale, z))
    return points


def lv95_to_wgs84(east: float, north: float) -> tuple[float, float]:
    y = (east - 2_600_000.0) / 1_000_000.0
    x = (north - 1_200_000.0) / 1_000_000.0
    lon = (
        2.6779094
        + 4.728982 * y
        + 0.791484 * y * x
        + 0.1306 * y * x * x
        - 0.0436 * y**3
    ) * 100 / 36
    lat = (
        16.9023892
        + 3.238272 * x
        - 0.270978 * y * y
        - 0.002528 * x * x
        - 0.0447 * y * y * x
        - 0.0140 * x**3
    ) * 100 / 36
    return lon, lat


def hiking_subdir(route_number: str) -> Path:
    n = int(route_number)
    if n >= 100:
        return HIKING_DIR / f"{n // 100}xx"
    if 1 <= n <= 19:
        return HIKING_DIR / "1-19"
    if 20 <= n <= 99:
        lo = (n // 20) * 20
        return HIKING_DIR / f"{lo}-{lo + 19}"
    return HIKING_DIR


def _safe_filename(name: str) -> str:
    name = name.replace("/", "-").replace("\\", "-")
    return name


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def schweizmobil_track_url(route_number: str, segment_number: str) -> str:
    if str(segment_number) == "0":
        return f"https://schweizmobil.ch/fr/hiking-in-switzerland/route-{route_number}"
    return (
        f"https://schweizmobil.ch/fr/hiking-in-switzerland/route-{route_number}/stage-{segment_number}"
    )


def points_to_gpx(
    points: list[tuple[float, float, float]],
    name: str,
    path: Path,
    link: str,
) -> None:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="crawler" xmlns="http://www.topografix.com/GPX/1/1">',
        "  <trk>",
        f"    <name>{_xml_escape(name)}</name>",
        f'    <link href="{_xml_escape(link)}"/>',
        "    <type>hiking</type>",
        "    <trkseg>",
    ]
    for east, north, ele in points:
        lon, lat = lv95_to_wgs84(east, north)
        lines.append(
            f'      <trkpt lat="{lat:.7f}" lon="{lon:.7f}"><ele>{ele:.1f}</ele></trkpt>'
        )
    lines.extend(["    </trkseg>", "  </trk>", "</gpx>", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def write_stage_gpx(
    geobuf: bytes,
    d1: str,
    d2: str,
    raw_text: str = "",
    start: str | None = None,
    end: str | None = None,
) -> None:
    if start is not None and end is not None:
        text = f"{clean_locality(start)} - {clean_locality(end)}"
        inverted = invert_stage_text(text)
    else:
        text = clean_stage_text(raw_text, d2)
        inverted = invert_stage_text(text)
    points = geobuf_points(geobuf)
    if len(points) < 2:
        raise ValueError("geobuf sans tracé")
    out_dir = hiking_subdir(d1)
    out_dir.mkdir(parents=True, exist_ok=True)
    if str(d2) == "0":
        forward_title = f"{d1} {text}"
        backward_title = f"{d1}r {inverted}"
    else:
        forward_title = f"{d1}.{d2} {text}"
        backward_title = f"{d1}.{d2}r {inverted}"
    link = schweizmobil_track_url(d1, d2)
    points_to_gpx(
        points, forward_title, out_dir / _safe_filename(f"{forward_title}.gpx"), link
    )
    points_to_gpx(
        list(reversed(points)),
        backward_title,
        out_dir / _safe_filename(f"{backward_title}.gpx"),
        link,
    )


def should_abort_request(request) -> bool:
    """Bloque uniquement les images dans Chromium. JS/CSS/fonts passent."""
    if request.resource_type == "image":
        return True
    path = urlparse(request.url).path.lower()
    return path.endswith(IMAGE_EXTENSIONS)


class StaticFilterMiddleware:
    """Ignore les requetes Scrapy vers des images."""

    @classmethod
    def from_crawler(cls, crawler):
        mw = cls()
        mw.crawler = crawler
        return mw

    def process_request(self, request):
        path = urlparse(request.url).path.lower()
        if path.endswith(IMAGE_EXTENSIONS):
            raise IgnoreRequest(f"Skip image: {request.url}")


class SkipUnrenderedCacheMiddleware:
    """Ne met pas en cache le coquille SPA avant execution du JS."""

    @classmethod
    def from_crawler(cls, crawler):
        mw = cls()
        mw.crawler = crawler
        return mw

    def process_response(self, request, response):
        if "/api/" in request.url:
            return response
        if b"element-header" not in response.body:
            request.meta["dont_cache"] = True
            spider = getattr(self.crawler, "spider", None)
            if spider is not None:
                spider.logger.warning(
                    "HTML sans hydratation JS, cache ignore: %s (%s bytes)",
                    response.url,
                    len(response.body),
                )
        return response


class QuotesSpider(scrapy.Spider):
    name = "quotes"

    async def start(self):
        yield scrapy.Request(
            LOCAL_ROUTES_URL,
            callback=self.parse_local_routes,
            errback=self.errback_playwright,
            meta={"playwright": True, "playwright_include_page": True},
        )
        for url in START_URLS:
            self.logger.debug("start: %s", url)
            yield scrapy.Request(
                url,
                callback=self.parse,
                dont_filter=True,
                meta=playwright_meta(wait_route_title=True),
                errback=self.errback_playwright,
            )

    def _iter_route_items(self, response):
        for item in response.css('a[data-cy="route-list-it"]'):
            href = item.css("::attr(href)").get()
            url = response.urljoin(href) if href else None
            yield {
                "text": item.css('p[data-cy="route-title"]').xpath("normalize-space()").get(),
                "url": url,
            }

    def parse(self, response):
        self.logger.debug(
            "parse status=%s flags=%s url=%s",
            response.status,
            response.flags,
            response.url,
        )
        if "cached" in response.flags:
            self.logger.info("[CACHED] %s", response.url)

        for row in self._iter_route_items(response):
            yield row
            if row["url"]:
                yield from self.enqueue_stage(row["url"], row["text"] or "")

    def enqueue_stage(self, url: str, text: str = ""):
        self.logger.debug("follow parsed url: %s", url)
        yield scrapy.Request(
            url,
            callback=self.parse_stage,
            errback=self.errback_playwright,
            meta=playwright_meta(wait_route_title=False),
        )
        ids = stage_ids(url)
        if ids:
            d1, d2 = ids
            api_meta = {
                "playwright": True,
                "playwright_include_page": True,
                "playwright_page_goto_kwargs": {
                    "timeout": TIMEOUT_SECS * 1000,
                    "wait_until": "commit",
                },
                "stage_d1": d1,
                "stage_d2": d2,
                "stage_text": text,
            }
            geobuf_url = (
                f"https://schweizmobil.ch/api/4/route_or_segment/hike/{d1}/{d2}.geobuf"
            )
            self.logger.debug("enqueue api: %s", geobuf_url)
            yield scrapy.Request(
                geobuf_url,
                callback=self.parse_api,
                errback=self.errback_playwright,
                meta={**api_meta, "playwright_include_page": True},
            )

    def errback_playwright(self, failure):
        self.logger.warning(
            "download failed: %s (%s)",
            failure.request.url,
            failure.value,
        )

    async def parse_local_routes(self, response):
        page = response.meta.get("playwright_page")
        raw = ""
        try:
            if page is not None:
                api_resp = await page.request.get(LOCAL_ROUTES_URL)
                raw = await api_resp.text()
            if not raw.strip():
                raw = (response.text or "").strip()
            routes = json.loads(raw)
        except json.JSONDecodeError:
            self.logger.warning(
                "local routes: JSON illisible (%s octets)",
                len(raw),
            )
            return
        finally:
            if page is not None:
                await page.close()
        if not isinstance(routes, list):
            self.logger.warning("local routes: JSON inattendu (%s)", type(routes))
            return
        self.logger.info("local routes: %s entrees", len(routes))
        for route in routes:
            land = route.get("land") or "hike"
            d1 = route.get("routeNumber")
            d2 = route.get("segmentNumber")
            start = route.get("start") or ""
            end = route.get("end") or ""
            if d1 is None or d2 is None:
                continue
            geobuf_url = (
                f"https://schweizmobil.ch/api/4/route_or_segment/"
                f"{land}/{d1}/{d2}.geobuf"
            )
            yield scrapy.Request(
                geobuf_url,
                callback=self.parse_api,
                errback=self.errback_playwright,
                meta={
                    "playwright": True,
                    "playwright_include_page": True,
                    "playwright_page_goto_kwargs": {
                        "timeout": TIMEOUT_SECS * 1000,
                        "wait_until": "commit",
                    },
                    "stage_d1": str(d1),
                    "stage_d2": str(d2),
                    "stage_start": start,
                    "stage_end": end,
                },
            )

    async def parse_api(self, response):
        page = response.meta.get("playwright_page")
        body = response.body
        try:
            if page is not None and (
                not body
                or body.lstrip()[:1] in {b"<", b"{"}
                or b"<html" in body[:200].lower()
            ):
                api_resp = await page.request.get(response.url)
                body = await api_resp.body()
        finally:
            if page is not None:
                await page.close()
        self.logger.debug(
            "parse_api status=%s url=%s bytes=%s",
            response.status,
            response.url,
            len(body),
        )
        if not response.url.endswith(".geobuf"):
            return
        if not body or body.lstrip()[:1] in {b"<", b"{"} or b"<html" in body[:200].lower():
            self.logger.warning("geobuf invalide, ignore: %s (%s octets)", response.url, len(body))
            return
        d1 = response.meta.get("stage_d1")
        d2 = response.meta.get("stage_d2")
        if not d1 or not d2:
            match = re.search(r"/hike/(\d+)/(\d+)\.geobuf", response.url)
            if not match:
                return
            d1, d2 = match.group(1), match.group(2)
        write_stage_gpx(
            body,
            str(d1),
            str(d2),
            response.meta.get("stage_text") or "",
            start=response.meta.get("stage_start"),
            end=response.meta.get("stage_end"),
        )
        self.logger.debug("gpx written for %s.%s", d1, d2)

    def parse_stage(self, response):
        self.logger.debug(
            "parse_stage status=%s flags=%s url=%s",
            response.status,
            response.flags,
            response.url,
        )
        if "cached" in response.flags:
            self.logger.info("[CACHED] %s", response.url)
        yield from self._iter_route_items(response)

def build_settings() -> dict:
    CHROMIUM_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHROMIUM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    chrome = chrome_executable()
    return {
        **LOG_SETTINGS,
        "DOWNLOAD_HANDLERS": {
            "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
            "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        },
        "DOWNLOADER_MIDDLEWARES": {
            "crawler.StaticFilterMiddleware": 150,
            "crawler.SkipUnrenderedCacheMiddleware": 901,
        },
        "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
        "ROBOTSTXT_OBEY": False,
        "CONCURRENT_REQUESTS": 2,
        "PLAYWRIGHT_BROWSER_TYPE": "chromium",
        "PLAYWRIGHT_LAUNCH_OPTIONS": {
            "executable_path": chrome,
            "headless": True,
            "args": [
                f"--disk-cache-dir={CHROMIUM_CACHE_DIR}",
                "--disk-cache-size=1073741824",
            ],
        },
        "PLAYWRIGHT_CONTEXTS": {
            "default": {
                "user_data_dir": str(CHROMIUM_USER_DATA_DIR),
                "executable_path": chrome,
                "headless": True,
                "viewport": {"width": 1920, "height": 1080},
                "service_workers": "block",
                "args": [
                    f"--disk-cache-dir={CHROMIUM_CACHE_DIR}",
                    "--disk-cache-size=1073741824",
                ],
            }
        },
        "DOWNLOAD_TIMEOUT": TIMEOUT_SECS,
        "PLAYWRIGHT_DEFAULT_NAVIGATION_TIMEOUT": TIMEOUT_SECS * 1000,
        "PLAYWRIGHT_ABORT_REQUEST": "crawler.should_abort_request",
        "HTTPCACHE_ENABLED": True,
        "HTTPCACHE_EXPIRATION_SECS": 86400,
        "HTTPCACHE_DIR": str(ROOT / "httpcache"),
        "HTTPCACHE_STORAGE": "scrapy.extensions.httpcache.FilesystemCacheStorage",
        "HTTPCACHE_POLICY": "scrapy.extensions.httpcache.DummyPolicy",
        "HTTPCACHE_IGNORE_HTTP_CODES": [404, 500, 502, 503],
        "HTTPCACHE_GZIP": True,
        "FEEDS": {str(ROOT / "quotes.json"): {"format": "json", "overwrite": True}},
    }


def attach_stdout_logging() -> None:
    root = logging.getLogger()
    has_stdout = any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        and getattr(handler, "stream", None) in {sys.stdout, sys.stderr}
        for handler in root.handlers
    )
    if not has_stdout:
        stream = logging.StreamHandler(sys.stdout)
        stream.setLevel(logging.INFO)
        stream.setFormatter(logging.Formatter(LOG_SETTINGS["LOG_FORMAT"]))
        root.addHandler(stream)
    logging.getLogger("scrapy").setLevel(logging.INFO)
    logging.getLogger("scrapy-playwright").setLevel(logging.INFO)


def main() -> None:
    LOG_PATH.write_text("", encoding="utf-8")
    process = CrawlerProcess(build_settings())
    attach_stdout_logging()
    process.crawl(QuotesSpider)
    process.start()


if __name__ == "__main__":
    main()
