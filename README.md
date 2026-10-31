# 대원제약 중앙연구소 외부정보 조사 에이전트 — 개발 가이드

Gemini Enterprise **Agent Designer**(노코드)로 에이전트를 만들고, 외부 공공 DB 연동은 **Cloud Run 위의 MCP 서버 3개**가 맡는 구조입니다.

```
연구원 → Gemini Enterprise 웹앱
          └ 라우터 에이전트 (Agent Designer)
              ├ 논문·특허 에이전트 ─ MCP#1 (daewon-mcp-papers) + Google Search
              └ 약물·물성·규제 에이전트 ─ MCP#2 (daewon-mcp-chem) + MCP#3 (daewon-mcp-reg) + Google Search(도메인 한정)
```

| 구성요소 | 위치 | 제공 도구 |
|---|---|---|
| MCP#1 논문·특허 | `src/papers_patents_server.py` | `search_pubmed` `get_pmc_fulltext` `search_kipris` `search_patents_bq` `get_patent_claims` `estimate_patent_expiry` |
| MCP#2 물성·활성·라벨 | `src/chem_server.py` | `pubchem_properties` `chembl_search_target` `chembl_activity` `dailymed_label` |
| MCP#3 허가·법령·IIG | `src/regulatory_server.py` | `mfds_product_search` `law_search` `law_get_text` `iig_lookup` |
| 공통(가드·감사로그·재시도) | `src/common.py` | 사내 코드 차단, 감사 로그, 레이트리밋 |
| 에이전트 지침 | `prompts/*.md` | Agent Designer 에 붙여넣을 Instructions |
| 배포/적재/점검 | `scripts/` | `deploy.sh` `load_csv_to_bq.py` `prepare_iig_for_datastore.py` `smoke_test.py` |
| 평가 | `tests/golden_set.csv`, `tests/test_parsers.py` | 골든셋 15문항, 단위 테스트 11개 |

> **검증 상태**: 파서·가드·계산기 단위 테스트(11개) 통과, 3개 서버가 기동되어 MCP(Streamable HTTP)로 도구 호출되는 것까지 확인했습니다. **실제 외부 API(NCBI, KIPRIS, 공공데이터포털, 법령정보 등) 호출은 작성 환경에서 외부 접속이 막혀 테스트하지 못했습니다.** 아래 8장의 "확인 필요 항목"을 키 발급 후 `smoke_test.py`로 반드시 검증하세요.

---

## 1. 개발 절차 한눈에 보기

| 단계 | 내용 | 산출물/완료 기준 |
|---|---|---|
| 0 | 사전 준비: GCP 프로젝트, Gemini Enterprise 앱, 권한, API 키 | 키 4종, 앱 생성 |
| 1 | 로컬 개발·테스트: 서버 실행 → `smoke_test.py` | 도구 호출 성공 |
| 2 | IIG 등 정기 데이터 BigQuery 적재 | `iig_lookup` 성공 |
| 3 | Cloud Run 배포 (`deploy.sh`) | `/mcp` URL 3개 |
| 4 | Gemini Enterprise 에 Custom MCP Server 데이터 스토어 등록 | 데이터 스토어 3개, 도구 활성화 |
| 5 | Agent Designer 로 에이전트 3개 구성 | 에이전트 공유 |
| 6 | 골든셋 평가 → 프롬프트·도구 개선 | 정확도·출처 누락률 기준 충족 |
| 7 | 운영: 로그 점검, 키 로테이션, 데이터 갱신 | 월간 점검 |

## 2. 사전 준비 (0단계)

**계정/권한**
- GCP 프로젝트(결제 연결), Gemini Enterprise 앱(이미 있으면 재사용)
- 본인에게: Cloud Run Admin, Cloud Build Editor, Service Account Admin, Secret Manager Admin, BigQuery Admin(또는 데이터셋 생성 권한), Discovery Engine Editor(데이터 스토어 등록용)
- 조직 정책 **"Disable custom mcp server connector for gemini enterprise"** 가 *Not enforced* 인지 확인(막혀 있으면 데이터 스토어 생성이 거부됨)

