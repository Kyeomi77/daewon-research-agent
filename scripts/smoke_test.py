#!/usr/bin/env python3
"""배포(또는 로컬 실행)한 MCP 서버에 붙어 도구 목록을 출력하고 샘플 호출을 수행한다.

로컬:  SERVER_MODULE=chem_server PORT=8082 python -m src.chem_server &
       python scripts/smoke_test.py --url http://localhost:8082/mcp --suite chem
Cloud Run(IAM 인증):
       python scripts/smoke_test.py --url https://daewon-mcp-chem-xxxx.a.run.app/mcp \
              --token "$(gcloud auth print-identity-token)" --suite chem
"""
import argparse
import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

SUITES = {
    "papers": [
        ("search_pubmed", {"query": "itraconazole solid dispersion bioavailability", "max_results": 3}),
        ("estimate_patent_expiry", {"filing_date": "2015-03-10"}),
        ("search_patents_bq", {"keywords": ["itraconazole", "solid dispersion"], "from_year": 2015, "dry_run": True}),
        ("search_kipris", {"keyword": "이트라코나졸 고체분산체", "max_results": 3}),
    ],
    "chem": [
        ("pubchem_properties", {"identifier": "itraconazole"}),
        ("chembl_activity", {"molecule": "gefitinib", "standard_type": "IC50", "max_results": 5}),
        ("dailymed_label", {"drug_name": "atorvastatin", "sections": ["inactive_ingredient"]}),
    ],
    "reg": [
        ("mfds_product_search", {"entp_name": "대원제약", "max_results": 3}),
        ("law_search", {"query": "의약품 품목허가 신고 심사 규정", "target": "admrul", "max_results": 3}),
        ("iig_lookup", {"ingredient": "POVIDONE", "route": "ORAL", "dosage_form": "TABLET", "max_results": 3}),
    ],
}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--token", default=None, help="Cloud Run IAM 인증용 ID 토큰")
    ap.add_argument("--suite", choices=SUITES, required=True)
    args = ap.parse_args()
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else None

    async with streamablehttp_client(args.url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("== tools:", [t.name for t in tools.tools])
            for name, kwargs in SUITES[args.suite]:
                res = await session.call_tool(name, kwargs)
                payload = res.structuredContent or json.loads(res.content[0].text)
                payload = payload.get("result", payload) if isinstance(payload, dict) else payload
                status = "ERROR" if isinstance(payload, dict) and "error" in payload else "OK"
                print(f"\n== {name} [{status}]")
                print(json.dumps(payload, ensure_ascii=False, indent=2)[:1500])


if __name__ == "__main__":
    asyncio.run(main())
