"""MCP#2 — 물성·활성·라벨 서버

도구
  pubchem_properties   PubChem: 계산 물성(SMILES, XLogP, TPSA…) + 문헌 실험값(pKa, 용해도, LogP, 융점) 및 출처
  chembl_search_target ChEMBL 타깃 이름 → ChEMBL ID
  chembl_activity      ChEMBL: IC50/Ki 등 생물활성(분자 또는 타깃 기준) + 타깃별 요약
  dailymed_label       DailyMed(FDA SPL): 활성·비활성 성분, 제형, 주요 라벨 섹션
"""
import asyncio
import statistics
from typing import Annotated, Any, Literal
from urllib.parse import quote

from defusedxml.ElementTree import fromstring
from pydantic import Field

from src.common import NotFound, ToolError, build_server, envelope, fetch, guard_query, run, safe_tool, truncate

mcp = build_server(
    "daewon-chem-bio-label",
    "의약품 화학·물성(PubChem), 생물활성(ChEMBL), 미국 허가 라벨 및 부형제(DailyMed) 조회 도구 모음. "
    "수치는 반드시 응답의 source/reference 와 함께 인용해야 한다.",
)

PUG = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
PUGVIEW = "https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound"
PROPS = "MolecularFormula,MolecularWeight,SMILES,ConnectivitySMILES,InChIKey,IUPACName,XLogP,TPSA,HBondDonorCount,HBondAcceptorCount,RotatableBondCount"
EXPERIMENTAL_HEADINGS = ["Dissociation Constants", "Solubility", "LogP", "Melting Point"]

CHEMBL = "https://www.ebi.ac.uk/chembl/api/data"
DAILYMED = "https://dailymed.nlm.nih.gov/dailymed/services/v2"


