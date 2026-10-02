# 빌드 컨텍스트: 저장소 루트 (docker build -f services/board/Dockerfile .)
# 같은 이미지를 board-api, board-worker(python -m board.worker), db-migrate(python -m board.migrate)가 함께 쓴다.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MIGRATIONS_DIR=/app/migrations

WORKDIR /app
# 고정된 버전(requirements.lock, `make lock` 으로 갱신)을 먼저 설치해 빌드를 재현 가능하게 하고 레이어 캐시를 살린다.
COPY services/board/requirements.lock ./
RUN pip install -r requirements.lock
COPY services/board/pyproject.toml ./
COPY services/board/src ./src
RUN pip install --no-deps . \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY db/migrations ./migrations

USER 10001
EXPOSE 8000 9100
CMD ["uvicorn", "board.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
