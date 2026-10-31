"""MCP#3 — 국내 허가·법령·IIG 서버

도구
  mfds_product_search   식약처 의약품 제품 허가정보(공공데이터포털 API)
  law_search            국가법령정보센터 Open API: 법령/행정규칙(고시) 검색
  law_get_text          법령/행정규칙 본문(조문) 조회, 키워드 주변만 발췌 가능
  iig_lookup            FDA IIG(비활성 성분 DB) — Gemini Enterprise 데이터 스토어에서 최대 사용량 조회
  
"""
import asyncio
import json
import os
import re
from typing import Annotated, Any, Literal

from defusedxml.ElementTree import fromstring
from pydantic import Field

from src.common import NotFound, ToolError, build_server, envelope, fetch, guard_query, run, safe_tool, truncate

mcp = build_server(
    "daewon-regulatory",
    "국내 의약품 허가정보(식약처), 법령·고시(국가법령정보센터), FDA 비활성 성분 DB(IIG) 조회 도구 모음. "
    "규제 판단은 하지 않고 원문 근거와 시행일/기준일을 함께 반환한다.",
)

DATA_GO_KR_KEY = os.environ.get("DATA_GO_KR_KEY", "b64ce4504b64b5ed321bdeeab7d022168b7229d4159817bf71103d72140d7725")
MFDS_PERMIT_URL = os.environ.get("MFDS_PERMIT_URL", "http://apis.data.go.kr/1471000/DrugPrdtPrmsnInfoService06/getDrugPrdtPrmsnInq06")
LAW_OC = os.environ.get("LAW_OC", "daewon2026")
LAW_BASE = "https://www.law.go.kr/DRF"
IIG_DS_PROJECT = os.environ.get("IIG_DS_PROJECT", "")
IIG_DS_LOCATION = os.environ.get("IIG_DS_LOCATION", "global")  # global | us | eu (데이터 스토어 위치)
IIG_DATASTORE_ID = os.environ.get("IIG_DATASTORE_ID", "")
# IIG 데이터의 컬럼명(적재 후 실제 컬럼명에 맞게 환경변수로 조정)
IIG_FIELD_INGREDIENT = os.environ.get("IIG_FIELD_INGREDIENT", "ingredient_name")
IIG_FIELD_ROUTE = os.environ.get("IIG_FIELD_ROUTE", "route")
IIG_FIELD_FORM = os.environ.get("IIG_FIELD_FORM", "dosage_form")


# =========================================================================== 식약처 허가정보
def parse_mfds_items(payload: Any) -> tuple[int, list[dict[str, Any]]]:
    """data.go.kr 응답(JSON)에서 (totalCount, items) 추출. items 가 list 또는 {'item': [...]} 두 형태 모두 처리."""
    if not isinstance(payload, dict):
        raise ToolError("식약처 API 응답 형식을 해석하지 못했습니다.")
    header = payload.get("header") or payload.get("response", {}).get("header") or {}
    code = str(header.get("resultCode", "00"))
    if code not in ("00", "0", "INFO-000"):
        raise ToolError(f"식약처 API 오류: {header.get('resultMsg', code)}", hint="DATA_GO_KR_KEY(Decoding 키)와 활용신청 승인 여부를 확인하세요.")
    body = payload.get("body") or payload.get("response", {}).get("body") or {}
    items = body.get("items") or []
    if isinstance(items, dict):
        items = items.get("item", [])
    if isinstance(items, dict):
        items = [items]
    return int(body.get("totalCount") or 0), [i.get("item", i) if isinstance(i, dict) else i for i in items]


