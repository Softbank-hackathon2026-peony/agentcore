"""LLM이 채우는 출력 형식 (구조화 출력).

LLM은 이 모양으로만 답할 수 있다. 다만 형식이 맞아도 내용은 틀릴 수 있으므로
값 검사는 analyze.py / dockerfile.py 에서 코드로 다시 한다.
"""
from typing import Literal

from pydantic import BaseModel, Field

TargetId = Literal["aws_lambda", "aws_ec2", "gcp_cloud_run", "aws_ecs_fargate", "gcp_compute_engine"]
Size = Literal["micro", "small", "medium"]
Verdict = Literal["추천", "적합", "과함", "낭비", "부적합"]


class Clue(BaseModel):
    file: str = Field(description="근거가 된 파일의 프로젝트 상대경로 (실제로 존재해야 함)")
    line: int | None = Field(default=None, description="근거가 있는 줄 번호. 모르면 null")
    finding: str = Field(description="그 파일에서 발견한 사실 (예: 'flask==3.0 의존성')")
    plain: str = Field(description="초보자용 쉬운 설명 한 문장 ('쉽게 말하면: ...')")
    tag: str = Field(description="짧은 태그 (예: '웹 API', '하루 1회 실행')")


class Candidate(BaseModel):
    target: TargetId
    fit: int = Field(ge=0, le=100, description="이 프로젝트에 얼마나 맞는지 0~100")
    verdict: Verdict
    why: str = Field(description="한두 문장 이유 (초보자용)")


class LLMRecommendation(BaseModel):
    summary: str = Field(description="이 프로젝트가 무엇인지 한 문장")
    target: TargetId = Field(description="최종 추천 배포 대상 (배포 가능한 대상 중에서)")
    container_port: int = Field(ge=1, le=65535, description="앱이 컨테이너 안에서 요청을 받는 포트")
    size: Size
    health_path: str = Field(description="200을 돌려주는 경로. 모르면 '/'")
    env: dict[str, str] = Field(default_factory=dict,
                                description="비밀이 아닌 기본 환경변수만. 키·토큰·비밀번호는 넣지 말 것")
    required_secrets: list[str] = Field(default_factory=list,
                                        description="사용자가 직접 넣어야 하는 비밀 환경변수 이름")
    reason: str = Field(description="왜 이 대상을 골랐는지 초보자용 2~3문장")
    clues: list[Clue] = Field(description="판단 근거 3~6개")
    candidates: list[Candidate] = Field(description="모든 대상을 적합한 순서로 (1위 = target)")
    supported: bool = Field(description="컨테이너 1개로 배포 가능한 프로젝트면 true")
    warnings: list[str] = Field(default_factory=list)


class DockerfileOut(BaseModel):
    dockerfile: str = Field(description="Dockerfile 전체 내용")
    container_port: int = Field(ge=1, le=65535)
    notes: list[str] = Field(default_factory=list, description="무엇을 근거로 어떻게 만들었는지")


class DockerfileFix(BaseModel):
    fixable: bool = Field(description="Dockerfile 수정으로 고칠 수 있는 에러면 true")
    cause: str = Field(description="빌드 실패 원인 한두 문장")
    dockerfile: str = Field(default="", description="고친 Dockerfile 전체 (fixable=false면 빈 문자열)")
    container_port: int = Field(default=8080, ge=1, le=65535)
    changes: list[str] = Field(default_factory=list, description="무엇을 바꿨는지")
