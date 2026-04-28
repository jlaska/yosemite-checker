# yosemite-checker

Check hotel availability at Yosemite National Park lodging properties via the `reservations.ahlsmsworld.com` booking system.

Uses Playwright (headless Chromium) because the site requires Google reCAPTCHA v3 — direct HTTP requests won't work.

## Setup

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/). Install Chromium once:

```bash
make setup
```

## Automated hourly monitoring (recommended)

Copy the config template, fill in your search parameters and [Pushover](https://pushover.net) credentials, then install the launchd job:

```bash
cp config.json.example config.json
$EDITOR config.json
make install
```

Sends a Pushover notification (priority 1, siren sound) whenever availability is found.

```bash
make run              # manual one-shot run (headless)
make run HEADLESS=0   # show browser window for debugging
make test-pushover    # verify Pushover credentials
make status           # check if launchd job is loaded
make logs             # tail ~/Library/Logs/yosemite-checker.log
make uninstall        # remove the launchd job
```

## Manual CLI usage

```
uv run yosemite_checker.py [options] --start-date YYYY-MM-DD --end-date YYYY-MM-DD
uv run yosemite_checker.py properties [-o json]
uv run yosemite_checker.py --config config.json [-o json]
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
| `--config FILE` | Load search parameters from a JSON file | off |
| `-o, --output table\|json` | Output format | table |
| `--no-headless` | Show browser window (debugging) | off |
| `--save-html FILE` | Save raw HTML to FILE | off |

### Examples

```bash
# List available properties
uv run yosemite_checker.py properties

# All properties, Jun 25–27
uv run yosemite_checker.py --start-date 2026-06-25 --end-date 2026-06-27

# Specific properties (code or alias)
uv run yosemite_checker.py --start-date 2026-06-25 --end-date 2026-06-27 \
  --property ahwahnee,valley-lodge

# Scan each night individually (recommended — the site suggests this)
uv run yosemite_checker.py --start-date 2026-06-25 --end-date 2026-06-30 \
  --property ahwahnee,valley-lodge --scan

# JSON output (pipe to jq, etc.)
uv run yosemite_checker.py --start-date 2026-06-25 --end-date 2026-06-27 \
  --property ahwahnee -o json | jq '.total_rooms'

# Run all searches from config file
uv run yosemite_checker.py --config config.json -o json
```

Exit code is `0` when availability is found, `1` when none.

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
- `--scan` mode reuses the same browser session across all date/property combinations
- Result parsing intercepts the site's Google Analytics `dataLayer` events for structured data; falls back to DOM parsing
- If rooms are found but details are incomplete, re-run with `--no-headless` to watch the browser
- `config.json` is gitignored — credentials never leave your machine
