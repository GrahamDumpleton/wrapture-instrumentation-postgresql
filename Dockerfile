# The environment the test matrix runs in: Debian with uv, and the
# one thing psycopg's pure Python implementation needs from the
# machine, a libpq. Nothing else is baked in: uv fetches whichever
# interpreter the run asks for and installs the test dependencies into
# a per-version environment, both on named volumes so they persist
# across runs (see compose.yml). The repository is bind-mounted at
# run time, so the image never goes stale as the code changes.

FROM ghcr.io/astral-sh/uv:debian-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
