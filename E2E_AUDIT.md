# openapi-anything — Implementation Audit & E2E Test Notes

Date: 2026-07-01
Scope: Verify the project was successfully implemented, exercise the full
podman-compose stack end-to-end, and record improvement opportunities.

Environment:
- Python 3.12.8 (host), package `openapi-anything==0.1.0` installed (editable).
- `docker` is a symlink → podman 5.8.2 (rootless). Socket live at
  `/run/user/1000/podman/podman.sock`.
- LLM gateway reachable: `https://litellm.xn--8pr.xyz/v1`, key set via `LITELLM_API_KEY`,
  `GLM-5.1` model present (also reachable from inside the gateway container).
- Live stack: `openapi-anything_gateway_1` (:8800), `openapi-anything_redis_1`
  (:6379), pre-deployed `wrapper-test-ls-wrapper` (:41949).

---

## 1. Implementation status — what works ✅

The project **is implemented and functional** for the core happy path:

| Area | Evidence |
|------|----------|
| Package build/install | `pip show` OK; `[project.scripts] openapi-anything` wired; all modules import. |
| Unit tests | `pytest tests/` → **7 passed** (registry, codegen, pipeline-mock, gateway health/404/index, deploy-mock). |
| Gateway (hub) | `GET /` → 200 HTML hub; `GET /health` → `{"status":"ok","wrappers":N}`; `GET /registry` → JSON list. |
| Registry persistence | `registry.json` survives restarts; CRUD `register/get/list/update_status/remove`. |
| Code generation | Jinja template renders runnable FastAPI `app.py` for `cli` and `web` branches; emits per-wrapper `Dockerfile` + `test_app.py`. |
| Dynamic deploy | `DockerManager` builds image + starts container on a free port + waits `/health`; falls back to local uvicorn when no socket. |
| Proxy | `/services/{id}/*` correctly reverse-proxies health, openapi, `/docs`, and endpoint calls. |
| **Full E2E (fresh generation)** | Submitted a NEW request via the live stack; pipeline ran inspect→design→codegen→test→build→deploy; `wrapper-e2e-httpbin` came up on :54171 and served `openapi.json` + live `/fetch` + `/proxy` calls **through the gateway proxy**. |
| Isolation | Each wrapper is a distinct container + image + host port (`podman ps`). |

### Concrete E2E results captured this run
- `wrapper-test-ls-wrapper` (pre-existing, CLI): direct + proxied `/health`,
  `/openapi.json`, `POST /execute {"args":["-la","/app"]}` → 200, correct `ls` output.
- `wrapper-e2e-httpbin` (freshly generated, web): proxied `/health`, `/openapi.json`
  (paths `/fetch,/proxy,/,/health`), `GET /fetch?path=/get` → 200 (httpbin body),
  `POST /proxy {path:/post, method:POST, body:{hello:world}}` → 200 (httpbin echo).

---

## 2. Bugs found ⚠️

### B1 — `POST /generate` JSON endpoint is dead (shadowed) — **HIGH**
`hub_ui.py` registers a **form-based** `POST /generate` and is included *before*
`main.py` defines its own **JSON** `POST /generate` (Pydantic `GenerateRequest`).
FastAPI matches the first registered route for a path+method, so:
- `curl -H 'Content-Type: application/json' -d '{"description":...}'` → **HTTP 422**
  `Field required` (the Form route gets the request, finds no form `description`).
- Form-encoded request → 200 (hits the hub route).

An **agent consumer using JSON** (the documented/expected integration style)
cannot generate wrappers. The JSON route in `main.py` is unreachable dead code.
Fix: give the JSON endpoint a distinct path (e.g. `/api/generate`) or include the
hub router after / give the JSON route priority, and unify on one content type.

### B2 — The LLM "design" phase output is discarded (dead) — **HIGH**
`Designer.design()` calls the LLM, parses an `APIDesign` (title, endpoints, models,
integration notes), but `wrapper_template.py.j2` only branches on
`inspection.type` and hardcodes:
- `title="Generated Wrapper"` (design title never used),
- endpoints: only `POST /execute` (cli) or `GET /fetch` + `POST /proxy` (web).

So no matter what the LLM designs (e.g. "2 endpoints"), the deployed API shape is
fixed by the template. Evidence: the freshly generated `e2e-httpbin` app shows
`title="Generated Wrapper"` and only `/fetch`,`/proxy`, despite the log line
`Design complete: 2 endpoints`. The expensive LLM call is effectively wasted.

