"""VGI worker exposing the Hacker News API to DuckDB/SQL (read-only).

    ATTACH 'hackernews' (TYPE vgi, LOCATION 'uv run hackernews_worker.py');
    SELECT rank, title, score FROM hackernews.top_stories ORDER BY rank LIMIT 30;
    SELECT author, text FROM hackernews.comments(8863) ORDER BY path;

No credentials are required: the Hacker News API is public, read-only, and
unauthenticated. Function names are bare (``item``, not ``hn_item``) because
they are already qualified by the catalog they live in.
"""

from __future__ import annotations

import json
import sys

import pyarrow as pa
from vgi import Worker
from vgi.arguments import Arguments
from vgi.catalog import Catalog, ReadOnlyCatalogInterface, Schema
from vgi.catalog.catalog_interface import CatalogInfo
from vgi.catalog.descriptors import Table

from vgi_hackernews import __version__
from vgi_hackernews.lists import (
    LIST_FUNCTIONS,
    MaxItemFunction,
    StoriesFunction,
    UpdatedItemsFunction,
    UpdatedUsersFunction,
)
from vgi_hackernews.lookups import LOOKUP_FUNCTIONS
from vgi_hackernews.meta import column_comments, docs, examples, keywords
from vgi_hackernews.schemas import FEED_SCHEMA, MAX_ITEM_SCHEMA, UPDATED_ITEMS_SCHEMA, UPDATED_USERS_SCHEMA
from vgi_hackernews.walks import WALK_FUNCTIONS

IMPLEMENTATION_VERSION = __version__
DATA_VERSION_SPEC = f"=={__version__}"
SOURCE_URL = "https://github.com/Query-farm/vgi-hackernews"

_FUNCTIONS = [*LIST_FUNCTIONS, *LOOKUP_FUNCTIONS, *WALK_FUNCTIONS]

#: Classifying tags every table and the schema carry, for faceted search.
_CLASSIFIERS = {"provider": "hacker-news", "domain": "tech-news"}

# --------------------------------------------------------------------------
# Catalog-level examples and the agent suite
# --------------------------------------------------------------------------

#: Examples the linter runs against a live worker, so every one must be true
#: whenever it runs. They lean on items that can no longer change — the 2007
#: Dropbox launch post, a 2008 poll, the site's first user — and assert only
#: shapes about the live lists, never their contents.
_EXECUTABLE_EXAMPLES = json.dumps(
    [
        {
            "name": "an_old_item_is_stable",
            "description": "Item 8863 is Dropbox's 2007 launch post; its title and author never change.",
            "sql": "SELECT title, author, type FROM hackernews.main.item(8863)",
            "expected_result": [["My YC app: Dropbox - Throw away your USB drive", "dhouston", "story"]],
        },
        {
            "name": "an_unassigned_id_is_no_row",
            "description": "An id with no item behind it returns nothing rather than an error.",
            "sql": "SELECT count(*) FROM hackernews.main.item(999999999999)",
            "expected_result": [[0]],
        },
        {
            "name": "unix_time_becomes_a_timestamp",
            "description": "created_at is a real instant: pg's account dates from 1160418092 in Unix time.",
            "sql": "SELECT username, CAST(epoch(created_at) AS BIGINT) FROM hackernews.main.user('pg')",
            "expected_result": [["pg", 1160418092]],
        },
        {
            "name": "a_poll_lists_its_options",
            "description": "A poll carries the ids of its options in parts.",
            "sql": "SELECT type, len(parts) FROM hackernews.main.item(126809)",
            "expected_result": [["poll", 3]],
        },
        {
            "name": "a_thread_is_rooted_and_nested",
            "description": "Every comment in a thread names its root, and the walk starts at depth 1.",
            "sql": (
                "SELECT bool_and(root_id = 8863), min(depth), bool_and(len(path) = depth) "
                "FROM hackernews.main.comments(8863)"
            ),
            "expected_result": [[True, 1, True]],
        },
        {
            "name": "a_depth_cap_stops_the_walk",
            "description": "max_depth => 1 returns exactly the story's direct replies.",
            "sql": (
                "SELECT (SELECT count(*) FROM hackernews.main.comments(8863, max_depth => 1)) "
                "= (SELECT len(kids) FROM hackernews.main.item(8863))"
            ),
            "expected_result": [[True]],
        },
        {
            "name": "a_limit_stops_a_ranking_early",
            "description": "A ranking streams in pages, so a LIMIT yields its rows without reading the rest.",
            "sql": "SELECT count(*) FROM (SELECT id FROM hackernews.main.top_stories LIMIT 30)",
            "expected_result": [[30]],
        },
        {
            "name": "ranks_are_dense_from_one",
            "description": "A list's ranks run 1..n with no gaps or repeats.",
            "sql": (
                "SELECT min(rank) = 1 AND max(rank) = count(*) AND count(DISTINCT rank) = count(*) "
                "FROM hackernews.main.job_stories"
            ),
            "expected_result": [[True]],
        },
        {
            "name": "html_is_decoded",
            "description": "html_to_text decodes entities and drops markup.",
            "sql": "SELECT hackernews.main.html_to_text('Don&#x27;t <i>panic</i> &amp; carry on')",
            "expected_result": [["Don't panic & carry on"]],
        },
        {
            "name": "the_site_has_tens_of_millions_of_items",
            "description": "max_item is the newest id, and ids are assigned in sequence.",
            "sql": "SELECT id > 40000000 FROM hackernews.main.max_item",
            "expected_result": [[True]],
        },
    ]
)

