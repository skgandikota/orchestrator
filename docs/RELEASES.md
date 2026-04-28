# Releases

This document describes how to cut a release and verify the published container
images for `orchestrator`.

## Container images

Two multi-arch images (`linux/amd64` + `linux/arm64`) are published to GHCR by
the [`release-image`](../.github/workflows/release-image.yml) workflow:

| Image | Contents |
|-------|----------|
| `ghcr.io/skgandikota/orchestrator` | Slim runtime (no browser deps) — built from `Dockerfile`. |
| `ghcr.io/skgandikota/orchestrator-browser` | Slim runtime + Playwright/Chromium — built from `Dockerfile.browser`. |

### Tag scheme

| Trigger | Tags applied |
|---------|--------------|
| Push to `main` | `:edge` |
| Push of tag `vX.Y.Z` | `:vX.Y.Z`, `:vX.Y`, `:vX`, `:latest` |
| `workflow_dispatch` | (no tag — manual run, useful for cache warming) |

The workflow uses `docker/metadata-action@v5`, `docker/setup-buildx-action@v3`,
`docker/setup-qemu-action@v3`, `docker/login-action@v3`, and
`docker/build-push-action@v6`. Build cache is GitHub Actions cache
(`type=gha`) scoped per variant. Provenance and SBOM attestations are enabled.

## Cutting a release

1. **Confirm `main` is green** — CI, CodeQL, and the most recent
   `release-image` run on `main` (which produces `:edge`) must all be green.
2. **Update the changelog** if applicable (or rely on GitHub-generated notes).
3. **Tag the release** from `main`:

   ```bash
   git checkout main
   git pull --ff-only
   git tag -s v0.1.0 -m "v0.1.0"
   git push origin v0.1.0
   ```

   Signed tags (`-s`) are preferred so the release artefact can be verified.

4. **Watch the workflow** — pushing the tag triggers `release-image`. The
   `build (slim)` and `build (browser)` jobs run in parallel; `verify` runs
   after both succeed and inspects each manifest list to confirm both
   architectures are present.

5. **Flip package visibility to public (one-time, per image)** — after the
   first successful publish, on github.com:
   `Your profile → Packages → orchestrator → Package settings → Change
   visibility → Public`. Repeat for `orchestrator-browser`. Documented in
   [`docs/DEPLOY.md`](DEPLOY.md).

6. **Publish a GitHub Release** referencing the tag. Auto-generated release
   notes are usually sufficient.

## Verifying a published image

After the workflow finishes, anyone can pull the image without authenticating
(once the package is public):

```bash
# Edge — built on every push to main
docker pull ghcr.io/skgandikota/orchestrator:edge

# Latest tagged release
docker pull ghcr.io/skgandikota/orchestrator:latest

# Specific version
docker pull ghcr.io/skgandikota/orchestrator:v0.1.0
```

Confirm the manifest is multi-arch:

```bash
docker buildx imagetools inspect ghcr.io/skgandikota/orchestrator:edge
# Expect to see entries for both linux/amd64 and linux/arm64.
```

Smoke-test the container:

```bash
docker run --rm --entrypoint python \
  ghcr.io/skgandikota/orchestrator:edge \
  -c "import orchestrator; print('ok')"
```

Run the HTTP API locally (the image's default `CMD`):

```bash
docker run --rm -p 8000:8000 \
  -v "$HOME/.config/orchestrator:/etc/orchestrator" \
  -v "$HOME/.local/share/orchestrator:/var/lib/orchestrator" \
  ghcr.io/skgandikota/orchestrator:edge
# Then:
curl http://localhost:8000/v1/models
```

## Re-running a release

If a release run fails part-way, re-run failed jobs from the Actions UI. The
workflow is idempotent — `docker/build-push-action` will reuse the GHA cache
and `metadata-action` will compute identical tags.

To force a fresh build without bumping the tag, use **Run workflow** from the
Actions tab (`workflow_dispatch`).
