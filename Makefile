# Hermes Social — build & deploy helpers.
#
#   make            → build core + overlay image (hermes-social:TAG)
#   make core       → build only the core Hermes image
#   make image      → build only the overlay (requires core)
#   make camofox    → clone + build the Camofox browser image
#   make up         → docker compose up -d (from deploy/)
#   make down       → docker compose down

TAG        ?= latest
CORE_IMAGE := hermes-social-core:$(TAG)
IMAGE      := hermes-social:$(TAG)

.PHONY: all core image camofox up down

all: core image

core:
	docker build -t $(CORE_IMAGE) core/

image:
	docker build --build-arg CORE_IMAGE=$(CORE_IMAGE) -t $(IMAGE) .

# Camofox is built from the upstream repo (MIT) — cloned as a sibling dir.
camofox:
	@[ -d ../camofox-browser ] || git clone https://github.com/jo-inc/camofox-browser ../camofox-browser
	cd ../camofox-browser && git pull --ff-only && make build

up:
	cd deploy && docker compose up -d

down:
	cd deploy && docker compose down
