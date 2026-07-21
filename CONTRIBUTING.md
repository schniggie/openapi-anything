# Contributing

- TDD: write a failing test before implementation code (see any file under `tests/`
  for the project's testing patterns — httpx `MockTransport` for HTTP, injected
  fake clients for redis/docker).
- `python -m pytest tests/ -v` and `ruff check .` must pass before opening a PR.
  Both run in CI (`.github/workflows/ci.yml`) on push and PR.
- Keep secrets out of code and docs — `LITELLM_API_KEY` and any target credentials
  belong in environment variables / `.env`, never in source or commit messages.
- See the `## Configuration` table in `README.md` for all environment variables.
