# Isolated interactive browser image (browser-worker-v1).
#
# Base: official Playwright Python image, per primary docs
# (https://playwright.dev/python/docs/docker — "Pull the image"). Pinned tag
# below tracks the pinned Python SDK so the container browser engine and the
# host tooling agree. Verified 2026-11-03 against the primary docs + MCR:
# current release is v1.64.1 (`mcr.microsoft.com/playwright/python` tags
# `:v1.64.1-noble`, Ubuntu 24.04 LTS Noble). Operator builds ONLY; no
# images/binaries are downloaded by the runtime itself (`--pull never`,
# pre-provisioned check).
#
#   docker build -t me-browser-playwright:v1.64.1 -f docker/browser/Dockerfile .
#
# Runtime invocation (host-owned, see `build_browser_docker_cmd`):
#   docker run -i --rm --pull never --name <owned> --network none
#     --user 65532:65532 --cap-drop ALL --security-opt no-new-privileges
#     --cpus 1.0 --memory 512m --pids-limit 128 --read-only
#     --tmpfs /tmp:rw,size=256m -e HOME=/tmp --workdir /tmp
#     -v <agentic-framework/src/me_runtime/tools>:/worker:ro
#     me-browser-playwright:v1.64.1 python /worker/browser_worker.py
#
# The container has no NIC; page JS/WebRTC cannot reach home/LAN. Every
# HTTP(S) request is intercepted in the worker and fulfilled via the host
# vetted broker over stdio JSONL. No home filesystem, credentials, sockets,
# or CDP ports are mounted/exposed.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Pin the SDK to the image version (per primary docs the base image does
# NOT bundle the Playwright package — install it separately, no browser
# download here; browsers are already in the base image).
RUN pip install --no-cache-dir "playwright==1.64.1" \
    && python -c "import importlib.metadata as m; assert m.version('playwright')=='1.64.1'; print('playwright sdk 1.64.1 pinned')"

# Worker code is mounted read-only at runtime (/worker); nothing else is
# baked in. Keep the image read-only friendly: no secrets, no volumes.
WORKDIR /tmp
