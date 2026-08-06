"""Authority-first scholarly discovery and evidence ingestion."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from behive.engine.db import connect

log = logging.getLogger(__name__)
AGENT = "BeHive-Research-OS/1.0 (verified scholarly discovery)"


def _get(url: str, accept: str = "application/json", timeout: int = 20) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": accept})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _plain(value: str | None) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _xml_text(value: str) -> str:
    try:
        return re.sub(r"\s+", " ", " ".join(ET.fromstring(value).itertext())).strip()
    except ET.ParseError:
        return _plain(value)


def _ensure_schema(db) -> None:
    for statement in (
        "ALTER TABLE hive_sources ADD COLUMN IF NOT EXISTS evidence_tier INTEGER",
        "ALTER TABLE hive_sources ADD COLUMN IF NOT EXISTS verification_status VARCHAR",
        "ALTER TABLE hive_sources ADD COLUMN IF NOT EXISTS persistent_id VARCHAR",
        "ALTER TABLE hive_sources ADD COLUMN IF NOT EXISTS provenance JSONB DEFAULT '{}'::jsonb",
        "ALTER TABLE hive_sources ADD COLUMN IF NOT EXISTS is_open_access BOOLEAN",
        "ALTER TABLE hive_sources ADD COLUMN IF NOT EXISTS is_primary BOOLEAN",
    ):
        db.execute(statement)


def _save(db, mission_id: str, *, url: str, title: str, abstract: str, source_type: str,
          method: str, persistent_id: str, tier: int, authority: int,
          open_access: bool | None, primary: bool | None, provenance: dict,
          content: str = "") -> bool:
    domain = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    existed = db.execute("SELECT 1 FROM hive_sources WHERE mission_id=? AND url=?", [mission_id, url]).fetchone()
    db.execute(
        """INSERT INTO hive_sources
           (mission_id,url,domain,title,snippet,source_type,harvest_method,score_total,
            score_relevance,score_freshness,score_authority,score_depth,status,evidence_tier,
            verification_status,persistent_id,provenance,is_open_access,is_primary)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?::jsonb,?,?) ON CONFLICT DO NOTHING""",
        [mission_id, url, domain, title[:512], abstract[:1024], source_type, method,
         min(100, 76 + authority), 25, 8, authority, 15 if content else 8, "scouted", tier,
         "registry_verified", persistent_id, json.dumps(provenance), open_access, primary],
    )
    words = len(content.split())
    if words >= 80:
        db.execute(
            """INSERT INTO hive_content
               (mission_id,url,domain,title,raw_text,word_count,harvest_method,language,
                quality_score,author,published_date,keywords,harvested_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NOW()) ON CONFLICT DO NOTHING""",
            [mission_id, url, domain, title[:512], content, words, method, "en",
             0.95 if words >= 500 else 0.82, str(provenance.get("authors") or "")[:500],
             str(provenance.get("published") or ""),
             json.dumps([persistent_id, "registry_verified", f"evidence_tier_{tier}"])],
        )
        db.execute("UPDATE hive_sources SET status='done' WHERE mission_id=? AND url=?", [mission_id, url])
    return not existed


def _europe_pmc(mission_id: str, topic: str, db, limit: int) -> dict:
    query = urllib.parse.urlencode({"query": topic, "format": "json", "resultType": "core", "pageSize": limit})
    results = json.loads(_get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?{query}"))\
        .get("resultList", {}).get("result", [])
    saved = full_text = abstracts = 0
    for item in results:
        source = item.get("source", "MED")
        record_id = item.get("id") or item.get("pmid")
        if not record_id:
            continue
        pmcid, doi = item.get("pmcid"), item.get("doi")
        persistent_id = f"PMCID:{pmcid}" if pmcid else f"PMID:{item.get('pmid') or record_id}"
        abstract = _plain(item.get("abstractText"))
        content, method = abstract, "europe_pmc_abstract"
        is_oa = str(item.get("isOpenAccess", "N")).upper() == "Y"
        if pmcid and is_oa and full_text < 8:
            try:
                candidate = _xml_text(_get(
                    f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML", "application/xml"))
                if len(candidate.split()) >= 500:
                    content, method, full_text = candidate, "europe_pmc_fulltext", full_text + 1
            except Exception as exc:
                log.debug("Europe PMC full text unavailable for %s: %s", pmcid, exc)
        if abstract:
            abstracts += 1
        provenance = {"registry": "Europe PMC", "pmid": item.get("pmid"), "pmcid": pmcid,
                      "doi": doi, "journal": item.get("journalTitle"),
                      "authors": item.get("authorString"),
                      "published": item.get("firstPublicationDate") or item.get("pubYear"),
                      "cited_by_count": item.get("citedByCount"), "open_access": is_oa}
        if _save(db, mission_id, url=f"https://europepmc.org/article/{source}/{record_id}",
                 title=item.get("title") or persistent_id, abstract=abstract,
                 source_type="scholarly_primary", method=method, persistent_id=persistent_id,
                 tier=1, authority=20, open_access=is_oa, primary=True,
                 provenance=provenance, content=content):
            saved += 1
    return {"registry": "europe_pmc", "found": len(results), "saved": saved,
            "full_text": full_text, "abstracts": abstracts}


def _crossref(mission_id: str, topic: str, db, limit: int) -> dict:
    params = {"query.bibliographic": topic, "rows": limit,
              "filter": "type:journal-article,has-abstract:1"}
    if os.environ.get("BEHIVE_CONTACT_EMAIL"):
        params["mailto"] = os.environ["BEHIVE_CONTACT_EMAIL"]
    results = json.loads(_get("https://api.crossref.org/works?" + urllib.parse.urlencode(params)))\
        .get("message", {}).get("items", [])
    saved = 0
    for item in results:
        doi, title = item.get("DOI"), " ".join(item.get("title") or [])
        if not doi or not title:
            continue
        abstract = _plain(item.get("abstract"))
        authors = ", ".join(" ".join(filter(None, (a.get("given"), a.get("family")))) for a in item.get("author", []))
        dates = (item.get("published-print") or item.get("published-online") or {}).get("date-parts") or []
        provenance = {"registry": "Crossref", "doi": doi, "publisher": item.get("publisher"),
                      "journal": (item.get("container-title") or [""])[0], "authors": authors,
                      "published": "-".join(map(str, dates[0])) if dates else "",
                      "references_count": item.get("references-count"),
                      "cited_by_count": item.get("is-referenced-by-count"), "type": item.get("type")}
        if _save(db, mission_id, url=f"https://doi.org/{doi}", title=title, abstract=abstract,
                 source_type="scholarly_registry", method="crossref_abstract",
                 persistent_id=f"DOI:{doi}", tier=2, authority=18, open_access=None,
                 primary=None, provenance=provenance, content=abstract):
            saved += 1
    return {"registry": "crossref", "found": len(results), "saved": saved}


def ingest_authority_sources(mission_id: str, topic: str, limit: int = 25) -> dict:
    """Ingest registry-verified records and usable text before broad web discovery."""
    db = connect()
    _ensure_schema(db)
    reports, errors = [], []
    for name, function in (("Europe PMC", _europe_pmc), ("Crossref", _crossref)):
        try:
            reports.append(function(mission_id, topic, db, limit))
        except Exception as exc:
            log.warning("Authority registry %s failed: %s", name, exc)
            errors.append({"registry": name, "error": str(exc)[:300]})
    counts = db.execute(
        """SELECT COUNT(*), COUNT(*) FILTER (WHERE status='done'),
                  COUNT(*) FILTER (WHERE evidence_tier=1)
           FROM hive_sources WHERE mission_id=? AND verification_status='registry_verified'""", [mission_id]
    ).fetchone()
    db.close()
    return {"registries": reports, "errors": errors, "verified_sources": counts[0],
            "evidence_ready": counts[1], "tier_one": counts[2]}
