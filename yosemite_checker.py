#!/usr/bin/env python3
"""
Yosemite hotel availability checker.

Uses Playwright to automate the reCAPTCHA-protected booking form at
reservations.ahlsmsworld.com and reports available rooms.
"""

import argparse
import asyncio
import calendar
import json
import os
import random
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from typing import Any

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

PROPERTIES = {
    "M": {"name": "The Ahwahnee",          "aliases": ["ahwahnee"]},
    "Y": {"name": "Yosemite Valley Lodge",  "aliases": ["valley-lodge"]},
    "D": {"name": "Curry Village",          "aliases": ["curry-village"]},
    "H": {"name": "Housekeeping Camp",      "aliases": ["housekeeping"]},
    "T": {"name": "Tuolumne Meadows Lodge", "aliases": ["tuolumne"]},
}

ALIAS_MAP: dict[str, str] = {}
for _code, _info in PROPERTIES.items():
    ALIAS_MAP[_code.lower()] = _code
    for _alias in _info["aliases"]:
        ALIAS_MAP[_alias.lower()] = _code

SEARCH_URL = "https://reservations.ahlsmsworld.com/Yosemite/Plan-Your-Trip"


def resolve_properties(raw: str) -> list[str]:
    codes = []
    for token in raw.split(","):
        key = token.strip().lower()
        code = ALIAS_MAP.get(key)
        if code is None:
            print(f"error: unknown property '{token.strip()}'. Run 'python yosemite_checker.py properties' to see valid options.", file=sys.stderr)
            sys.exit(1)
        codes.append(code)
    return list(dict.fromkeys(codes))  # deduplicate, preserve order


