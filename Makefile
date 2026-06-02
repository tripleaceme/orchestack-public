# OrcheStack — convenience commands.
#
# This Makefile is for OrcheStack-internal operations only (building bundles,
# running the dev stack locally, building images from source). It does NOT
# include monorepo / subtree-push commands — those live outside this published
# repository.
#
# Run `make` or `make help` for the list of targets.
#
# Conventions:
#   - All paths in recipes are relative to this Makefile's directory.
#   - Recipes don't `cd` into subfolders — the working directory stays at
#     the repo root throughout. Anything that needs a different working
#     directory uses `-C` (for git, make) or explicit paths.

# ---------- Configuration ----------------------------------------------------

# Pin the Docker registry / image names here so a future namespace move is one
# Makefile edit, not a sweep across recipes.
DOCKER_NS         := tripleaceme
AUTH_IMAGE        := $(DOCKER_NS)/orchestack-auth
ORCHESTRATOR_IMAGE := $(DOCKER_NS)/orchestack-orchestrator
DASHBOARD_IMAGE   := $(DOCKER_NS)/orchestack-dashboard

# The compose file the dev-* targets operate on. Override on the command line
# (`make dev-up COMPOSE_FILE=path/to/other.yml`) for non-default setups.
COMPOSE_FILE      := system/docker/docker-compose.yml

# ---------- Help (default target) -------------------------------------------

.DEFAULT_GOAL := help

# `make help` parses each target's `## <text>` comment and prints them in
# the order they appear. Add `## <description>` to any target you want to
# show up in `make help`; targets without that comment stay hidden.
.PHONY: help
help: ## Show this list of targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_.-]+:.*?## / {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---------- Local development -----------------------------------------------

.PHONY: dev-up
dev-up: ## Bring up the local stack (docker compose up -d)
	docker compose -f $(COMPOSE_FILE) up -d

.PHONY: dev-down
dev-down: ## Stop the local stack (preserves the postgres volume)
	docker compose -f $(COMPOSE_FILE) down

.PHONY: dev-reset
dev-reset: ## Stop + wipe the postgres volume + restart (fresh DB)
	docker compose -f $(COMPOSE_FILE) down -v
	docker compose -f $(COMPOSE_FILE) up -d

.PHONY: dev-logs
dev-logs: ## Tail logs from every service (Ctrl-C to exit)
	docker compose -f $(COMPOSE_FILE) logs -f

.PHONY: dev-ps
dev-ps: ## Show the state of every container
	docker compose -f $(COMPOSE_FILE) ps

.PHONY: dev-pull
dev-pull: ## Pull every image referenced in the compose file
	docker compose -f $(COMPOSE_FILE) pull

# ---------- Image builds (from local source, not Docker Hub) ----------------

.PHONY: image-auth
image-auth: ## Build the orchestack-auth image locally from system/auth/
	docker build -t $(AUTH_IMAGE):dev -f system/auth/Dockerfile .

.PHONY: image-orchestrator
image-orchestrator: ## Build the orchestack-orchestrator image locally (M2)
	docker build -t $(ORCHESTRATOR_IMAGE):dev -f system/orchestrator/Dockerfile system/orchestrator

.PHONY: image-dashboard
image-dashboard: ## Build the orchestack-dashboard image locally (M3)
	docker build -t $(DASHBOARD_IMAGE):dev -f system/dashboard/Dockerfile system/dashboard

# ---------- Runtime bundles -------------------------------------------------

.PHONY: bundle
bundle: ## Build a runtime tarball locally (same as the CI release workflow)
	./scripts/build-bundle.sh

.PHONY: bundle-clean
bundle-clean: ## Remove any leftover bundle files at the repo root
	rm -f orchestack-runtime*.tar.gz orchestack-runtime*.tar.gz.sha256

# ---------- Releases --------------------------------------------------------

.PHONY: tag-release
tag-release: ## Tag a release (call with VERSION=0.1.1, fires CI on push)
	@if [ -z "$(VERSION)" ]; then \
		echo "Usage: make tag-release VERSION=0.1.1"; \
		exit 1; \
	fi
	git tag -a v$(VERSION) -m "OrcheStack v$(VERSION)"
	@echo
	@echo "Tagged v$(VERSION). To publish, push the tag:"
	@echo "  git push <remote> v$(VERSION)"

# ---------- Verification -----------------------------------------------------

.PHONY: verify
verify: ## Confirm the local stack is serving (curl /signup, expects 200)
	@curl -fsSI http://localhost/signup > /dev/null \
		&& echo "/signup serving 200 OK" \
		|| (echo "/signup is not serving, run 'make dev-up' or check 'make dev-logs'"; exit 1)
