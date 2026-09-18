"""The HTTP layer: ordering, deduplication, retries, and what a miss looks like."""

from __future__ import annotations

import httpx2
import pytest

from vgi_hackernews import hn_api as api

from .conftest import FakeSite, story


class TestFetchItems:
    def test_results_come_back_in_the_order_asked(self, site: FakeSite) -> None:
        """A ranking fetched concurrently must still be a ranking."""
        ids = list(range(1, 81))
        site.items = {i: story(i) for i in ids}
        shuffled = ids[::-1]
        assert [i["id"] for i in api.fetch_items(shuffled)] == shuffled

    def test_a_missing_item_is_none_in_place(self, site: FakeSite) -> None:
        """The API answers a missing item with 200 and `null`, not a 404."""
        site.items = {1: story(1), 3: story(3)}
        found = api.fetch_items([1, 2, 3])
        assert [f and f["id"] for f in found] == [1, None, 3]

    def test_duplicates_are_fetched_once(self, site: FakeSite) -> None:
        """A LATERAL can repeat an id; one request serves every repeat."""
        site.items = {7: story(7)}
        found = api.fetch_items([7, 7, 7])
        assert [f["id"] for f in found] == [7, 7, 7]
        assert site.item_requests() == [7]

    def test_non_positive_ids_make_no_request(self, site: FakeSite) -> None:
        assert api.fetch_items([0, -5]) == [None, None]
        assert site.requests == []

    def test_nothing_asked_makes_no_request(self, site: FakeSite) -> None:
        assert api.fetch_items([]) == []
        assert site.requests == []


class TestLists:
    def test_feed_reads_the_named_endpoint(self, site: FakeSite) -> None:
        site.feeds = {"ask": [5, 4, 3]}
        assert api.feed("ask") == [5, 4, 3]
        assert site.requests == ["/v0/askstories.json"]

    def test_an_unknown_feed_is_refused_before_any_request(self, site: FakeSite) -> None:
        with pytest.raises(ValueError, match="unknown feed"):
            api.feed("../item/1")
        assert site.requests == []

    def test_non_integer_ids_are_dropped(self, site: FakeSite) -> None:
        site.feeds = {"top": [1, "2", None, 3.5, True, 4]}
        assert api.feed("top") == [1, 4]

    def test_updates_split_items_from_profiles(self, site: FakeSite) -> None:
        site.updated_items = [10, 11]
        site.updated_users = ["pg", "dang"]
        assert api.updates() == ([10, 11], ["pg", "dang"])

    def test_repeats_are_dropped_keeping_first_position(self, site: FakeSite) -> None:
        """The tables built on these lists declare primary keys the optimizer may trust."""
        site.feeds = {"top": [3, 1, 3, 2, 1]}
        site.updated_items = [9, 9, 8]
        site.updated_users = ["a", "b", "a"]
        assert api.feed("top") == [3, 1, 2]
        assert api.updates() == ([9, 8], ["a", "b"])

    def test_max_item(self, site: FakeSite) -> None:
        site.items = {1: story(1), 99: story(99)}
        assert api.max_item() == 99


class TestUsers:
    @pytest.mark.parametrize("name", ["pg", "dang", "some_user", "a-b", "X9"])
    def test_real_usernames_are_accepted(self, name: str) -> None:
        assert api.is_username(name)

    @pytest.mark.parametrize("name", ["", "../item/1", "a/b", "a b", "a.json", "a?x=1", "a#b", "é"])
    def test_anything_else_is_not_a_username(self, name: str) -> None:
        assert not api.is_username(name)

    def test_an_impossible_name_is_answered_locally(self, site: FakeSite) -> None:
        """No request is made, so no value from SQL can reshape the path."""
        assert api.user("../../maxitem") is None
        assert site.requests == []

    def test_case_is_preserved(self, site: FakeSite) -> None:
        site.users = {"pg": {"id": "pg", "created": 1, "karma": 2}}
        assert api.user("pg") is not None
        assert api.user("PG") is None


class TestErrorsAndRetries:
    def _client(self, responses: list[httpx2.Response | Exception]) -> tuple[httpx2.Client, list[int]]:
        calls: list[int] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            calls.append(1)
            next_response = responses.pop(0)
            if isinstance(next_response, Exception):
                raise next_response
            return next_response

        return httpx2.Client(transport=httpx2.MockTransport(handler)), calls

    @pytest.fixture(autouse=True)
    def _no_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(api, "_RETRY_BASE_SECONDS", 0.0)

    def test_a_transient_status_is_retried(self) -> None:
        client, calls = self._client([httpx2.Response(503), httpx2.Response(200, json=42)])
        assert api.max_item(client=client) == 42
        assert len(calls) == 2

    def test_a_dropped_connection_is_retried(self) -> None:
        client, calls = self._client([httpx2.ConnectError("reset"), httpx2.Response(200, json=42)])
        assert api.max_item(client=client) == 42
        assert len(calls) == 2

    def test_retries_give_up_with_the_status(self) -> None:
        client, calls = self._client([httpx2.Response(429)] * 5)
        with pytest.raises(api.HackerNewsError) as err:
            api.max_item(client=client)
        assert err.value.status == 429
        assert len(calls) == 5

    def test_a_client_error_is_not_retried(self) -> None:
        client, calls = self._client([httpx2.Response(400, json={"error": "Invalid path"})])
        with pytest.raises(api.HackerNewsError, match="400"):
            api.max_item(client=client)
        assert len(calls) == 1

    def test_a_non_json_success_names_the_path(self) -> None:
        """An error page served as 200 must not surface as a bare JSONDecodeError."""
        page = httpx2.Response(200, text="<html>maintenance</html>", headers={"content-type": "text/html"})
        client, _ = self._client([page])
        with pytest.raises(api.HackerNewsError, match=r"/maxitem\.json.*text/html"):
            api.max_item(client=client)
