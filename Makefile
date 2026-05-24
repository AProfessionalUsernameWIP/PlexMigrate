# Hestia-MediaManager v0.8.0 - convenience Makefile.
#
#   make docker  →  build and start the full web stack (backend + frontend)
#                   via docker compose. Idempotent - `make docker` after
#                   changes rebuilds whatever images need rebuilding.

.PHONY: docker docker-rebuild help clean

# Default target - `make` with no args prints the help.
help:
	@echo "Hestia-MediaManager - Make targets:"
	@echo ""
	@echo "  make docker         Build and start the full web stack (backend + frontend)."
	@echo "                      Uses Docker layer cache - fast when only pip deps changed."
	@echo ""
	@echo "  make docker-rebuild Force a no-cache backend rebuild then start everything."
	@echo "                      Use this after any Python source change to guarantee"
	@echo "                      the container picks up the new code. Slower than"
	@echo "                      make docker but bypasses the WSL2/Windows inode-caching"
	@echo "                      issue that can cause Docker to silently serve stale code."
	@echo ""
	@echo "  make clean          Remove the ./venv directory (does not touch snapshots or logs)."

docker:
	docker compose up --build

# Force a clean backend rebuild - bypasses Docker's layer cache for the
# services/ and server/ COPY steps. Use this whenever Python source files
# change, especially on Windows / WSL2 where inode-timestamp-based caching
# can cause `docker compose up --build` to silently keep a stale image.
# With DOCKER_BUILDKIT=1 in .env, content hashing is used instead, which
# makes `make docker` reliable too - but `make docker-rebuild` is the
# belt-and-suspenders option if you're unsure.
docker-rebuild:
	docker compose build --no-cache backend
	docker compose up -d

clean:
	rm -rf venv
