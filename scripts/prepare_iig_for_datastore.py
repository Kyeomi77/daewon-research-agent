#!/usr/bin/env python3
"""FDA IIG CSV → Gemini Enterprise 구조화 데이터 스토어용 파일 준비(+선택: GCS 업로드, 가져오기 실행).

1) 정규화: 헤더를 snake_case 로, 모든 행에 data_date / source_url 컬럼 추가(응답에서 기준일 인용용)
2) 출력: JSON Lines(.jsonl, 'custom' 스키마용) 와 CSV(.csv, 'csv' 스키마용) 둘 다 생성
3) --gcs-uri 지정 시 업로드, --import-to 지정 시 데이터 스토어로 가져오기(FULL = 전체 교체)

사용 예
  python scripts/prepare_iig_for_datastore.py --csv data/iig.csv --data-date 2026-10 \
      --out-dir data/iig_out \
      --gcs-uri gs://uk-adc-core-geminienterprise-rdkit-storage/iig_2026-10.jsonl \
      --import-to projects/uk-adc-core-geminienterprise/locations/global/collections/default_collection/dataStores/daewon-research-ds_1794032917835

--import-to 는 문서 가져오기 REST(documents:import)를 호출합니다(gcloud 인증 사용).
처음에는 --gcs-uri 만 주고 콘솔(데이터 스토어 만들기 → Cloud Storage → 구조화 데이터)에서 가져오기를 해도 됩니다.
"""
import argparse
import csv
import json
import re
import subprocess
import sys
from pathlib import Path


def norm(h: str) -> str:
    h = re.sub(r"[^0-9a-zA-Z]+", "_", h.strip().lower()).strip("_")
    return h if h and not h[0].isdigit() else f"c_{h}"


def convert(src: Path, out_dir: Path, data_date: str, source_url: str) -> tuple[Path, Path, int, list[str]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / (src.stem + ".jsonl")
    csv_path = out_dir / (src.stem + "_normalized.csv")
    n = 0
    with src.open(newline="", encoding="utf-8-sig") as f_in:
        reader = csv.reader(f_in)
        cols = [norm(h) for h in next(reader)]
        if len(set(cols)) != len(cols):
            sys.exit(f"정규화 후 컬럼명이 중복됩니다: {cols}")
        out_cols = cols + ["data_date", "source_url"]
        with jsonl_path.open("w", encoding="utf-8") as fj, csv_path.open("w", newline="", encoding="utf-8") as fc:
            w = csv.writer(fc)
            w.writerow(out_cols)
            for row in reader:
                if not any(c.strip() for c in row):
                    continue
                row = (row + [""] * len(cols))[: len(cols)]
                rec = {c: v.strip() for c, v in zip(cols, row)}
                rec["data_date"] = data_date
                rec["source_url"] = source_url
                fj.write(json.dumps(rec, ensure_ascii=False) + "\n")
                w.writerow([rec[c] for c in out_cols])
                n += 1
    return jsonl_path, csv_path, n, out_cols


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, type=Path)
    ap.add_argument("--data-date", required=True)
    ap.add_argument("--source-url", default="https://www.accessdata.fda.gov/scripts/cder/iig/index.cfm")
    ap.add_argument("--out-dir", type=Path, default=Path("data/iig_out"))
    ap.add_argument("--gcs-uri", help="업로드할 JSONL 의 gs:// 경로 (예: gs://bucket/iig/iig_2026-10.jsonl)")
    ap.add_argument("--import-to", help="projects/P/locations/L/collections/default_collection/dataStores/DS_ID")
    ap.add_argument("--schema", choices=["custom", "csv"], default="custom", help="가져오기 데이터 스키마(기본 custom=JSONL)")
    args = ap.parse_args()

    jsonl_path, csv_path, n, cols = convert(args.csv, args.out_dir, args.data_date, args.source_url)
    print(f"OK: {n} rows, columns={cols}\n  {jsonl_path}\n  {csv_path}")
    size_mb = jsonl_path.stat().st_size / 1e6
    print(f"  JSONL {size_mb:.1f} MB (구조화 JSONL 한도: 파일당 2GB, 행당 1MB)")

    if args.gcs_uri:
        if not args.gcs_uri.startswith("gs://"):
            sys.exit("--gcs-uri 는 gs:// 로 시작해야 합니다.")
        local = jsonl_path if args.schema == "custom" else csv_path
        uri = args.gcs_uri if args.schema == "custom" else re.sub(r"\.jsonl$", ".csv", args.gcs_uri)
        subprocess.run(["gcloud", "storage", "cp", str(local), uri], check=True)
        print(f"업로드 완료: {uri}")

        if args.import_to:
            token = subprocess.run(["gcloud", "auth", "print-access-token"], check=True, capture_output=True, text=True).stdout.strip()
            m = re.search(r"/locations/([^/]+)/", args.import_to)
            loc = m.group(1) if m else "global"
            host = "discoveryengine.googleapis.com" if loc == "global" else f"{loc}-discoveryengine.googleapis.com"
            body = {"gcsSource": {"inputUris": [uri], "dataSchema": args.schema}, "reconciliationMode": "FULL"}
            url = f"https://{host}/v1/{args.import_to}/branches/0/documents:import"
            res = subprocess.run(
                ["curl", "-sS", "-X", "POST", url, "-H", f"Authorization: Bearer {token}", "-H", "Content-Type: application/json", "-d", json.dumps(body)],
                check=True, capture_output=True, text=True,
            )
            print("가져오기 요청 결과(오퍼레이션):", res.stdout[:600])
            print("진행 상황은 콘솔의 데이터 스토어 > Activity 탭에서 확인하세요. 완료 후 문서 수가 위 행 수와 같은지 대조하세요.")
    else:
        print("다음 단계: JSONL 을 GCS 에 올리고 콘솔에서 가져오기(README 4-B 참고).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