**API 키 발급 (모두 무료)**
| 키 | 발급처 | 환경변수 |
|---|---|---|
| NCBI API Key (선택, 속도 3→10 req/s) | NCBI 계정 설정 | `NCBI_API_KEY` |
| KIPRIS Plus accessKey | plus.kipris.or.kr 회원가입 → 특허·실용신안 검색 서비스 신청 | `KIPRIS_ACCESS_KEY` |
| 공공데이터포털 인증키 | data.go.kr → "식품의약품안전처_의약품 제품 허가정보" 활용신청 (**Decoding 키** 사용) | `DATA_GO_KR_KEY` |
| 국가법령정보 OC | open.law.go.kr Open API 신청(**호출 서버 IP 등록 필요**, 7장) | `LAW_OC` |

**사내 코드 차단 규칙(중요)**: `.env`/`deploy.sh` 의 `BLOCK_PATTERNS` 에 대원제약 개발코드·후보물질 코드의 정규식을 반드시 넣으세요. 이 규칙에 걸린 질의는 외부 API로 나가지 않습니다(특허 신규성·정보 유출 방지).

## 3. 로컬 개발·테스트 (1단계)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest
cp .env.example .env      # 값 채우기 후:  set -a; source .env; set +a

pytest -q                 # 오프라인 단위 테스트

# 서버별 실행(포트 분리)
SERVER_MODULE=papers_patents_server PORT=8081 python -m src.papers_patents_server &
SERVER_MODULE=chem_server           PORT=8082 python -m src.chem_server &
SERVER_MODULE=regulatory_server     PORT=8083 python -m src.regulatory_server &

python scripts/smoke_test.py --url http://localhost:8081/mcp --suite papers
python scripts/smoke_test.py --url http://localhost:8082/mcp --suite chem
python scripts/smoke_test.py --url http://localhost:8083/mcp --suite reg
```

BigQuery 도구(`search_patents_bq`, `iig_lookup`)는 로컬에서 `gcloud auth application-default login` 후 동작합니다. `search_patents_bq` 는 `dry_run=true` 로 먼저 **예상 스캔량**을 보고, 한도(`BQ_MAX_BYTES_BILLED`, 기본 300GB)를 넘으면 실행을 거부합니다.

## 4. 정기 데이터 적재 — FDA IIG (2단계)

FDA IIG 는 **BigQuery** 또는 **Gemini Enterprise 데이터 스토어** 중 하나에 적재할 수 있고, `IIG_BACKEND` 환경변수로 선택합니다. 두 경우 모두 FDA 사이트에서 데이터 파일을 내려받아(xlsx면 CSV로 저장) 시작하며, 컬럼명은 자동 정규화(`Ingredient Name` → `ingredient_name`)됩니다. `iig_lookup` 은 기본적으로 `ingredient_name`, `route`, `dosage_form` 컬럼이 있다고 가정하므로 **적재 후 실제 컬럼명을 확인**하고 다르면 `IIG_FIELD_*` 환경변수로 맞추세요.

| 비교 | A. BigQuery (`IIG_BACKEND=bigquery`) | B. 데이터 스토어 (`IIG_BACKEND=datastore`) |
|---|---|---|
| 조회 방식 | SQL 조건 검색 → 조건에 맞는 행을 **전수** 조회 | 검색(Search API) 결과 상위 N건을 훑은 뒤 코드에서 재필터 → 행이 많으면 **일부 누락 가능**(응답에 `possibly_incomplete` 표시) |
| 수치 신뢰도 | 높음(결정적) | 중간(검색 랭킹 의존, 투여경로·제형으로 좁히면 개선) |
| 사용 편의 | 테이블 적재 + SQL 권한 | 콘솔에서 가져오기만 하면 됨, BigQuery 불필요 |
| 에이전트 직접 연결 | 불가(MCP 도구 경유) | **가능**(MCP 없이 Agent Designer 에서 데이터 스토어를 바로 연결 — 4-B 참고) |
| 기준일 표기 | 테이블 description | 각 행의 `data_date` 컬럼 |
| 권한 | `bigquery.dataViewer` | `discoveryengine.viewer` |

> **권장**: 최대 사용량처럼 *정확한 값이 중요한 조회*는 A(BigQuery). BigQuery를 쓰지 않거나 설정을 단순하게 하고 싶다면 B로 시작하고, 정확도는 골든셋 9번 문항(포비돈 등)으로 검증하세요.

### 4-A. BigQuery 적재
```bash
python scripts/load_csv_to_bq.py --csv data/iig.csv \
  --table <PROJECT>.research_ref.fda_iig --data-date 2026-10 \
  --source-url https://www.accessdata.fda.gov/scripts/cder/iig/index.cfm
