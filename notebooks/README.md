# Hacker News notebooks

[01 · Front-page rank and score describe different orderings](01_rank_is_not_score_order.ipynb) materializes a bounded ranking sample, preserves eligibility and missing-value diagnostics, and compares rank order with score order. The experiment widens a local prefix from 30 to 100 ranks.

## Run

Install [uv](https://docs.astral.sh/uv/) and Git. Python 3.13–3.14 is required; uv can install it. Internet is needed for initial dependencies and the worker download. No local worker checkout or credentials are required.

```sh
cd notebooks
uv sync --locked
uv run --locked python execute.py --timeout 300
```

Haybarn 2.0.0rc11 runs the notebook. The visible literal ATTACH uses `uvx --from git+https://github.com/Query-farm/vgi-hackernews@v0.3.0 ... vgi-hackernews`; the tag resolves to `831a83838db94f91cb3e064b6ea900ef753c10bb`. Runtime pins are vgi-python 0.51.0, vgi-rpc 0.53.1, httpx2 2.13.0 and pyarrow 25.0.1. uv fetches and caches the Git package; this does not assume a PyPI package. Upgrade the pin deliberately and rerun defaults, experiment and live validation.

Optional editor kernel:

```sh
uv run --locked python -m ipykernel install --user --name vgi-hackernews-notebooks --display-name "Python (vgi-hackernews notebooks)"
```

Use this directory as working directory and Restart Kernel → Run All. The runner creates a temporary kernel and replaces saved outputs only after every cell succeeds.

## Experiment and live mode

```sh
uv run --locked python execute.py --parameters '{"TOP_N":100}' --output-dir artifacts/top-100
uv run --locked python execute.py --parameters '{"DATA_MODE":"live"}' --output-dir artifacts/live
```

Restore `TOP_N=30`, `DATA_MODE="replay"` for defaults. Overrides require a separate output destination. Recorded responses, helper, branding, docs and lock accompany the executed copy. Generated artifacts, logs and virtual environments are ignored.

## Recorded source and request bounds

The [official Hacker News API](https://github.com/HackerNews/API) provides a ranked list of IDs and separate item payloads. `fixtures/manifest.json` records exact URLs, UTC request start/completion, HTTP status, body sizes and SHA-256 hashes. `topstories.json` is the complete unmodified 500-ID response, and the 500 `item-*.json` bodies are complete unmodified responses for all its IDs. They were captured anonymously on 2026-11-14 with eight concurrent item requests after the ranking request. The first 100 and remaining 400 bodies were acquired in separate batches; every individual timestamp is retained, so this is explicitly not an atomic snapshot. These are actual public responses, not synthetic data. Content belongs to its authors and is published by Hacker News; see the repository NOTICE. The analysis outputs identifiers and aggregate values rather than reproducing story text.

`replay_source.py` verifies all hashes and serves only these paths on an ephemeral loopback port. Unknown paths fail; there is no network forwarding or fallback. The documented `HN_BASE_URL` worker setting points at replay in default mode and explicitly at the official Firebase API in live mode. The real worker hydrates items, preserves ranks, normalizes fields and emits Arrow rows. Cleanup restores the previous environment setting.

The worker hydrates batches of 100 IDs. SQL materializes the whole top-list source (the API bounds it at 500 IDs) before selecting rank prefixes locally. Replay checks exactly 501 requests and all expected paths: one ranking and 500 items. A LIMIT is a returned-row limit, not a network-request bound: the tested client began additional hydration even after LIMIT 100 had returned its 100 rows. This notebook avoids that ambiguity by consuming the complete bounded source. No replay request may escape the recording, and no fallback is allowed. Rank is frozen from one ID list; item fields are hydrated afterwards. Missing live items can leave gaps in returned ranks, which remain visible.

The age reference is acquisition completion, while each item timestamp is its UTC creation time. No score-change or vote-time series is fetched. Replay experiments retain the same source and clock; live results may change.

## Analytical population

The selected prefix uses the API's 1-based ranks. Only non-deleted, non-dead stories with known nonnegative scores enter score comparisons. Jobs and unavailable scores are reported separately; zero scores remain valid. Missing/future creation times leave age unavailable without removing an otherwise comparable score.

Every unordered pair is counted once using `a.rank < b.rank`. A disagreement has `a.score < b.score`: the earlier-ranked story has fewer points. Equal-score pairs are reported and excluded from the disagreement fraction. The denominator is the selected eligible population, not all Hacker News stories. The highest-score table is also restricted to that population. The scatter uses a symmetric log score scale with a linear region from 0 to 10; the bar chart uses a linear score axis.

The result does not infer the ranking algorithm, story quality, moderation decisions, author performance, causality or vote velocity. Widening the prefix changes the population rather than holding other ranking factors constant.
