# Deploying the coracle with Docker

The coracle ships two production images:

| Variant | Dockerfile | Approx. size | When to use |
| --- | --- | --- | --- |
| **slim** (default) | [`Dockerfile`](../Dockerfile) | < 250 MB | Production. No browser. |
| **browser** | [`Dockerfile.browser`](../Dockerfile.browser) | ~600 MB | Opt-in for the browser-fallback search path (#9). Bakes Playwright + Chromium. |

The slim image **does not** install Ollama or any model weights. Run Ollama
as a sibling container and point the coracle at it via
`OLLAMA_BASE_URL` (see [`p7-docker-compose`](https://github.com/skgandikota/coracle/issues/47)).

---

## Build

```bash
# Slim, production image (default target).
docker build -t coracle:slim --target runtime .

# Browser-fallback variant.
docker build -t coracle:browser -f Dockerfile.browser .

# Multi-arch (amd64 + arm64) — see issue #48 for the CI matrix.
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t ghcr.io/skgandikota/coracle:dev \
  --target runtime \
  --push .
```

The `--target=runtime` arg lets CI build only the slim variant without
also producing the (unused) `builder` stage as a final image.

## Run

```bash
docker run --rm \
  --name coracle \
  -p 8000:8000 \
  -v $(pwd)/config:/etc/coracle:ro \
  -v coracle-data:/var/lib/coracle \
  -e OLLAMA_BASE_URL=http://ollama:11434 \
  coracle:slim
```

Mounts:

- `/etc/coracle` — read-only config directory. The default config path
  inside the image is `/etc/coracle/config.yaml`.
- `/var/lib/coracle` — writable data directory (SQLite cache, logs).

The image runs as the non-root `coracle` user (UID 1000); make sure
host-side bind mounts are readable / writable by that UID.

### MCP-stdio mode

`stdio` is not a network protocol, so port 8000 is irrelevant here. Run the
container with `-i` and override the command:

```bash
docker run --rm -i \
  -v $(pwd)/config:/etc/coracle:ro \
  coracle:slim mcp
```

### CLI one-shots

```bash
docker run --rm \
  -v $(pwd)/config:/etc/coracle:ro \
  coracle:slim cli mcp list
```

### Browser-fallback (opt-in)

Two ways to get Playwright + Chromium at runtime:

1. **Use the browser image** (simplest, recommended for ephemeral runs):

   ```bash
   docker run --rm -p 8000:8000 coracle:browser
   ```

2. **Mount the host's Playwright cache into the slim image** (keeps your
   production image small and shares one browser cache across containers):

   ```bash
   docker run --rm \
     -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
     -v $HOME/.cache/ms-playwright:/ms-playwright:ro \
     -p 8000:8000 \
     coracle:slim
   ```

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `OLLAMA_BASE_URL` | _unset_ | Base URL of the sibling Ollama container, e.g. `http://ollama:11434`. |
| `CORACLE_CONFIG` | `/etc/coracle/config.yaml` | Path to the YAML config file inside the container. |
| `CORACLE_DATA_DIR` | `/var/lib/coracle` | Writable directory for SQLite + logs. |
| `PYTHONUNBUFFERED` | `1` | Stream stdout/stderr without buffering (set in the image). |
| `PYTHONDONTWRITEBYTECODE` | `1` | Disable `.pyc` writes (set in the image). |
| `PLAYWRIGHT_BROWSERS_PATH` | `/opt/playwright` (browser image only) | Where Playwright looks for browser binaries. Override to share a host cache. |

## Healthcheck

The image declares:

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/v1/models || exit 1
```

`docker ps` will show `(healthy)` once `/v1/models` returns 200.

## Troubleshooting

### Ollama unreachable

`/v1/models` returns 502 and the coracle logs
`connection refused: ollama:11434`.

- Ensure both containers are on the same Docker network.
- Verify `OLLAMA_BASE_URL` matches the sibling service name (in
  `docker compose`, this is the service key, not `localhost`).
- From the coracle container: `curl -fsS $OLLAMA_BASE_URL/api/tags`.

### Port 8000 already in use

```
Error: bind: address already in use
```

Pick a different host port: `-p 18000:8000`. The container always listens
on `8000` internally; map it wherever you like on the host.

### Permission denied on volume mounts

The container runs as UID 1000. Bind-mounted host directories must be
readable (and `/var/lib/coracle` writable) by that UID:

```bash
sudo chown -R 1000:1000 ./data
```

Named volumes (`-v coracle-data:/var/lib/coracle`) avoid this
entirely — Docker creates them with the right ownership.

### Image is too large

Confirm you built the slim image with `--target runtime` (not the
`builder` stage), and that `.dockerignore` is in place — a stray
`.venv/` or `tests/` in the build context can balloon the layer cache.

```bash
docker image ls coracle:slim --format '{{.Size}}'
```

## Quick start with docker compose

The repo ships a `compose.yaml` that brings up the coracle together
with a local Ollama. This is the recommended way to run the stack on a
workstation that doesn't already have Ollama installed.

```bash
git clone https://github.com/skgandikota/coracle.git
cd coracle

cp .env.example .env
${EDITOR:-vi} .env          # fill in any free-tier API keys you want active

# 1. First-time model pre-pull (one-shot, ~10–20 min on a fresh machine).
docker compose --profile init up --exit-code-from ollama-init ollama-init

# 2. Start the stack.
docker compose up -d

# 3. Smoke test.
curl -fsS http://localhost:8000/v1/models
```

### Workflows

| Goal | Command |
| --- | --- |
| Cold start (models already pulled) | `docker compose up -d` |
| Pre-pull / refresh models | `docker compose --profile init up ollama-init` |
| Tail coracle + ollama logs | `docker compose logs -f --tail=200` |
| Exec a shell in the coracle | `docker compose exec coracle sh` |
| Stop, keep volumes (model cache + data) | `docker compose down` |
| Stop **and wipe** model cache + data | `docker compose down -v` |

### Browser-fallback variant

```bash
docker compose -f compose.yaml -f compose.browser.yaml up -d
```

The overlay swaps the coracle build to `Dockerfile.browser` so the
optional browser-fallback search path (#9) works without mounting a host
Playwright cache.

### Customizing the deployment

Copy `docker-compose.override.yml.example` to `docker-compose.override.yml`
and uncomment the snippet you need (different host port, expose Ollama on
the host, switch Ollama to the NVIDIA GPU runtime, mount a Playwright
cache for the browser variant). Compose loads the override automatically.

### Volumes

The stack uses two named, project-prefixed volumes so multiple checkouts
don't collide:

- `coracle_ollama-models` — persists the Ollama model cache.
- `coracle_orch-data` — coracle runtime data (SQLite cache, audit log).

`docker compose down` leaves both intact. `docker compose down -v` removes
them.

## Make targets

For local convenience the top-level `Makefile` exposes:

```bash
make docker-build   # build coracle:slim
make docker-run     # run coracle:slim with sensible defaults

make compose-up     # docker compose up -d
make compose-init   # one-shot model pre-pull (profile: init)
make compose-logs   # tail logs from the running stack
make compose-pull   # refresh base images
make compose-down   # stop the stack (keeps volumes)
```