bq show --schema --format=prettyjson <PROJECT>:research_ref.fda_iig | head -50
```

### 4-B. Gemini Enterprise 데이터 스토어 적재
공식 문서 기준, Cloud Storage 의 **구조화 데이터는 JSON Lines/NDJSON(파일당 2GB·행당 1MB 미만, 요청당 최대 1,000파일) 또는 헤더가 있는 CSV** 로 가져올 수 있고, 스키마는 자동 감지됩니다.

1. 파일 준비(정규화 + 모든 행에 `data_date`/`source_url` 추가, JSONL·CSV 동시 생성)
   ```bash
   python scripts/prepare_iig_for_datastore.py --csv data/iig.csv --data-date 2026-10 \
     --out-dir data/iig_out --gcs-uri gs://<BUCKET>/iig/iig_2026-10.jsonl
   ```
2. 콘솔: Gemini Enterprise → **Data stores → Create data store → Cloud Storage** → 데이터 유형 **구조화 데이터(JSONL)** 선택 → 위 gs:// 경로 지정 → 1회 가져오기(one-time) 선택 → 생성
   (스크립트로 가져오려면 `--import-to projects/<P>/locations/global/collections/default_collection/dataStores/<DS_ID>` 를 추가. `reconciliationMode=FULL` 이라 갱신 시 이전 문서가 교체됩니다.)
3. 가져오기가 끝나면 데이터 스토어의 **문서 수 = 스크립트가 출력한 행 수** 인지 대조하고, 스키마의 필드명을 확인
4. 사용 방법 두 가지 중 선택
   - **(권장) MCP 도구 경유**: `IIG_BACKEND=datastore IIG_DS_PROJECT=<P> IIG_DS_LOCATION=global IIG_DATASTORE_ID=<DS_ID>` 로 배포(`deploy.sh` 가 `roles/discoveryengine.viewer` 부여). 에이전트 지침·출력 형식이 BigQuery 방식과 동일.
   - **에이전트에 직접 연결**: 데이터 스토어를 Gemini Enterprise 앱에 연결한 뒤 Agent Designer 에서 해당 데이터 스토어를 에이전트의 도구로 지정. 코드는 필요 없지만, 자연어 검색 결과를 모델이 요약하는 방식이라 **수치·행 누락 위험이 더 큽니다** — 지침에 "IIG 값은 검색된 행의 원문 값만 표로 인용, 행이 많으면 투여경로·제형을 되묻기"를 추가하세요.
5. 갱신(분기 1회 권장): 같은 명령을 새 `--data-date` 로 재실행(FULL 교체). 데이터 스토어의 *주기적 가져오기*(1·3·5일)는 공개 미리보기이므로 FDA 갱신 주기에는 수동 갱신으로 충분합니다.

같은 방식으로 니트로사민 AI limit 표 등 다른 정기 자료도 적재해 비슷한 도구로 확장할 수 있습니다.

## 5. Cloud Run 배포 (3단계)

```bash
gcloud auth login && gcloud config set project <PROJECT_ID>
export PROJECT_ID=<PROJECT_ID> CONTACT_EMAIL=<연구소IT메일> \
       BLOCK_PATTERNS='\bDW[-_ ]?\d{3,}\b'      # ← 사내 코드 정규식으로 교체

./scripts/deploy.sh all         # 또는 papers | chem | reg

