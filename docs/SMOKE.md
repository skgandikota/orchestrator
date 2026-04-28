# Smoke tests

Smoke tests live in `tests/smoke/` and validate the Phase 1 RAM thesis end
to end: a single 7B model holds the slot at any time, and swapping models
unloads the prior resident with `keep_alive=0` before loading the next.

Two variants exist for every smoke scenario:

| Marker             | Daemon needed | Default      | Where it runs        |
| ------------------ | ------------- | ------------ | -------------------- |
| `@pytest.mark.smoke` | No (mocked)   | Always runs  | CI + local           |
| `@pytest.mark.live`  | Real Ollama   | Skipped      | Local M1 only        |

## Run the mocked smoke suite

```bash
pytest -m smoke -q
```

This is the default that CI exercises -- it never touches the network or
spawns Ollama processes.

## Run the live RAM swap-cycle on the M1

Pre-flight (one-off):

```bash
ollama pull qwen2.5:7b qwen2.5-coder:7b
ollama serve   # in another terminal
```

Then:

```pwsh
# PowerShell
./scripts/smoke_ram_swap.ps1
```

```bash
# bash
./scripts/smoke_ram_swap.sh
```

Both wrappers run `pytest -m live --live tests/smoke/test_ram_swap.py -s`.
The `--live` flag is required: without it, `@pytest.mark.live` tests are
skipped (see `tests/smoke/conftest.py`).
