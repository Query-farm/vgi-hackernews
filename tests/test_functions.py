"""Function bodies driven against a fake Hacker News.

What these pin down is the contract each shape of function owes DuckDB: a
paged scan emits a page per tick and finishes, so a LIMIT can stop it; a
blended function emits once per input batch with provenance, so a LATERAL can
stamp each driving row onto its own output.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from vgi_hackernews.lists import (
    FeedArgs,
    MaxItemFunction,
    StoriesFunction,
    UpdatedItemsFunction,
    UpdatedUsersFunction,
)
from vgi_hackernews.lookups import ItemArgs, ItemFunction, UserArgs, UserFunction
from vgi_hackernews.paging import PAGE_SIZE
from vgi_hackernews.walks import (
    CommentsArgs,
    CommentsFunction,
    RecentArgs,
    RecentItemsFunction,
    SubmissionsArgs,
    SubmissionsFunction,
)

from .conftest import Collector, FakeSite, Params, comment, run_blended, run_scan, story


class TestStories:
    def test_ranks_follow_the_list_order(self, site: FakeSite) -> None:
        site.feeds = {"top": [30, 10, 20]}
        site.items = {i: story(i) for i in (10, 20, 30)}
        rows = run_scan(StoriesFunction, FeedArgs(feed="top")).rows()
        assert [(r["rank"], r["id"]) for r in rows] == [(1, 30), (2, 10), (3, 20)]

    def test_one_page_per_tick(self, site: FakeSite) -> None:
        """Pages are what let a LIMIT stop the walk; one giant batch would not."""
        ids = list(range(1, 251))
        site.feeds = {"new": ids}
        site.items = {i: story(i) for i in ids}
        out = run_scan(StoriesFunction, FeedArgs(feed="new"))
        assert [b.num_rows for b in out.batches] == [PAGE_SIZE, PAGE_SIZE, 50]

    def test_the_first_tick_costs_one_page_not_the_list(self, site: FakeSite) -> None:
        ids = list(range(1, 501))
        site.feeds = {"top": ids}
        site.items = {i: story(i) for i in ids}
        params = Params(args=FeedArgs(feed="top"), output_schema=StoriesFunction.FIXED_SCHEMA)
        out = Collector()
        state = StoriesFunction.initial_state(params)
        StoriesFunction.process(params, state, out)
        assert len(out.rows()) == PAGE_SIZE
        assert len(site.item_requests()) == PAGE_SIZE

    def test_the_list_is_read_once(self, site: FakeSite) -> None:
        """Re-reading per page would let a reshuffle duplicate or drop a story."""
        ids = list(range(1, 251))
        site.feeds = {"top": ids}
        site.items = {i: story(i) for i in ids}
        run_scan(StoriesFunction, FeedArgs(feed="top"))
        assert site.requests.count("/v0/topstories.json") == 1

    def test_a_vanished_item_is_skipped_but_ranks_hold(self, site: FakeSite) -> None:
        site.feeds = {"top": [1, 2, 3]}
        site.items = {1: story(1), 3: story(3)}
        rows = run_scan(StoriesFunction, FeedArgs(feed="top")).rows()
        assert [(r["rank"], r["id"]) for r in rows] == [(1, 1), (3, 3)]

    def test_an_empty_list_finishes_without_emitting(self, site: FakeSite) -> None:
        out = run_scan(StoriesFunction, FeedArgs(feed="job"))
        assert out.finished and out.batches == []

    def test_projection_is_honoured(self, site: FakeSite) -> None:
        site.feeds = {"top": [1]}
        site.items = {1: story(1)}
        schema = pa.schema([StoriesFunction.FIXED_SCHEMA.field("title")])
        out = run_scan(StoriesFunction, FeedArgs(feed="top"), schema)
        assert out.rows() == [{"title": "Story 1"}]


class TestChangeFeed:
    def test_updated_items_are_ranked_in_feed_order(self, site: FakeSite) -> None:
        site.updated_items = [5, 3]
        site.items = {5: story(5), 3: comment(3, parent=5)}
        rows = run_scan(UpdatedItemsFunction, None).rows()
        assert [(r["rank"], r["id"], r["type"]) for r in rows] == [(1, 5, "story"), (2, 3, "comment")]

    def test_updated_users_are_hydrated(self, site: FakeSite) -> None:
        site.updated_users = ["alice", "ghost", "bob"]
        site.users = {
            "alice": {"id": "alice", "created": 1, "karma": 10, "submitted": [1]},
            "bob": {"id": "bob", "created": 2, "karma": 20},
        }
        rows = run_scan(UpdatedUsersFunction, None).rows()
        assert [(r["rank"], r["username"], r["karma"]) for r in rows] == [(1, "alice", 10), (3, "bob", 20)]
        assert rows[1]["submitted"] == []

    def test_max_item(self, site: FakeSite) -> None:
        site.items = {1: story(1), 42: story(42)}
        assert run_scan(MaxItemFunction, None).rows() == [{"id": 42}]


class TestItemLookup:
    def test_provenance_skips_misses_and_nulls(self, site: FakeSite) -> None:
        """Without parent_rows, a LATERAL would stamp row 2's story onto row 0's output."""
        site.items = {10: story(10), 30: story(30)}
        out = run_blended(ItemFunction, ItemArgs(id=0), "id", [10, 20, None, 30])
        assert [r["id"] for r in out.rows()] == [10, 30]
        assert out.parent_rows == [[0, 3]]

    def test_one_batch_per_input_batch(self, site: FakeSite) -> None:
        site.items = {i: story(i) for i in range(1, 301)}
        out = run_blended(ItemFunction, ItemArgs(id=0), "id", list(range(1, 301)))
        assert len(out.batches) == 1 and out.batches[0].num_rows == 300

    def test_no_cache_unless_asked(self, site: FakeSite) -> None:
        site.items = {1: story(1)}
        assert run_blended(ItemFunction, ItemArgs(id=0), "id", [1]).cache_controls == [None]

    def test_cache_ttl_turns_on_per_value_memoization(self, site: FakeSite) -> None:
        site.items = {1: story(1)}
        (control,) = run_blended(ItemFunction, ItemArgs(id=0, cache_ttl=60), "id", [1]).cache_controls
        assert control.ttl == 60 and control.per_value


class TestUserLookup:
    def test_provenance_and_case_sensitivity(self, site: FakeSite) -> None:
        site.users = {"pg": {"id": "pg", "created": 1160418092, "karma": 157316}}
        out = run_blended(UserFunction, UserArgs(username=""), "username", ["PG", "pg", None, "../x"])
        assert [r["username"] for r in out.rows()] == ["pg"]
        assert out.parent_rows == [[1]]
        # Fetched concurrently, so the arrival order is the scheduler's, not ours.
        assert sorted(site.requests) == ["/v0/user/PG.json", "/v0/user/pg.json"]


def _thread(site: FakeSite) -> None:
    """Story 1 with replies 2 and 3; 2 has a reply 4; 4 has a reply 5. Story 10: reply 11."""
    site.items = {
        1: story(1, kids=[2, 3]),
        2: comment(2, parent=1, kids=[4]),
        3: comment(3, parent=1),
        4: comment(4, parent=2, kids=[5]),
        5: comment(5, parent=4),
        10: story(10, kids=[11]),
        11: comment(11, parent=10),
    }


class TestComments:
    def test_every_depth_is_walked_and_the_root_excluded(self, site: FakeSite) -> None:
        _thread(site)
        rows = run_blended(CommentsFunction, CommentsArgs(id=0), "id", [1]).rows()
        assert sorted(r["id"] for r in rows) == [2, 3, 4, 5]
        assert all(r["root_id"] == 1 for r in rows)

    def test_sorting_by_path_is_display_order(self, site: FakeSite) -> None:
        _thread(site)
        rows = run_blended(CommentsFunction, CommentsArgs(id=0), "id", [1]).rows()
        in_order = sorted(rows, key=lambda r: r["path"])
        assert [(r["id"], r["depth"], r["path"]) for r in in_order] == [
            (2, 1, [1]),
            (4, 2, [1, 1]),
            (5, 3, [1, 1, 1]),
            (3, 1, [2]),
        ]

    def test_max_depth_stops_the_descent(self, site: FakeSite) -> None:
        _thread(site)
        rows = run_blended(CommentsFunction, CommentsArgs(id=0, max_depth=1), "id", [1]).rows()
        assert sorted(r["id"] for r in rows) == [2, 3]
        assert set(site.item_requests()) == {1, 2, 3}, "fetched below the depth cap"

    def test_several_roots_keep_their_own_provenance(self, site: FakeSite) -> None:
        """Under LATERAL each story's comments must land on that story's row."""
        _thread(site)
        out = run_blended(CommentsFunction, CommentsArgs(id=0), "id", [10, None, 1])
        by_root = {r["id"]: (r["root_id"], p) for r, p in zip(out.rows(), out.parent_rows[0], strict=True)}
        assert by_root[11] == (10, 0)
        assert {by_root[i] for i in (2, 3, 4, 5)} == {(1, 2)}

    def test_deleted_comments_are_kept_so_the_tree_stays_whole(self, site: FakeSite) -> None:
        _thread(site)
        site.items[2] = {"id": 2, "deleted": True, "parent": 1, "type": "comment", "kids": [4]}
        rows = {r["id"]: r for r in run_blended(CommentsFunction, CommentsArgs(id=0), "id", [1]).rows()}
        assert rows[2]["deleted"] and rows[2]["author"] is None
        assert 4 in rows and 5 in rows

    def test_a_missing_root_yields_nothing(self, site: FakeSite) -> None:
        out = run_blended(CommentsFunction, CommentsArgs(id=0), "id", [999])
        assert out.rows() == [] and out.parent_rows == [[]]


class TestSubmissions:
    def test_posts_in_profile_order_a_page_at_a_time(self, site: FakeSite) -> None:
        submitted = list(range(300, 0, -1))
        site.users = {"pg": {"id": "pg", "submitted": submitted}}
        site.items = {i: story(i) for i in submitted}
        out = run_scan(SubmissionsFunction, SubmissionsArgs(username="pg"))
        assert [b.num_rows for b in out.batches] == [PAGE_SIZE] * 3
        assert [r["id"] for r in out.rows()] == submitted

    @pytest.mark.parametrize("name", ["nobody", "PG", "../x"])
    def test_an_unknown_user_is_empty(self, site: FakeSite, name: str) -> None:
        site.users = {"pg": {"id": "pg", "submitted": [1]}}
        out = run_scan(SubmissionsFunction, SubmissionsArgs(username=name))
        assert out.finished and out.rows() == []


class TestRecentItems:
    def test_walks_back_from_the_newest_id(self, site: FakeSite) -> None:
        site.items = {i: story(i) for i in range(1, 251)}
        rows = run_scan(RecentItemsFunction, RecentArgs(count=150)).rows()
        assert [r["id"] for r in rows] == list(range(250, 100, -1))

    def test_gaps_are_skipped(self, site: FakeSite) -> None:
        site.items = {i: story(i) for i in (10, 8, 5)}
        rows = run_scan(RecentItemsFunction, RecentArgs(count=10)).rows()
        assert [r["id"] for r in rows] == [10, 8, 5]

    def test_the_window_does_not_run_below_one(self, site: FakeSite) -> None:
        site.items = {i: story(i) for i in range(1, 4)}
        run_scan(RecentItemsFunction, RecentArgs(count=100))
        assert min(site.item_requests()) == 1
