from unittest.mock import patch

from behive.engine import authority


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