# =========================================================================== PubChem
def extract_pugview_values(js: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    """PUG-View JSON 에서 Information 항목의 값과 출처를 추출한다."""
    rec = js.get("Record", {})
    refs = {r.get("ReferenceNumber"): r for r in rec.get("Reference", [])}
    out: list[dict[str, Any]] = []

    def walk(sec: dict[str, Any]) -> None:
        for info in sec.get("Information", []):
            val = info.get("Value", {})
            texts = [s.get("String", "") for s in val.get("StringWithMarkup", [])]
            if not texts and "Number" in val:
                texts = [" ".join(str(n) for n in val["Number"]) + (" " + val["Unit"] if val.get("Unit") else "")]
            ref = refs.get(info.get("ReferenceNumber"), {})
            for t in texts:
                if t and len(out) < limit:
                    out.append({"value": truncate(t, 400), "reference": ref.get("SourceName"), "reference_title": truncate(ref.get("Name", ""), 120), "reference_url": ref.get("URL")})
        for sub in sec.get("Section", []):
            walk(sub)

    for s in rec.get("Section", []):
        walk(s)
    return out


async def _pubchem_experimental(cid: int) -> dict[str, list[dict[str, Any]]]:
    async def one(heading: str):
        try:
            js = await fetch(f"{PUGVIEW}/{cid}/JSON", params={"heading": heading})
            return heading, extract_pugview_values(js)
        except NotFound:
            return heading, []

    pairs = await asyncio.gather(*(one(h) for h in EXPERIMENTAL_HEADINGS))
    return {h: v for h, v in pairs}


@mcp.tool()
@safe_tool
async def pubchem_properties(
    identifier: Annotated[str, Field(description="화합물 일반명(영문, 예: 'itraconazole'), CID 숫자, 또는 SMILES")],
    identifier_type: Annotated[Literal["name", "cid", "smiles"], Field(description="identifier 의 종류")] = "name",
    include_experimental: Annotated[bool, Field(description="문헌 실험값(pKa·용해도·LogP·융점)과 출처 포함 여부")] = True,
) -> dict[str, Any]:
    """PubChem 에서 화합물의 SMILES/분자량/XLogP/TPSA 등 계산 물성과, 문헌에 기재된 pKa·용해도·LogP·융점 실험값(출처 포함)을 반환한다. 예측 pKa 는 제공하지 않는다."""
    guard_query(identifier)
    if identifier_type == "name":
        js = await fetch(f"{PUG}/compound/name/{quote(identifier, safe='')}/property/{PROPS}/JSON")
    elif identifier_type == "cid":
        if not identifier.isdigit():
            raise ToolError("cid 는 숫자여야 합니다.")
        js = await fetch(f"{PUG}/compound/cid/{identifier}/property/{PROPS}/JSON")
    else:
        js = await fetch(f"{PUG}/compound/smiles/property/{PROPS}/JSON", method="POST", data={"smiles": identifier}, cache_ttl=0)
    props = js.get("PropertyTable", {}).get("Properties", [])
    if not props:
        raise NotFound("PubChem 에서 해당 화합물을 찾지 못했습니다.")
    main = props[0]
    cid = main.pop("CID")
    data: dict[str, Any] = {
        "cid": cid,
        "computed_properties": main,
        "other_cids": [p["CID"] for p in props[1:6]],
    }
    if include_experimental:
        data["experimental"] = await _pubchem_experimental(cid)
    return envelope(
        "PubChem (NCBI/NLM)",
        data,
        source_url=f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
        note="computed_properties 는 PubChem 계산값, experimental 은 각 reference 문헌/DB 의 보고값입니다. 조건(온도·pH·용매)은 원문 확인이 필요합니다.",
    )


# =========================================================================== ChEMBL
@mcp.tool()
@safe_tool
async def chembl_search_target(
    name: Annotated[str, Field(description="타깃 단백질/유전자 이름. 예: 'EGFR', 'HMG-CoA reductase'")],
    max_results: Annotated[int, Field(ge=1, le=10)] = 5,
) -> dict[str, Any]:
    """ChEMBL 에서 타깃 이름으로 target_chembl_id 를 찾는다(chembl_activity 의 입력으로 사용)."""
    guard_query(name)
    js = await fetch(f"{CHEMBL}/target/search.json", params={"q": name, "limit": max_results})
    rows = [
        {"target_chembl_id": t.get("target_chembl_id"), "name": t.get("pref_name"), "type": t.get("target_type"), "organism": t.get("organism")}
        for t in js.get("targets", [])
    ]
    return envelope("ChEMBL (EMBL-EBI)", {"targets": rows}, source_url="https://www.ebi.ac.uk/chembl/")


@mcp.tool()
@safe_tool
async def chembl_activity(
    molecule: Annotated[str | None, Field(description="분자 일반명 또는 CHEMBL ID. 예: 'gefitinib', 'CHEMBL939'")] = None,
    target_chembl_id: Annotated[str | None, Field(description="타깃 CHEMBL ID. 예: 'CHEMBL203'")] = None,
    standard_type: Annotated[str, Field(description="활성 종류. IC50, Ki, Kd, EC50 등")] = "IC50",
    max_results: Annotated[int, Field(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """ChEMBL 에서 생물활성(IC50/Ki 등)을 조회하고 타깃별 요약(건수·최솟값·중앙값·최댓값, nM 단위만)을 함께 반환한다. molecule 또는 target_chembl_id 중 하나는 필수."""
    if not molecule and not target_chembl_id:
        raise ToolError("molecule 또는 target_chembl_id 중 하나는 필요합니다.")
    guard_query(molecule, target_chembl_id)
    params: dict[str, Any] = {"standard_type": standard_type, "limit": 100}
    resolved = None
    if molecule:
        if molecule.upper().startswith("CHEMBL"):
            mol_id = molecule.upper()
        else:
            ms = await fetch(f"{CHEMBL}/molecule/search.json", params={"q": molecule, "limit": 5})
            cands = ms.get("molecules", [])
            if not cands:
                raise NotFound("ChEMBL 에서 해당 분자를 찾지 못했습니다.")
            mol_id = cands[0]["molecule_chembl_id"]
            resolved = {"molecule_chembl_id": mol_id, "pref_name": cands[0].get("pref_name"), "max_phase": cands[0].get("max_phase"),
                        "other_candidates": [{"id": c["molecule_chembl_id"], "name": c.get("pref_name")} for c in cands[1:]]}
        params["molecule_chembl_id"] = mol_id
    if target_chembl_id:
        params["target_chembl_id"] = target_chembl_id.upper()
    js = await fetch(f"{CHEMBL}/activity.json", params=params)
    acts = [a for a in js.get("activities", []) if a.get("standard_value") not in (None, "")]
    rows = [
        {
            "molecule": a.get("molecule_pref_name") or a.get("molecule_chembl_id"),
            "target": a.get("target_pref_name"),
            "target_chembl_id": a.get("target_chembl_id"),
            "type": a.get("standard_type"),
            "relation": a.get("standard_relation"),
            "value": a.get("standard_value"),
            "units": a.get("standard_units"),
            "pchembl": a.get("pchembl_value"),
            "assay_type": a.get("assay_type"),
            "assay_chembl_id": a.get("assay_chembl_id"),
            "document_chembl_id": a.get("document_chembl_id"),
        }
        for a in acts
    ]
    summary: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r["units"] != "nM":
            continue
        s = summary.setdefault(r["target_chembl_id"] or "?", {"target": r["target"], "values": []})
        try:
            s["values"].append(float(r["value"]))
        except (TypeError, ValueError):
            pass
    summ_out = [
        {"target_chembl_id": k, "target": v["target"], "n": len(v["values"]), "min_nM": min(v["values"]), "median_nM": statistics.median(v["values"]), "max_nM": max(v["values"])}
        for k, v in summary.items()
        if v["values"]
    ]
    summ_out.sort(key=lambda x: -x["n"])
    return envelope(
        "ChEMBL (EMBL-EBI)",
        {"resolved_molecule": resolved, "total_in_page": len(rows), "per_target_summary_nM": summ_out[:15], "activities": rows[:max_results]},
        source_url="https://www.ebi.ac.uk/chembl/",
        note="측정조건(assay)·세포주·종이 서로 다른 값이 섞여 있습니다. 수치 비교 시 assay_chembl_id 별 확인이 필요합니다. 한 번에 최대 100건 중 일부만 표시됩니다.",
    )


# =========================================================================== DailyMed
NS = {"v3": "urn:hl7-org:v3"}
SECTION_CODES = {
    "description": "34089-3",
    "indications": "34067-9",
    "dosage_and_administration": "34068-7",
    "dosage_forms_and_strengths": "43678-2",
    "contraindications": "34070-3",
    "warnings": "34071-1",
    "warnings_and_precautions": "43685-7",
    "adverse_reactions": "34084-4",
    "drug_interactions": "34073-7",
    "clinical_pharmacology": "34090-1",
    "how_supplied": "34069-5",
    "inactive_ingredient": "51727-6",
}
SectionName = Literal[
    "description", "indications", "dosage_and_administration", "dosage_forms_and_strengths", "contraindications", "warnings",
    "warnings_and_precautions", "adverse_reactions", "drug_interactions", "clinical_pharmacology", "how_supplied", "inactive_ingredient",
]


def parse_spl(xml_text: str, sections: list[str], max_chars: int) -> dict[str, Any]:
    root = fromstring(xml_text)
    active: list[str] = []
    inactive: list[str] = []
    for ing in root.iter("{urn:hl7-org:v3}ingredient"):
        cls = ing.get("classCode", "")
        name_el = ing.find("v3:ingredientSubstance/v3:name", NS)
        name = " ".join("".join(name_el.itertext()).split()) if name_el is not None else ""
        if not name:
            continue
        if cls.startswith("ACT") and name not in active:
            active.append(name)
        elif cls == "IACT" and name not in inactive:
            inactive.append(name)
    forms: list[str] = []
    for fc in root.iter("{urn:hl7-org:v3}formCode"):
        dn = fc.get("displayName")
        if dn and dn not in forms:
            forms.append(dn)
    wanted = {SECTION_CODES[s]: s for s in sections if s in SECTION_CODES}
    found: dict[str, str] = {}
    for sec in root.iter("{urn:hl7-org:v3}section"):
        code = sec.find("v3:code", NS)
        if code is None or code.get("code") not in wanted:
            continue
        key = wanted[code.get("code")]
        if key not in found:
            found[key] = truncate("".join(sec.itertext()), max_chars)
    title_el = root.find("v3:title", NS)
    return {
        "title": " ".join("".join(title_el.itertext()).split()) if title_el is not None else None,
        "active_ingredients": active,
        "inactive_ingredients": inactive,
        "dosage_forms": forms,
        "sections": found,
        "sections_not_found": [s for s in sections if s not in found],
    }


@mcp.tool()
@safe_tool
async def dailymed_label(
    drug_name: Annotated[str | None, Field(description="의약품 일반명/상품명(영문). 예: 'atorvastatin'")] = None,
    set_id: Annotated[str | None, Field(description="DailyMed SET ID(UUID). 후보 목록에서 특정 라벨을 고를 때 사용")] = None,
    sections: Annotated[list[SectionName], Field(description="가져올 라벨 섹션. 기본: description, inactive_ingredient, dosage_forms_and_strengths")] = ["description", "inactive_ingredient", "dosage_forms_and_strengths"],
    max_chars_per_section: Annotated[int, Field(ge=500, le=8000)] = 2500,
) -> dict[str, Any]:
    """DailyMed 의 미국 의약품 라벨(SPL)에서 활성/비활성(부형제) 성분, 제형, 선택한 섹션 본문을 반환한다. drug_name 만 주면 후보 목록과 첫 번째 라벨의 내용을 반환한다."""
    if not drug_name and not set_id:
        raise ToolError("drug_name 또는 set_id 중 하나는 필요합니다.")
    guard_query(drug_name)
    candidates: list[dict[str, Any]] = []
    if not set_id:
        js = await fetch(f"{DAILYMED}/spls.json", params={"drug_name": drug_name, "pagesize": 5})
        candidates = [{"set_id": d["setid"], "title": d["title"], "version": d.get("spl_version"), "published": d.get("published_date")} for d in js.get("data", [])]
        if not candidates:
            raise NotFound("DailyMed 에서 해당 라벨을 찾지 못했습니다.")
        set_id = candidates[0]["set_id"]
    xml = await fetch(f"{DAILYMED}/spls/{quote(set_id, safe='')}.xml", as_="text", cache_ttl=3600)
    data = parse_spl(xml, list(sections), max_chars_per_section)
    data["set_id"] = set_id
    if candidates:
        data["candidates"] = candidates
    return envelope(
        "DailyMed (NLM) — FDA 승인 라벨(SPL)",
        data,
        source_url=f"https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid={set_id}",
        note="라벨은 제조사/제품별로 부형제가 다릅니다(후보가 여러 건이면 set_id 별로 확인). 국내 허가 사항과 동일하지 않습니다.",
    )


if __name__ == "__main__":
    run(mcp)