#: The agent-suitability suite, public half: what an analyst is asked. The
#: grader half (reference SQL, criteria) lives in vgi-agent-tests.yaml, because
#: anything published here is visible to the agent being graded.
_AGENT_TEST_TASKS = json.dumps(
    [
        {
            "name": "front_page",
            "prompt": (
                "What are the titles of the five stories at the top of the Hacker News front page right "
                "now, in order?"
            ),
        },
        {
            "name": "newest_submissions",
            "prompt": (
                "Which three stories were submitted to Hacker News most recently? Give their titles and "
                "authors."
            ),
        },
        {
            "name": "on_both_lists",
            "prompt": (
                "How many stories are on the Hacker News front page and on its 'best' list at the same time?"
            ),
        },
        {
            "name": "best_right_now",
            "prompt": "Which story on Hacker News' 'best' list has the highest score at the moment?",
        },
        {
            "name": "busiest_ask_hn",
            "prompt": "Of the Ask HN questions currently listed, which one has drawn the most comments?",
        },
        {
            "name": "top_show_hn",
            "prompt": "What is the highest-scoring Show HN post currently listed on Hacker News?",
        },
        {
            "name": "who_is_hiring",
            "prompt": "Which job ads are listed on Hacker News right now? Give their titles.",
        },
        {
            "name": "dropbox_launch",
            "prompt": (
                "Hacker News item 8863 is Dropbox's launch post. What was its title, and who posted it?"
            ),
        },
        {
            "name": "poll_results",
            "prompt": (
                "Hacker News item 126809 is a poll. How many votes did each of its options get, most first?"
            ),
        },
        {
            "name": "thread_regular",
            "prompt": "Who wrote the most comments in the discussion under Hacker News item 8863?",
        },
        {
            "name": "comment_search",
            "prompt": (
                'In the discussion under Hacker News item 8863, how many comments contain the word "don\'t"?'
            ),
        },
        {
            "name": "account_age",
            "prompt": "In what year did the Hacker News user pg create their account?",
        },
        {
            "name": "recent_posting_mix",
            "prompt": (
                "Of the 100 most recent posts by the Hacker News user pg, how many are stories rather than "
                "comments?"
            ),
        },
        {
            "name": "site_activity",
            "prompt": (
                "Among the last 300 items posted anywhere on Hacker News, are there more comments than "
                "stories?"
            ),
        },
        {
            "name": "what_is_changing",
            "prompt": (
                "Which kinds of item (story, comment, and so on) appear in Hacker News' feed of recently "
                "changed items right now?"
            ),
        },
        {
            "name": "active_accounts",
            "prompt": (
                "Among the Hacker News user profiles that changed most recently, does anyone have more than "
                "1,000 karma?"
            ),
        },
        {
            "name": "site_size",
            "prompt": (
                "Roughly how many items have ever been posted to Hacker News, in whole millions rounded down?"
            ),
        },
    ]
)

