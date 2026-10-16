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
from behive.engine.research_intent import assess_relevance, build_query_families

log = logging.getLogger(__name__)
AGENT = "BeHive-Research-OS/1.0 (verified scholarly discovery)"


def _get(url: str, accept: str = "application/json", timeout: int = 20) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept": accept})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _post_json(url: str, payload: dict, timeout: int = 25) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"User-Agent": AGENT, "Accept": "application/json", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _plain(value: str | None) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


_QUERY_STOPWORDS = {
    "about", "after", "against", "among", "and", "are", "benefit", "benefits",
    "boosting", "complementary", "could", "does", "effect", "effects", "for", "from", "have",
    "how", "into", "levels", "other", "reduce", "reducing", "research", "the",
    "their", "therapies", "therapy", "these", "this", "what", "when", "where", "which", "with",
    "animal", "authoritative", "biochemical", "biomedical", "clinical", "data", "databases",
    "direct", "distinguish", "evidence", "human", "inference", "mechanistic", "pharmacokinetics",
    "not", "peer-reviewed", "primary", "prioritize", "registered", "safety", "search", "snippets",
    "studies", "trials", "treat",
}


def _is_relevant(topic: str, title: str, abstract: str = "") -> bool:
    """Explainable entity-and-context admission, shared by sources and claims."""
    return bool(assess_relevance(topic, title, abstract)["accepted"])


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
    from behive.engine.evidence_ledger import ensure_schema
    ensure_schema(db)


