import io
import urllib.error
from unittest.mock import patch

from behive.engine import authority


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"ok": true}'


class Result:
    def fetchone(self):
        return (0, 0, 0)


class DB:
    def __init__(self):
        self.closed = False

    def execute(self, sql, params=None):
        return Result()

    def close(self):
        self.closed = True


def test_primary_study_routes_to_primary_literature_registries():
    db = DB()
    report = {"registry": "test", "found": 1, "saved": 1}
    with patch.object(authority, "connect", return_value=db), \
         patch.object(authority, "_ensure_schema"), \
         patch.object(authority, "_europe_pmc", return_value=report) as pmc, \
         patch.object(authority, "_pubmed", return_value=report) as pubmed, \
         patch.object(authority, "_crossref", return_value=report) as crossref:
        result = authority.ingest_authority_sources("mission", "TDP-43", 8, "primary_study")

    pmc.assert_called_once()
    pubmed.assert_called_once()
    crossref.assert_not_called()
    assert result["source_family"] == "primary_study"
    assert db.closed


def test_dataset_routes_to_geo_and_citation_index():
    db = DB()
    report = {"registry": "test", "found": 1, "saved": 1}
    with patch.object(authority, "connect", return_value=db), \
         patch.object(authority, "_ensure_schema"), \
         patch.object(authority, "_geo", return_value=report) as geo, \
         patch.object(authority, "_openalex", return_value=report) as openalex, \
         patch.object(authority, "_europe_pmc", return_value=report) as pmc, \
         patch.object(authority, "_clinical_trials", return_value=report) as trials:
        authority.ingest_authority_sources("mission", "TDP-43", 8, "dataset")

    geo.assert_called_once()
    openalex.assert_called_once()
    pmc.assert_called_once()
    trials.assert_not_called()


def test_regulatory_routes_to_trial_and_funding_registries():
    db = DB()
    report = {"registry": "test", "found": 1, "saved": 1}
    with patch.object(authority, "connect", return_value=db), \
         patch.object(authority, "_ensure_schema"), \
         patch.object(authority, "_clinical_trials", return_value=report) as trials, \
         patch.object(authority, "_nih_reporter", return_value=report) as reporter, \
         patch.object(authority, "_crossref", return_value=report) as crossref:
        authority.ingest_authority_sources("mission", "TDP-43", 8, "regulatory")

    trials.assert_called_once()
    reporter.assert_called_once()
    crossref.assert_not_called()


def test_targeted_probe_is_not_expanded_again():
    db = DB()
    report = {"registry": "test", "found": 1, "saved": 1}
    query = "NAD+ homeostasis in ALS motor neurons"
    with patch.object(authority, "connect", return_value=db), \
         patch.object(authority, "_ensure_schema"), \
         patch.object(authority, "_europe_pmc", return_value=report) as pmc, \
         patch.object(authority, "_pubmed", return_value=report) as pubmed:
        result = authority.ingest_authority_sources(
            "mission", query, 8, "primary_study", expand_queries=False
        )

    pmc.assert_called_once_with("mission", query, db, 8)
    pubmed.assert_called_once_with("mission", query, db, 8)
    assert len(result["registries"]) == 2
    assert all(item["purpose"] == "targeted_probe" for item in result["registries"])


def test_http_429_is_retried_with_retry_after():
    error = urllib.error.HTTPError(
        "https://example.test", 429, "rate limited", {"Retry-After": "1"}, io.BytesIO()
    )
    with patch("urllib.request.urlopen", side_effect=[error, Response()]) as urlopen, \
         patch.object(authority.time, "sleep") as sleep:
        result = authority._get("https://example.test")

    assert result == '{"ok": true}'
    assert urlopen.call_count == 2
    sleep.assert_called_once_with(1.0)