_CATEGORIES = json.dumps(
    [
        {
            "name": "rankings",
            "title": "Story Rankings",
            "description": (
                "The ranked lists behind the site's pages: front page, newest, best, Ask HN, Show HN, jobs."
            ),
            "keywords": ["front page", "top stories", "ranking", "ask hn", "show hn", "jobs"],
        },
        {
            "name": "items",
            "title": "Items & Threads",
            "description": (
                "Single stories and comments by id, whole discussion trees, and the newest posts site-wide."
            ),
            "keywords": ["item", "story", "comment", "thread", "discussion", "poll"],
        },
        {
            "name": "users",
            "title": "Users",
            "description": "Public profiles, karma and the history of what each person has posted.",
            "keywords": ["user", "profile", "karma", "author", "submissions"],
        },
        {
            "name": "changes",
            "title": "Live Changes",
            "description": "What the site's change feed reports as having moved in the last few moments.",
            "keywords": ["updates", "changes", "activity", "live"],
        },
    ]
)

_CATALOG_TAGS = {
    **_CLASSIFIERS,
    "vgi.title": "Hacker News — Stories, Threads & Users",
    "vgi.source_url": SOURCE_URL,
    "vgi.author": "Query Farm LLC <hello@query.farm>",
    "vgi.copyright": (
        "Worker (c) 2026 Query Farm LLC - https://query.farm. Stories, comments and profiles "
        "(c) their authors, published by Y Combinator's Hacker News."
    ),
    "vgi.license": "MIT",
    "vgi.support_contact": "https://github.com/Query-farm/vgi-hackernews/issues",
    "vgi.support_policy_url": "https://github.com/Query-farm/vgi-hackernews/blob/main/README.md",
    "vgi.keywords": keywords(
        "hacker news",
        "hn",
        "ycombinator",
        "tech news",
        "front page",
        "comments",
        "discussion",
        "karma",
    ),
    "vgi.executable_examples": _EXECUTABLE_EXAMPLES,
    "vgi.agent_test_tasks": _AGENT_TEST_TASKS,
    "vgi.doc_llm": (
        "Live data from Hacker News, Y Combinator's technology news site: what is on the front "
        "page and the other ranked lists right now, every story and comment by id, whole "
        "discussion threads, and public user profiles with karma. Reach for it to answer what "
        "the tech community is reading and saying, how a story was received, or what a person "
        "has posted. Start from a ranking table such as `top_stories` to find a story, then "
        "follow its id into the thread. Read-only and public: no credentials, no posting."
    ),
    "vgi.doc_md": (
        "[Hacker News](https://news.ycombinator.com) is a link-sharing and discussion site run by "
        "Y Combinator. Members submit stories — links, Ask HN questions, Show HN launches, job "
        "ads — and discuss them in threaded comments; votes decide what reaches the front page.\n\n"
        "### How the data is shaped\n\n"
        "Everything posted is an *item* with a numeric id: stories, comments, polls, poll options "
        "and jobs share one id sequence and one set of columns. Items point at each other by id — "
        "a comment names its `parent`, a story lists its replies in `kids` — so reading a "
        "discussion means following ids, which the thread function does for you.\n\n"
        "### Freshness and cost\n\n"
        "This is the live site through its official API, not an archive: every query reads the "
        "current state, and nothing is cached unless you ask. The API has no search and no "
        "batch endpoint, so each item costs one request; requests run 32 at a time and rows "
        "stream in pages, so a `LIMIT` stops early. A whole ranking reads in a second or two, "
        "a large thread in a few seconds.\n\n"
        "### Access\n\n"
        "The API is public and unauthenticated, and this worker only reads. Content belongs to "
        "its authors."
    ),
}

_SCHEMA_EXAMPLES = examples(
    (
        "The front page right now, with each story's discussion size",
        "SELECT rank, title, score, descendants AS comments "
        "FROM hackernews.main.top_stories ORDER BY rank LIMIT 30",
    ),
    (
        "Which sites dominate the front page",
        "SELECT regexp_extract(url, '^https?://(?:www\\.)?([^/]+)', 1) AS site, count(*) AS stories "
        "FROM hackernews.main.top_stories WHERE url IS NOT NULL "
        "GROUP BY site ORDER BY stories DESC, site LIMIT 15",
    ),
    (
        "Read the whole discussion of the current top story, as displayed",
        "SELECT depth, author, hackernews.main.html_to_text(text) AS comment "
        "FROM hackernews.main.comments((SELECT id FROM hackernews.main.top_stories WHERE rank = 1)) "
        "WHERE NOT deleted ORDER BY path",
    ),
    (
        "The karma of each front-page poster",
        "SELECT s.rank, s.author, u.karma FROM "
        "(SELECT rank, author FROM hackernews.main.top_stories WHERE rank <= 30) s, "
        "LATERAL hackernews.main.user(s.author, cache_ttl => 300) u ORDER BY s.rank",
    ),
)

