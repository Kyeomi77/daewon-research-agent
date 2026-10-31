#!/usr/bin/env bash
# 3개 MCP 서버를 Cloud Run 에 배포한다.
#   사용: PROJECT_ID=my-proj ./scripts/deploy.sh [all|papers|chem|reg]
# 사전: gcloud auth login && gcloud config set project $PROJECT_ID
set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID 를 지정하세요}"
REGION="${REGION:-asia-northeast3}"            # 서울
CONTACT_EMAIL="${CONTACT_EMAIL:-research-it@example.co.kr}"
BQ_PROJECT="${BQ_PROJECT:-$PROJECT_ID}"
IIG_DS_PROJECT="${IIG_DS_PROJECT:-$PROJECT_ID}"
IIG_DS_LOCATION="${IIG_DS_LOCATION:-global}"
IIG_DATASTORE_ID="${IIG_DATASTORE_ID:-}"          # Data Store ID (필수)
BLOCK_PATTERNS="${BLOCK_PATTERNS:-\\bDW[-_ ]?\\d{3,}\\b}"   # 사내 코드 형식에 맞게 반드시 수정
TARGET="${1:-all}"

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
# Gemini Enterprise(Discovery Engine) 서비스 에이전트 — Cloud Run 호출 권한 부여 대상
# (Google 문서 'Set up authentication for a custom MCP server on Cloud Run' 으로 계정을 재확인하세요)
DE_AGENT="service-${PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com"

echo ">> API 활성화"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com bigquery.googleapis.com --project "$PROJECT_ID"

ensure_sa() {  # $1=name
  gcloud iam service-accounts describe "$1@$PROJECT_ID.iam.gserviceaccount.com" --project "$PROJECT_ID" >/dev/null 2>&1 \
    || gcloud iam service-accounts create "$1" --display-name "$1" --project "$PROJECT_ID"
}

ensure_secret() {  # $1=name  (값은 직접 입력: echo -n 'KEY' | gcloud secrets versions add NAME --data-file=-)
  gcloud secrets describe "$1" --project "$PROJECT_ID" >/dev/null 2>&1 \
    || { gcloud secrets create "$1" --replication-policy=automatic --project "$PROJECT_ID"; \
         echo "!! 시크릿 '$1' 을 만들었습니다. 값을 등록하세요:  echo -n '<KEY>' | gcloud secrets versions add $1 --data-file=- --project $PROJECT_ID"; }
}

grant_secret() {  # $1=sa $2=secret
  gcloud secrets add-iam-policy-binding "$2" --member "serviceAccount:$1@$PROJECT_ID.iam.gserviceaccount.com" \
    --role roles/secretmanager.secretAccessor --project "$PROJECT_ID" >/dev/null
}

deploy() {  # $1=service $2=module $3=sa $4=secrets-spec $5=extra-env
  echo ">> 배포: $1"
  gcloud run deploy "$1" --source . --region "$REGION" --project "$PROJECT_ID" \
    --service-account "$3@$PROJECT_ID.iam.gserviceaccount.com" \
    --no-allow-unauthenticated --min-instances 0 --max-instances 5 --cpu 1 --memory 512Mi --timeout 120 \
    --set-env-vars "^@^SERVER_MODULE=$2@CONTACT_EMAIL=$CONTACT_EMAIL@BLOCK_PATTERNS=$BLOCK_PATTERNS@BQ_PROJECT=$BQ_PROJECT$5" \
    ${4:+--set-secrets "$4"}
  gcloud run services add-iam-policy-binding "$1" --region "$REGION" --project "$PROJECT_ID" \
    --member "serviceAccount:$DE_AGENT" --role roles/run.invoker >/dev/null
  echo "   URL: $(gcloud run services describe "$1" --region "$REGION" --project "$PROJECT_ID" --format 'value(status.url)')/mcp"
}

if [[ "$TARGET" == all || "$TARGET" == papers ]]; then
  ensure_sa mcp-papers
  gcloud projects add-iam-policy-binding "$BQ_PROJECT" --member "serviceAccount:mcp-papers@$PROJECT_ID.iam.gserviceaccount.com" --role roles/bigquery.jobUser >/dev/null
  ensure_secret ncbi-api-key; ensure_secret kipris-access-key
  grant_secret mcp-papers ncbi-api-key; grant_secret mcp-papers kipris-access-key
  deploy daewon-mcp-papers papers_patents_server mcp-papers "NCBI_API_KEY=ncbi-api-key:latest,KIPRIS_ACCESS_KEY=kipris-access-key:latest" ""
fi

if [[ "$TARGET" == all || "$TARGET" == chem ]]; then
  ensure_sa mcp-chem
  deploy daewon-mcp-chem chem_server mcp-chem "" ""
fi

if [[ "$TARGET" == all || "$TARGET" == reg ]]; then
  ensure_sa mcp-reg
  : "${IIG_DATASTORE_ID:?IIG 데이터 조회를 위한 IIG_DATASTORE_ID 가 필요합니다}"
  gcloud projects add-iam-policy-binding "$IIG_DS_PROJECT" --member "serviceAccount:mcp-reg@$PROJECT_ID.iam.gserviceaccount.com" --role roles/discoveryengine.viewer >/dev/null
  ensure_secret data-go-kr-key; ensure_secret law-oc
  grant_secret mcp-reg data-go-kr-key; grant_secret mcp-reg law-oc
  IIG_ENV="@IIG_DS_PROJECT=$IIG_DS_PROJECT@IIG_DS_LOCATION=$IIG_DS_LOCATION@IIG_DATASTORE_ID=$IIG_DATASTORE_ID"
  deploy daewon-mcp-reg regulatory_server mcp-reg "DATA_GO_KR_KEY=data-go-kr-key:latest,LAW_OC=law-oc:latest" "$IIG_ENV"
  echo "!! 국가법령정보 Open API 는 호출 서버 IP 등록이 필요합니다. Cloud NAT 고정 IP 구성은 README 7장을 참고하세요."
fi
echo "완료."
