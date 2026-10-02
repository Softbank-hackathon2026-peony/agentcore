# 빌드 컨텍스트: 저장소 루트 (docker build -f services/auth/Dockerfile .)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
# 고정된 버전(requirements.lock, `make lock` 으로 갱신)을 먼저 설치해 빌드를 재현 가능하게 하고 레이어 캐시를 살린다.
COPY services/auth/requirements.lock ./
RUN pip install -r requirements.lock
COPY services/auth/pyproject.toml ./
COPY services/auth/src ./src
RUN pip install --no-deps . \
 && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app

USER 10001
EXPOSE 8000
CMD ["uvicorn", "auth.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