# 최초 1회: 시크릿 값 등록(스크립트가 빈 시크릿을 만들어 줍니다)
echo -n '<KIPRIS_KEY>'    | gcloud secrets versions add kipris-access-key --data-file=-
echo -n '<NCBI_KEY>'      | gcloud secrets versions add ncbi-api-key --data-file=-
echo -n '<DATA_GO_KR_KEY>'| gcloud secrets versions add data-go-kr-key --data-file=-
echo -n '<LAW_OC>'        | gcloud secrets versions add law-oc --data-file=-
./scripts/deploy.sh all         # 시크릿 등록 후 재배포(새 버전 반영)
```

- 서비스는 `--no-allow-unauthenticated`(비공개)이며, Gemini Enterprise 의 Discovery Engine 서비스 에이전트에만 `roles/run.invoker` 를 부여합니다.
- IIG 읽기 권한: BigQuery 방식이면 `mcp-reg` 서비스 계정에 해당 **데이터셋** `roles/bigquery.dataViewer`, 데이터 스토어 방식이면 `roles/discoveryengine.viewer`(`deploy.sh` 가 자동 부여). 데이터 스토어 방식 배포: `IIG_BACKEND=datastore IIG_DATASTORE_ID=<DS_ID> ./scripts/deploy.sh reg`
- 배포 후 IAM 토큰으로 점검:
  ```bash
  python scripts/smoke_test.py --url https://<서비스URL>/mcp --token "$(gcloud auth print-identity-token)" --suite chem
  ```

## 6. Gemini Enterprise 설정 (4~5단계)

> 콘솔 메뉴명·화면은 버전에 따라 다를 수 있습니다. 아래는 공식 문서 기준 흐름이며, 막히면 "Set up custom MCP server" 문서를 따르세요.

**A. Agent Designer 활성화** — Gemini Enterprise 앱 → Configurations → Feature Management → *Enable agent designer* → Save

**B. Custom MCP Server 데이터 스토어 3개 등록** (Gemini Enterprise → Data stores → 데이터 소스 선택에서 *Custom MCP Server (Preview)*)
1. MCP Server URL: `https://<서비스>/mcp` (HTTPS, **StreamableHTTP 만 지원**)
2. 설명: `prompts/mcp_server_descriptions.md` 의 문구 사용
3. 인증 정보 입력(Cloud Run IAM 방식이면 5장의 invoker 권한이 필요. 화면에 Authorization/Token URL 을 요구하면 7장의 "인증" 항목 참고)
4. 생성 후 데이터 스토어를 **앱에 연결**하고, 기본값이 *비활성*인 **도구/액션을 직접 활성화**
5. 이그레스 FQDN 허용 목록에 3개 서비스 도메인 추가(요구되는 경우)

**C. 에이전트 구성(Agent Designer)** — 웹앱에서 에이전트 만들기

| 에이전트 | Instructions | 연결할 도구 | 비고 |
|---|---|---|---|
| 논문·특허 | `prompts/papers_patents_agent.md` | daewon-mcp-papers, Google Search | |
| 약물·물성·규제 | `prompts/drug_reg_agent.md` | daewon-mcp-chem, daewon-mcp-reg, Google Search | Search 는 허용 도메인을 지침으로 제한 |
| 라우터 | `prompts/router_agent.md` | 위 두 에이전트(지원 시) | 하위 에이전트 호출이 안 되면 라우터 생략하고 2개를 각각 공유 + 사용 가이드 배포 |

각 에이전트마다 *Preview/테스트 → 공유 범위를 연구소 그룹으로 제한 → 게시* 순서로 진행합니다.

## 7. 보안·운영 체크리스트

- **외부 질의 가드**: 사내 코드·`대외비`·`confidential` 표시·500자 초과 질의는 도구 단계에서 차단(`common.guard_query`). 프롬프트의 금지 지시는 *보조 수단*이며, 차단의 본체는 이 코드입니다.
- **감사 로그**: 모든 도구 호출이 JSON 으로 Cloud Logging 에 남습니다(도구명, 인자, 소요시간, 성공/거부). 주 1회 `event="tool_call" status="rejected"` 를 점검해 사내 코드 유출 시도를 확인하세요. 질의어 기록이 부담되면 `AUDIT_LOG_QUERIES=false`.
- **키 관리**: 모두 Secret Manager 에 보관, 서비스 계정별 최소 권한. 오류 메시지에는 URL/키가 포함되지 않도록 설계됨.
- **BigQuery 비용 상한**: `BQ_MAX_BYTES_BILLED` 로 쿼리당 상한, 필요하면 프로젝트 일일 쿼터도 설정.
- **인증**: Cloud Run 비공개 + IAM. Gemini Enterprise 가 Custom MCP 연결에서 OAuth(Authorization/Token URL)를 요구하는 화면이면, ① 문서의 "Cloud Run 인증" 절차가 우선인지 ② OAuth 필요 시 서버 앞단에 OAuth 프록시를 두는지를 Google 문서/지원에서 확인해야 합니다(작성 시점에 확정하지 못한 부분).
- **국가법령정보 IP 등록**: Cloud Run 은 송신 IP가 고정이 아닙니다. `mcp-reg` 서비스를 Serverless VPC Access(또는 Direct VPC egress) + **Cloud NAT 고정 IP** 로 나가게 구성하고, 그 IP를 open.law.go.kr 에 등록하세요.
- **라이선스**: DrugBank, Drugs.com, PharmaExcipients, J-PlatPat 은 자동 수집/API 연동 대상에서 제외했습니다(약관·유료 라이선스 확인 후 별도 도입).
- **면책 문구**: 모든 규제·특허 응답에는 "법적 판단 아님, RA/IP 확인 필요" 가 포함되도록 설계되어 있습니다. 삭제하지 마세요.

