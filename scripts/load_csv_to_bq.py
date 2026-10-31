#!/usr/bin/env python3
"""FDA IIG / 니트로사민 AI limit 표 등 '정기 다운로드 데이터'를 BigQuery 테이블로 적재한다.

사용 예 (FDA IIG: 사이트에서 내려받은 CSV, xlsx 라면 CSV 로 저장 후 사용)
  python scripts/load_csv_to_bq.py \
      --csv data/iig_2026-10.csv \
      --table my-project.research_ref.fda_iig \
      --data-date 2026-10 \
      --source-url https://www.accessdata.fda.gov/scripts/cder/iig/index.cfm

- 헤더는 소문자/스네이크 케이스로 정규화됩니다(예: 'Ingredient Name' -> ingredient_name).
- 매번 WRITE_TRUNCATE 로 전체 교체하며, 테이블 description 에 기준일·출처를 기록합니다
  (iig_lookup 응답의 table_description 으로 에이전트가 기준일을 인용).
"""
import argparse
import csv
import re
import sys
import tempfile
from pathlib import Path

from google.cloud import bigquery


def norm(h: str) -> str:
    h = re.sub(r"[^0-9a-zA-Z]+", "_", h.strip().lower()).strip("_")
    return h if h and not h[0].isdigit() else f"c_{h}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, type=Path)
    ap.add_argument("--table", required=True, help="project.dataset.table")
    ap.add_argument("--data-date", required=True, help="원본 데이터 기준일/버전 (예: 2026-10)")
    ap.add_argument("--source-url", required=True)
    ap.add_argument("--project", default=None)
    ap.add_argument("--location", default="asia-northeast3", help="데이터셋 위치(기존 데이터셋이 있으면 그 위치)")
    args = ap.parse_args()

    project, dataset, _ = args.table.split(".")
    client = bigquery.Client(project=args.project or project)
    ds = bigquery.Dataset(f"{project}.{dataset}")
    ds.location = args.location
    client.create_dataset(ds, exists_ok=True)

    with args.csv.open(newline="", encoding="utf-8-sig") as src, tempfile.NamedTemporaryFile("w", newline="", suffix=".csv", delete=False, encoding="utf-8") as tmp:
        reader = csv.reader(src)
        writer = csv.writer(tmp)
        header = next(reader)
        cols = [norm(h) for h in header]
        if len(set(cols)) != len(cols):
            sys.exit(f"정규화 후 컬럼명이 중복됩니다: {cols}")
        writer.writerow(cols)
        n = 0
        for row in reader:
            writer.writerow(row)
            n += 1
        tmp_path = tmp.name

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.CSV,
        skip_leading_rows=1,
        autodetect=True,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        allow_quoted_newlines=True,
    )
    with open(tmp_path, "rb") as f:
        client.load_table_from_file(f, args.table, job_config=job_config).result()

    table = client.get_table(args.table)
    table.description = f"기준일/버전: {args.data_date} | 출처: {args.source_url} | 적재 행수: {n}"
    client.update_table(table, ["description"])
    print(f"OK: {args.table} <- {n} rows ({args.data_date}) columns={cols}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