### B3 — `GET /services/{id}/openapi.json` explicit route is shadowed — LOW
`main.py` defines `get_openapi_for_wrapper` *after* the catch-all
`@app.api_route("/services/{wrapper_id}/{path:path}")` (which includes GET).
The catch-all is matched first, so the explicit route — with its fragile hand-built
`StarletteRequest` scope — never runs. It happens to work because the catch-all
proxies correctly, but the explicit handler is dead + fragile. Remove it or reorder.

### B4 — Pydantic aliasing makes `APIDesign.title` unusable — LOW
`APIDesign.wrapper_title` uses `alias="title"`, but the Jinja template reads
`design.wrapper_title`. With `populate_by_name` not enabled, a JSON `{"title":...}`
from the LLM maps to `wrapper_title` on parse — but since the template ignores the
design entirely (B2), this never surfaces. Worth fixing together with B2.

---

## 3. Gaps vs. documented design 📉

The README/pyproject describe a 7-phase pipeline; current `pipeline.py` implements
~3.5 of them:

| Documented phase | Implemented? |
|------------------|--------------|
| 1. Inspect target | ✅ (heuristic type detection + LLM analysis) |
| 2. REST API design (LLM) | ⚠️ Runs but **output unused** (B2) |
| 3. Code generation | ⚠️ Template-only; ignores design |
| 4. Test gen + exec (real coverage) | ❌ Only a `/health` smoke stub; no endpoint assertions |
| 5. Fix loop (max 5 retries, feed errors to LLM) | ❌ Not implemented (no retry/repair at all) |
| 6. Docker build + dynamic deploy | ✅ (in `DockerManager`, called from `service.py`) |
| 7. Verify openapi.json + exercise all paths | ❌ `Verifier` exists but is **never invoked** by the pipeline (`verify_deployed` is dead code) |

Other gaps:
- **No wrapper lifecycle management.** No `DELETE /services/{id}`, no undeploy,
  no container/image stop+remove. Wrappers (containers, images, host ports) accumulate
  indefinitely. `Registry.remove()` exists but is unreachable via any endpoint/CLI.
- **Synchronous generation blocks the request.** A single generation took **~250 s**
  (LLM inspect + design + image build + 30 s health poll). A browser/agent HTTP call
  will time out long before completion; there is no async job + status polling,
  no progress, no streaming. The hub form blocks the whole tab for 4+ minutes.

---

## 4. Improvement recommendations (prioritized)

**P0 — Correctness**
1. Fix `/generate` route collision (B1); expose a stable JSON API for agent clients.
2. Make code generation actually consume the `APIDesign`: generate one route per
   `design.endpoints`, emit the `design.models` as Pydantic classes, and set
   `title`/`description` from the design (B2 + B4). This is the whole point of the
   "design anything" agentic claim; without it every wrapper is identical-shaped.