_SCHEMA_TAGS = {
    **_CLASSIFIERS,
    "vgi.title": "Hacker News Live Data",
    "vgi.categories": _CATEGORIES,
    "vgi.keywords": keywords("hacker news", "stories", "comments", "users", "rankings", "threads"),
    "vgi.example_queries": _SCHEMA_EXAMPLES,
    "vgi.doc_llm": (
        "The whole read-only Hacker News API in one schema. The ranking tables need no "
        "arguments and are the way in: each row is a story with its id, author and score. From "
        "there an item id opens the discussion under it or any single item, and an author opens "
        "that person's profile and history. Item, user and thread lookups accept a subquery or "
        "a correlated LATERAL column; a user's full history and the newest items stream in pages "
        "and take a literal argument."
    ),
    "vgi.doc_md": (
        "One schema holding everything the Hacker News API offers.\n\n"
        "### Finding your way in\n\n"
        "Start from a ranking: they are ordinary tables, one per page of the site, each row a "
        "story in rank order. A story's `id` leads into its discussion, and its `author` to the "
        "poster's profile.\n\n"
        "### Two shapes of function\n\n"
        "Lookups by key — one item, one user, one comment thread — are *blended*: pass a "
        "literal or a scalar subquery, or join them to a column with `LATERAL`, and every key in "
        "the batch is fetched concurrently. That is how to follow a list of ids such as a "
        "story's `kids` or a poll's `parts`, and how to read the thread under a story another "
        "query found.\n\n"
        "Walks with no useful bound — a user's whole posting history, the newest items on the "
        "site — are *streaming scans*: they emit a page at a time so a `LIMIT` stops them. "
        "DuckDB does not allow a subquery or a column as an ordinary table function's argument, "
        "so these take a literal.\n\n"
        "### HTML and time\n\n"
        "Comment bodies and profile text are HTML with every special character escaped, so an "
        "apostrophe is stored as `&#x27;`. Decode with `html_to_text()` before searching them. "
        "Times arrive from the API as Unix seconds and are returned as UTC instants."
    ),
}

# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------


def _feed_table(
    *,
    name: str,
    feed: str,
    comment: str,
    title: str,
    llm: str,
    md: str,
    example: tuple[str, str],
    extra_keywords: tuple[str, ...],
    cardinality_estimate: int,
    cardinality_max: int,
) -> Table:
    """One ranking table: ``stories(feed)`` with the list name bound as its argument."""
    return Table(
        name=name,
        function=StoriesFunction,
        arguments=Arguments(positional=(pa.scalar(feed),)),
        comment=comment,
        tags=docs(
            category="rankings",
            llm=llm,
            md=md,
            example_queries=examples(example),
            extra={
                **_CLASSIFIERS,
                "vgi.title": title,
                "vgi.keywords": keywords("hacker news", "stories", "ranking", *extra_keywords),
            },
        ),
        column_comments=column_comments(FEED_SCHEMA),
        primary_key=(("id",),),
        unique=(("rank",),),
        not_null=("rank", "id"),
        cardinality_estimate=cardinality_estimate,
        cardinality_max=cardinality_max,
    )


_RANK_NOTE = (
    "Rows stream in rank order, 100 per batch and one request per story, so a `LIMIT` "
    "reads only what it returns; an `ORDER BY` reads the whole list first."
)

