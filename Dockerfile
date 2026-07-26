# Hermes Social — overlay image.
#
# Layers the social/ payload on top of the core Hermes image without
# modifying core. Build the core first (make core), then this (make image);
# `make` runs both in order.
#
# The skills land in /opt/hermes-social/skills, which the runtime picks up
# via `skills.external_dirs` in config.yaml (see social/config/).
ARG CORE_IMAGE=hermes-social-core:latest
FROM ${CORE_IMAGE}

COPY social/skills/ /opt/hermes-social/skills/
COPY social/config/ /opt/hermes-social/config/
COPY social/tools/  /opt/hermes-social/tools/

# Read-only payload for the unprivileged hermes user (UID 10000).
RUN chmod -R a+rX /opt/hermes-social