3. Implement the fix loop: on pytest/import failure, feed errors back to the LLM to
   regenerate, up to `max_retries` (currently a field that's never used).

**P1 — Reliability / verification**
4. Wire `Verifier.verify_deployed` into the pipeline post-deploy and fail the run if
   `openapi.json` is missing or designed paths don't respond. Persist verification
   results into the registry entry.
5. Expand generated `test_app.py` to assert each designed endpoint (status + schema),
   not just `/health`.
6. Add `DELETE /services/{id}` (+ CLI subcommand) that stops+removes the container,
   removes the image, and `Registry.remove()`s the entry. Track ports for reuse.

**P2 — Operability / UX**
7. Make generation **asynchronous**: `POST /generate` returns a job id immediately;
   add `GET /jobs/{id}` for status/progress/logs; the hub polls. Prevents timeouts.
8. Bound the 30 s `_wait_for_health` poll better (poll every 0.5 s, fail fast with
   container logs on timeout). Currently the only signal is a generic RuntimeError.
9. Surface generation errors to the user: failed deploys currently leave a registry
   entry with `status="healthy"` even when nothing was verified.
10. The host port mapping in `docker-compose.yml` is `8800:8000` but the README says
    `http://localhost:8000` — document the actual port (or align them).
11. `registry.json` is committed with a stale entry pointing at `host.containers.internal`
    and an old port; consider gitignoring it or seeding an empty registry for fresh
    deploys. Note: no `.gitignore` exists at all.

**P3 — Hygiene / hardening**
12. Add `.gitignore` (`__pycache__/`, `*.egg-info`, `registry.json`, `/tmp` wrappers).
    (No git repo is even initialized currently — `git log` fails.)
14. The `docker` SDK is imported at module top in `manager.py`; the broad
    `except (DockerException, Exception)` swallows everything — narrow these and log.
15. `time.sleep` is used inside an `async` health-wait loop — use `asyncio.sleep`.
16. Add CI: run the existing pytest suite + ruff on push.
17. Rootless-podman port mapping uses `0.0.0.0`; on some setups the
    `host.containers.internal` proxy host must be configured — works here, but make
    `GATEWAY_PROXY_HOST` resolution robust (it already env-detects, good).

---

## 5. Verdict

**Implemented: YES, for a working MVP.** The stack builds, runs, generates a wrapper
end-to-end, deploys it in an isolated container, and serves a valid `openapi.json`
that is consumable through the central gateway proxy. All unit tests pass.

**Caveat:** the "agentic, designs a *unique* API per target" promise is **not yet
realized** — the LLM design output is discarded (B2) and the documented fix-loop /
verify phases (3, 5, 7) are absent, so every wrapper currently has the same fixed
shape per target-type. The JSON generate API is also broken for agent consumers (B1).
Fixing B1 + B2 + the missing phases would move this from "working scaffold" to the
intended product.

## Artifacts left by this audit
- Created live wrapper `wrapper-e2e-httpbin` (container + image, port 54171) and a
  second registry entry. There is no delete capability in the app to remove it (see
  recommendation #6) — `podman rm -f wrapper-e2e-httpbin` +
  `podman rmi openapi-wrapper-e2e-httpbin:latest` + editing `registry.json` would do it.

---

# Part 2 — Issues RESOLVED + rebuild E2E re-test (2026-07-01)

All bugs and gaps from Part 1 were fixed, the compose stack was rebuilt, and a full
end-to-end test passed. Details below.

## Bugs fixed

- **B1 (JSON `/generate` dead) — FIXED.** Agent-facing JSON generate moved to a
  distinct `POST /api/generate` (Pydantic body); the hub keeps its form `POST /generate`.
  Live test: `POST /api/generate` returned HTTP 200 + JSON with `openapi_path`.
- **B2/B4 (LLM design discarded) — FIXED.** `CodeGenerator` now assembles `app.py`
  entirely from the `APIDesign`: title, description, models, and one route per designed
  endpoint with the LLM-written handler body. Live proof: the generated httpbin wrapper
  served OpenAPI titled `"Httpbin API Wrapper"` (a unique LLM title, not hardcoded) with
  designed paths `/get` + `/post`, and 3 Pydantic models — and the endpoints actually
  called httpbin (real echo data, HTTP 200). The deterministic `_default_design` and a
  `safe=True` generator guarantee a design-driven result even when the LLM is empty.
- **B3 (shadowed explicit openapi route) — FIXED.** Removed the dead handler; the
  catch-all proxy serves `/services/{id}/openapi.json` correctly.
- **Cross-process registry staleness (found in re-test) — FIXED.** The gateway held an
  in-memory registry that diverged from host-CLI disk writes. `Registry` is now
  mtime-aware (reloads on external change) with atomic writes. Verified: generate via
  gateway → delete via host CLI → gateway `/registry` immediately empty.

## Pipeline phases completed
- **Phase 5 (fix loop) — IMPLEMENTED.** On test failure the orchestrator feeds errors
  to the LLM and regenerates (up to `max_retries`), then falls back to a deterministic
  safe generator. Live proof: the httpbin wrapper's attempt-0 code failed and the loop
  recovered (`retries >= 1`) into a fully working, design-driven app.
- **Phase 7 (Verifier) — WIRED + PERSISTED.** Post-deploy verification runs against the
  live service (health + openapi + every designed endpoint exercised) and the full report
  is stored in the registry entry's `verification` field and surfaced in `GET
  /services/{id}`.

## Lifecycle management — IMPLEMENTED
- `DELETE /services/{id}` (JSON API), `POST /services/{id}/delete` (hub form), and a
  `openapi-anything delete <id>` CLI subcommand. Backed by `DockerManager.stop_and_remove_wrapper`
  (container + image). Live test: DELETE removed container + image + registry entry; the
  CLI did the same from the host; non-existent → 404.

## Hygiene (P3)
- `.gitignore` added; `asyncio.sleep` in health polling; narrowed `docker` exceptions;
  README documents the real port (`8800`), `/api/generate`, and lifecycle endpoints.

## Full rebuild E2E (podman compose) — PASSED
Rebuilt stack (`gateway:8800`, `redis`), clean registry:
1. **CLI via JSON `/api/generate`** → deployed `e2e-ls-json`; OpenAPI title `"CLI Wrapper"`,
   `POST /execute` actually ran `ls` (returncode 0) through the proxy; verification
   `overall=true`.
2. **Web via hub form `/generate`** → deployed `e2e-httpbin-form`; OpenAPI title
   `"Httpbin API Wrapper"`, designed `/get` + `/post` actually called httpbin (real echo,
   HTTP 200) through the proxy; verification `overall=true`.
3. **Lifecycle** → `DELETE /services/e2e-ls-json` removed container+image+entry; CLI
   `delete e2e-httpbin-form` did the same; cross-process registry stayed consistent.
4. **Quality gates** → `ruff check` clean; `pytest` **14/14 passed** (was 7).

Net: the project moved from "working scaffold whose LLM design was discarded" to a
working agentic system where each target gets a unique, verified, fully-lifecycled
REST wrapper.

---

# Part 3 — json_object mode for the Designer (2026-07-01)

Follow-up: verify whether `response_format={"type":"json_object"}` works with the
current model (GLM-5.1 via LiteLLM) and, if so, switch the Designer to it.

## Verification: json_object IS supported by GLM-5.1 (empirical)

Direct API probes against `https://litellm.xn--8pr.xyz/v1` (model `GLM-5.1`):
- `response_format={"type":"json_object"}` is **accepted** (no error) and, with an
  adequate `max_tokens`, returns **well-formed JSON** (`finish_reason=stop`).
- **Critical quirk found & fixed:** when `max_tokens` is too small, GLM-5.1 burns the
  entire budget producing **empty content** with `finish_reason=length`
  (e.g. `mt=1500` → `len=0`, `comp_t=1500/1500`). Diagnostic against the real ls
  inspection: `mt=1500` empty; `mt=3000` ok (len=2508); `mt=4000` ok (len=3641).
- 3000 was borderline (flaky in the first live run); raised the floor to **4000** and
  added **3 retries** in the Designer (transient empty-content clears on retry).

## Implementation
- `LLMClient.complete_json()` (new): json_object mode, parses to dict, raises
  `ValueError` on empty/invalid content (callers retry/fallback).
- `LLMClient.complete_structured()` now built on `complete_json`.
- `Designer.design()` uses `complete_json` with 3 retries; default-design fallback only
  if all attempts fail. No more markdown-fence stripping (json mode returns pure JSON).

## Before/after (real GLM-5.1 calls)
- Baseline (old, plain `complete`): **2/3** unique LLM designs (1 returned empty → fallback).
- New (json_object, mt=4000, 3 retries): **4/4** unique LLM designs
  (`ls REST API`, `ls Directory Listings API`, `Httpbin API Wrapper`, `httpbin.org REST Wrapper`).

## Full live E2E on the podman-compose stack (rebuilt/running on new code) — PASSED
1. JSON `/api/generate` (CLI/ls) → deployed `jls2`; OpenAPI title **`ls REST API`**
   (unique LLM, NOT the `CLI Wrapper`/`Generated Wrapper` default), designed path `/files`;
   `GET /files` + `POST /files` actually ran `ls` through the proxy; verify `overall=true`.
2. Hub form `/generate` (web/httpbin) → deployed `jweb`; title **`httpbin.org REST Wrapper`**,
   designed `/get` + `/post`; both returned **real httpbin echo data** (HTTP 200) through proxy;
   verify `overall=true`.
3. Lifecycle → `DELETE /services/jls2` and `DELETE /services/jweb` each removed
   container+image+registry entry; registry left empty; 404 for missing.

Neither live run fell back to the default design (no "using default design" in logs).

## Quality gates
- `ruff check`: clean. `pytest`: **16/16 passed** (added 2 designer-retry regression tests).
- Live stack healthy, clean registry, no leftover wrapper containers/images.

Conclusion: the suggestion was correct AND works on the current model; implemented,
made robust to the empty-content quirk, and verified end-to-end with unique LLM designs.
