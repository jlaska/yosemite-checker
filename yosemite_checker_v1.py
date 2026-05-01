#!/usr/bin/env python3
"""
Yosemite hotel availability checker.

Uses Playwright to automate the reCAPTCHA-protected booking form at
reservations.ahlsmsworld.com and reports available rooms.
"""

import argparse
import asyncio
import json
import sys
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

SEARCH_URL = "https://reservations.ahlsmsworld.com/Yosemite/Search/Accomodations/"
# SEARCH_URL = "https://reservations.ahlsmsworld.com/Yosemite/Plan-Your-Trip"


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


# ──────────────────────────────────────────────────────────────────────────────
# Browser automation
# ──────────────────────────────────────────────────────────────────────────────

class YosemiteChecker:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = await self._browser.new_context(
            viewport={"width": 1320, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
            # Override Sec-CH-UA client hint headers to remove HeadlessChrome —
            # reCAPTCHA Enterprise reads these and refuses to issue tokens for
            # headless browsers.
            extra_http_headers={
                "Sec-CH-UA": '"Not/A)Brand";v="8", "Chromium";v="145", "Google Chrome";v="145"',
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"macOS"',
            },
        )
        # Patch JS-visible automation signals reCAPTCHA checks
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
        self._console_log: list[str] = []
        self._page.on("console", lambda msg: self._console_log.append(f"[{msg.type}] {msg.text}"))
        self._page.on("requestfailed", lambda req: self._console_log.append(
            f"[requestfailed] {req.failure} — {req.url}"
        ))
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

    async def dump_diagnostics(self, label: str) -> str:
        """Save current DOM and console log to a timestamped file pair, return base path."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = label.replace(" ", "_").replace("/", "-")
        base = f"diag_{safe}_{ts}"
        try:
            html = await self._page.content()
            with open(f"{base}.html", "w", encoding="utf-8") as f:
                f.write(html)
        except Exception as e:
            print(f"  (could not save HTML: {e})", file=sys.stderr)
        with open(f"{base}.log", "w", encoding="utf-8") as f:
            f.write(f"URL: {self._page.url}\n\n")
            f.write("\n".join(self._console_log))
        return base

    async def search(
        self,
        arrival: datetime,
        departure: datetime,
        property_code: str,
        adults: int,
        children: int,
        rooms: int,
    ) -> list[dict[str, Any]]:
        page = self._page
        self._console_log.clear()

        # Inject a dataLayer interceptor before navigating so we capture push events
        captured_items: list[dict] = []

        async def intercept_data_layer():
            await page.evaluate("""() => {
                window.__capturedRooms = [];
                const orig = window.dataLayer ? window.dataLayer.push.bind(window.dataLayer) : null;
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

        arr_str = fmt_date(arrival)
        dep_str = fmt_date(departure)
        arr_nd  = arrival.strftime("%Y-%m-%d")
        dep_nd  = departure.strftime("%Y-%m-%d")

        # Retry loop: if the page's reCAPTCHA/blockUI init gets stuck, reload and try again.
        max_attempts = 3
        retry_delay = 15  # seconds between attempts
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                print(f"  waiting {retry_delay}s before retry (attempt {attempt}/{max_attempts}) ...", file=sys.stderr)
                await page.wait_for_timeout(retry_delay * 1000)

            await page.goto(SEARCH_URL, wait_until="load", timeout=90_000)
            await page.wait_for_timeout(3_000)
            await page.wait_for_load_state("domcontentloaded")

            await intercept_data_layer()

            # Set property
            await page.select_option("#box-widget_ProductSelection", property_code)
            await page.wait_for_timeout(500)

            # Set dates — fill both the hidden native date input and the visible text input.
            # The datepicker syncs from the _nd input via change events.
            await page.evaluate(f"""() => {{
                const arrNd  = document.querySelector('#box-widget_ArrivalDate_nd');
                const arrVis = document.querySelector('#box-widget_ArrivalDate');
                const depNd  = document.querySelector('#box-widget_DepartureDate_nd');
                const depVis = document.querySelector('#box-widget_DepartureDate');
                if (arrNd)  {{ arrNd.value  = '{arr_nd}';  arrNd.dispatchEvent(new Event('change', {{bubbles:true}})); }}
                if (arrVis) {{ arrVis.value = '{arr_str}'; arrVis.dispatchEvent(new Event('change', {{bubbles:true}})); }}
                if (depNd)  {{ depNd.value  = '{dep_nd}';  depNd.dispatchEvent(new Event('change', {{bubbles:true}})); }}
                if (depVis) {{ depVis.value = '{dep_str}'; depVis.dispatchEvent(new Event('change', {{bubbles:true}})); }}
            }}""")
            await page.wait_for_timeout(500)

            # Set guest counts
            await page.select_option("#box-widget_Adults",   str(adults))
            await page.select_option("#box-widget_Children", str(children))
            await page.select_option("#box-widget_UnitCount", str(rooms))
            await page.wait_for_timeout(1_500)

            # Wait for the blockUI overlay (shown during reCAPTCHA init) to clear.
            # If it stays stuck the page's JS has wedged; reload and retry.
            try:
                await page.wait_for_selector(".blockUI.blockOverlay", state="hidden", timeout=60_000)
            except Exception:
                if attempt == max_attempts:
                    raise
                print(f"  blockUI overlay stuck after 60s, reloading page ...", file=sys.stderr)
                continue

            # Submit — target the enabled button inside the actual search form
            # (there's also a disabled button in the hidden overview widget)
            await page.click('form[name="wxa-form-search"] input.wxa-form-button')

            # Wait for results (redirect chain: PleaseWait → Results).
            # Also watch for a server-side validation error ("Action not allowed")
            # which keeps the page on the search URL with an error banner.
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
                if attempt == max_attempts:
                    raise RuntimeError("'Action not allowed' validation error persisted after all retries")
                print("  'Action not allowed' from server, reloading and retrying ...", file=sys.stderr)
                continue

            break  # successfully reached results page

        await page.wait_for_load_state("load")
        await page.wait_for_timeout(2_000)

        # Attempt to collect GA dataLayer items
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

    parser.add_argument("--start-date", metavar="YYYY-MM-DD", help="Check-in date")
    parser.add_argument("--end-date",   metavar="YYYY-MM-DD", help="Check-out date")
    parser.add_argument("--property", metavar="CODE_OR_ALIAS",
                        help="Comma-separated property codes/aliases (default: all). "
                             "E.g. --property ahwahnee,valley-lodge or --property M,Y")
    parser.add_argument("--adults",   type=int, default=2, metavar="N", help="Number of adults (default: 2)")
    parser.add_argument("--children", type=int, default=0, metavar="N", help="Number of children age 12 and under (default: 0)")
    parser.add_argument("--rooms",    type=int, default=1, metavar="N", help="Number of rooms (default: 1)")
    parser.add_argument("--scan", action="store_true",
                        help="Check each single night individually across the date range")
    parser.add_argument("-o", "--output", choices=["table", "json"], default="table",
                        help="Output format (default: table)")
    parser.add_argument("--config", metavar="FILE",
                        help="JSON config file with search parameters (runs all defined searches)")
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

    headless = not args.no_headless

    # --config mode: run all searches defined in the JSON file
    if getattr(args, "config", None):
        with open(args.config) as f:
            cfg = json.load(f)

        all_results: list[dict] = []
        async with YosemiteChecker(headless=headless) as checker:
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
                    save_html=getattr(args, "save_html", None),
                )
                all_results.extend(results)

        search_meta = {"config": args.config}
        if args.output == "json":
            print_json(all_results, search_meta)
        else:
            print_table(all_results, search_meta)
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

    async with YosemiteChecker(headless=headless) as checker:
        all_results = await _run_searches(
            checker, windows, prop_codes,
            adults=args.adults,
            children=args.children,
            rooms=args.rooms,
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
