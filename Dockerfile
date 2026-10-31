FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

# 하나의 이미지로 3개 서비스를 배포합니다. SERVER_MODULE 로 실행 대상을 고릅니다.
#   papers_patents_server | chem_server | regulatory_server
ENV SERVER_MODULE=papers_patents_server
ENV PORT=8080
CMD ["sh", "-c", "exec python -m src.${SERVER_MODULE}"]