## 8. 작성 환경에서 검증하지 못한 항목 (키 발급 후 확인)

| 항목 | 확인 방법 | 수정 위치 |
|---|---|---|
| KIPRIS Plus 오퍼레이션명·파라미터(`freeSearchInfo`, `applicantNameSearchInfo`, `docsStart`=페이지번호 여부, 응답 필드명) | KIPRIS Plus 서비스 명세 대조, `smoke_test.py --suite papers` | `KIPRIS_BASE_URL`, `search_kipris`, `parse_kipris_xml` |
| 식약처 허가정보 API 버전(`...Service06/getDrugPrdtPrmsnInq06`)과 응답 필드 | 공공데이터포털 활용신청 페이지의 End Point/명세 | `MFDS_PERMIT_URL`, `mfds_product_search` |
| 국가법령정보 JSON 응답 구조(`law`/`admrul` 검색·본문) | `law_search` → `law_get_text` 실호출 | `law_search`, `collect_law_text` |
| BigQuery 공개 특허 스키마(`claims_localized`, `assignee_harmonized`, `cpc` 등) | `bq show --schema patents-public-data:patents.publications` | `search_patents_bq`, `get_patent_claims` |
| FDA IIG 컬럼명 | 4-A 의 `bq show --schema` 또는 데이터 스토어 스키마 화면 | `IIG_FIELD_*` 환경변수 |
| 데이터 스토어 Search API 응답에서 `struct_data` 로 행이 나오는지, `custom` 스키마 JSONL 가져오기 형식, `serving_config` 경로(`default_config`) | `iig_lookup` 실호출, 가져오기 오퍼레이션 결과 | `_query_iig_ds`, `prepare_iig_for_datastore.py` |
| PubChem 속성명(`SMILES`/`ConnectivitySMILES`) 및 PUG-View heading | `pubchem_properties` 실호출 | `PROPS`, `EXPERIMENTAL_HEADINGS` |
| PMC ID 변환 API 주소 | `get_pmc_fulltext(pmid=…)` 실호출 | `get_pmc_fulltext` |
| Gemini Enterprise 의 Custom MCP 인증 방식, 에이전트 간 호출 지원 | 콘솔·공식 문서 | 6장 B·C |

## 9. 평가 방법 (6단계)

1. `tests/golden_set.csv` 의 15문항에 **정답/기대값을 연구원이 먼저 확정**(웹 원문 대조 결과를 `verify_by` 열 기준으로 기록)
2. 에이전트에 질문 → 확인 항목: ① 올바른 도구 호출 ② 수치·번호가 원문과 일치 ③ 출처·기준일 포함 ④ 규제/침해 판단을 하지 않음 ⑤ 사내 코드 차단(14번)
3. 합격선(권장): 수치 정확도 100%(틀린 수치 0건), 출처 누락 0건, 가드 문항 100% 차단
4. 도구 변경·프롬프트 변경 때마다 같은 골든셋으로 회귀 테스트

## 10. 확장 아이디어

- `nitrosamine_ai_lookup`: FDA/EMA AI limit 표를 `load_csv_to_bq.py` 로 적재해 구조화 조회(원문 검색보다 정확)
- 경쟁사 출원 연도별 추이: `search_patents_bq` 에 연도 집계 모드 추가
- 사내 문서(SOP·과거 보고서) 데이터 스토어를 같은 에이전트에 연결해 외부+내부 통합 조회(별도 보안 검토 필요)
- 라우터를 ADK 로 구현해 Agent Runtime 에 올리고 Gemini Enterprise 에 등록(Agent Designer 의 하위 에이전트 기능이 부족할 때)
