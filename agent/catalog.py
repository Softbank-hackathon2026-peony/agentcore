"""배포 대상 목록 (허용 목록).

LLM은 반드시 여기 있는 id 중에서만 고른다. 목록 밖의 값은 코드가 버린다.
`deployable`은 지금 우리 Worker가 실제로 배포할 수 있는지 여부다.
배포 못 하는 대상도 "비교용 후보"로는 보여줄 수 있다 (1~5순위 표시용).
"""
import os

# 크기별 스펙은 Terraform-worker 모듈(modules/ec2, modules/lambda)과 같게 맞춘다.
TARGETS: dict[str, dict] = {
    "aws_lambda": {
        "cloud": "aws", "architecture": "lambda", "label": "AWS Lambda",
        "sizes": {"micro": "메모리 512MB", "small": "메모리 1024MB", "medium": "메모리 2048MB"},
        "permissions": ["CloudWatch Logs 쓰기 (실행 로그)"],
        "good_for": "요청이 가끔 오고 요청 하나가 짧게 끝나는 웹 API·웹앱",
        "limits": "요청당 최대 30초(Worker 설정), 상태·파일을 서버에 남길 수 없음, WebSocket 불가",
    },
    "aws_ec2": {
        "cloud": "aws", "architecture": "ec2", "label": "AWS EC2",
        "sizes": {"micro": "t3.micro", "small": "t3.small", "medium": "t3.medium"},
        "permissions": ["ECR 이미지 읽기", "외부 접속 80번 포트만 열림"],
        "good_for": "항상 켜져 있어야 하는 서버, 실시간 연결, 오래 걸리는 요청",
        "limits": "켜져 있는 동안 계속 과금",
    },
    # 컨테이너 여러 개(앱·워커·DB·캐시·프록시)를 서버 1대에서 Docker Compose 로 함께 실행 (Worker modules/ec2_compose)
    "aws_ec2_compose": {
        "cloud": "aws", "architecture": "ec2_compose", "label": "AWS EC2 (Docker Compose)",
        "sizes": {"micro": "t3.micro", "small": "t3.small", "medium": "t3.medium"},
        "permissions": ["ECR 이미지 읽기", "외부 접속 80번 포트만 열림 (DB·캐시 포트는 서버 안에서만)"],
        "good_for": "DB·캐시·워커·프록시처럼 컨테이너 여러 개가 같이 떠야 하는 앱",
        "limits": "켜져 있는 동안 계속 과금, 서버 1대라 DB 데이터는 서버를 지우면 사라짐, 컨테이너가 많으면 medium 권장",
    },
    "gcp_cloud_run": {
        "cloud": "gcp", "architecture": "cloud_run", "label": "Google Cloud Run",
        "sizes": {"micro": "메모리 512MiB", "small": "메모리 1GiB", "medium": "메모리 2GiB"},
        "permissions": ["Artifact Registry 이미지 읽기", "공개 접속 허용"],
        "good_for": "컨테이너 웹앱을 요청 있을 때만 실행 (Lambda보다 제약이 적음)",
        "limits": "요청 단위 실행, 로컬 파일은 임시",
    },
    "aws_ecs_fargate": {
        "cloud": "aws", "architecture": "ecs_fargate", "label": "AWS ECS Fargate",
        "sizes": {"micro": "0.25 vCPU / 0.5GB", "small": "0.5 vCPU / 1GB", "medium": "1 vCPU / 2GB"},
        "permissions": ["ECR 이미지 읽기", "로드밸런서 공개"],
        "good_for": "항상 켜진 컨테이너 서비스를 서버 관리 없이",
        "limits": "로드밸런서 비용이 추가로 듦",
    },
    "gcp_compute_engine": {
        "cloud": "gcp", "architecture": "compute_engine", "label": "Google Compute Engine",
        "sizes": {"micro": "e2-micro", "small": "e2-small", "medium": "e2-medium"},
        "permissions": ["Artifact Registry 이미지 읽기", "외부 접속 80번 포트"],
        "good_for": "GCP에서 항상 켜진 서버",
        "limits": "켜져 있는 동안 계속 과금",
    },
}

SIZES = ("micro", "small", "medium")
# 우리 Worker 모듈이 정한 요청 상한 (Terraform-worker modules/lambda timeout=30, modules/cloud_run timeout="60s").
# InfraFit 은 플랫폼 상한(Lambda 900초 등)으로 판정하므로 추천을 쓸 때 이 값으로 한 번 더 거른다 (inventory.worker_limit)
WORKER_REQUEST_SECONDS = {"aws_lambda": 30, "gcp_cloud_run": 60}
# 컨테이너 여러 개를 한 번에 실행하는 대상. 컨테이너가 1개인 앱의 후보에는 넣지 않는다
MULTI_CONTAINER = {"aws_ec2_compose"}

# 실제 배포 가능한 대상. Worker가 지원을 늘리면 환경변수로 바꾼다.
_DEFAULT_DEPLOYABLE = "aws_lambda,aws_ec2,aws_ec2_compose,gcp_cloud_run"
DEPLOYABLE = {t.strip() for t in os.environ.get("PAWPLOY_DEPLOYABLE", _DEFAULT_DEPLOYABLE).split(",")
              if t.strip() in TARGETS}


def is_deployable(target_id: str) -> bool:
    return target_id in DEPLOYABLE


def describe_for_prompt() -> str:
    lines = []
    for tid, t in TARGETS.items():
        flag = "배포 가능" if is_deployable(tid) else "비교용 (지금은 배포 불가)"
        sizes = ", ".join(f"{k}={v}" for k, v in t["sizes"].items())
        lines.append(f"- {tid} [{flag}] {t['label']}: 적합 = {t['good_for']} / 제약 = {t['limits']} / 크기 = {sizes}")
    return "\n".join(lines)
