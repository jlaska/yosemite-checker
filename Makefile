PLIST_NAME  := com.jlaska.yosemite-checker
PLIST_SRC   := $(PLIST_NAME).plist
PLIST_DEST  := $(HOME)/Library/LaunchAgents/$(PLIST_NAME).plist
LOG_FILE    := $(HOME)/Library/Logs/yosemite-checker.log
CONFIG      := config.json

.PHONY: setup install uninstall run test-pushover status logs clean release version help

help:
	@echo "Yosemite availability checker"
	@echo ""
	@echo "  make setup              Install Chromium for Playwright (one-time)"
	@echo "  make install            Install hourly launchd job"
	@echo "  make uninstall          Remove launchd job"
	@echo "  make run                Run a check right now (manual)"
	@echo "  make test-pushover      Send a test Pushover notification"
	@echo "  make status             Show launchd job status"
	@echo "  make logs               Tail the log file"
	@echo "  make clean              Remove logs and diagnostic files"
	@echo "  make version            Show current release version"
	@echo "  make release VERSION=x.y.z  Tag and push a new release"

setup:
	uv run playwright install chromium

install: _check-config
	mkdir -p "$(HOME)/Library/LaunchAgents"
	cp "$(PLIST_SRC)" "$(PLIST_DEST)"
	launchctl bootstrap gui/$$(id -u) "$(PLIST_DEST)"
	@echo "Installed. Runs every hour. Use 'make status' to verify."

uninstall:
	-launchctl bootout gui/$$(id -u) "$(PLIST_DEST)"
	-rm "$(PLIST_DEST)"
	@echo "Uninstalled."

run: _check-config
	HEADLESS=$(HEADLESS) \
	START_DATE=$(START_DATE) END_DATE=$(END_DATE) PROPERTY=$(PROPERTY) \
	ADULTS=$(ADULTS) CHILDREN=$(CHILDREN) ROOMS=$(ROOMS) SCAN=$(SCAN) \
	PUSHOVER_USER_KEY=$(PUSHOVER_USER_KEY) PUSHOVER_API_TOKEN=$(PUSHOVER_API_TOKEN) \
	bash run_checker.sh

test-pushover: _check-config
	@user_key=$$(python3 -c "import json; print(json.load(open('$(CONFIG)'))['pushover']['user_key'])"); \
	api_token=$$(python3 -c "import json; print(json.load(open('$(CONFIG)'))['pushover']['api_token'])"); \
	curl -s \
		--form-string "token=$$api_token" \
		--form-string "user=$$user_key" \
		--form-string "title=Yosemite Checker Test" \
		--form-string "message=Test notification from yosemite-checker" \
		--form-string "priority=1" \
		--form-string "sound=siren" \
		https://api.pushover.net/1/messages.json && echo " Pushover notification sent"

status:
	@launchctl print gui/$$(id -u)/$(PLIST_NAME) 2>/dev/null || echo "Job not loaded. Run 'make install' first."

logs:
	mkdir -p "$$(dirname "$(LOG_FILE)")"
	tail -f "$(LOG_FILE)"

clean:
	rm -f "$(LOG_FILE)" diag_*.html diag_*.log

version:
	@git describe --tags --abbrev=0 2>/dev/null || echo "(no tags yet)"

release:
	@test -n "$(VERSION)" || (echo "ERROR: specify a version, e.g. make release VERSION=0.2.0" && exit 1)
	@git diff --quiet && git diff --cached --quiet || (echo "ERROR: uncommitted changes — commit or stash first" && exit 1)
	@echo "Tagging v$(VERSION) and pushing..."
	git tag v$(VERSION)
	git push origin main
	git push origin v$(VERSION)
	@echo "Done. Watch CI at https://github.com/jlaska/yosemite-checker/actions"

_check-config:
	@if [ -z "$(PUSHOVER_USER_KEY)" ] || [ -z "$(PUSHOVER_API_TOKEN)" ]; then \
		test -f "$(CONFIG)" || (echo "ERROR: $(CONFIG) not found. Copy config.json.example to config.json and fill in your credentials." && exit 1); \
		python3 -c "\
import json, sys; \
cfg = json.load(open('$(CONFIG)')); \
po = cfg.get('pushover', {}); \
(print('ERROR: Fill in pushover credentials in $(CONFIG) or set PUSHOVER_USER_KEY/PUSHOVER_API_TOKEN env vars') or sys.exit(1)) \
if not po.get('user_key') or not po.get('api_token') else None"; \
	fi
