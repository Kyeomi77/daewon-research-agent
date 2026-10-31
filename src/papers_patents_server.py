"""MCP#1 — 논문·특허 서버

도구
  search_pubmed            PubMed 검색(초록 포함)
  get_pmc_fulltext         PMC 오픈액세스 전문 조회
  search_kipris            KIPRIS Plus 국내 특허 검색(출원/등록/행정상태)
  search_patents_bq        BigQuery 공개 특허 데이터셋(글로벌) 검색
  get_patent_claims        BigQuery 에서 특정 공보의 청구항 조회(비용 큼)
  estimate_patent_expiry   출원일 기준 기본 존속기간(20년) 추정 계산기
"""
import asyncio
import os
import re
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from defusedxml.ElementTree import fromstring
from pydantic import Field

from src.common import (
    CONTACT_EMAIL,
    NotFound,
    RateLimiter,
    ToolError,
    build_server,
    envelope,
    fetch,
    guard_query,
    run,
    safe_tool,
    truncate,
)

mcp = build_server(
    "daewon-papers-patents",
    "의약품 R&D용 논문(PubMed/PMC)과 특허(KIPRIS, 글로벌 공개 특허 BigQuery) 조사 도구 모음. "
    "모든 응답은 출처(source)와 조회일(retrieved_at)을 포함한다.",
)

NCBI_KEY = os.environ.get("NCBI_API_KEY", "23f5f81401ddbd44ae0ff7d817cc217b9609")
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
NCBI_LIMITER = RateLimiter(8 if NCBI_KEY else 2.5)  # 키 있으면 10/s, 없으면 3/s 한도 아래로

KIPRIS_KEY = os.environ.get("KIPRIS_ACCESS_KEY", "=2Ysm5xR9bj0fjrGP6PVqTRV3TKi3fMUfTef3lJRQSc=")
KIPRIS_BASE = os.environ.get("KIPRIS_BASE_URL", "http://plus.kipris.or.kr/openapi/rest/patUtiModInfoSearchSevice")

BQ_PROJECT = os.environ.get("BQ_PROJECT") or None
BQ_MAX_BYTES = int(os.environ.get("BQ_MAX_BYTES_BILLED", str(300 * 10**9)))
BQ_TABLE = "`patents-public-data.patents.publications`"


# =========================================================================== PubMed / PMC
def _ncbi(extra: dict[str, Any]) -> dict[str, Any]:
    p: dict[str, Any] = {"tool": "daewon-research-agent", **extra}
    if CONTACT_EMAIL:
        p["email"] = CONTACT_EMAIL
    if NCBI_KEY:
        p["api_key"] = NCBI_KEY
    return p


