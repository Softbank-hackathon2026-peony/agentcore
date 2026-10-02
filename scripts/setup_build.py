"""이미지 빌드 인프라 (그림 09~10): CodeBuild 가 Dockerfile 로 빌드해서 AWS ECR · GCP Artifact Registry 에 올린다.

만드는 것 (이미 있으면 재사용·갱신):
  1. ECR 저장소      pawploy-apps          : AWS 배포용 이미지 (푸시 때 스캔, 최근 50개만 보관)
  2. IAM 역할        pawploy-codebuild     : 소스·빌드 파일 읽기, ECR 푸시, GCP 키 읽기, 로그
  3. CodeBuild       pawploy-build         : buildspec 은 agent/buildfiles.py 의 고정 템플릿을 그대로 넣는다

GCP Artifact Registry 는 여기서 만들지 않는다 (이미 있는 저장소를 쓴다).
  --gcp-ar-repo     asia-northeast3-docker.pkg.dev/<프로젝트>/<저장소>
  --gcp-key-secret  쓰기 권한(roles/artifactregistry.writer) 있는 GCP 서비스계정 키 JSON 이 든 Secrets Manager 이름
둘 다 주면 프로젝트 기본값으로 넣어서 모든 빌드가 ECR + AR 양쪽에 올린다. 안 주면 ECR 만.

사용: AWS_PROFILE=peony .venv/Scripts/python scripts/setup_build.py [--gcp-ar-repo ... --gcp-key-secret ...] [--dry-run]
"""
import argparse
import json
import sys
import time
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.buildfiles import buildspec  # noqa: E402

REGION = "ap-northeast-2"
ECR_REPO = "pawploy-apps"
ROLE_NAME = "pawploy-codebuild"
PROJECT = "pawploy-build"
IMAGE = "aws/codebuild/amazonlinux-x86_64-standard:5.0"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gcp-ar-repo", default="")
    ap.add_argument("--gcp-key-secret", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if bool(a.gcp_ar_repo) != bool(a.gcp_key_secret):
        sys.exit("--gcp-ar-repo 와 --gcp-key-secret 은 같이 줘야 합니다")

    account = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    agent_bucket = f"pawploy-agent-{account}"
    # Main Server 가 올리는 소스 스냅샷 버킷 (노영진)
    source_buckets = [agent_bucket, f"fawploy-source-{account}-{REGION}"]
    print(f"[계정] {account}  [ECR] {ECR_REPO}  [역할] {ROLE_NAME}  [CodeBuild] {PROJECT}  "
          f"[GCP] {a.gcp_ar_repo or '없음 (ECR 만)'}")
    if a.dry_run:
        return

    repo_uri = ensure_ecr()
    role_arn = ensure_role(account, source_buckets, a.gcp_key_secret)
    ensure_project(role_arn, repo_uri, a.gcp_ar_repo, a.gcp_key_secret)
    print(f"\n[완료] start_build(projectName={PROJECT!r}) 에 SOURCE_URI · BUILD_FILES_URI · IMAGE_TAG 만 넘기면 된다")
    print(f"ECR_REPO_URI={repo_uri}")


def ensure_ecr() -> str:
    ecr = boto3.client("ecr", region_name=REGION)
    try:
        repo = ecr.describe_repositories(repositoryNames=[ECR_REPO])["repositories"][0]
        print(f"[ECR] 있음: {repo['repositoryUri']}")
    except ecr.exceptions.RepositoryNotFoundException:
        repo = ecr.create_repository(repositoryName=ECR_REPO, imageScanningConfiguration={"scanOnPush": True},
                                     imageTagMutability="MUTABLE")["repository"]
        print(f"[ECR] 만듦: {repo['repositoryUri']}")
    ecr.put_lifecycle_policy(repositoryName=ECR_REPO, lifecyclePolicyText=json.dumps({"rules": [{
        "rulePriority": 1, "description": "최근 50개만 보관",
        "selection": {"tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": 50},
        "action": {"type": "expire"}}]}))
    return repo["repositoryUri"]


def ensure_role(account: str, source_buckets: list[str], gcp_key_secret: str) -> str:
    iam = boto3.client("iam")
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "codebuild.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": account}}}]}
    stmts = [
        {"Sid": "Logs", "Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{REGION}:{account}:log-group:/aws/codebuild/{PROJECT}*"},
        {"Sid": "EcrLogin", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
        {"Sid": "EcrPush", "Effect": "Allow", "Resource": f"arn:aws:ecr:{REGION}:{account}:repository/{ECR_REPO}",
         "Action": ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]},
        {"Sid": "ReadSource", "Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"],
         "Resource": [r for b in source_buckets for r in (f"arn:aws:s3:::{b}", f"arn:aws:s3:::{b}/*")]},
    ]
    if gcp_key_secret:
        stmts.append({"Sid": "GcpKey", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
                      "Resource": f"arn:aws:secretsmanager:{REGION}:{account}:secret:{gcp_key_secret}-*"})
    try:
        arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
        print(f"[역할] 있음: {arn}")
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust),
                              Description="Pawploy CodeBuild image build")["Role"]["Arn"]
        print(f"[역할] 만듦: {arn}")
        time.sleep(10)  # IAM 전파 대기
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="pawploy-build",
                        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": stmts}))
    return arn


def ensure_project(role_arn: str, repo_uri: str, gcp_ar_repo: str, gcp_key_secret: str):
    cb = boto3.client("codebuild", region_name=REGION)
    env = [{"name": "ECR_REPO_URI", "value": repo_uri, "type": "PLAINTEXT"},
           {"name": "GCP_AR_REPO", "value": gcp_ar_repo, "type": "PLAINTEXT"},
           {"name": "GCP_SA_KEY_SECRET", "value": gcp_key_secret, "type": "PLAINTEXT"}]
    spec = dict(
        name=PROJECT, description="Pawploy: Dockerfile 빌드 → ECR (+ GCP Artifact Registry) 푸시",
        source={"type": "NO_SOURCE", "buildspec": buildspec()}, artifacts={"type": "NO_ARTIFACTS"},
        environment={"type": "LINUX_CONTAINER", "image": IMAGE, "computeType": "BUILD_GENERAL1_SMALL",
                     "privilegedMode": True, "environmentVariables": env},
        serviceRole=role_arn, timeoutInMinutes=20, queuedTimeoutInMinutes=30,
        logsConfig={"cloudWatchLogs": {"status": "ENABLED", "groupName": f"/aws/codebuild/{PROJECT}"}},
    )
    if cb.batch_get_projects(names=[PROJECT])["projects"]:
        cb.update_project(**spec)
        print(f"[CodeBuild] 갱신: {PROJECT}")
    else:
        for i in range(6):   # 새 역할은 CodeBuild 가 바로 못 맡을 때가 있다
            try:
                cb.create_project(**spec)
                break
            except cb.exceptions.InvalidInputException as e:
                if "role" not in str(e).lower() or i == 5:
                    raise
                time.sleep(10)
        print(f"[CodeBuild] 만듦: {PROJECT}")


if __name__ == "__main__":
    main()