def parse_date(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        print(f"error: invalid date '{s}', expected YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)


def fmt_date(d: datetime) -> str:
    return d.strftime("%m/%d/%Y")


def fmt_date_short(d: datetime) -> str:
    return d.strftime("%m/%d")


GREEN  = "\033[32m"
YELLOW = "\033[33m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def render_calendar(year: int, month_0: int, avail: dict[int, str], label: str,
                    highlight_day: int | None = None) -> str:
    """Render a cal-style month grid with ANSI-colored availability.

    month_0 is 0-indexed (0=Jan) to match jQuery UI datepicker convention.
    Significant days are green, limited are yellow. The highlight_day
    (the target check-in or check-out day) is rendered bold.
    """
    month = month_0 + 1  # calendar module uses 1-indexed months
    month_name = calendar.month_name[month]
    title = f"{month_name} {year}"

    lines = [f"  {label} calendar:", ""]
    lines.append(f"      {title:^20}")
    lines.append("  Su Mo Tu We Th Fr Sa")

    cal = calendar.monthcalendar(year, month)
    for week in cal:
        cells = []
        for day in week:
            if day == 0:
                cells.append("  ")
            else:
                s = avail.get(day, "none")
                ds = f"{day:2d}"
                bold = BOLD if day == highlight_day else ""
                if s == "significant":
                    cells.append(f"{bold}{GREEN}{ds}{RESET}")
                elif s == "limited":
                    cells.append(f"{bold}{YELLOW}{ds}{RESET}")
                else:
                    cells.append(f"{bold}{ds}{RESET}" if bold else ds)
        lines.append("  " + " ".join(cells))

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Browser automation
# ──────────────────────────────────────────────────────────────────────────────

class YosemiteChecker:
    def __init__(self, headless: bool = True, browser_ws: str | None = None):
        self.headless = headless
        self._browser_ws = browser_ws
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        if self._browser_ws:
            self._browser = await self._playwright.chromium.connect_over_cdp(self._browser_ws)
        else:
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
        self._console_log: list[str] = []
        await self._new_context()
        return self

    async def __aexit__(self, *_):
        if self._page:
            await self._page.close()
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def _new_context(self) -> None:
        """Create a fresh browser context and page with clean cookies/session.

        In CDP mode (SockpuppetBrowser), also reconnects to get a brand-new
        Chrome process rather than just a new context in the same process.
        """
        if self._page:
            await self._page.close()
        if self._context:
            await self._context.close()
        if self._browser_ws and self._browser:
            await self._browser.close()
            self._browser = await self._playwright.chromium.connect_over_cdp(self._browser_ws)
        self._context = await self._browser.new_context(
            viewport={"width": 1320, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-CH-UA": '"Not/A)Brand";v="8", "Chromium";v="145", "Google Chrome";v="145"',
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"macOS"',
            },
        )
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            if (navigator.userAgentData) {
                const brands = [
                    {brand: 'Not/A)Brand',    version: '8'},
                    {brand: 'Chromium',        version: '145'},
                    {brand: 'Google Chrome',   version: '145'},
                ];
                Object.defineProperty(navigator, 'userAgentData', {get: () => ({
                    brands,
                    mobile: false,
                    platform: 'macOS',
                    getHighEntropyValues: async () => ({
                        brands,
                        fullVersionList: [
                            {brand: 'Not/A)Brand',  version: '8.0.0.0'},
                            {brand: 'Chromium',      version: '145.0.0.0'},
                            {brand: 'Google Chrome', version: '145.0.0.0'},
                        ],
                        mobile: false,
                        platform: 'macOS',
                        platformVersion: '10_15_7',
                        architecture: 'x86',
                        bitness: '64',
                        model: '',
                        uaFullVersion: '145.0.0.0',
                    }),
                })});
            }
        """)
        self._page = await self._context.new_page()
        self._page.set_default_timeout(90_000)
        self._console_log = []
        self._page.on("console", lambda msg: self._console_log.append(f"[{msg.type}] {msg.text}"))
        self._page.on("requestfailed", lambda req: self._console_log.append(
            f"[requestfailed] {req.failure} — {req.url}"
        ))

    async def dump_diagnostics(self, label: str) -> str:
        """Save current DOM and console log to a timestamped file pair, return base path.

        Also prints the log to stderr so it appears in kubectl logs from in-cluster runs.
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = label.replace(" ", "_").replace("/", "-")
        base = f"diag_{safe}_{ts}"

        lines: list[str] = []
        lines.append(f"URL: {self._page.url}")
        try:
            token_val = await self._page.evaluate(
                "() => { const t = document.querySelector('#box-widget_RecaptchaToken'); return t ? t.value : 'NOT FOUND'; }"
            )
            lines.append(f"RecaptchaToken: {'<populated>' if token_val else '<empty>'}")
        except Exception:
            lines.append("RecaptchaToken: <could not read>")
        lines.append("")
        lines.extend(self._console_log)
        log_content = "\n".join(lines)

        try:
            html = await self._page.content()
            with open(f"{base}.html", "w", encoding="utf-8") as f:
                f.write(html)
        except Exception as e:
            print(f"  (could not save HTML: {e})", file=sys.stderr)

        with open(f"{base}.log", "w", encoding="utf-8") as f:
            f.write(log_content)

        # Print to stderr so diagnostics appear in kubectl logs for in-cluster runs
        print(f"\n--- diagnostic log: {base} ---", file=sys.stderr)
        print(log_content, file=sys.stderr)
        print("--- end diagnostic log ---", file=sys.stderr)

        return base

    async def _wait_for_loading(self, page: Page) -> None:
        """Wait for wxa-widget-loading or blockUI overlays to appear and clear."""
        try:
            await page.wait_for_selector(".wxa-widget-loading", state="visible", timeout=2_000)
            await page.wait_for_selector(".wxa-widget-loading", state="hidden", timeout=30_000)
        except Exception:
            pass
        try:
            overlay = await page.query_selector(".blockUI.blockOverlay")
            if overlay and await overlay.is_visible():
                await page.wait_for_selector(".blockUI.blockOverlay", state="hidden", timeout=30_000)
        except Exception:
            pass

    async def _human_delay(self, page: Page) -> None:
        await page.wait_for_timeout(random.randint(250, 1000))

    async def _read_datepicker_cells(self, page: Page) -> dict[int, str]:
        """Parse all day cells from the currently open datepicker into a day->availability map."""
        cells = await page.evaluate("""() => {
            const tds = document.querySelectorAll(
                '#ui-datepicker-div table.ui-datepicker-calendar tbody td'
            );
            return Array.from(tds).map(td => ({
                classes: td.className,
                text: (td.querySelector('a') || td.querySelector('span') || td).textContent.trim(),
            }));
        }""")

        avail: dict[int, str] = {}
        for cell in cells:
            cls = cell["classes"]
            text = cell["text"].strip()
            if "ui-datepicker-other-month" in cls or not text or text == "\xa0":
                continue
            try:
                day = int(text)
            except ValueError:
                continue
            if "ui-datepickerAvail-significant" in cls:
                avail[day] = "significant"
            elif "ui-datepickerAvail-limited" in cls:
                avail[day] = "limited"
            else:
                avail[day] = "none"
        return avail

    async def _scan_calendar_availability(self, page: Page, label: str, year: int, month: int,
                                           highlight_day: int | None = None) -> dict[int, str]:
        """Read datepicker cells and print a calendar grid to stderr. month is 0-indexed."""
        avail = await self._read_datepicker_cells(page)
        print(render_calendar(year, month, avail, label, highlight_day=highlight_day) + "\n", file=sys.stderr)
        return avail

    async def _select_date_via_picker(
        self,
        page: Page,
        container_class: str,
        target_date: datetime,
        label: str,
    ) -> tuple[bool, dict[int, str]]:
        """Open the jQuery UI datepicker for container_class, navigate to target_date,
        scan availability, and click the day if available.

        Returns (selected, avail) — selected is True if the day was clicked,
        False if no availability. avail is the day->level map for the month.
        """
        year = target_date.year
        month = target_date.month - 1  # jQuery UI datepicker uses 0-indexed months
        day = target_date.day

        await page.click(f"#box-widget > form .{container_class} .input-group-addon i")
        await self._human_delay(page)
        await page.wait_for_selector("#ui-datepicker-div", state="visible", timeout=5_000)
        await self._wait_for_loading(page)

        await page.select_option("#ui-datepicker-div select.ui-datepicker-year", str(year))
        await self._human_delay(page)
        await self._wait_for_loading(page)

        await page.select_option("#ui-datepicker-div select.ui-datepicker-month", str(month))
        await self._human_delay(page)
        await self._wait_for_loading(page)

        avail = await self._scan_calendar_availability(page, label, year, month, highlight_day=day)

        if avail.get(day, "none") == "none":
            print(f"  {label} {target_date.strftime('%Y-%m-%d')}: no availability — skipping", file=sys.stderr)
            await page.keyboard.press("Escape")
            return False, avail

        day_links = await page.query_selector_all(
            f'#ui-datepicker-div td[data-month="{month}"][data-year="{year}"][data-handler="selectDay"] a'
        )
        for link in day_links:
            if (await link.inner_text()).strip() == str(day):
                await link.click()
                await self._wait_for_loading(page)
                return True, avail

        raise RuntimeError(
            f"Could not find clickable day {day} in datepicker for {target_date.strftime('%Y-%m-%d')}"
        )

    async def search(
        self,
        arrival: datetime,
        departure: datetime,
        property_code: str,
        adults: int,
        children: int,
        rooms: int,
        retries: int = 3,
    ) -> list[dict[str, Any]]:
        page = self._page
        self._console_log.clear()

        async def intercept_data_layer():
            await page.evaluate("""() => {
                window.__capturedRooms = [];
                window.dataLayer = window.dataLayer || [];
                const origPush = window.dataLayer.push.bind(window.dataLayer);
                window.dataLayer.push = function(obj) {
                    if (obj && obj.event === 'view_search_results' && obj.ecommerce) {
                        const items = (obj.ecommerce.items || []);
                        window.__capturedRooms = window.__capturedRooms.concat(items);
                    }
                    return origPush(obj);
                };
            }""")

        max_attempts = max(1, retries)
        retry_delay  = 15
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                print(f"  waiting {retry_delay}s before retry (attempt {attempt}/{max_attempts}) ...", file=sys.stderr)
                await page.wait_for_timeout(retry_delay * 1000)

            # 1. Navigate to the Plan-Your-Trip landing page
            await page.goto(SEARCH_URL, wait_until="load", timeout=90_000)
            await page.wait_for_load_state("domcontentloaded")
            await intercept_data_layer()
            await self._wait_for_loading(page)

            # 2. Select property in the initial widget (value format "2:CODE")
            await page.select_option("#box-widget_InitialProductSelection", f"2:{property_code}")
            await self._wait_for_loading(page)

            # 3-5. Set rooms, adults, children
            await page.select_option("#box-widget_UnitCount", str(rooms))
            await self._wait_for_loading(page)
            await page.select_option("#box-widget_Adults", str(adults))
            await self._wait_for_loading(page)
            await page.select_option("#box-widget_Children", str(children))
            await self._wait_for_loading(page)

            # 6. Set check-in via datepicker; scans calendar and returns False if unavailable
            checkin_ok, _ = await self._select_date_via_picker(
                page, "wxa-input-container-ArrivalDate", arrival, "Check-in"
            )
            if not checkin_ok:
                return []

            # 7. Dismiss any auto-opened departure datepicker, then set check-out
            try:
                dp = await page.query_selector("#ui-datepicker-div")
                if dp and await dp.is_visible():
                    await page.keyboard.press("Escape")
                    await self._human_delay(page)
            except Exception:
                pass

            checkout_ok, _ = await self._select_date_via_picker(
                page, "wxa-input-container-DepartureDate", departure, "Check-out"
            )
            if not checkout_ok:
                return []

            # 8. Wait for reCAPTCHA to signal readiness before clicking.
            #    The blockUI overlay is shown while reCAPTCHA initializes;
            #    clearing it means it is ready to generate a token on submit.
            #    The token itself is populated BY the click handler, not before.
            try:
                await page.wait_for_selector(".blockUI.blockOverlay", state="hidden", timeout=30_000)
            except Exception:
                if attempt == max_attempts:
                    raise RuntimeError("reCAPTCHA never became ready after all retries")
                print("  reCAPTCHA not ready (blockUI stuck), resetting context ...", file=sys.stderr)
                await self._new_context()
                page = self._page
                continue

            await page.hover("#box-widget > form .wxa-input-container-form-button-panel input.wxa-form-button")
            await self._human_delay(page)
            await page.click("#box-widget > form .wxa-input-container-form-button-panel input.wxa-form-button")
            await self._wait_for_loading(page)

            # 9. Wait for results (redirect: PleaseWait → Results)
            # Also detect "Action not allowed" validation error
            deadline = asyncio.get_event_loop().time() + 90
            action_not_allowed = False
            while True:
                url = page.url
                if "Results" in url or "results" in url:
                    break
                err_el = await page.query_selector("ul.validation-summary-errors")
                if err_el and await err_el.is_visible():
                    action_not_allowed = True
                    break
                if asyncio.get_event_loop().time() > deadline:
                    break
                await page.wait_for_timeout(2_000)
                try:
                    await page.wait_for_url("**/Results**", timeout=4_000)
                    break
                except Exception:
                    pass

            if action_not_allowed:
                property_name = PROPERTIES[property_code]["name"]
                print(
                    f"  Unable to gather room details — 'Action Not Allowed' browser response",
                    file=sys.stderr,
                )
                if attempt < max_attempts:
                    print("  retrying ...", file=sys.stderr)
                    await self._new_context()
                    page = self._page
                    continue
                return [{
                    "property": property_name,
                    "room_type": "Room details unavailable (Action Not Allowed)",
                    "price": "N/A",
                    "dates": f"{fmt_date(arrival)} - {fmt_date(departure)}",
                    "checkin": arrival.date().isoformat(),
                    "checkout": departure.date().isoformat(),
                }]

            break  # successfully reached results page

        await page.wait_for_load_state("load")
        await page.wait_for_timeout(2_000)

        # Collect GA dataLayer items (primary)
        try:
            raw_items = await page.evaluate("() => window.__capturedRooms || []")
            if raw_items:
                property_name = PROPERTIES[property_code]["name"]
                return [_map_ga_item(item, property_name, arrival, departure) for item in raw_items]
        except Exception:
            pass

        # Fallback: DOM parsing
        return await self._parse_dom(page, property_code, arrival, departure)

    async def _parse_dom(
        self,
        page: Page,
        property_code: str,
        arrival: datetime,
        departure: datetime,
    ) -> list[dict[str, Any]]:
        property_name = PROPERTIES[property_code]["name"]

        # No results?
        no_results = await page.query_selector("#box-results-empty-outer")
        if no_results:
            visible = await no_results.is_visible()
            if visible:
                return []

        # Check for results tab
        results_tab = await page.query_selector("#tabsSearchResults")
        if results_tab is None:
            return []

        results: list[dict] = []

        # The site renders room cards. Common patterns from the AHLSMS system:
        # Each unit is a panel/card with a title and price.
        selectors = [
            ".wxa-search-result-panel",
            ".wxa-result",
            ".panel.panel-default .panel-body",
            "[class*='result'][class*='panel']",
            ".accommodation-item",
        ]

        elements = []
        for sel in selectors:
            elements = await page.query_selector_all(sel)
            if elements:
                break

        for el in elements:
            try:
                name_el = await el.query_selector("h2, h3, h4, .panel-title, [class*='name'], [class*='title']")
                name = (await name_el.inner_text()).strip() if name_el else "Unknown Room"

                price_el = await el.query_selector("[class*='price'], [class*='rate'], .price, .rate")
                price = (await price_el.inner_text()).strip() if price_el else "N/A"

                results.append({
                    "property": property_name,
                    "room_type": name,
                    "price": price,
                    "dates": f"{fmt_date(arrival)} - {fmt_date(departure)}",
                    "checkin": arrival.date().isoformat(),
                    "checkout": departure.date().isoformat(),
                })
            except Exception:
                continue

        # If we still have nothing but results tab exists, note availability without detail
        if not results:
            results.append({
                "property": property_name,
                "room_type": "(rooms available — see browser for details)",
                "price": "N/A",
                "dates": f"{fmt_date(arrival)} - {fmt_date(departure)}",
                "checkin": arrival.date().isoformat(),
                "checkout": departure.date().isoformat(),
            })

        return results


