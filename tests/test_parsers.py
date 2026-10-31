"""네트워크 없이 실행 가능한 단위 테스트(파서·가드·계산기). 실행: pytest -q"""
import asyncio

import pytest

from src import common
from src.chem_server import extract_pugview_values, parse_spl
from src.papers_patents_server import add_years, estimate_patent_expiry, parse_kipris_xml, parse_pmc_xml, parse_pubmed_xml
from src.regulatory_server import _first_list_of_dicts, collect_law_text, parse_mfds_items

PUBMED_XML = """<?xml version="1.0"?>
<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, 1st January 2024//EN" "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_240101.dtd">
<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>111</PMID><Article>
<Journal><Title>Int J Pharm</Title><JournalIssue><PubDate><Year>2023</Year></PubDate></JournalIssue></Journal>
<ArticleTitle>Solid dispersion of <i>itraconazole</i>.</ArticleTitle>
<Abstract><AbstractText Label="BACKGROUND">Poor solubility.</AbstractText><AbstractText Label="RESULTS">AUC doubled.</AbstractText></Abstract>
<AuthorList><Author><LastName>Kim</LastName><Initials>A</Initials></Author><Author><LastName>Lee</LastName><Initials>B</Initials></Author>
<Author><LastName>Park</LastName><Initials>C</Initials></Author><Author><LastName>Choi</LastName><Initials>D</Initials></Author></AuthorList>
<PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList></Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="doi">10.1/xyz</ArticleId><ArticleId IdType="pmc">PMC999</ArticleId></ArticleIdList></PubmedData></PubmedArticle></PubmedArticleSet>"""

PMC_XML = """<pmc-articleset><article><front><article-meta><title-group><article-title>T</article-title></title-group>
<permissions><license><license-p>CC BY 4.0</license-p></license></permissions></article-meta></front>
<body><sec><title>Methods</title><p>We did A.</p><p>Then B.</p></sec><sec><title>Results</title><p>It worked.</p></sec></body></article></pmc-articleset>"""

KIPRIS_XML = """<response><header><successYN>Y</successYN></header><body><items>
<item><inventionTitle>고체분산체</inventionTitle><applicantName>대원제약</applicantName><applicationNumber>1020200000001</applicationNumber>
<registerStatus>등록</registerStatus><astrtCont>요약</astrtCont></item></items><count><totalCount>1</totalCount></count></body></response>"""

SPL_XML = """<document xmlns="urn:hl7-org:v3"><title>ATORVA TABLETS</title><component><structuredBody><component><section>
<code code="48780-1"/><subject><manufacturedProduct><manufacturedProduct><formCode displayName="TABLET, FILM COATED"/>
<ingredient classCode="ACTIB"><ingredientSubstance><name>ATORVASTATIN CALCIUM</name></ingredientSubstance></ingredient>
<ingredient classCode="IACT"><ingredientSubstance><name>LACTOSE MONOHYDRATE</name></ingredientSubstance></ingredient>
<ingredient classCode="IACT"><ingredientSubstance><name>MAGNESIUM STEARATE</name></ingredientSubstance></ingredient>
</manufacturedProduct></manufacturedProduct></subject></section></component>
<component><section><code code="34089-3"/><title>DESCRIPTION</title><text><paragraph>Atorvastatin is a statin.</paragraph></text></section></component>
</structuredBody></component></document>"""

PUGVIEW = {"Record": {"Reference": [{"ReferenceNumber": 7, "SourceName": "HSDB", "Name": "ITRACONAZOLE", "URL": "https://x"}],
                      "Section": [{"TOCHeading": "A", "Section": [{"TOCHeading": "Dissociation Constants",
                                   "Information": [{"ReferenceNumber": 7, "Value": {"StringWithMarkup": [{"String": "pKa = 3.7"}]}}]}]}]}}


def test_pubmed_parse():
    r = parse_pubmed_xml(PUBMED_XML)[0]
    assert r["pmid"] == "111" and r["year"] == "2023" and r["doi"] == "10.1/xyz" and r["pmcid"] == "PMC999"
    assert r["authors"][-1] == "et al." and "BACKGROUND: Poor solubility." in r["abstract"]
    assert r["title"] == "Solid dispersion of itraconazole."


def test_pmc_parse():
    d = parse_pmc_xml(PMC_XML, 1000)
    assert d["has_fulltext"] and d["sections"][0]["title"] == "Methods" and "Then B." in d["sections"][0]["text"]
    assert parse_pmc_xml("<pmc-articleset><article><front/></article></pmc-articleset>", 1000)["has_fulltext"] is False


def test_kipris_parse_and_error():
    total, items = parse_kipris_xml(KIPRIS_XML)
    assert total == 1 and items[0]["applicantName"] == "대원제약"
    with pytest.raises(common.ToolError):
        parse_kipris_xml("<response><header><successYN>N</successYN><resultMsg>bad key</resultMsg></header></response>")


def test_spl_parse():
    d = parse_spl(SPL_XML, ["description", "warnings"], 500)
    assert d["active_ingredients"] == ["ATORVASTATIN CALCIUM"]
    assert d["inactive_ingredients"] == ["LACTOSE MONOHYDRATE", "MAGNESIUM STEARATE"]
    assert d["dosage_forms"] == ["TABLET, FILM COATED"] and "statin" in d["sections"]["description"]
    assert d["sections_not_found"] == ["warnings"]


