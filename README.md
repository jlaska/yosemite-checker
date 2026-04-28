# yosemite-checker

Check hotel availability at Yosemite National Park lodging properties via the `reservations.ahlsmsworld.com` booking system.

Uses Playwright (headless Chromium) because the site requires Google reCAPTCHA v3 — direct HTTP requests won't work.

## Setup

```bash
pip install playwright
playwright install chromium
```

Or reuse the venv from the reverse-api-engineer project if you have it:

```bash
/path/to/venv/bin/python yosemite_checker.py ...
```

## Usage

```
python yosemite_checker.py [options] [--start-date YYYY-MM-DD --end-date YYYY-MM-DD]
python yosemite_checker.py properties [-o json]
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `--start-date YYYY-MM-DD` | Check-in date | required |
| `--end-date YYYY-MM-DD` | Check-out date | required |
| `--property CODE_OR_ALIAS` | Comma-separated properties (see `properties`) | all |
| `--adults N` | Number of adults | 2 |
| `--children N` | Children age 12 and under | 0 |
| `--rooms N` | Number of rooms | 1 |
| `--scan` | Check each single night individually | off |
| `-o, --output table\|json` | Output format | table |
| `--no-headless` | Show browser window (debugging) | off |
| `--save-html FILE` | Save raw HTML to FILE | off |

### List available properties

```bash
python yosemite_checker.py properties
python yosemite_checker.py properties -o json
```

### Check availability

```bash
# All properties, Jun 25–27
python yosemite_checker.py --start-date 2026-06-25 --end-date 2026-06-27

# Specific properties (code or alias)
python yosemite_checker.py --start-date 2026-06-25 --end-date 2026-06-27 \
  --property ahwahnee,valley-lodge

# With guests
python yosemite_checker.py --start-date 2026-06-25 --end-date 2026-06-27 \
  --adults 2 --children 1

# Scan each night individually (recommended — the site suggests this)
python yosemite_checker.py --start-date 2026-06-25 --end-date 2026-06-30 \
  --property ahwahnee,valley-lodge --scan

# JSON output (pipe to jq, etc.)
python yosemite_checker.py --start-date 2026-06-25 --end-date 2026-06-27 \
  --property ahwahnee -o json | jq '.total_rooms'
```

## Properties

| Code | Alias | Name |
|------|-------|------|
| M | ahwahnee | The Ahwahnee |
| Y | valley-lodge | Yosemite Valley Lodge |
| D | curry-village | Curry Village |
| H | housekeeping | Housekeeping Camp |
| T | tuolumne | Tuolumne Meadows Lodge |

## Notes

- Each search takes ~20–30 seconds due to browser startup + reCAPTCHA wait
- `--scan` mode reuses the same browser session, so N nights × M properties = N×M searches without re-launching
- Result parsing uses the site's Google Analytics `dataLayer` events for structured data; falls back to DOM parsing
- If rooms are found but details are incomplete, re-run with `--no-headless` to watch the browser
