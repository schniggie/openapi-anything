# openapi-anything

Agentic system that, given a natural language request describing "anything" to connect, writes/tests/deploys live code for a REST API wrapper around the target.

## Goal
- Input: Free-form NL description (e.g. "wrap the CLI from this github repo into a REST API", "expose this web-only online service via proper REST API")
- Output: Unique deployed FastAPI service instance serving correct auto-generated `openapi.json` at `/services/{id}/openapi.json`
- Consuming agents fetch the openapi.json and use the REST endpoints to interact with the wrapped target.
- Full web hub + central API gateway proxying to isolated per-wrapper Docker containers.

## Quickstart

```bash
# Install
pip install -e .

# Generate a wrapper (CLI) — runs the full pipeline + deploys
openapi-anything generate "wrap the ls command as a REST API"

# Undeploy a wrapper (stops+removes its container and image)
openapi-anything delete <wrapper_id>

# Run the hub/gateway
openapi-anything serve

# Or with the compose stack (Podman or Docker)
podman compose up --build      # or: docker compose up --build
```

The compose stack exposes the gateway on **http://localhost:8800** (host port `8800`
maps to container port `8000`). Visit the hub UI in your browser.

## API

The gateway serves both an HTML hub and a JSON API for agent clients:

| Method | Path | Body | Purpose |
|--------|------|------|---------|
| `POST` | `/api/generate` | `{"description": "...", "wrapper_id": "...", "secrets": {"ENV": "val"}}` | **JSON** async generate+deploy: returns `202 {"job_id", "poll"}` immediately. `secrets` become wrapper env vars (values never enter generated code/LLM prompts/registry — names only) |
| `GET`  | `/jobs/{job_id}` | – | Poll a generation job (status + live `phase` + result) |
| `GET`  | `/jobs` | – | List generation jobs (newest first) |
| `POST` | `/jobs/{job_id}/cancel` | – | Cancel a queued/running job (409 if finished) |
| `GET`  | `/services/{id}/_logs?tail=100` | – | Wrapper container logs (gateway meta route, not proxied) |
| `GET`  | `/services/{id}/_source` | – | Generated `app.py` of the wrapper |
| `POST` | `/services/{id}/_regenerate` | `{"description": "..."}` (optional) | Re-run the pipeline for an existing wrapper (409 while a job is active); previous verification feeds the new design |
| `POST` | `/mcp` | JSON-RPC | **MCP server** (Streamable HTTP): every wrapper's endpoints as tools + `list_apis`/`generate_api`/`regenerate_api`/`job_status` meta tools |
| `POST` | `/services/{id}/mcp` | JSON-RPC | MCP server scoped to one wrapper (no meta tools) |
| `POST` | `/generate` | form: `description`, `wrapper_id` | Hub form generate — starts a job, hub auto-refreshes (returns HTML) |
| `GET`  | `/` | – | Hub UI |
| `GET`  | `/registry` | – | List deployed wrappers |
| `GET`  | `/metrics` | – | Per-wrapper traffic: requests, errors, avg latency, last used |
| `GET`  | `/services/{id}/openapi.json` | – | Wrapper's OpenAPI spec (proxied) |
| `GET/POST/...` | `/services/{id}/{path}` | – | Reverse-proxy to the wrapper container |
| `DELETE` | `/services/{id}` | – | Undeploy a wrapper (container+image removed) |
| `POST` | `/services/{id}/delete` | – | Hub-friendly undeploy (browsers can't DELETE) |

> Note: `POST /generate` is form-encoded for the browser hub; agents should use the
> JSON **`/api/generate`** endpoint. They intentionally live on distinct paths.
> Generation is **asynchronous**: both endpoints return immediately and the pipeline
> runs in the background — agents poll `GET /jobs/{job_id}` until `status` is
> `completed` (result includes `openapi_path`) or `failed` (includes `error`).

### MCP (Model Context Protocol)

Agents can skip OpenAPI parsing entirely and connect via MCP:

```bash
claude mcp add --transport http openapi-anything http://localhost:8800/mcp
```

Every deployed wrapper's endpoints appear as tools (`{wrapper_id}__{method}_{path}`),
alongside meta tools: `list_apis`, `generate_api` (submit a new wrapper build, get a
`job_id`), and `job_status`. An agent can literally ask for a new API and use its
tools once the job completes. Tool schemas are derived live from each wrapper's
`openapi.json` (cached `MCP_SPEC_TTL` s).

## Architecture

**Podman users**: `podman compose up --build` (or `podman-compose`). The DockerManager
auto-detects the Podman socket; no docker.sock mount needed.

- **Central gateway** (`openapi_anything.gateway`):
  - Proxies `/services/{wrapper_id}/*` → the correct backend container.
  - Provides `POST /api/generate` (JSON) + `POST /generate` (hub form) and the web hub UI.
  - Runs generation **asynchronously** as in-memory jobs (`GET /jobs`, `GET /jobs/{id}`);
    the hub shows a jobs table and auto-refreshes while jobs are active.
  - Manages wrapper lifecycle: `DELETE /services/{id}` undeploys.
- **Generator pipeline** (`openapi_anything.generator`, LLM-driven via LiteLLM/GLM-5.1):
  1. **Inspect** target (git, subprocess, http + LLM analysis), enriched with **web
     research** via a SearxNG instance (`SEARXNG_BASE_URL`, default
     `https://searxng.schnigg.ie`; JSON API required). Results flow into both the
     inspection analysis and the design prompt; search outages degrade gracefully.
  2. **Design** REST API — the LLM produces title, description, endpoints, Pydantic
     models, **and per-endpoint handler bodies**.
  3. **Generate code** — a deterministic assembler turns the design into a complete
     `app.py`. **The whole design is consumed** (title/description/models/endpoints are
     never hardcoded by the generator).
  4. **Test** — generated pytest smoke (health + openapi).
  5. **Fix loop** — on failure, errors are fed back to the LLM and the app is
     regenerated (up to 5 retries), with a deterministic safe fallback that always works.
  6. **Build + deploy** — per-wrapper Docker image + container on a free port.
  7. **Verify** — pre-deploy (TestClient health/openapi) **and** post-deploy (live
     health + openapi + every designed endpoint exercised); the report is persisted
     into the registry entry.
- Tech: Python + FastAPI (auto OpenAPI), Docker SDK, httpx, openai (LiteLLM), pytest, Jinja2.

## Example NL Requests
- "wrap the ls command as a REST API"
- "make a REST wrapper for httpbin.org that exposes /get /post"
- "wrap the todo web app at example.com/todos into CRUD REST endpoints"

Each produces a distinct service with its own openapi.json usable by agents.

## Configuration

All knobs are environment variables (set them in `docker-compose.yml` or a `.env` file;
values are read at runtime, not import time):

| Variable | Default | Purpose |
|----------|---------|---------|
| `LITELLM_BASE_URL` | `https://litellm.xn--8pr.xyz/v1` | OpenAI-compatible LLM proxy |
| `LITELLM_API_KEY` | *(required, no default)* | Proxy API key — set via `.env`, never commit it |
| `LLM_MODEL` | `GLM-5.2` | Model id (must match proxy `/v1/models`) |
| `LLM_REQUEST_TIMEOUT` | `120` | Per-request timeout (s) |
| `LLM_JSON_MAX_TOKENS` | `8000` | JSON-mode budget — reasoning models burn hidden thinking tokens against it |
| `SEARXNG_BASE_URL` | `https://searxng.schnigg.ie` | SearxNG instance for inspector web research (JSON API) |
| `SEARXNG_TIMEOUT` | `10` | Search request timeout (s) |
| `SEARXNG_MAX_RESULTS` | `5` | Research hits fed into inspection/design |
| `PIPELINE_MAX_RETRIES` | `5` | LLM fix-loop attempts before the safe fallback |
| `WRAPPER_OUTPUT_BASE` | `/tmp/openapi-anything-wrappers` | Generated wrapper code directory |
| `PROXY_TIMEOUT` | `30` | Gateway → wrapper proxy timeout (s) |
| `MCP_SPEC_TTL` | `30` | Seconds MCP tool schemas are cached per wrapper |
| `REDIS_URL` | unset (in-memory) | Job persistence backend; compose sets `redis://redis:6379/0`. History survives restarts; interrupted jobs are marked failed |
| `JOBS_HISTORY_MAX` | `200` | Terminal jobs kept in history before pruning oldest |
| `METRICS_FLUSH_INTERVAL` | `30` | Seconds between metric flushes to redis |
| `HEALTH_SWEEP_INTERVAL` | `30` | Seconds between registry health sweeps |
| `HEALTH_PROBE_TIMEOUT` | `2` | Per-wrapper /health probe timeout (s) |
| `GATEWAY_PROXY_HOST` | `127.0.0.1` | Host wrappers are reachable on from the gateway |
| `DOCKER_HOST` | podman socket autodetect | Container runtime socket |

## Development
```bash
python -m pytest tests/ -v
ruff check .
```

(Full pipeline implementation per plan phases.)