def _save(db, mission_id: str, *, url: str, title: str, abstract: str, source_type: str,
          method: str, persistent_id: str, tier: int, authority: int,
          open_access: bool | None, primary: bool | None, provenance: dict,
          content: str = "") -> bool:
    topic_row = db.execute("SELECT topic FROM hive_missions WHERE id=?", [mission_id]).fetchone()
    relevance = assess_relevance(topic_row[0] if topic_row else "", title, abstract or content)
    if not relevance["accepted"]:
        from behive.engine.evidence_ledger import record
        record(mission_id, "scout", "source", "rejected", item_id=url,
               reason="entity/context relevance gate", details=relevance, db=db)
        return False
    domain = urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    existed = db.execute("SELECT 1 FROM hive_sources WHERE mission_id=? AND url=?", [mission_id, url]).fetchone()
    db.execute(
        """INSERT INTO hive_sources
           (mission_id,url,domain,title,snippet,source_type,harvest_method,score_total,
            score_relevance,score_freshness,score_authority,score_depth,status,evidence_tier,
            verification_status,persistent_id,provenance,is_open_access,is_primary)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?::jsonb,?,?) ON CONFLICT DO NOTHING""",
        [mission_id, url, domain, title[:512], abstract[:1024], source_type, method,
         min(100, round(relevance["score"] * 45) + 8 + authority + (15 if content else 8)),
         round(relevance["score"] * 45), 8, authority, 15 if content else 8, "scouted", tier,
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
    from behive.engine.evidence_ledger import record
    record(mission_id, "scout", "source", "admitted", item_id=url,
           reason="matched canonical research intent", details=relevance, db=db)
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
        title = item.get("title") or persistent_id
        if not _is_relevant(topic, title, abstract):
            continue
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
                 title=title, abstract=abstract,
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
        if not _is_relevant(topic, title, abstract):
            continue
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


def _pubmed(mission_id: str, topic: str, db, limit: int) -> dict:
    params = urllib.parse.urlencode({"db":"pubmed","term":topic,"retmode":"json","retmax":limit,"sort":"relevance"})
    ids = json.loads(_get(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?{params}"))\
        .get("esearchresult", {}).get("idlist", [])
    if not ids: return {"registry":"pubmed","found":0,"saved":0}
    xml = _get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urllib.parse.urlencode(
        {"db":"pubmed","id":",".join(ids),"retmode":"xml"}), "application/xml")
    root = ET.fromstring(xml); saved = 0
    for article in root.findall(".//PubmedArticle"):
        pmid = _plain(article.findtext(".//PMID")); title = _xml_text(ET.tostring(article.find(".//ArticleTitle"), encoding="unicode")) if article.find(".//ArticleTitle") is not None else pmid
        abstract = " ".join(_plain("".join(x.itertext())) for x in article.findall(".//AbstractText"))
        if not pmid or not title: continue
        if not _is_relevant(topic, title, abstract): continue
        publication_types = [_plain(x.text) for x in article.findall(".//PublicationType")]
        provenance={"registry":"PubMed","pmid":pmid,"journal":_plain(article.findtext(".//Journal/Title")),
                    "published":_plain(article.findtext(".//PubDate/Year")),"publication_types":publication_types}
        if _save(db,mission_id,url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",title=title,abstract=abstract,
                 source_type="scholarly_primary",method="pubmed_abstract",persistent_id=f"PMID:{pmid}",tier=1,
                 authority=20,open_access=None,primary=not any("Review" in x for x in publication_types),
                 provenance=provenance,content=abstract): saved += 1
    return {"registry":"pubmed","found":len(ids),"saved":saved}


def _clinical_trials(mission_id: str, topic: str, db, limit: int) -> dict:
    params=urllib.parse.urlencode({"query.term":topic,"pageSize":min(limit,100),"format":"json"})
    studies=json.loads(_get(f"https://clinicaltrials.gov/api/v2/studies?{params}")) .get("studies",[])
    saved=0
    for study in studies:
        protocol=study.get("protocolSection",{}); identification=protocol.get("identificationModule",{})
        nct=identification.get("nctId"); title=identification.get("briefTitle") or identification.get("officialTitle")
        if not nct or not title: continue
        design=protocol.get("designModule",{}); status=protocol.get("statusModule",{}); outcomes=protocol.get("outcomesModule",{})
        content=" ".join(filter(None,[title,identification.get("officialTitle"),
            (protocol.get("descriptionModule",{}) or {}).get("briefSummary"),
            json.dumps(design,default=str),json.dumps(outcomes,default=str)]))
        if not _is_relevant(topic, title, content): continue
        provenance={"registry":"ClinicalTrials.gov","nct_id":nct,"overall_status":status.get("overallStatus"),
                    "study_type":design.get("studyType"),"phases":design.get("phases") or []}
        if _save(db,mission_id,url=f"https://clinicaltrials.gov/study/{nct}",title=title,abstract=content[:4000],
                 source_type="clinical_trial_registry",method="clinicaltrials_v2",persistent_id=f"NCT:{nct}",
                 tier=1,authority=20,open_access=True,primary=True,provenance=provenance,content=content): saved+=1
    return {"registry":"clinicaltrials.gov","found":len(studies),"saved":saved}


def _openalex(mission_id: str, topic: str, db, limit: int) -> dict:
    params={"search":topic,"per-page":min(limit,50),"select":"id,doi,title,publication_year,type,authorships,primary_location,cited_by_count,open_access"}
    results=json.loads(_get("https://api.openalex.org/works?"+urllib.parse.urlencode(params))).get("results",[]); saved=0
    for item in results:
        oid=str(item.get("id") or "").rsplit("/",1)[-1]; title=item.get("title"); doi=item.get("doi")
        if not oid or not title: continue
        if not _is_relevant(topic, title): continue
        location=item.get("primary_location") or {}; landing=location.get("landing_page_url") or doi or item.get("id")
        authors=", ".join((a.get("author") or {}).get("display_name","") for a in item.get("authorships") or [])
        provenance={"registry":"OpenAlex","openalex_id":oid,"doi":doi,"published":item.get("publication_year"),
                    "type":item.get("type"),"authors":authors,"cited_by_count":item.get("cited_by_count"),
                    "open_access":item.get("open_access") or {}}
        if _save(db,mission_id,url=landing,title=title,abstract=title,source_type="scholarly_citation_index",
                 method="openalex_index",persistent_id=f"OPENALEX:{oid}",tier=2,authority=17,
                 open_access=(item.get("open_access") or {}).get("is_oa"),primary=None,provenance=provenance): saved+=1
    return {"registry":"openalex","found":len(results),"saved":saved}


def _geo(mission_id: str, topic: str, db, limit: int) -> dict:
    params=urllib.parse.urlencode({"db":"gds","term":topic,"retmode":"json","retmax":limit,"sort":"relevance"})
    ids=json.loads(_get(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?{params}"))\
        .get("esearchresult",{}).get("idlist",[])
    if not ids: return {"registry":"ncbi_geo","found":0,"saved":0}
    summary=json.loads(_get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?"+urllib.parse.urlencode(
        {"db":"gds","id":",".join(ids),"retmode":"json"}))).get("result",{}); saved=0
    for uid in ids:
        item=summary.get(uid) or {}; accession=item.get("accession"); title=item.get("title")
        if not accession or not title: continue
        content=" ".join(filter(None,[title,item.get("summary"),item.get("overall_design"),item.get("organism")]))
        if not _is_relevant(topic, title, content): continue
        provenance={"registry":"NCBI GEO","accession":accession,"organism":item.get("taxon"),
                    "samples":item.get("n_samples"),"published":item.get("pdat")}
        if _save(db,mission_id,url=f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={accession}",title=title,
                 abstract=content,source_type="experimental_dataset",method="ncbi_geo",persistent_id=f"GEO:{accession}",
                 tier=1,authority=20,open_access=True,primary=True,provenance=provenance,content=content): saved+=1
    return {"registry":"ncbi_geo","found":len(ids),"saved":saved}


def _nih_reporter(mission_id: str, topic: str, db, limit: int) -> dict:
    words=[x for x in re.findall(r"[A-Za-z0-9-]{3,}",topic) if x.lower() not in {"study","studies","research","effect","effects"}][:12]
    data=_post_json("https://api.reporter.nih.gov/v2/projects/search",{"criteria":{"advanced_text_search":{"operator":"and","search_field":"all","search_text":" ".join(words)}},"offset":0,"limit":min(limit,25)})
    results=data.get("results") or []; saved=0
    for item in results:
        project_num=item.get("project_num") or item.get("core_project_num"); title=item.get("project_title")
        if not project_num or not title: continue
        content=" ".join(filter(None,[title,item.get("abstract_text"),item.get("terms")]))
        if not _is_relevant(topic, title, content): continue
        org=item.get("organization") or {}; provenance={"registry":"NIH RePORTER","project_num":project_num,
            "fiscal_year":item.get("fiscal_year"),"organization":org.get("org_name"),"principal_investigators":item.get("principal_investigators") or []}
        if _save(db,mission_id,url=f"https://reporter.nih.gov/project-details/{urllib.parse.quote(str(item.get('appl_id') or project_num))}",
                 title=title,abstract=content[:4000],source_type="funded_research_registry",method="nih_reporter",
                 persistent_id=f"NIH:{project_num}",tier=2,authority=19,open_access=True,primary=False,
                 provenance=provenance,content=content): saved+=1
    return {"registry":"nih_reporter","found":len(results),"saved":saved}


def ingest_authority_sources(mission_id: str, topic: str, limit: int = 25, source_family: str | None = None) -> dict:
    """Ingest registry-verified records and usable text before broad web discovery."""
    db = connect()
    _ensure_schema(db)
    routes={
        "primary_study": (("Europe PMC",_europe_pmc),("PubMed",_pubmed)),
        "dataset": (("NCBI GEO",_geo),("OpenAlex",_openalex),("Europe PMC",_europe_pmc)),
        "registry": (("ClinicalTrials.gov",_clinical_trials),("NIH RePORTER",_nih_reporter),("PubMed",_pubmed)),
        "regulatory": (("ClinicalTrials.gov",_clinical_trials),("NIH RePORTER",_nih_reporter)),
        "thesis": (("OpenAlex",_openalex),("Crossref",_crossref)),
        "multilingual": (("Europe PMC",_europe_pmc),("OpenAlex",_openalex)),
        "patent": (("Crossref",_crossref),("OpenAlex",_openalex)),
    }
    selected=routes.get(source_family) or (("Europe PMC",_europe_pmc),("Crossref",_crossref),("PubMed",_pubmed),("OpenAlex",_openalex))
    reports, errors = [], []
    families = build_query_families(topic)
    # Query families prevent one broad natural-language query from determining
    # the entire corpus. Admission still uses the canonical mission topic.
    per_query = max(3, min(8, limit // max(1, len(families))))
    for name, function in selected:
        for family in families:
            try:
                # Registry calls use the focused query. _save independently
                # enforces relevance against the canonical mission topic.
                report = function(mission_id, family["query"], db, per_query)
                report["purpose"] = family["purpose"]
                report["query"] = family["query"]
                reports.append(report)
            except Exception as exc:
                log.warning("Authority registry %s failed for %s: %s", name, family["purpose"], exc)
                errors.append({"registry": name, "purpose": family["purpose"], "error": str(exc)[:300]})
    counts = db.execute(
        """SELECT COUNT(*), COUNT(*) FILTER (WHERE status='done'),
                  COUNT(*) FILTER (WHERE evidence_tier=1)
           FROM hive_sources WHERE mission_id=? AND verification_status='registry_verified'""", [mission_id]
    ).fetchone()
    db.close()
    return {"source_family":source_family or "general","registries": reports, "errors": errors,
            "verified_sources": counts[0],"evidence_ready": counts[1], "tier_one": counts[2]}