def _txt(el) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def parse_pubmed_xml(xml_text: str, include_abstract: bool = True, abstract_max: int = 1500) -> list[dict[str, Any]]:
    root = fromstring(xml_text)
    out: list[dict[str, Any]] = []
    for art in root.iter("PubmedArticle"):
        mc = art.find("MedlineCitation")
        if mc is None:
            continue
        a = mc.find("Article")
        if a is None:
            continue
        pmid = _txt(mc.find("PMID"))
        year = _txt(a.find("Journal/JournalIssue/PubDate/Year"))
        if not year:
            m = re.search(r"\d{4}", _txt(a.find("Journal/JournalIssue/PubDate/MedlineDate")))
            year = m.group(0) if m else ""
        authors = []
        for au in a.findall("AuthorList/Author"):
            name = _txt(au.find("CollectiveName")) or f"{_txt(au.find('LastName'))} {_txt(au.find('Initials'))}".strip()
            if name:
                authors.append(name)
        doi = pmcid = ""
        for aid in art.findall("PubmedData/ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi":
                doi = _txt(aid)
            elif aid.get("IdType") == "pmc":
                pmcid = _txt(aid)
        rec: dict[str, Any] = {
            "pmid": pmid,
            "title": _txt(a.find("ArticleTitle")),
            "journal": _txt(a.find("Journal/Title")),
            "year": year,
            "authors": authors[:3] + (["et al."] if len(authors) > 3 else []),
            "doi": doi or None,
            "pmcid": pmcid or None,
            "publication_types": [_txt(t) for t in a.findall("PublicationTypeList/PublicationType")],
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        }
        if include_abstract:
            parts = []
            for at in a.findall("Abstract/AbstractText"):
                label = at.get("Label")
                parts.append(f"{label}: {_txt(at)}" if label else _txt(at))
            rec["abstract"] = truncate(" ".join(parts), abstract_max) or None
        out.append(rec)
    return out


@mcp.tool()
@safe_tool
async def search_pubmed(
    query: Annotated[str, Field(description="PubMed 검색식. 자연어 또는 PubMed 문법(예: 'itraconazole AND solid dispersion[tiab]'). 영문 권장.")],
    max_results: Annotated[int, Field(ge=1, le=30, description="반환 건수(최대 30)")] = 10,
    min_year: Annotated[int | None, Field(ge=1900, le=2100, description="발행연도 하한")] = None,
    max_year: Annotated[int | None, Field(ge=1900, le=2100, description="발행연도 상한")] = None,
    sort: Annotated[Literal["relevance", "date"], Field(description="정렬: 관련도 또는 최신순")] = "relevance",
    include_abstract: bool = True,
) -> dict[str, Any]:
    """PubMed 논문을 검색해 제목·저널·연도·저자·DOI·PMCID·초록을 반환한다. 전문은 get_pmc_fulltext 로 조회."""
    guard_query(query)
    params = _ncbi({"db": "pubmed", "term": query, "retmax": max_results, "retmode": "json", "sort": "pub_date" if sort == "date" else "relevance"})
    if min_year or max_year:
        params.update({"datetype": "pdat", "mindate": str(min_year or 1900), "maxdate": str(max_year or date.today().year)})
    res = await fetch(f"{EUTILS}/esearch.fcgi", params=params, limiter=NCBI_LIMITER)
    er = res.get("esearchresult", {})
    ids = er.get("idlist", [])
    if not ids:
        return envelope("PubMed (NCBI E-utilities)", {"total_count": 0, "articles": []}, note="검색 결과 없음. 검색어를 넓히거나 동의어를 추가하세요.")
    xml = await fetch(
        f"{EUTILS}/efetch.fcgi",
        params=_ncbi({"db": "pubmed", "id": ",".join(ids), "retmode": "xml"}),
        as_="text",
        limiter=NCBI_LIMITER,
    )
    articles = parse_pubmed_xml(xml, include_abstract=include_abstract)
    return envelope(
        "PubMed (NCBI E-utilities)",
        {"total_count": int(er.get("count", len(articles))), "translated_query": er.get("querytranslation"), "articles": articles},
        source_url="https://pubmed.ncbi.nlm.nih.gov/?term=" + query.replace(" ", "+"),
    )


def parse_pmc_xml(xml_text: str, max_chars: int) -> dict[str, Any]:
    root = fromstring(xml_text)
    art = next(root.iter("article"), None)
    if art is None:
        return {"has_fulltext": False, "sections": []}
    title = _txt(next(art.iter("article-title"), None))
    lic = next(art.iter("license"), None)
    body = art.find("body")
    sections: list[dict[str, str]] = []
    used = 0
    if body is not None:
        blocks = body.findall("sec") or [body]
        for sec in blocks:
            text = " ".join(_txt(p) for p in sec.iter("p") if _txt(p))
            if not text:
                continue
            room = max_chars - used
            if room <= 0:
                break
            text = truncate(text, room)
            used += len(text)
            sections.append({"title": _txt(sec.find("title")) if sec is not body else "(본문)", "text": text})
    return {
        "title": title,
        "has_fulltext": bool(sections),
        "license": truncate(_txt(lic), 300) if lic is not None else None,
        "sections": sections,
        "truncated": used >= max_chars,
    }


@mcp.tool()
@safe_tool
async def get_pmc_fulltext(
    pmcid: Annotated[str | None, Field(description="예: 'PMC1234567'")] = None,
    pmid: Annotated[str | None, Field(description="PMCID 를 모를 때 PMID 로 변환 조회")] = None,
    max_chars: Annotated[int, Field(ge=1000, le=60000, description="반환할 본문 최대 글자 수")] = 20000,
) -> dict[str, Any]:
    """PMC 오픈액세스 논문의 섹션별 본문을 반환한다. 비오픈액세스 논문은 전문이 없으며 이 경우 has_fulltext=false."""
    if not pmcid and not pmid:
        raise ToolError("pmcid 또는 pmid 중 하나는 필요합니다.")
    if not pmcid:
        if not re.fullmatch(r"\d{1,9}", pmid or ""):
            raise ToolError("pmid 형식이 올바르지 않습니다.")
        conv = await fetch(
            "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/",
            params={"ids": pmid, "format": "json", "tool": "daewon-research-agent", "email": CONTACT_EMAIL},
            limiter=NCBI_LIMITER,
        )
        recs = conv.get("records") or []
        pmcid = recs[0].get("pmcid") if recs else None
        if not pmcid:
            return envelope("PMC", {"has_fulltext": False}, note="해당 PMID 는 PMC 에 등재되어 있지 않습니다. PubMed 초록만 이용 가능합니다.")
    m = re.fullmatch(r"(?:PMC)?(\d{1,9})", pmcid.strip(), re.IGNORECASE)
    if not m:
        raise ToolError("pmcid 형식이 올바르지 않습니다. 예: PMC1234567")
    xml = await fetch(f"{EUTILS}/efetch.fcgi", params=_ncbi({"db": "pmc", "id": m.group(1), "retmode": "xml"}), as_="text", limiter=NCBI_LIMITER)
    data = parse_pmc_xml(xml, max_chars)
    note = None if data["has_fulltext"] else "전문이 제공되지 않는 논문입니다(비오픈액세스 등). 출판사 사이트/구독 경로를 확인하세요."
    return envelope("PMC (NCBI)", {"pmcid": f"PMC{m.group(1)}", **data}, source_url=f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{m.group(1)}/", note=note)


# =========================================================================== KIPRIS
def parse_kipris_xml(xml_text: str) -> tuple[int, list[dict[str, str]]]:
    root = fromstring(xml_text)
    ok = _txt(next(root.iter("successYN"), None))
    if ok.upper() == "N":
        msg = _txt(next(root.iter("resultMsg"), None))
        raise ToolError("KIPRIS API 오류: " + (msg or "인증키/파라미터를 확인하세요."), hint="accessKey 및 서비스 명세(오퍼레이션명/파라미터)를 KIPRIS Plus 에서 확인하세요.")
    total_el = next(root.iter("totalCount"), None)
    total = int(_txt(total_el)) if total_el is not None and _txt(total_el).isdigit() else 0
    items = []
    for it in root.iter("item"):
        d = {child.tag: _txt(child) for child in it}
        items.append(d)
    return total, items


@mcp.tool()
@safe_tool
async def search_kipris(
    keyword: Annotated[str | None, Field(description="자유검색어(명칭·요약·청구항 등). 한글/영문")] = None,
    applicant: Annotated[str | None, Field(description="출원인명(경쟁사 동향 조사용)")] = None,
    patent_type: Annotated[Literal["patent", "utility", "both"], Field(description="특허/실용신안/전체")] = "patent",
    status_contains: Annotated[str | None, Field(description="행정상태 필터(결과 후처리). 예: '등록', '공개', '거절', '소멸', '취하', '무효'")] = None,
    page: Annotated[int, Field(ge=1, le=50)] = 1,
    max_results: Annotated[int, Field(ge=1, le=30)] = 10,
) -> dict[str, Any]:
    """KIPRIS Plus 로 국내 특허·실용신안을 검색하고 출원번호·출원일·출원인·행정상태(등록/공개/거절 등)·등록번호·요약을 반환한다."""
    if not keyword and not applicant:
        raise ToolError("keyword 또는 applicant 중 하나는 필요합니다.")
    if not KIPRIS_KEY:
        raise ToolError("KIPRIS_ACCESS_KEY 가 설정되지 않았습니다.")
    guard_query(keyword, applicant)
    base = {
        "patent": "true" if patent_type in ("patent", "both") else "false",
        "utility": "true" if patent_type in ("utility", "both") else "false",
        "docsStart": page,
        "docsCount": max_results,
        "accessKey": KIPRIS_KEY,
    }
    if keyword:
        url, params = f"{KIPRIS_BASE}/freeSearchInfo", {**base, "word": keyword}
    else:
        url, params = f"{KIPRIS_BASE}/applicantNameSearchInfo", {**base, "applicant": applicant}
    xml = await fetch(url, params=params, as_="text")
    total, items = parse_kipris_xml(xml)
    rows = []
    for d in items:
        if applicant and keyword and applicant.lower() not in d.get("applicantName", "").lower():
            continue
        if status_contains and status_contains not in d.get("registerStatus", ""):
            continue
        rows.append(
            {
                "application_number": d.get("applicationNumber"),
                "application_date": d.get("applicationDate"),
                "title": d.get("inventionTitle"),
                "applicant": d.get("applicantName"),
                "status": d.get("registerStatus"),
                "register_number": d.get("registerNumber") or None,
                "register_date": d.get("registerDate") or None,
                "open_number": d.get("openNumber") or None,
                "open_date": d.get("openDate") or None,
                "ipc": d.get("ipcNumber"),
                "abstract": truncate(d.get("astrtCont", ""), 600),
            }
        )
    return envelope(
        "KIPRIS Plus (특허정보원)",
        {"total_count": total, "page": page, "patents": rows},
        source_url="https://www.kipris.or.kr",
        note="행정상태·권리범위는 KIPRIS 원문/등록원부에서 최종 확인이 필요합니다(법적 판단 아님).",
    )


# =========================================================================== BigQuery 공개 특허
_bq_client = None


def _bq():
    global _bq_client
    if _bq_client is None:
        from google.cloud import bigquery

        _bq_client = bigquery.Client(project=BQ_PROJECT, location="US")  # 공개 데이터셋 위치=US
    return _bq_client


def _run_bq(sql: str, params: list, dry_run_only: bool) -> tuple[int, list[dict[str, Any]]]:
    from google.cloud import bigquery

    client = _bq()
    dry = client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params, dry_run=True, use_query_cache=False))
    est = int(dry.total_bytes_processed or 0)
    if est > BQ_MAX_BYTES:
        raise ToolError(
            f"예상 스캔량 {est / 1e9:.0f}GB 가 한도 {BQ_MAX_BYTES / 1e9:.0f}GB 를 초과합니다.",
            hint="출원연도 하한을 높이거나 국가 수를 줄이세요. 한도는 BQ_MAX_BYTES_BILLED 로 조정합니다.",
        )
    if dry_run_only:
        return est, []
    cfg = bigquery.QueryJobConfig(query_parameters=params, maximum_bytes_billed=BQ_MAX_BYTES)
    rows = [dict(r) for r in client.query(sql, job_config=cfg).result(timeout=120)]
    return est, rows


