"""환경변수 설정. 배포 환경마다 바뀌는 값은 전부 여기서 읽는다."""
import os

REGION = os.environ.get("PAWPLOY_REGION", "ap-northeast-2")
# 서울 리전에서 이 계정이 호출 가능한 모델 (2026-10-02 확인: sonnet-4-6 OK, sonnet-5 계열은 AccessDenied)
MODEL_ID = os.environ.get("PAWPLOY_MODEL_ID", "global.anthropic.claude-sonnet-4-6")
# 결과를 저장할 버킷. 없으면 결과를 응답에만 담고 저장하지 않는다.
ARTIFACT_BUCKET = os.environ.get("PAWPLOY_ARTIFACT_BUCKET", "")
# 빌드·Terraform 자동 수정 최대 횟수 (10/2 팀 결정: 3회)
MAX_ATTEMPTS = int(os.environ.get("PAWPLOY_MAX_ATTEMPTS", "3"))
# 에이전트가 도구를 부를 수 있는 최대 턴 수 (무한 루프 방지)
MAX_TURNS = int(os.environ.get("PAWPLOY_MAX_TURNS", "12"))
# InfraFit 인벤토리(별도 프로세스) 시간 제한(초). 0 이하면 인벤토리를 돌리지 않는다.
INVENTORY_TIMEOUT = int(os.environ.get("PAWPLOY_INVENTORY_TIMEOUT", "60"))