_FEED_TABLES = [
    _feed_table(
        name="top_stories",
        feed="top",
        comment=(
            "The Hacker News front page ranking: up to 500 stories and job ads, in the order the site shows "
            "them"
        ),
        title="Front Page",
        llm=(
            "What is on the Hacker News front page right now, in ranked order — the place to start "
            "for 'what is the tech world talking about today'. Rank 1 is the top slot and ranks "
            "1-30 are the site's first page. Job ads are ranked here too; filter `type = 'story'` "
            "to drop them. Each `id` opens the story's discussion via `comments()`."
        ),
        md=(
            "The ranking behind the site's home page, up to 500 deep.\n\n"
            "### What decides the order\n\n"
            "Hacker News ranks by votes, decayed by age, and adjusted by moderators and flags — "
            "so a lower-scored new story can sit above a higher-scored old one. `rank` is the "
            "site's order; `score` is only one input to it.\n\n"
            "### Cost\n\n" + _RANK_NOTE
        ),
        example=(
            "The first page of Hacker News, stories only",
            "SELECT rank, title, score, descendants AS comments FROM hackernews.main.top_stories "
            "WHERE type = 'story' ORDER BY rank LIMIT 30",
        ),
        extra_keywords=("front page", "top", "homepage"),
        cardinality_estimate=500,
        cardinality_max=500,
    ),
    _feed_table(
        name="new_stories",
        feed="new",
        comment="The 500 most recently submitted Hacker News stories, newest first",
        title="Newest Submissions",
        llm=(
            "The latest submissions to Hacker News, newest first, before voting has sorted them — "
            "what the site's 'new' page shows. Use it to catch a story the moment it is posted, or "
            "to see what is being submitted rather than what is popular. Most rows have a score of "
            "one or two; that is the point of the list."
        ),
        md=(
            "Up to 500 of the most recent submissions, in the order the 'new' page lists them.\n\n"
            "### Turnover\n\n"
            "New stories arrive every minute or so and the list holds roughly the last ten "
            "hours of them, so two reads a few minutes apart overlap only partly. Poll it, keyed "
            "on `id`, to follow everything submitted.\n\n"
            "### Cost\n\n" + _RANK_NOTE
        ),
        example=(
            "The newest submissions that are already drawing votes",
            "SELECT title, author, score, created_at FROM hackernews.main.new_stories "
            "WHERE score > 1 ORDER BY created_at DESC LIMIT 20",
        ),
        extra_keywords=("new", "newest", "latest", "submissions"),
        cardinality_estimate=500,
        cardinality_max=500,
    ),
    _feed_table(
        name="best_stories",
        feed="best",
        comment="Hacker News' 'best' list: the highest-voted recent stories",
        title="Highest-Voted Recent Stories",
        llm=(
            "The highest-voted stories of the last few days, as the site's 'best' page ranks them. "
            "Reach for it for 'what did people like most this week', where the front page answers "
            "'what is hot this hour'. Scores here run far higher than on the front page."
        ),
        md=(
            "Hacker News' 'best' ranking — highest-voted recent links — typically 200 stories "
            "deep.\n\n"
            "### Versus the front page\n\n"
            "The front page decays by age hour to hour; this list holds on to the strongest stories "
            "for days. Join the two on `id` to see which of today's front-page stories are also "
            "among the week's best.\n\n"
            "### Cost\n\n" + _RANK_NOTE
        ),
        example=(
            "The ten highest-scoring stories of the last few days",
            "SELECT title, score, descendants AS comments FROM hackernews.main.best_stories "
            "ORDER BY score DESC LIMIT 10",
        ),
        extra_keywords=("best", "highest voted", "popular"),
        cardinality_estimate=200,
        cardinality_max=500,
    ),
    _feed_table(
        name="ask_stories",
        feed="ask",
        comment="Ask HN questions currently listed, as the Ask HN page ranks them",
        title="Ask HN",
        llm=(
            "The Ask HN questions the site's 'ask' page lists right now: members asking the "
            "community for advice, opinions or experiences. These are text posts — the question "
            "is in `text` (HTML) and `url` is NULL — and the answers are the discussion, which "
            "`comments()` returns."
        ),
        md=(
            "Up to 200 Ask HN posts, in the ranking of the site's 'ask' page; usually far fewer.\n\n"
            "### Reading a question\n\n"
            "An Ask HN post has no link: its body is in `text`, as HTML. Decode it with "
            "`html_to_text()`. Its answers are the thread under its `id`.\n\n"
            "### Cost\n\n" + _RANK_NOTE
        ),
        example=(
            "The Ask HN questions drawing the most answers",
            "SELECT title, descendants AS answers, hackernews.main.html_to_text(text) AS question "
            "FROM hackernews.main.ask_stories ORDER BY answers DESC LIMIT 10",
        ),
        extra_keywords=("ask hn", "questions", "advice"),
        cardinality_estimate=50,
        cardinality_max=200,
    ),
    _feed_table(
        name="show_stories",
        feed="show",
        comment="Show HN posts currently listed: things members have built, as the Show HN page ranks them",
        title="Show HN",
        llm=(
            "The Show HN posts the site's 'show' page lists right now — projects, products and "
            "demos members have built and want feedback on. The launch link is in `url` and any "
            "write-up in `text`; the feedback is the discussion under each `id`."
        ),
        md=(
            "Up to 200 Show HN posts, in the ranking of the site's 'show' page.\n\n"
            "### What counts\n\n"
            "Show HN is for something the poster made that others can try, so this is a live "
            "sample of what people are launching. Titles start with 'Show HN:'.\n\n"
            "### Cost\n\n" + _RANK_NOTE
        ),
        example=(
            "The Show HN launches getting the strongest response",
            "SELECT title, url, score, descendants AS comments FROM hackernews.main.show_stories "
            "ORDER BY score DESC LIMIT 10",
        ),
        extra_keywords=("show hn", "launches", "projects"),
        cardinality_estimate=150,
        cardinality_max=200,
    ),
    _feed_table(
        name="job_stories",
        feed="job",
        comment="Job ads from Y Combinator companies currently on the Hacker News jobs page",
        title="Jobs",
        llm=(
            "The job ads currently on the site's jobs page, posted by Y Combinator companies. "
            "Each is a job item: the role is in `title`, the details in `url` or `text`. Jobs "
            "cannot be voted on or discussed, so `score` carries no signal and `descendants` is "
            "NULL. Monthly 'Who is hiring?' threads are ordinary stories, not these."
        ),
        md=(
            "Up to 200 job ads, as the jobs page ranks them; usually a few dozen.\n\n"
            "### Not the hiring threads\n\n"
            "The monthly 'Ask HN: Who is hiring?' threads, where any company can post, are "
            "stories with the ads in their comments. This list is only the ads YC companies place "
            "directly.\n\n"
            "### Cost\n\n" + _RANK_NOTE
        ),
        example=(
            "Current job ads, newest first",
            "SELECT title, url, created_at FROM hackernews.main.job_stories ORDER BY created_at DESC",
        ),
        extra_keywords=("jobs", "hiring", "careers", "ycombinator"),
        cardinality_estimate=30,
        cardinality_max=200,
    ),
]