@mcp.tool()
@safe_tool
async def mfds_product_search(
    item_name: Annotated[str | None, Field(description="제품명(한글 일부). 예: '아토르바스타틴'")] = None,
    entp_name: Annotated[str | None, Field(description="업체명. 예: '대원제약'")] = None,
    item_seq: Annotated[str | None, Field(description="품목기준코드(9자리)")] = None,
    ingredient_contains: Annotated[str | None, Field(description="결과 중 주성분/원료명에 이 문자열이 포함된 건만 남김(후처리)")] = None,
    page: Annotated[int, Field(ge=1, le=100)] = 1,
    max_results: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """식약처 의약품 제품 허가정보를 검색해 품목명·업체·허가일·전문/일반·성상·주성분·보관방법·유효기간·ATC 등을 반환한다. 효능·용법·주의사항 원문은 source_url(의약품안전나라)에서 확인."""
    if not (item_name or entp_name or item_seq):
        raise ToolError("item_name, entp_name, item_seq 중 하나 이상은 필요합니다.")
    if not DATA_GO_KR_KEY:
        raise ToolError("DATA_GO_KR_KEY 가 설정되지 않았습니다.")
    guard_query(item_name, entp_name, item_seq)
    params: dict[str, Any] = {"serviceKey": DATA_GO_KR_KEY, "pageNo": page, "numOfRows": max_results, "type": "json"}
    if item_name:
        params["item_name"] = item_name
    if entp_name:
        params["entp_name"] = entp_name
    if item_seq:
        params["item_seq"] = item_seq
    raw = await fetch(MFDS_PERMIT_URL, params=params, as_="text")
    if raw.lstrip().startswith("<"):  # 인증 오류 등은 XML 로 내려옴
        msg = ""
        try:
            root = fromstring(raw)
            msg = " ".join(t.text or "" for t in root.iter() if t.tag in ("returnAuthMsg", "resultMsg", "errMsg")).strip()
        except Exception:  # noqa: BLE001
            pass
        raise ToolError("식약처 API 가 오류를 반환했습니다: " + (msg or "인증키/활용신청 상태 확인 필요"))
    try:
        payload = json.loads(raw)
    except ValueError:
        raise ToolError("식약처 API 응답을 JSON으로 해석하지 못했습니다.") from None
    total, items = parse_mfds_items(payload)
    rows = []
    for it in items:
        ingr = " ".join(str(it.get(k, "")) for k in ("MAIN_ITEM_INGR", "INGR_NAME", "MATERIAL_NAME"))
        if ingredient_contains and ingredient_contains.lower() not in ingr.lower():
            continue
        seq = it.get("ITEM_SEQ")
        rows.append(
            {
                "item_seq": seq,
                "item_name": it.get("ITEM_NAME"),
                "company": it.get("ENTP_NAME"),
                "permit_date": it.get("ITEM_PERMIT_DATE"),
                "etc_otc": it.get("ETC_OTC_CODE"),
                "appearance": truncate(it.get("CHART") or "", 200) or None,
                "main_ingredient": truncate(it.get("MAIN_ITEM_INGR") or it.get("INGR_NAME") or "", 300) or None,
                "material": truncate(it.get("MATERIAL_NAME") or "", 300) or None,
                "storage": truncate(it.get("STORAGE_METHOD") or "", 150) or None,
                "valid_term": it.get("VALID_TERM"),
                "pack_unit": truncate(it.get("PACK_UNIT") or "", 150) or None,
                "atc_code": it.get("ATC_CODE"),
                "detail_url": f"https://nedrug.mfds.go.kr/pbp/CCBBB01/getItemDetail?itemSeq={seq}" if seq else None,
            }
        )
    return envelope(
        "식품의약품안전처 의약품 제품 허가정보 (공공데이터포털)",
        {"total_count": total, "page": page, "products": rows},
        source_url="https://nedrug.mfds.go.kr",
        note="허가사항(효능·용법·주의사항)·변경이력은 detail_url(의약품안전나라)에서 최신본을 확인하세요.",
    )


# =========================================================================== 국가법령정보
def _first_list_of_dicts(node: Any) -> list[dict[str, Any]]:
    """검색 응답 JSON 은 최상위 키가 target 별로 달라서(LawSearch/AdmRulSearch…) 첫 dict 리스트를 찾는다."""
    if isinstance(node, list) and node and all(isinstance(x, dict) for x in node):
        return node
    if isinstance(node, dict):
        for v in node.values():
            found = _first_list_of_dicts(v)
            if found:
                return found
        # 결과가 1건이면 리스트가 아니라 dict 로 내려오는 경우
        if any(k in node for k in ("법령명한글", "행정규칙명")):
            return [node]
    return []


def collect_law_text(node: Any, out: list[str] | None = None) -> list[str]:
    """법령/행정규칙 본문 JSON 에서 '...내용' 키의 문자열을 순서대로 수집."""
    out = [] if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            if "내용" in k and isinstance(v, str):
                out.append(" ".join(v.split()))
            elif "내용" in k and isinstance(v, list):
                out.extend(" ".join(x.split()) for x in v if isinstance(x, str))
                for x in v:
                    if not isinstance(x, str):
                        collect_law_text(x, out)
            else:
                collect_law_text(v, out)
    elif isinstance(node, list):
        for x in node:
            collect_law_text(x, out)
    return out


@mcp.tool()
@safe_tool
async def law_search(
    query: Annotated[str, Field(description="법령명/고시명/키워드. 예: '약사법', '의약품 품목허가 신고 심사 규정', '의약품동등성시험기준'")],
    target: Annotated[Literal["law", "admrul"], Field(description="law=법령(법·시행령·시행규칙), admrul=행정규칙(고시·훈령·예규)")] = "admrul",
    max_results: Annotated[int, Field(ge=1, le=30)] = 10,
) -> dict[str, Any]:
    """국가법령정보센터에서 법령 또는 행정규칙(식약처 고시 등)을 검색해 명칭·소관부처·공포/발령일·시행일·doc_id 를 반환한다. 본문은 law_get_text 로 조회."""
    if not LAW_OC:
        raise ToolError("LAW_OC 가 설정되지 않았습니다.")
    guard_query(query)
    js = await fetch(f"{LAW_BASE}/lawSearch.do", params={"OC": LAW_OC, "target": target, "type": "JSON", "query": query, "display": max_results})
    rows = []
    for d in _first_list_of_dicts(js):
        if target == "law":
            rows.append({"name": d.get("법령명한글"), "doc_id": d.get("법령일련번호"), "ministry": d.get("소관부처명"), "promulgated": d.get("공포일자"), "effective": d.get("시행일자"), "kind": d.get("법령구분명"), "link": d.get("법령상세링크")})
        else:
            rows.append({"name": d.get("행정규칙명"), "doc_id": d.get("행정규칙일련번호"), "ministry": d.get("소관부처명"), "issued": d.get("발령일자"), "effective": d.get("시행일자"), "kind": d.get("행정규칙종류"), "link": d.get("행정규칙상세링크")})
    if not rows:
        return envelope("국가법령정보센터", {"results": []}, note="검색 결과가 없습니다. 법령명을 줄이거나 target(law/admrul)을 바꿔 보세요.")
    return envelope("국가법령정보센터 Open API", {"results": rows}, source_url="https://www.law.go.kr", note="동일 명칭의 이전 버전이 함께 나올 수 있으므로 시행일자가 가장 최신인 항목을 확인하세요.")


@mcp.tool()
@safe_tool
async def law_get_text(
    doc_id: Annotated[str, Field(description="law_search 결과의 doc_id(법령일련번호 또는 행정규칙일련번호)")],
    target: Annotated[Literal["law", "admrul"], Field(description="law_search 에서 사용한 target 과 동일하게")] = "admrul",
    keyword: Annotated[str | None, Field(description="지정하면 해당 키워드가 들어간 조문만 발췌(권장)")] = None,
    max_chars: Annotated[int, Field(ge=1000, le=30000)] = 8000,
) -> dict[str, Any]:
    """법령/행정규칙의 조문 본문을 반환한다. 긴 규정은 keyword 로 관련 조문만 발췌할 것."""
    if not LAW_OC:
        raise ToolError("LAW_OC 가 설정되지 않았습니다.")
    if not re.fullmatch(r"\d{1,12}", doc_id):
        raise ToolError("doc_id 는 숫자여야 합니다.")
    guard_query(keyword)
    params = {"OC": LAW_OC, "target": target, "type": "JSON", ("MST" if target == "law" else "ID"): doc_id}
    js = await fetch(f"{LAW_BASE}/lawService.do", params=params, cache_ttl=3600)
    paras = collect_law_text(js)
    if not paras:
        raise NotFound("본문을 찾지 못했습니다. doc_id/target 조합을 확인하세요.")
    if keyword:
        paras = [p for p in paras if keyword in p]
        if not paras:
            return envelope("국가법령정보센터 Open API", {"doc_id": doc_id, "excerpts": []}, note=f"'{keyword}' 가 포함된 조문이 없습니다.")
    full = "\n".join(paras)
    return envelope("국가법령정보센터 Open API", {"doc_id": doc_id, "excerpts": full[:max_chars], "truncated": len(full) > max_chars}, source_url="https://www.law.go.kr")


# =========================================================================== FDA IIG (Gemini Enterprise 데이터 스토어)
def _pick(row: dict[str, Any], key: str) -> str:
    return str(row.get(key) or "")


def filter_iig_rows(rows: list[dict[str, Any]], ingredient: str, route: str | None, dosage_form: str | None) -> list[dict[str, Any]]:
    """검색 엔진(의미/키워드 검색)이 돌려준 후보를 '부분 문자열 일치'로 다시 걸러 정확도를 보장한다."""
    ing, rt, fm = ingredient.upper(), (route or "").upper(), (dosage_form or "").upper()
    out = []
    for r in rows:
        if ing not in _pick(r, IIG_FIELD_INGREDIENT).upper():
            continue
        if rt and rt not in _pick(r, IIG_FIELD_ROUTE).upper():
            continue
        if fm and fm not in _pick(r, IIG_FIELD_FORM).upper():
            continue
        out.append(r)
    return out


_de_client = None
IIG_DS_MAX_SCAN = int(os.environ.get("IIG_DS_MAX_SCAN", "500"))  # 검색 결과를 최대 몇 건까지 훑을지


def _de():
    global _de_client
    if _de_client is None:
        from google.api_core.client_options import ClientOptions
        from google.cloud import discoveryengine_v1 as de

        endpoint = "discoveryengine.googleapis.com" if IIG_DS_LOCATION == "global" else f"{IIG_DS_LOCATION}-discoveryengine.googleapis.com"
        _de_client = de.SearchServiceClient(client_options=ClientOptions(api_endpoint=endpoint))
    return _de_client


def _query_iig(ingredient: str, route: str | None, dosage_form: str | None, limit: int) -> tuple[list[dict[str, Any]], str | None, bool]:
    from google.cloud import discoveryengine_v1 as de

    if not (IIG_DS_PROJECT and IIG_DATASTORE_ID):
        raise ToolError("IIG_DS_PROJECT / IIG_DATASTORE_ID 가 설정되지 않았습니다.")
    serving_config = (
        f"projects/{IIG_DS_PROJECT}/locations/{IIG_DS_LOCATION}/collections/default_collection/dataStores/{IIG_DATASTORE_ID}/servingConfigs/default_config"
    )
    query = " ".join(x for x in (ingredient, route, dosage_form) if x)
    req = de.SearchRequest(serving_config=serving_config, query=query, page_size=100)
    pager = _de().search(req)

    scanned: list[dict[str, Any]] = []
    exhausted = True
    for res in pager:  # 페이지를 자동으로 넘어가며 순회
        d = de.Document.to_dict(res.document)
        row = d.get("struct_data") or d.get("derived_struct_data") or {}
        if row:
            scanned.append(row)
        if len(scanned) >= IIG_DS_MAX_SCAN:
            exhausted = False
            break
    matched = filter_iig_rows(scanned, ingredient, route, dosage_form)
    # 후보를 다 못 훑었거나 결과가 limit 를 넘으면 '불완전 가능성'을 알린다.
    incomplete = (not exhausted) or len(matched) > limit
    desc = None
    for r in matched:  # prepare_iig_for_datastore.py 가 모든 행에 기록한 기준일/출처
        if r.get("data_date"):
            desc = f"기준일/버전: {r.get('data_date')} | 출처: {r.get('source_url', '')}"
            break
    return matched[:limit], desc, incomplete


@mcp.tool()
@safe_tool
async def iig_lookup(
    ingredient: Annotated[str, Field(description="부형제 성분명(영문, 일부 가능). 예: 'POVIDONE', 'MAGNESIUM STEARATE'")],
    route: Annotated[str | None, Field(description="투여경로. 예: 'ORAL'")] = None,
    dosage_form: Annotated[str | None, Field(description="제형. 예: 'TABLET', 'CAPSULE'")] = None,
    max_results: Annotated[int, Field(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """FDA IIG(Inactive Ingredient Database) 데이터 스토어 적재본에서 부형제의 투여경로·제형별 최대 사용량(potency/maximum daily exposure)을 조회한다."""
    guard_query(ingredient, route, dosage_form)
    rows, desc, incomplete = await asyncio.to_thread(_query_iig, ingredient, route, dosage_form, max_results)
    label = "Gemini Enterprise 데이터 스토어 적재본"
    if not rows:
        return envelope(
            f"FDA IIG ({label})",
            {"rows": []},
            note="일치하는 항목이 없습니다. 영문 성분명 철자/염 형태를 바꿔 보세요. (데이터 스토어는 검색 기반이라 후보 상위 일부만 훑습니다. 투여경로·제형 조건을 빼고 다시 시도해 보세요.)",
        )
    note = "IIG 는 FDA 승인 의약품에 사용된 선례 수준(최대 사용량)이며, 안전성 보증 한도나 국내 허가 근거가 아닙니다. table_description 의 기준일을 함께 인용하세요."
    if incomplete:
        note += " ※ 조건에 맞는 행이 더 있을 수 있습니다(결과 일부만 표시). 투여경로·제형을 지정해 범위를 좁혀 다시 조회하세요."
    return envelope(
        f"FDA Inactive Ingredient Database ({label})",
        {"rows": rows, "table_description": desc, "possibly_incomplete": incomplete},
        source_url="https://www.accessdata.fda.gov/scripts/cder/iig/index.cfm",
        note=note,
    )


if __name__ == "__main__":
    run(mcp)
