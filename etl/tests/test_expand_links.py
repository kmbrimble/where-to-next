"""Tests for etl/expand_links.py — the one-off short-link migration. HTTP mocked."""
from __future__ import annotations

from etl.expand_links import expand_links, find_short_link_candidates, resolve_with_429_backoff
from etl.geocode import ShortLinkResolver


HEADERS = ["Day", "Plan", "Address", "Links", "Notes", "row_type"]


def make_rows(rows: list[list[str]]) -> list[list[str]]:
    return [HEADERS, *rows]


def test_find_short_link_candidates_scans_links_and_notes():
    rows = make_rows([
        ["1", "Stop A", "", "https://maps.app.goo.gl/abc", "", "stop"],
        ["1", "Stop B", "already, here", "", "see https://maps.app.goo.gl/def for pin", "stop"],
        ["1", "Stop C", "", "", "", "stop"],  # no short link — excluded
    ])
    candidates, index = find_short_link_candidates(rows)

    assert len(candidates) == 2
    assert candidates[0]["row_num"] == 2
    assert candidates[0]["url"] == "https://maps.app.goo.gl/abc"
    assert candidates[0]["address"] == ""
    assert candidates[1]["address"] == "already, here"


def test_dry_run_makes_zero_network_calls():
    calls = []
    resolver = ShortLinkResolver(follow=lambda url: calls.append(url))
    rows = make_rows([["1", "Stop A", "", "https://maps.app.goo.gl/abc", "", "stop"]])
    candidates, _ = find_short_link_candidates(rows)

    report = expand_links(candidates, live=False, resolver=resolver)

    assert calls == []
    assert report.rows[0]["status"] == "would resolve"


def test_writes_address_only_when_empty():
    class FakeWorksheet:
        def __init__(self):
            self.updates = []

        def batch_update(self, updates):
            self.updates.extend(updates)

    resolver = ShortLinkResolver(follow=lambda url: "https://www.google.com/maps/@49.6725,-123.1583,15z", delay=0)
    rows = make_rows([["1", "Stop A", "", "https://maps.app.goo.gl/abc", "", "stop"]])
    candidates, index = find_short_link_candidates(rows)
    ws = FakeWorksheet()

    report = expand_links(candidates, live=True, resolver=resolver, worksheet=ws, address_col=index["address"])

    assert report.rows[0]["status"] == "written"
    assert ws.updates == [{"range": "C2", "values": [["49.6725, -123.1583"]]}]


def test_skips_when_address_already_set_without_overwrite_flag():
    class FakeWorksheet:
        def batch_update(self, updates):
            raise AssertionError("must not write when Address is already set")

    resolver = ShortLinkResolver(follow=lambda url: "https://www.google.com/maps/@49.6725,-123.1583,15z", delay=0)
    rows = make_rows([["1", "Stop A", "123 Main St", "https://maps.app.goo.gl/abc", "", "stop"]])
    candidates, index = find_short_link_candidates(rows)

    report = expand_links(candidates, live=True, resolver=resolver, worksheet=FakeWorksheet(), address_col=index["address"])

    assert report.rows[0]["status"] == "skipped"
    assert "already set" in report.rows[0]["detail"]


def test_overwrite_address_flag_respected():
    class FakeWorksheet:
        def __init__(self):
            self.updates = []

        def batch_update(self, updates):
            self.updates.extend(updates)

    resolver = ShortLinkResolver(follow=lambda url: "https://www.google.com/maps/@49.6725,-123.1583,15z", delay=0)
    rows = make_rows([["1", "Stop A", "123 Main St", "https://maps.app.goo.gl/abc", "", "stop"]])
    candidates, index = find_short_link_candidates(rows)
    ws = FakeWorksheet()

    report = expand_links(
        candidates, live=True, resolver=resolver, overwrite_address=True, worksheet=ws, address_col=index["address"],
    )

    assert report.rows[0]["status"] == "written"
    assert len(ws.updates) == 1


def test_429_backoff_then_abort_reports_remaining():
    attempts = []

    def follow(url):
        attempts.append(url)
        import urllib.error
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)

    resolver = ShortLinkResolver(follow=follow, delay=0)
    rows = make_rows([
        ["1", "Stop A", "", "https://maps.app.goo.gl/a", "", "stop"],
        ["1", "Stop B", "", "https://maps.app.goo.gl/b", "", "stop"],
    ])
    candidates, index = find_short_link_candidates(rows)

    report = expand_links(candidates, live=True, resolver=resolver, base_delay=0, address_col=index["address"])

    assert report.rate_limited
    assert report.remaining == 1  # stopped on the first, second never attempted
    assert report.rows[-1]["status"] == "aborted"


def test_resolve_with_429_backoff_retries_then_succeeds():
    calls = {"n": 0}
    import urllib.error

    def follow(url):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
        return "https://www.google.com/maps/@1,2,15z"

    resolver = ShortLinkResolver(follow=follow, delay=0)
    result = resolve_with_429_backoff(resolver, "https://maps.app.goo.gl/x", base_delay=0)

    assert result.ok
    assert calls["n"] == 3