_UPDATED_ITEMS_TABLE = Table(
    name="updated_items",
    function=UpdatedItemsFunction,
    comment="Items Hacker News reports as just changed — new replies, votes, edits — in their current state",
    tags=docs(
        category="changes",
        llm=(
            "The stories and comments Hacker News' change feed lists at this moment, each in its "
            "current state. Reach for it to see what is active right now — threads gaining "
            "replies, stories gaining votes — as opposed to what is ranked. The feed is a short "
            "rolling window of a few dozen items, so it answers 'right now', not 'today'."
        ),
        md=(
            "The item half of the API's change feed, hydrated.\n\n"
            "### What a change is\n\n"
            "The feed says only that an item changed, not how: a reply adds to `kids`, a vote "
            "moves `score`, an edit rewrites `text`. To see motion, read twice and compare on "
            "`id`.\n\n"
            "### Size\n\n"
            "A few dozen rows, so one page of requests."
        ),
        example_queries=examples(
            (
                "Which stories are gaining activity right now",
                "SELECT title, score, descendants AS comments FROM hackernews.main.updated_items "
                "WHERE type = 'story' ORDER BY score DESC",
            ),
        ),
        extra={
            **_CLASSIFIERS,
            "vgi.title": "Recently Changed Items",
            "vgi.keywords": keywords("updates", "changes", "activity", "live", "items"),
        },
    ),
    column_comments=column_comments(UPDATED_ITEMS_SCHEMA),
    primary_key=(("id",),),
    unique=(("rank",),),
    not_null=("rank", "id"),
    cardinality_estimate=60,
)

