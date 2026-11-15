# Hacker News API examples

Three executed SQL-first notebooks use Haybarn and the public **v0.3.0** worker.
Each starts with an Open in Colab link, release ATTACH and editable parameters.

| Notebook | Question | Experiment |
| --- | --- | --- |
| [Rank and score](01_rank_is_not_score_order.ipynb) | Does observed rank agree with descending score? Do the table and parameterized feed agree on recorded inputs? | TOP_N: 30 → 100 |
| [Thread depth](02_depth_changes_the_thread_you_count.ipynb) | How do direct replies, deeper replies and missing bodies change the count? | MAX_DEPTH: 1 → 2 |
| [Recent IDs](03_an_id_window_is_not_a_time_window.ipynb) | What kinds of items occupy an ID window, and how much time does it span? | ID_WINDOW: 25 → 100 |

## Run

Install [uv](https://docs.astral.sh/uv/) and Git, then:

```sh
cd notebooks
uv sync --locked
uv run --locked vgi-notebooks --timeout 300
uv run --locked vgi-notebooks 02_depth_changes_the_thread_you_count.ipynb \
  --parameters '{"MAX_DEPTH":2}' --output-dir artifacts/deeper
```

The local environment pins the shared runner. Colab installs only the base
[shared package](https://github.com/Query-farm/vgi-example-notebooks) at v0.3.41,
retaining compatible preloaded plotting/kernel libraries. Its bootstrap retrieves
four immutable, hash-verified companions without cloning or a submodule. GitHub does
not fold executable setup; connection SQL stays visible. Charts are SVG, including
verified GitHub assets. PNGs are ignored review artifacts.

The short `uvx --python 3.13 --from git+https://github.com/Query-farm/vgi-hackernews@v0.3.0 vgi-hackernews`
launcher uses the release's own dependencies separately from the notebook kernel.
Release revision: `831a83838db94f91cb3e064b6ea900ef753c10bb`. Initial setup needs
network access; no local worker checkout or credential is required.

## Source and request bounds

Defaults replay complete, unmodified anonymous responses from the
[official Hacker News API](https://github.com/HackerNews/API), not synthetic data.
Content belongs to its authors; see the root NOTICE. Outputs use identifiers,
timestamps, counts and lengths rather than reproducing comment bodies.

- `ranking-pack.json.gz` preserves every byte/hash/timestamp from the original
  manifest and 501 response files, which are retained. One top-list response and
  500 items were acquired on 2026-11-14. The table/function comparison consumes
  both complete bounded sources: 1,002 replay requests.
- `thread-pack.json.gz` contains root 8863, its author's current public profile and
  51 replies through depth 2, acquired on 2026-11-15 UTC. Depth 1 uses 36 requests:
  two root reads, one profile and 33 replies. Depth 2 adds 18 requests. This is
  neither the entire thread nor a historical profile snapshot.
- `recent-pack.json.gz` contains one maximum-ID response and its 100 item responses,
  acquired on 2026-11-15 UTC. Two maximum reads plus 100 items produce 102 requests.
  The candidate grid is checked against the recorded anchor. The notebook explicitly
  explains the uncertainty if a live newest item is unavailable.

Each compressed JSON contains exact response bytes in Base64, URLs, statuses,
individual UTC timestamps and SHA-256 hashes. `hackernews_example.py` verifies them
before serving only recorded paths on an ephemeral loopback port. Unknown requests
fail; there is no forwarding or fallback. Cleanup restores `HN_BASE_URL`. Retrieval
and hydration are not atomic. Visible SQL materializes API calls before analysis.

Set `DATA_MODE="live"` explicitly for a new public acquisition; all three live paths
were exercised. Results can refresh between reads. SQL LIMIT does not guarantee a
matching request bound because the worker hydrates batches; these examples use
finite feed/depth/ID boundaries instead.

## Interpretation and validation

The saved 30-rank prefix has 29 eligible stories: 124 of 404 unequal-score pairs
(30.7%) disagree with descending score. Widening to 100 ranks yields 99 eligible
stories and 2,231 of 4,821 comparable pairs (46.3%). These are different selected
populations, not estimates of ranking logic, story quality or voting velocity.

Depth 1 returns 33 replies including one absent body; depth 2 returns 51. The root's
71 live descendants are a different population. Decoding changes 18 of 32 non-null
body lengths at depth 1; absent text remains NULL. No sentiment is inferred.

The 25-ID prefix contains 24 comments and one story over 4.58 minutes; the 100-ID
prefix contains 92 comments and eight stories over 16.15 minutes. Count divided by
span is not a general arrival-rate estimate.

The full catalog has 16 logical `(schema,name)` surfaces: ten functions and nine
tables, with three same-name aliases counted once. Eight are demonstrated, meeting
the fleet threshold; remaining feeds, changes and submission history stay explicit
in [coverage.json](coverage.json). Current source only adds metadata examples and
an equivalent schema-path representation. Evidence is exact release execution and
current-interface compatibility, not execution of current-main worker code.

Defaults, all three parameter experiments, all three live runs, and a notebook-only
Python 3.13 kernel with five libraries preloaded passed. Those library versions
were preserved and `pip check` remained clean. This is local Colab-compatible setup
verification; the hosted Colab UI was not manually operated. Existing green source
CI was reused without rebuilding the worker.

The shared [standard](https://github.com/Query-farm/vgi-example-notebooks/blob/main/docs/standard.md)
and [coverage format](https://github.com/Query-farm/vgi-example-notebooks/blob/main/docs/coverage.md)
live in the shared repository.