def _fmt_date(v: Any) -> str | None:
    s = str(v or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() and s != "00000000" else None


def _gp_url(pub_no: str) -> str:
    return "https://patents.google.com/patent/" + pub_no.replace("-", "")


def _clean_term(t: str) -> str:
    return re.sub(r"[%_\\]", " ", t).strip().lower()


@mcp.tool()
@safe_tool
async def search_patents_bq(
    keywords: Annotated[list[str], Field(min_length=1, max_length=6, description="모두 포함(AND)되어야 하는 영문 키워드. 예: ['itraconazole','solid dispersion']")],
    assignee: Annotated[str | None, Field(description="출원인/양수인명 일부(영문). 예: 'hanmi'")] = None,
    countries: Annotated[list[str], Field(description="공보 국가코드. 기본 US,EP,WO,KR,JP")] = ["US", "EP", "WO", "KR", "JP"],
    from_year: Annotated[int, Field(ge=1990, le=2100, description="출원연도 하한")] = 2010,
    cpc_prefix: Annotated[str | None, Field(description="CPC 분류 접두. 예: 'A61K9' (제형)")] = None,
    max_results: Annotated[int, Field(ge=1, le=50)] = 20,
    dry_run: Annotated[bool, Field(description="true 이면 쿼리하지 않고 예상 스캔량(GB)만 반환")] = False,
) -> dict[str, Any]:
    """Google 공개 특허 데이터셋(BigQuery)에서 제목·요약에 키워드가 모두 포함된 글로벌 특허 공보를 검색한다(영문 기준, 최신 공보는 반영이 지연될 수 있음)."""
    guard_query(*keywords, assignee)
    terms = [_clean_term(k) for k in keywords if _clean_term(k)]
    if not terms:
        raise ToolError("유효한 키워드가 없습니다.")
    if any(not re.fullmatch(r"[A-Z]{2}", c) for c in countries) or not countries:
        raise ToolError("countries 는 2글자 국가코드 목록이어야 합니다. 예: ['US','EP']")
    if cpc_prefix and not re.fullmatch(r"[A-HY]\d{0,2}[A-Z]?\d{0,4}", cpc_prefix):
        raise ToolError("cpc_prefix 형식이 올바르지 않습니다. 예: A61K9")

    from google.cloud import bigquery as bq

    params = [
        bq.ScalarQueryParameter("from_date", "INT64", from_year * 10000 + 101),
        bq.ArrayQueryParameter("countries", "STRING", countries),
        bq.ScalarQueryParameter("lim", "INT64", max_results),
    ]
    raw_filters = ""
    if assignee:
        raw_filters += " AND EXISTS (SELECT 1 FROM UNNEST(assignee_harmonized) a WHERE LOWER(a.name) LIKE CONCAT('%', @assignee, '%'))"
        params.append(bq.ScalarQueryParameter("assignee", "STRING", _clean_term(assignee)))
    if cpc_prefix:
        raw_filters += " AND EXISTS (SELECT 1 FROM UNNEST(cpc) c WHERE STARTS_WITH(c.code, @cpc))"
        params.append(bq.ScalarQueryParameter("cpc", "STRING", cpc_prefix))
    text_filters = ""
    for i, t in enumerate(terms):
        text_filters += f" AND (LOWER(title) LIKE CONCAT('%', @t{i}, '%') OR LOWER(abstract) LIKE CONCAT('%', @t{i}, '%'))"
        params.append(bq.ScalarQueryParameter(f"t{i}", "STRING", t))

    sql = f"""
    WITH base AS (
      SELECT publication_number, country_code, kind_code, family_id,
             filing_date, priority_date, publication_date, grant_date,
             COALESCE((SELECT x.text FROM UNNEST(title_localized) x WHERE x.language = 'en' LIMIT 1),
                      title_localized[SAFE_OFFSET(0)].text) AS title,
             COALESCE((SELECT x.text FROM UNNEST(abstract_localized) x WHERE x.language = 'en' LIMIT 1),
                      abstract_localized[SAFE_OFFSET(0)].text) AS abstract,
             ARRAY(SELECT a.name FROM UNNEST(assignee_harmonized) a LIMIT 5) AS assignees,
             ARRAY(SELECT c.code FROM UNNEST(cpc) c LIMIT 5) AS cpc_codes
      FROM {BQ_TABLE}
      WHERE filing_date >= @from_date AND country_code IN UNNEST(@countries){raw_filters}
    )
    SELECT * FROM base WHERE TRUE{text_filters}
    ORDER BY publication_date DESC
    LIMIT @lim
    """
    est, rows = await asyncio.to_thread(_run_bq, sql, params, dry_run)
    if dry_run:
        return envelope("Google Patents Public Data (BigQuery)", {"estimated_scan_gb": round(est / 1e9, 1), "limit_gb": round(BQ_MAX_BYTES / 1e9, 1)})
    out = []
    for r in rows:
        out.append(
            {
                "publication_number": r["publication_number"],
                "family_id": r["family_id"],
                "title": r["title"],
                "assignees": r["assignees"],
                "filing_date": _fmt_date(r["filing_date"]),
                "priority_date": _fmt_date(r["priority_date"]),
                "publication_date": _fmt_date(r["publication_date"]),
                "grant_date": _fmt_date(r["grant_date"]),
                "cpc": r["cpc_codes"],
                "abstract": truncate(r["abstract"] or "", 600),
                "url": _gp_url(r["publication_number"]),
            }
        )
    return envelope(
        "Google Patents Public Data (BigQuery)",
        {"returned": len(out), "scanned_gb": round(est / 1e9, 1), "patents": out},
        source_url="https://patents.google.com",
        note="공개 데이터셋은 갱신 지연·번역 품질 한계가 있으며 법적 상태(유지/소멸)는 반영되지 않습니다. 권리 상태는 각국 특허청에서 확인하세요.",
    )


@mcp.tool()
@safe_tool
async def get_patent_claims(
    publication_number: Annotated[str, Field(description="BigQuery 형식 공보번호. 예: 'US-9123456-B2' (search_patents_bq 결과의 publication_number)")],
    max_chars: Annotated[int, Field(ge=500, le=20000)] = 6000,
) -> dict[str, Any]:
    """특정 특허 공보의 청구항 전문(영문 우선)을 BigQuery 에서 조회한다. 스캔 비용이 크므로 후보를 좁힌 뒤 1건씩 호출할 것."""
    if not re.fullmatch(r"[A-Z]{2}-[A-Za-z0-9]+-[A-Z][0-9]?", publication_number):
        raise ToolError("publication_number 형식이 올바르지 않습니다. 예: US-9123456-B2")
    from google.cloud import bigquery as bq

    sql = f"""
    SELECT publication_number,
           COALESCE((SELECT x.text FROM UNNEST(claims_localized) x WHERE x.language = 'en' LIMIT 1),
                    claims_localized[SAFE_OFFSET(0)].text) AS claims
    FROM {BQ_TABLE} WHERE publication_number = @pn LIMIT 1
    """
    est, rows = await asyncio.to_thread(_run_bq, sql, [bq.ScalarQueryParameter("pn", "STRING", publication_number)], False)
    if not rows:
        raise NotFound("해당 공보번호를 찾지 못했습니다.")
    return envelope(
        "Google Patents Public Data (BigQuery)",
        {"publication_number": publication_number, "claims": truncate(rows[0]["claims"] or "", max_chars), "scanned_gb": round(est / 1e9, 1)},
        source_url=_gp_url(publication_number),
        note="청구항 해석(권리범위·회피 가능성)은 변리사 검토가 필요합니다.",
    )


# =========================================================================== 존속기간 계산
def _parse_date(s: str) -> date:
    m = re.fullmatch(r"(\d{4})[-./]?(\d{2})[-./]?(\d{2})", s.strip())
    if not m:
        raise ToolError("날짜 형식은 YYYY-MM-DD 또는 YYYYMMDD 입니다.")
    try:
        return date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        raise ToolError("존재하지 않는 날짜입니다.") from None


def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 2/29
        return d.replace(year=d.year + years, day=28)


@mcp.tool()
@safe_tool
async def estimate_patent_expiry(
    filing_date: Annotated[str, Field(description="출원일(PCT 의 경우 국제출원일). YYYY-MM-DD 또는 YYYYMMDD")],
    extension_days: Annotated[int, Field(ge=0, le=3650, description="이미 확인된 존속기간 연장/조정 일수(KR 존속기간연장, US PTA/PTE 등). 모르면 0")] = 0,
) -> dict[str, Any]:
    """출원일 + 20년 + 연장일수로 '기본 만료 예정일'을 계산한다. 법적 만료일이 아니라 검토 출발점이다."""
    d = _parse_date(filing_date)
    base = add_years(d, 20)
    final = base + timedelta(days=extension_days)
    return envelope(
        "계산기(내장)",
        {
            "filing_date": d.isoformat(),
            "base_expiry": base.isoformat(),
            "extension_days_applied": extension_days,
            "estimated_expiry": final.isoformat(),
            "caveats": [
                "존속기간 연장(KR 의약품 허가 지연 최대 5년, US PTE/PTA)은 별도 확인 필요",
                "연차료 미납·무효·포기·터미널 디스클레이머가 있으면 더 빨리 소멸될 수 있음",
                "패밀리 특허는 국가별 출원일/국제출원일 기준이 서로 다름",
                "물질·용도·제형·결정형 특허는 각각 만료일이 다름",
            ],
        },
        note="법적 효력이 없는 추정치입니다. 최종 판단은 변리사/IP팀 확인이 필요합니다.",
    )


if __name__ == "__main__":
    run(mcp)