_UPDATED_USERS_TABLE = Table(
    name="updated_users",
    function=UpdatedUsersFunction,
    comment="User profiles Hacker News reports as just changed, usually because their karma moved",
    tags=docs(
        category="changes",
        llm=(
            "The user profiles Hacker News' change feed lists at this moment, each in its current "
            "state. A profile changes whenever its karma does, so this is a sample of whose posts "
            "are being voted on right now. A few dozen rows; for a specific person use `user()`."
        ),
        md=(
            "The profile half of the API's change feed, hydrated.\n\n"
            "### A sample, not a directory\n\n"
            "The API cannot list users; this feed and item authors are the only ways to discover "
            "usernames. It lists a few dozen recently changed profiles, turning over within "
            "minutes.\n\n"
            "### Payload\n\n"
            "Each row carries the user's whole `submitted` list, which for a long-standing account "
            "is thousands of ids."
        ),
        example_queries=examples(
            (
                "Recently active users, by karma",
                "SELECT username, karma, created_at, len(submitted) AS posts "
                "FROM hackernews.main.updated_users ORDER BY karma DESC",
            ),
        ),
        extra={
            **_CLASSIFIERS,
            "vgi.title": "Recently Changed Users",
            "vgi.keywords": keywords("updates", "users", "profiles", "karma", "active"),
        },
    ),
    column_comments=column_comments(UPDATED_USERS_SCHEMA),
    primary_key=(("username",),),
    unique=(("rank",),),
    not_null=("rank", "username"),
    cardinality_estimate=20,
)

_MAX_ITEM_TABLE = Table(
    name="max_item",
    function=MaxItemFunction,
    comment="The id of the newest item on Hacker News, as a single row",
    tags=docs(
        category="items",
        llm=(
            "One row holding the newest item id on Hacker News. Because every story and comment "
            "takes the next id in sequence, it doubles as a running count of everything ever "
            "posted, and the difference between two readings is how much was posted in between."
        ),
        md=(
            "A single row, a single column.\n\n"
            "### Uses\n\n"
            "Ids are assigned in one sequence across all item types, so this is both the newest "
            "item and the size of the site. `recent_items()` walks back from it, which is usually "
            "the easier way to read what it points at."
        ),
        example_queries=examples(
            (
                "The newest item on the site, fetched in full",
                "SELECT i.type, i.author, i.created_at FROM hackernews.main.max_item m, "
                "LATERAL hackernews.main.item(m.id) i",
            ),
        ),
        extra={
            **_CLASSIFIERS,
            "vgi.title": "Newest Item Id",
            "vgi.keywords": keywords("max item", "newest", "item count", "latest id"),
        },
    ),
    column_comments=column_comments(MAX_ITEM_SCHEMA),
    primary_key=(("id",),),
    not_null=("id",),
    cardinality_estimate=1,
    cardinality_max=1,
)

_HACKERNEWS_CATALOG = Catalog(
    name="hackernews",
    default_schema="main",
    comment=(
        "Hacker News front page, stories, comment threads and user profiles, read live from the official API"
    ),
    tags=_CATALOG_TAGS,
    source_url=SOURCE_URL,
    schemas=[
        Schema(
            path=("main",),
            comment="Live, read-only Hacker News data from the public Firebase API — no credentials required",
            tags=_SCHEMA_TAGS,
            functions=list(_FUNCTIONS),
            tables=[*_FEED_TABLES, _UPDATED_ITEMS_TABLE, _UPDATED_USERS_TABLE, _MAX_ITEM_TABLE],
        ),
    ],
)


class HackerNewsCatalog(ReadOnlyCatalogInterface):
    """Advertises the single read-only catalog with its versions and source."""

    catalog = _HACKERNEWS_CATALOG
    catalog_name = _HACKERNEWS_CATALOG.name

    def catalogs(self) -> list[CatalogInfo]:
        """Advertise the catalog with its implementation version and data version spec."""
        return [
            CatalogInfo(
                name=self._effective_catalog_name,
                implementation_version=IMPLEMENTATION_VERSION,
                data_version_spec=DATA_VERSION_SPEC,
                source_url=SOURCE_URL,
                attach_option_specs=[spec.serialize() for spec in self.attach_option_specs],
            )
        ]


class HackerNewsWorker(Worker):
    """Worker process hosting the read-only Hacker News catalog."""

    catalog = _HACKERNEWS_CATALOG
    catalog_interface = HackerNewsCatalog


def main() -> None:
    """Run the worker (stdio by default; pass ``--http`` for the HTTP server)."""
    HackerNewsWorker.main()


def main_http() -> None:
    """Run the worker over HTTP."""
    argv = sys.argv[1:]
    if "--http" not in argv:
        argv = ["--http", *argv]
    sys.argv = [sys.argv[0], *argv]
    HackerNewsWorker.main()
