<p align="center">
  <a href="https://query.farm/vgi/">
    <img src="https://raw.githubusercontent.com/Query-farm/vgi-hackernews/main/docs/vgi-logo.png" alt="Vector Gateway Interface logo" width="320">
  </a>
</p>

<h1 align="center">vgi-hackernews</h1>

<p align="center">
  <a href="https://news.ycombinator.com">Hacker News</a> as ordinary DuckDB tables — the front page and<br>
  every other ranking, any story or comment by id, whole discussion threads, and user profiles.<br>
  A <strong>read-only</strong> <a href="https://query.farm/vgi/">VGI</a> worker, built by <a href="https://query.farm">🚜 Query.Farm</a>
</p>

<p align="center">
  <a href="https://github.com/Query-farm/vgi-hackernews/actions/workflows/ci.yml"><img src="https://github.com/Query-farm/vgi-hackernews/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.13%2B-blue.svg" alt="Python 3.13+">
  <a href="https://query.farm/vgi/"><img src="https://img.shields.io/badge/VGI-Vector%20Gateway%20Interface-2f7d32.svg" alt="VGI"></a>
</p>

---

> **No credentials required.** The [Hacker News API](https://github.com/HackerNews/API)
> is public, unauthenticated and read-only. Every request this worker makes goes
> through one `GET` chokepoint in `hn_api.py` — the only module that imports the
> HTTP client — and `tests/test_readonly_guard.py` fails the build if that stops
> being true. No value from SQL can change where a request goes: item ids are
> integers, and a username that could not be a Hacker News username is answered
> locally without a request.

```sql
ATTACH 'hackernews' (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-hackernews vgi-hackernews');

-- The front page, right now
SELECT rank, title, score, descendants AS comments
FROM (SELECT * FROM hackernews.top_stories LIMIT 30) ORDER BY rank;

-- The whole discussion under today's #1 story, in the order the site shows it
SELECT depth, author, hackernews.html_to_text(text) AS comment
FROM hackernews.comments((SELECT id FROM hackernews.top_stories WHERE rank = 1))
WHERE NOT deleted ORDER BY path;
```

**Using this from an LLM or agent?** [`llms.txt`](llms.txt) is a self-contained
guide in the [llmstxt.org](https://llmstxt.org) format: every table, function
and column, the rules that keep answers correct, and recipes that the live test
suite executes verbatim.

## Run

Nothing to clone or install: `uvx` fetches the worker from GitHub, builds it
once, and runs it from its cache. All it needs is
[uv](https://docs.astral.sh/uv/). DuckDB spawns the worker itself, so the
`ATTACH` is the whole setup:

```sql
FORCE INSTALL vgi FROM community;
LOAD vgi;
ATTACH 'hackernews' (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-hackernews vgi-hackernews');
```

Run it from [haybarn](https://pypi.org/project/haybarn/), Query Farm's DuckDB
distribution, whose community channel carries the `vgi` extension build that
speaks the current VGI protocol.

An unpinned `LOCATION` tracks `main`: each launch asks GitHub for the current
commit and rebuilds only when it has moved. Pin a release tag for a
deployment, so the worker cannot change under you:

```sql
ATTACH 'hackernews' (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-hackernews@v0.1.1 vgi-hackernews');
```

### As an HTTP server

The same no-clone install serves the HTTP transport, for one worker shared by
several clients or run on another machine:

```bash
uvx --from git+https://github.com/Query-farm/vgi-hackernews vgi-hackernews-http --port 8000
```

```sql
ATTACH 'hackernews' (TYPE vgi, LOCATION 'http://localhost:8000');
```

Pass `--port` explicitly: without it a free port is picked at random. The
server listens on `127.0.0.1` unless started with `--host 0.0.0.0`, and answers
`/health` for probes.

### Container image

`ghcr.io/query-farm/vgi-hackernews` serves both transports, for `linux/amd64`
and `linux/arm64`. It runs as an unprivileged user, needs no credentials, and
only needs outbound HTTPS to `hacker-news.firebaseio.com`.

```bash
docker run -p 8000:8000 ghcr.io/query-farm/vgi-hackernews   # HTTP on :8000, /health for probes
```

```sql
ATTACH 'hackernews' (TYPE vgi, LOCATION 'http://localhost:8000');

-- Or let DuckDB spawn the container itself over stdio: nothing to install but Docker
ATTACH 'hackernews' (TYPE vgi, LOCATION 'docker run -i --rm ghcr.io/query-farm/vgi-hackernews stdio');
```

A release tag publishes `X.Y.Z`, `X.Y` and `latest`; every push to `main`
publishes `edge`. Images are built and published by
`.github/workflows/docker-publish.yml` through the fleet's shared workflow,
which boots each architecture and checks `/health` before anything is pushed.

### From a clone

Inside a checkout, two scripts run the worker straight from the working tree,
which is how to try a local change. Each carries a PEP 723 header pinning its
dependencies, so nothing needs installing first:

```bash
uv run hackernews_worker.py        # stdio: what DuckDB spawns
uv run serve.py --port 8000        # HTTP
```

```sql
ATTACH 'hackernews' (TYPE vgi, LOCATION 'uv run hackernews_worker.py');
```

That `LOCATION` resolves the script against DuckDB's working directory, so it
only works when DuckDB is started inside the clone.

### Developing

```bash
uv sync                  # dependencies, including haybarn for the live tier
uv run pytest            # offline suite: no network
uv run pytest -m live    # SQL through a real ATTACH, against the live API
uv run ruff check .      # lint
```

## Surface

Names are bare — they are already qualified by the `hackernews` catalog.

### Tables

| Table | Rows |
|---|---|
| `top_stories` | The front page ranking, up to 500 deep (job ads included) |
| `new_stories` | The 500 newest submissions — roughly the last ten hours |
| `best_stories` | The highest-voted recent stories, typically 200 |
| `ask_stories` | Ask HN questions, as the Ask page ranks them |
| `show_stories` | Show HN launches, as the Show page ranks them |
| `job_stories` | Job ads from YC companies on the jobs page |
| `updated_items` | Items the change feed lists as just changed, hydrated |
| `updated_users` | Profiles the change feed lists as just changed, hydrated |
| `max_item` | One row: the newest item id, and so the count of everything posted |

The six rankings share one shape — a `rank` column followed by the full item —
and all scan one function, `stories(feed)`, with the list name bound as the
table's argument. The other three tables share their names with the functions
that scan them (`max_item` and `max_item()`): DuckDB keeps tables and table
functions in separate namespaces, so no `all_` prefix is needed to tell them
apart.

### Functions

| Function | Shape | Named arguments |
|---|---|---|
| `item(id)` | blended | `cache_ttl` |
| `user(username)` | blended | `cache_ttl` |
| `comments(id)` | blended | `max_depth` |
| `stories(feed)` | paged scan | — |
| `submissions(username)` | paged scan | — |
| `recent_items(count)` | paged scan | — |
| `html_to_text(html)` | scalar | — |

**Blended** functions (`RowTransformFunction`) take their key as a literal, a
scalar subquery, or a correlated column under `LATERAL`, and fetch every key in
an input batch concurrently. That is how the API's relationships — which are
bare ids — become joins:

```sql
-- How a 2008 poll's votes split: follow its `parts` to each option
SELECT o.text AS option, o.score AS votes
FROM (SELECT unnest(parts) AS part FROM hackernews.item(126809)) p,
     LATERAL hackernews.item(p.part) o
ORDER BY votes DESC;

-- The karma of everyone on the first page
SELECT s.rank, s.author, u.karma
FROM (SELECT rank, author FROM hackernews.top_stories WHERE rank <= 30) s,
     LATERAL hackernews.user(s.author, cache_ttl => 300) u
ORDER BY s.rank;
```

**Paged scans** emit one page of 100 rows per tick, so a `LIMIT` stops the walk
and cancellation lands between pages. The price is that DuckDB does not let a
subquery or a column reach an ordinary table function's argument — `Table
function cannot contain subqueries` — so their argument must be a literal.

Which shape a function gets follows from whether its work is bounded. A thread
is: the largest run to a few thousand comments. A posting history is not
(`pg` alone has over 15,000 posts), and neither is the item sequence
(nearly 50 million). So `comments()` is blended and walks its whole tree before
returning, while `submissions()` and `recent_items()` stream.

### Items

Every story, comment, job, poll and poll option is an *item*, and every
item-returning object uses the same columns: `id`, `type`, `author`,
`created_at`, `title`, `url`, `text`, `score`, `descendants`, `parent`, `poll`,
`kids`, `parts`, `deleted`, `dead`. Three are renamed from the API:

| API field | Column | Why |
|---|---|---|
| `by` | `author` | `BY` is a DuckDB keyword and would need quoting in every query |
| `time` | `created_at` | The value changes type — Unix seconds become `TIMESTAMP WITH TIME ZONE` |
| user `id` | `username` | An item's `id` is a number and a user's is a string |

And absence is given the meaning the API documents: `deleted` and `dead` are
sent only when true, so a missing flag is `false`, never NULL — otherwise
`WHERE NOT dead` would match nothing. A missing `kids` is an empty list.

## Using it well

**`comments()` returns the whole tree; `path` puts it in order.** `path` is the
comment's 1-based position among its siblings at each level, so `ORDER BY path`
reproduces the site's nesting exactly. Deleted and dead comments are kept,
flagged, because replies hang beneath them. A story's `descendants` counts only
the live ones, so it equals `count(*)` here once both flags are filtered out.
`max_depth => 1` fetches only the direct replies, which is the cheap way to read
the reactions to many stories at once:

```sql
SELECT s.rank, s.title, count(c.id) AS replies
FROM (SELECT rank, id, title FROM hackernews.top_stories WHERE rank <= 10) s,
     LATERAL hackernews.comments(s.id, max_depth => 1) c
GROUP BY ALL ORDER BY s.rank;
```

**Decode `text` before searching it.** Comment bodies, text posts and profile
`about` fields are HTML with every special character entity-escaped: an
apostrophe is stored as `&#x27;` and a slash as `&#x2F;`, so a plain-text
pattern silently misses. `html_to_text()` decodes entities, turns `<p>` into
blank lines, and replaces each link with its full address (the site truncates
long URLs in the visible link text, but not in the link). Titles are already
plain text.

**Each row is a request.** The API has no search, no filtering and no batch
endpoint: a ranking returns bare ids, and each id costs one more request.
Requests run 32 at a time over pooled connections — 500 items in a second or two
— and paged scans emit 100 rows per batch, in list order.

**How `LIMIT` saves requests.** A `LIMIT` is never sent to the worker: DuckDB
has no way to pass one to a table function. It works anyway, because DuckDB
stops pulling pages once the limit is met and a paged scan only fetches when
pulled — so `LIMIT 10` costs one page, and so does `WHERE type = 'story' LIMIT
10`. What cannot stop early is anything that must see every row first: a
`WHERE` with no `LIMIT` (`WHERE rank <= 30` fetches all 500 stories, since the
predicate is not sent either), an aggregate, or `ORDER BY ... LIMIT`. Rows
arrive in list order, so bound the scan in a subquery and sort outside it:

```sql
-- One page of requests, where ORDER BY rank LIMIT 30 costs five
SELECT rank, title FROM (SELECT * FROM hackernews.top_stories LIMIT 30) ORDER BY rank;
```

The walks have their own bounds, because a `LIMIT` cannot shorten work done
inside a single batch: `recent_items(count)` takes how far back to go, and
`comments(id, max_depth => n)` how deep to descend.

**Rankings are snapshots.** A list's ids are read once when its scan starts and
frozen for the rest of it, so each `rank` appears exactly once even while the
site reshuffles. The items are fetched just after, so a `score` can be a few
seconds newer than the rank it sits beside.

**Caching is opt-in.** Hacker News marks every response `Cache-Control:
no-cache`, and nothing here second-guesses that. `item()` and `user()` take
`cache_ttl => N`, which caches each result for N seconds and turns on per-value
memoization — worth it when a `LATERAL` repeats a key, such as the same author
across a thread.

## Design notes

**No filter pushdown.** The API has no query parameters to translate a
predicate into, so there is nothing for pushdown to save; and a worker that
accepts pushdown must apply every required predicate exactly, which in the
current `vgi-python` means evaluating it in an embedded DuckDB pinned to one
exact version. DuckDB applies every `WHERE` itself instead. Projection pushdown
is on everywhere, so only the columns a query uses are built.

**Everything that can fail is total.** One odd field on one item must not fail
the batch of a hundred items it arrived in, so every conversion turns an
unusable value into NULL rather than raising: a non-integer id in `kids` is
dropped, an out-of-range timestamp is NULL. Transient HTTP failures —
throttling, 5xx, dropped connections — are retried with backoff, because a
`LATERAL` fan-out issues thousands of requests. A request that still fails is a
failed query, not a silently missing row.

**Why `recent_items()` has a ceiling.** `count` accepts 1 to 50,000. The site
takes in tens of thousands of items a day, and at roughly 300 a second a walk
of the full bound takes a few minutes — the most one call should spend without
the caller asking twice. The window is fixed when the call starts, so items
posted during a long walk are not included.

## Quality gates

The catalog is linted with
[vgi-lint](https://github.com/Query-farm/vgi-lint-check), every rule enabled
(`vgi-lint.toml`):

```bash
vgi-lint lint --audit-waivers
```

It passes with no findings, at assurance level L2 — every example query and
executable example run against the live worker, every scan probed for prompt
first rows. One waiver is declared and audited: `max_item` is singular among
plural tables because it holds one value and mirrors the API's `maxitem`.

The agent-suitability suite asks an analyst 17 questions that between them
touch every table and function. The prompts are published in the catalog; the
graders — reference SQL and success criteria — live in `vgi-agent-tests.yaml`,
out of the agent's sight. `vgi-lint simulate --verify-references` confirms
every reference is stable across repeated runs.

| Tier | Command | Network |
|---|---|---|
| Offline tests | `uv run pytest` | none — a fake Hacker News in `tests/conftest.py` |
| Structural lint | `vgi-lint lint --no-execute` | none |
| Live tests | `uv run pytest -m live` | SQL through a real ATTACH |
| Behavioural lint | `vgi-lint lint` | executes every example |

## License

Copyright 2026 Query Farm LLC - https://query.farm

MIT — see [LICENSE](LICENSE). The worker is Query Farm's; the stories,
comments and profiles it returns belong to their authors and are published by
Y Combinator's Hacker News. See [NOTICE](NOTICE).
