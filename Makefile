# PlexMigrate v0.8.0 — convenience Makefile.
#
# Two targets:
#   make docker  →  build and start the full web stack (backend + frontend)
#                   via docker compose. Idempotent — `make docker` after
#                   changes rebuilds whatever images need rebuilding.
#   make cli     →  create ./venv with all pip deps and print the
#                   activation command. For users who only want to run
#                   the terminal CLI (no Docker, no web frontend).
#
# This file detects whether `python3` or `python` is on PATH and uses
# whichever is available. On Windows the `make cli` target also prints
# the correct PowerShell activation path.

.PHONY: docker cli help clean

# Default target — `make` with no args prints the help.
help:
	@echo "PlexMigrate v0.8.0 — Make targets:"
	@echo ""
	@echo "  make docker   Build and start the full web stack (backend + frontend)."
	@echo "                Runs: docker compose up --build"
	@echo ""
	@echo "  make cli      Create ./venv, install pip deps, print activation command."
	@echo "                Use this if you only want the terminal CLI and don't"
	@echo "                want to use Docker."
	@echo ""
	@echo "  make clean    Remove the ./venv directory (does not touch exports or logs)."

docker:
	docker compose up --build

# Pick a python executable. `python3` on macOS/Linux, `python` on Windows.
PYTHON := $(shell command -v python3 2>/dev/null || command -v python 2>/dev/null)

cli:
	@if [ -z "$(PYTHON)" ]; then \
		echo "ERROR: no python3 or python on PATH. Install Python 3.9+ first."; \
		exit 1; \
	fi
	$(PYTHON) -m venv venv
	./venv/bin/pip install --upgrade pip || ./venv/Scripts/pip install --upgrade pip
	./venv/bin/pip install -r requirements.txt || ./venv/Scripts/pip install -r requirements.txt
	@echo ""
	@echo "Virtual environment ready."
	@echo ""
	@echo "Activate it with:"
	@echo ""
	@echo "  Linux / macOS :   source venv/bin/activate"
	@echo "  Windows (PS)  :   .\\venv\\Scripts\\Activate.ps1"
	@echo "  Windows (cmd) :   venv\\Scripts\\activate.bat"
	@echo ""
	@echo "Then run the CLI as before:"
	@echo ""
	@echo "  python plexmigrate.py --export --server http://localhost:32400"
	@echo ""

clean:
	rm -rf venv