def _map_ga_item(item: dict, property_name: str, arrival: datetime, departure: datetime) -> dict[str, Any]:
    price_raw = item.get("price", "N/A")
    price = f"${price_raw}" if price_raw and price_raw != "N/A" else "N/A"
    try:
        price = f"${float(price_raw):,.2f}"
    except (TypeError, ValueError):
        pass

    return {
        "property": property_name,
        "room_type": item.get("item_name") or item.get("item_variant") or "Unknown Room",
        "price": price,
        "dates": f"{fmt_date(arrival)} - {fmt_date(departure)}",
        "checkin": arrival.date().isoformat(),
        "checkout": departure.date().isoformat(),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Output formatting
# ──────────────────────────────────────────────────────────────────────────────

def print_table(results: list[dict], search_meta: dict) -> None:
    if not results:
        start = search_meta["start_date"]
        end   = search_meta["end_date"]
        print(f"No availability found for {start} - {end}")
        return

    cols = {
        "PROPERTY":  max(len("PROPERTY"),  max(len(r["property"])  for r in results)),
        "ROOM TYPE": max(len("ROOM TYPE"), max(len(r["room_type"]) for r in results)),
        "PRICE":     max(len("PRICE"),     max(len(r["price"])     for r in results)),
        "DATES":     max(len("DATES"),     max(len(r["dates"])     for r in results)),
    }

    header = "  ".join(h.ljust(w) for h, w in cols.items())
    print(header)
    print("  ".join("-" * w for w in cols.values()))

    for r in results:
        row = (
            r["property"].ljust(cols["PROPERTY"]) + "  " +
            r["room_type"].ljust(cols["ROOM TYPE"]) + "  " +
            r["price"].ljust(cols["PRICE"]) + "  " +
            r["dates"]
        )
        print(row)

    n = len(results)
    props = len({r["property"] for r in results})
    print(f"\n{n} room{'s' if n != 1 else ''} available across {props} propert{'ies' if props != 1 else 'y'}")


def print_json(results: list[dict], search_meta: dict) -> None:
    output = {
        "search": search_meta,
        "results": results,
        "total_rooms": len(results),
    }
    print(json.dumps(output, indent=2))


def _send_pushover(results: list[dict], search_meta: dict) -> None:
    user_key  = os.environ.get("PUSHOVER_USER_KEY", "")
    api_token = os.environ.get("PUSHOVER_API_TOKEN", "")
    if not user_key or not api_token:
        return
    lines = [
        f"{r['property']} — {r['room_type']} — {r['price']} ({r['dates']})"
        for r in results
    ]
    n = len(results)
    lines.append(f"\nTotal: {n} room{'s' if n != 1 else ''} available")
    payload = urllib.parse.urlencode({
        "token":    api_token,
        "user":     user_key,
        "title":    "🏕 Yosemite Availability!",
        "message":  "\n".join(lines),
        "priority": "1",
        "sound":    "siren",
    }).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                "https://api.pushover.net/1/messages.json",
                data=payload,
                method="POST",
            ),
            timeout=10,
        )
        print("Pushover notification sent", file=sys.stderr)
    except Exception as e:
        print(f"Pushover notification failed: {e}", file=sys.stderr)


