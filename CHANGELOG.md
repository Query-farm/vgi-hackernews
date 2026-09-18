# Changelog

## 0.1.2

Documentation only; the worker is unchanged from 0.1.1.

- The README leads with the no-clone install:
  `uvx --from git+https://github.com/Query-farm/vgi-hackernews vgi-hackernews`
  as the `ATTACH` location, needing only uv. An unpinned location tracks
  `main`; a release tag pins it.
- The HTTP server gets the same no-clone form, with the explicit `--port` it
  needs (the entry point's default of 0 picks a random free port).
- Running from a checkout moves to its own "From a clone" section, as the way
  to try local changes.

## 0.1.1

- A container image, `ghcr.io/query-farm/vgi-hackernews`, serving both the HTTP
  and stdio transports on `linux/amd64` and `linux/arm64`, published by the
  fleet's shared `docker-publish` workflow after the full CI suite passes. It
  runs as an unprivileged user with its own writable home, where vgi keeps its
  state store.
- Requires vgi-python 0.34.1. Earlier releases created that state store on
  `import vgi`, so a worker whose home was not writable died before it could
  start; that is how the image first failed, and it is fixed upstream rather
  than only worked around here.
- `ci/check-version.sh`, which refuses a release tag that does not match
  `vgi_hackernews.__version__`.

## 0.1.0

First release.

### Surface

- Six ranking tables — `top_stories`, `new_stories`, `best_stories`,
  `ask_stories`, `show_stories`, `job_stories` — each a `rank` plus the full
  item, all scanning `stories(feed)`.
- The change feed as `updated_items` and `updated_users`, hydrated, and the
  newest item id as `max_item`. Each shares its name with the function behind
  it.
- Blended lookups that take a literal, a scalar subquery or a `LATERAL` column:
  `item(id)`, `user(username)`, and `comments(id)`, which walks a whole reply
  tree and returns it with a `path` that sorts into display order.
- Paged scans that stop at a `LIMIT`: `submissions(username)` and
  `recent_items(count)`.
- `html_to_text()`, because comment bodies are entity-escaped HTML and a plain
  text search on them silently misses.

### For agents

- `llms.txt`, a self-contained usage guide in the
  [llmstxt.org](https://llmstxt.org) format: every table, function and column,
  the rules that keep answers correct, and recipes. `tests/test_llms_txt.py`
  fails if a table, function, column or feed name goes missing from it, and
  every recipe runs in the live suite.

### Validation

- vgi-lint with every rule enabled: no findings, assurance level L2
  (structural and behavioural), one audited waiver.
- A 17-task agent suite touching every table and function, with its graders in
  `vgi-agent-tests.yaml`; every reference verified stable.
- An offline suite against a fake Hacker News, and a live tier of SQL through a
  real ATTACH.
