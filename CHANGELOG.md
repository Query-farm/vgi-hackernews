# Changelog

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

### Validation

- vgi-lint with every rule enabled: no findings, assurance level L2
  (structural and behavioural), one audited waiver.
- A 17-task agent suite touching every table and function, with its graders in
  `vgi-agent-tests.yaml`; every reference verified stable.
- An offline suite against a fake Hacker News, and a live tier of SQL through a
  real ATTACH.
