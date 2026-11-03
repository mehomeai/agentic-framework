# Browser image build / provision (operator only)

The runtime never pulls or downloads images/binaries. The home operator
pre-provisions the isolated browser image on the home host. Pinned to the
current primary-docs release (verified 2026-11-03 against
https://playwright.dev/python/docs/docker and the MCR tag list:
`v1.64.1-noble`, Ubuntu 24.04 LTS Noble; SDK pin matches the image tag):

```sh
# from the agentic-framework checkout
docker build -t me-browser-playwright:v1.64.1 -f docker/browser/Dockerfile .
docker image inspect me-browser-playwright:v1.64.1

# optional override (default is the tag above)
export ME_RUNTIME_DOCKER_IMAGE_BROWSER=me-browser-playwright:v1.64.1
export ME_RUNTIME_SANDBOX=docker   # code_exec gate (unchanged)
```

Verify (no network, no mounts beyond the read-only worker dir):

```sh
echo '{"v":1,"id":1,"op":"close"}' | docker run -i --rm --pull never \
  --network none --user 65532:65532 --cap-drop ALL \
  --security-opt no-new-privileges --cpus 1.0 --memory 512m \
  --pids-limit 128 --read-only --tmpfs /tmp:rw,size=256m \
  -e HOME=/tmp --workdir /tmp \
  -v $PWD/src/me_runtime/tools:/worker:ro \
  me-browser-playwright:v1.64.1 python /worker/browser_worker.py
```

Production never falls back to host Chrome — no env var, API field, or
model request can enable one. If Docker or the image is absent,
`browser_open` fails closed with `prerequisite_missing`.

Test-harness synthetic DOM smoke (explicit test-only worker DI, never
production): `DirectDomSmokeTests` in `tests/test_browser_interactive.py`
passes `test_worker_popen_factory(system_chrome)` directly to the manager
and runs the real worker against injected synthetic HTML. It needs the
small `playwright` pip package only (`pip install -e ".[browser]"`) plus
a host system Chrome; no browser binary is downloaded.
