
export PYTHONPATH=.

# ==============================================================================
# 1. GCP 기본 설정 및 프로젝트 ID
# ==============================================================================
export PROJECT_ID="uk-adc-core-geminienterprise"
export REGION="asia-northeast3"
export BUCKET_NAME="uk-adc-core-geminienterprise-rdkit-storage"

# ==============================================================================
# 2. FDA IIG 조회용 Gemini Enterprise 데이터 스토어 설정
# ==============================================================================
export IIG_DS_PROJECT="uk-adc-core-geminienterprise"
export IIG_DATASTORE_ID="daewon-research-ds_1794032917835"
export IIG_DS_LOCATION="global"
export IIG_FIELD_INGREDIENT="ingredient_name"
export IIG_FIELD_ROUTE="route"
export IIG_FIELD_FORM="dosage_form"
export IIG_DS_MAX_SCAN="500"

# ==============================================================================
# 3. 외부 공공/연구 API 인증 키 (필수 및 선택)
# ==============================================================================
# 식약처 의약품 제품 허가정보 (공공데이터포털 Decoding 키)
export DATA_GO_KR_KEY="b64ce4504b64b5ed321bdeeab7d022168b7229d4159817bf71103d72140d7725"

# 국가법령정보센터 Open API 발급 OC 코드 (호출 서버 IP 등록 필요)
export LAW_OC="daewon2026"

# KIPRIS Plus 국내 특허 검색 API Key
export KIPRIS_ACCESS_KEY="=2Ysm5xR9bj0fjrGP6PVqTRV3TKi3fMUfTef3lJRQSc="

# NCBI E-utilities API Key (선택: 초당 호출 속도 3회 -> 10회 완화)
export NCBI_API_KEY="23f5f81401ddbd44ae0ff7d817cc217b9609"

# ==============================================================================
# 4. 보안 가드 및 감사 로그 설정
# ==============================================================================
# 사내 개발코드 정규식 (해당 패턴이 포함된 질의는 외부 API 전송 차단)
export BLOCK_PATTERNS="\\bDW[-_ ]?\\d{3,}\\b"

# 질의 최대 글자 수 제한
export MAX_QUERY_CHARS="500"

# 외부 API 호출 시 전달될 관리자 연락처
export CONTACT_EMAIL="research-it@example.co.kr"

# Cloud Logging에 질의 상세 인자 기록 여부 (true | false)
export AUDIT_LOG_QUERIES="true"

# ==============================================================================
# 5. 글로벌 특허 BigQuery 설정
# ==============================================================================
export BQ_PROJECT="uk-adc-core-geminienterprise"
export BQ_MAX_BYTES_BILLED="322122547200"  # 300GB 한도 (바이트 단위)

# ==============================================================================
# 6. 로컬 서버 실행 포트 및 모듈 (로컬 실행 시 사용)
# ==============================================================================
export PORT="8080"
export LOG_LEVEL="INFO"

# ==============================================================================
# 7. Google patents Biqqury 실행을 위해 인증
# ==============================================================================
gcloud auth application-default login
gcloud auth application-default set-quota-project "uk-adc-core-geminienterprise"
gcloud auth application-default print-access-token
export GOOGLE_APPLICATION_CREDENTIALS="/Users/jameskim/Downloads/TKCO/daewon-research-agent/credentials/gcp-sa-key.json"