def print_properties(fmt: str) -> None:
    rows = [
        {"code": code, "alias": info["aliases"][0], "name": info["name"]}
        for code, info in PROPERTIES.items()
    ]
    if fmt == "json":
        print(json.dumps(rows, indent=2))
        return

    cols = {
        "CODE":  max(len("CODE"),  max(len(r["code"])  for r in rows)),
        "ALIAS": max(len("ALIAS"), max(len(r["alias"]) for r in rows)),
        "NAME":  max(len("NAME"),  max(len(r["name"])  for r in rows)),
    }
    header = "  ".join(h.ljust(w) for h, w in cols.items())
    print(header)
    print("  ".join("-" * w for w in cols.values()))
    for r in rows:
        print(r["code"].ljust(cols["CODE"]) + "  " + r["alias"].ljust(cols["ALIAS"]) + "  " + r["name"])


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check Yosemite hotel availability",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # List all properties
  python yosemite_checker.py properties

  # Check availability for 2 adults, June 25-27
  python yosemite_checker.py --start-date 2026-06-25 --end-date 2026-06-27 --adults 2

  # Check specific properties
  python yosemite_checker.py --start-date 2026-06-25 --end-date 2026-06-27 --property ahwahnee,valley-lodge

  # Scan each night individually (recommended for better results)
  python yosemite_checker.py --start-date 2026-06-25 --end-date 2026-06-28 --scan

  # JSON output
  python yosemite_checker.py --start-date 2026-06-25 --end-date 2026-06-27 -o json