def test_pugview():
    v = extract_pugview_values(PUGVIEW)
    assert v == [{"value": "pKa = 3.7", "reference": "HSDB", "reference_title": "ITRACONAZOLE", "reference_url": "https://x"}]


def test_mfds_items_shapes():
    ok = {"header": {"resultCode": "00"}, "body": {"totalCount": 2, "items": [{"ITEM_SEQ": "1"}, {"ITEM_SEQ": "2"}]}}
    assert parse_mfds_items(ok)[0] == 2
    nested = {"header": {"resultCode": "00"}, "body": {"totalCount": 1, "items": {"item": [{"ITEM_SEQ": "1"}]}}}
    assert parse_mfds_items(nested)[1] == [{"ITEM_SEQ": "1"}]
    with pytest.raises(common.ToolError):
        parse_mfds_items({"header": {"resultCode": "30", "resultMsg": "SERVICE KEY ERROR"}})


def test_law_helpers():
    js = {"AdmRulSearch": {"admrul": [{"행정규칙명": "의약품동등성시험기준", "행정규칙일련번호": "1"}]}}
    assert _first_list_of_dicts(js)[0]["행정규칙일련번호"] == "1"
    body = {"AdmRulService": {"조문": [{"조문내용": "제1조 목적"}, {"조문내용": "제2조 정의"}]}}
    assert collect_law_text(body) == ["제1조 목적", "제2조 정의"]


def test_expiry():
    from datetime import date

    assert add_years(date(2012, 2, 29), 1) == date(2013, 2, 28)  # 윤일 → 평년
    assert add_years(date(2012, 2, 29), 20) == date(2032, 2, 29)  # 2032 는 윤년
    out = asyncio.run(estimate_patent_expiry(filing_date="2012-05-14", extension_days=0))
    assert out["data"]["base_expiry"] == "2032-05-14"
    bad = asyncio.run(estimate_patent_expiry(filing_date="2012/13/45"))
    assert "error" in bad


def test_guard(monkeypatch):
    monkeypatch.setattr(common, "_BLOCKS", common._block_patterns() + [__import__("re").compile(r"DW-\d{3,}")])
    common.guard_query("itraconazole solid dispersion")
    with pytest.raises(common.ToolError):
        common.guard_query("DW-1234 선행특허")
    with pytest.raises(common.ToolError):
        common.guard_query("이 문서는 대외비 입니다")
    with pytest.raises(common.ToolError):
        common.guard_query("x" * 1000)


def test_iig_filter_rows():
    from src.regulatory_server import filter_iig_rows

    rows = [
        {"ingredient_name": "POVIDONE K30", "route": "ORAL", "dosage_form": "TABLET", "maximum_potency": "10 mg"},
        {"ingredient_name": "POVIDONE", "route": "ORAL", "dosage_form": "TABLET, FILM COATED", "maximum_potency": "20 mg"},
        {"ingredient_name": "POVIDONE", "route": "TOPICAL", "dosage_form": "GEL", "maximum_potency": "1 %"},
        {"ingredient_name": "CROSPOVIDONE", "route": "ORAL", "dosage_form": "TABLET", "maximum_potency": "5 mg"},
        {"ingredient_name": "LACTOSE", "route": "ORAL", "dosage_form": "TABLET", "maximum_potency": "100 mg"},
    ]
    assert len(filter_iig_rows(rows, "povidone", None, None)) == 4          # 부분 일치(CROSPOVIDONE 포함)
    assert len(filter_iig_rows(rows, "POVIDONE", "oral", "tablet")) == 3     # 경로·제형 부분 일치
    assert [r["maximum_potency"] for r in filter_iig_rows(rows, "POVIDONE", "TOPICAL", None)] == ["1 %"]
    assert filter_iig_rows(rows, "STARCH", None, None) == []


def test_prepare_iig_convert(tmp_path):
    import importlib.util, json, pathlib

    spec = importlib.util.spec_from_file_location("prep", pathlib.Path(__file__).parents[1] / "scripts" / "prepare_iig_for_datastore.py")
    prep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prep)
    src = tmp_path / "iig.csv"
    src.write_text("﻿Ingredient Name,Route,Dosage Form,Maximum Potency (mg)\nPOVIDONE,ORAL,TABLET,10\n,,,\nLACTOSE,ORAL,\"TABLET, FILM COATED\",100\n", encoding="utf-8")
    jl, cs, n, cols = prep.convert(src, tmp_path / "out", "2026-10", "https://x")
    assert n == 2 and cols == ["ingredient_name", "route", "dosage_form", "maximum_potency_mg", "data_date", "source_url"]
    recs = [json.loads(l) for l in jl.read_text(encoding="utf-8").splitlines()]
    assert recs[1] == {"ingredient_name": "LACTOSE", "route": "ORAL", "dosage_form": "TABLET, FILM COATED", "maximum_potency_mg": "100", "data_date": "2026-10", "source_url": "https://x"}
    assert cs.read_text(encoding="utf-8").splitlines()[0].startswith("ingredient_name,route")
