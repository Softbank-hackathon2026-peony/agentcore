"""테스트용 가짜 Brain: 모델을 부르지 않고 정해진 답을 돌려준다."""
from agent.schemas import Candidate, Clue, DockerfileFix, DockerfileOut, LLMRecommendation

GOOD_DOCKERFILE = """FROM python:3.12-slim
WORKDIR /app
COPY app.py .
CMD ["python", "app.py"]
"""


def recommendation(**over) -> LLMRecommendation:
    data = dict(
        summary="PORT 환경변수로 뜨는 파이썬 웹서버",
        target="aws_lambda", container_port=8080, size="small", health_path="/",
        env={"APP_MODE": "test", "OPENAI_API_KEY": "sk-leak", "PORT": "1234"},
        required_secrets=[], reason="요청이 가끔 오는 가벼운 서버라 Lambda가 맞아요.",
        clues=[Clue(file="app.py", line=7, finding="PORT 기본값 8080", plain="8080 포트로 받아요", tag="포트"),
               Clue(file="ghost.py", line=1, finding="없는 파일", plain="-", tag="가짜"),
               Clue(file="app.py", line=999, finding="범위 밖 줄", plain="-", tag="줄")],
        candidates=[Candidate(target="aws_ec2", fit=40, verdict="낭비", why="항상 켜둘 필요 없음"),
                    Candidate(target="aws_lambda", fit=90, verdict="추천", why="짧은 요청")],
        supported=True, warnings=[],
    )
    data.update(over)
    return LLMRecommendation(**data)


class FakeBrain:
    def __init__(self, rec=None, df=None, fix=None):
        self.rec = rec or recommendation()
        self.df = df or DockerfileOut(dockerfile=GOOD_DOCKERFILE, container_port=8080, notes=["샘플 기반"])
        self.fix = fix
        self.calls = []

    def analyze(self, src, scan, revision):
        self.calls.append(("analyze", revision))
        return self.rec, self.df

    def fix_dockerfile(self, src, scan, dockerfile, build_log, failed_phase):
        self.calls.append(("fix", failed_phase))
        return self.fix or DockerfileFix(fixable=True, cause="pip 패키지 이름 오타",
                                         dockerfile=GOOD_DOCKERFILE.replace("slim", "slim-bookworm"),
                                         container_port=8080, changes=["베이스 이미지 변경"])