""",
    )

    subparsers = parser.add_subparsers(dest="subcommand")
    props_parser = subparsers.add_parser("properties", help="List available properties and their codes")
    props_parser.add_argument("-o", "--output", choices=["table", "json"], default="table",
                              help="Output format (default: table)")

    parser.add_argument("--start-date", metavar="YYYY-MM-DD",
                        default=os.environ.get("START_DATE"),
                        help="Check-in date (env: START_DATE)")
    parser.add_argument("--end-date", metavar="YYYY-MM-DD",
                        default=os.environ.get("END_DATE"),
                        help="Check-out date (env: END_DATE)")
    parser.add_argument("--property", metavar="CODE_OR_ALIAS",
                        default=os.environ.get("PROPERTY"),
                        help="Comma-separated property codes/aliases (default: all). "
                             "E.g. --property ahwahnee,valley-lodge or --property M,Y (env: PROPERTY)")
    parser.add_argument("--adults", type=int,
                        default=int(os.environ["ADULTS"]) if "ADULTS" in os.environ else 2,
                        metavar="N", help="Number of adults (default: 2, env: ADULTS)")
    parser.add_argument("--children", type=int,
                        default=int(os.environ["CHILDREN"]) if "CHILDREN" in os.environ else 0,
                        metavar="N", help="Number of children age 12 and under (default: 0, env: CHILDREN)")
    parser.add_argument("--rooms", type=int,
                        default=int(os.environ["ROOMS"]) if "ROOMS" in os.environ else 1,
                        metavar="N", help="Number of rooms (default: 1, env: ROOMS)")
    parser.add_argument("--scan", action="store_true",
                        default=os.environ.get("SCAN", "").lower() in ("1", "true"),
                        help="Check each single night individually across the date range (env: SCAN=1)")
    parser.add_argument("--retries", type=int, default=3, metavar="N",
                        help="Number of attempts per search before giving up (default: 3)")
    parser.add_argument("-o", "--output", choices=["table", "json"], default="table",
                        help="Output format (default: table)")
    parser.add_argument("--config", metavar="FILE",
                        help="JSON config file with search parameters (runs all defined searches)")
    parser.add_argument("--browser-ws", metavar="URL",
                        help="Connect to remote browser via CDP (e.g. ws://sockpuppetbrowser:3000) "
                             "instead of launching local Chromium. Also reads BROWSER_WS env var.")
    parser.add_argument("--no-headless", action="store_true",
                        help="Show browser window (useful for debugging)")
    parser.add_argument("--save-html", metavar="FILE",
                        help="Save raw HTML of each results page to FILE (use {n} for numbering)")
    return parser


def _build_windows(start: datetime, end: datetime, scan: bool) -> list[tuple[datetime, datetime]]:
    if scan:
        windows = []
        day = start
        while day < end:
            windows.append((day, day + timedelta(days=1)))
            day += timedelta(days=1)
        return windows
    return [(start, end)]


async def _run_searches(
    checker: "YosemiteChecker",
    windows: list[tuple[datetime, datetime]],
    prop_codes: list[str],
    adults: int,
    children: int,
    rooms: int,
    retries: int,
    save_html: str | None,
) -> list[dict]:
    all_results: list[dict] = []
    html_counter = 0

    for arrival, departure in windows:
        for code in prop_codes:
            prop_name = PROPERTIES[code]["name"]
            if len(windows) > 1 or len(prop_codes) > 1:
                print(
                    f"Checking {prop_name}: {arrival.strftime('%Y-%m-%d')} → {departure.strftime('%Y-%m-%d')} ...",
                    file=sys.stderr,
                )

            try:
                results = await checker.search(
                    arrival=arrival,
                    departure=departure,
                    property_code=code,
                    adults=adults,
                    children=children,
                    rooms=rooms,
                    retries=retries,
                )
            except Exception as exc:
                label = f"{code}_{arrival.strftime('%Y-%m-%d')}"
                base = await checker.dump_diagnostics(label)
                print(f"  error: {exc}", file=sys.stderr)
                print(f"  diagnostics saved to {base}.html and {base}.log", file=sys.stderr)
                raise

            if save_html:
                html_counter += 1
                fname = save_html.replace("{n}", str(html_counter))
                if "{n}" not in save_html and html_counter > 1:
                    base_name, _, ext = save_html.rpartition(".")
                    fname = f"{base_name}_{html_counter}.{ext}" if base_name else f"{save_html}_{html_counter}"
                page_html = await checker._page.content()
                with open(fname, "w", encoding="utf-8") as f:
                    f.write(page_html)

            all_results.extend(results)

    return all_results


async def run(args: argparse.Namespace) -> None:
    if args.subcommand == "properties":
        print_properties(args.output)
        return

    headless   = not args.no_headless
    browser_ws = getattr(args, "browser_ws", None) or os.environ.get("BROWSER_WS")

    # --config mode: run all searches defined in the JSON file
    if getattr(args, "config", None):
        with open(args.config) as f:
            cfg = json.load(f)

        all_results: list[dict] = []
        async with YosemiteChecker(headless=headless, browser_ws=browser_ws) as checker:
            for search_def in cfg.get("searches", []):
                start = parse_date(search_def["start_date"])
                end   = parse_date(search_def["end_date"])
                prop_codes = resolve_properties(search_def["property"]) if search_def.get("property") else list(PROPERTIES.keys())
                windows = _build_windows(start, end, search_def.get("scan", False))
                results = await _run_searches(
                    checker, windows, prop_codes,
                    adults=search_def.get("adults", 2),
                    children=search_def.get("children", 0),
                    rooms=search_def.get("rooms", 1),
                    retries=args.retries,
                    save_html=getattr(args, "save_html", None),
                )
                all_results.extend(results)

        search_meta = {"config": args.config}
        if args.output == "json":
            print_json(all_results, search_meta)
        else:
            print_table(all_results, search_meta)
        if all_results:
            _send_pushover(all_results, search_meta)
        sys.exit(0 if all_results else 1)

    # Normal CLI mode
    if not args.start_date or not args.end_date:
        print("error: --start-date and --end-date are required (or use --config)", file=sys.stderr)
        sys.exit(1)

    start = parse_date(args.start_date)
    end   = parse_date(args.end_date)

    if start >= end:
        print("error: --start-date must be before --end-date", file=sys.stderr)
        sys.exit(1)

    if start.date() < datetime.now().date():
        print("error: --start-date cannot be in the past", file=sys.stderr)
        sys.exit(1)

    prop_codes = resolve_properties(args.property) if args.property else list(PROPERTIES.keys())
    windows = _build_windows(start, end, args.scan)

    async with YosemiteChecker(headless=headless, browser_ws=browser_ws) as checker:
        all_results = await _run_searches(
            checker, windows, prop_codes,
            adults=args.adults,
            children=args.children,
            rooms=args.rooms,
            retries=args.retries,
            save_html=getattr(args, "save_html", None),
        )

    search_meta = {
        "start_date": args.start_date,
        "end_date":   args.end_date,
        "adults":     args.adults,
        "children":   args.children,
        "rooms":      args.rooms,
        "properties": prop_codes,
        "scan":       args.scan,
    }

    if args.output == "json":
        print_json(all_results, search_meta)
    else:
        print_table(all_results, search_meta)

    if all_results:
        _send_pushover(all_results, search_meta)

    sys.exit(0 if all_results else 1)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.subcommand is None and not args.start_date and not getattr(args, "config", None):
        parser.print_help()
        sys.exit(0)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
