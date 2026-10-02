# Pawploy AgentCore 에이전트

사용자 프로젝트 소스를 읽고 **배포 방식 추천 + 빌드 파일 생성**을 하는 AI 에이전트입니다.
Amazon Bedrock AgentCore Runtime(서울)에 올라가고, Main Server가 `mode`를 정해서 호출합니다.

| mode | 그림 단계 | 담당 | 상태 |
|---|---|---|---|
| `analyze` | 05~08 분석·추천 + Dockerfile·buildspec 생성 (수정 요청 재분석 포함) | 이주호 (InfraFit 연결·인젝션 방어: 고준서) | ✅ 배포본 실제 호출, 평가 13/15 · 인젝션 5/5 |
| `fix_build` | 11~13 빌드 실패 → Dockerfile 수정 (최대 3회) | 이주호 | ⚠️ 오프라인 테스트만 (CodeBuild 생기면 실제 로그로 확인) |
| `gen_terraform` | 18~20 승인안 → AWS·GCP Terraform 모듈 동시 생성 | 이주호 (Worker 규격 맞춤: 고준서) | ✅ ec2·lambda·cloud_run 실제 생성 → `terraform validate` 통과 + Worker `iac.py` 위반 0건 |
| `fix_terraform` | 23~25 Terraform 실패 → 수정 (최대 3회) | 이주호 | ✅ 실제 validate 에러 수정 (로그에 없던 오타까지) → validate 통과 |

## 설계 원칙

- **판단은 LLM, 검사는 코드.** LLM 출력은 외부 입력처럼 다룬다. 허용 목록·범위·실제 파일 대조를 코드로 다시 한다.
- **사용자 코드는 데이터.** 파일 안의 지시는 따르지 않는다(프롬프트 인젝션). 에이전트 도구는 `list_files`, `read_file`(읽기 전용) 두 개뿐이다.
- **buildspec은 LLM이 만들지 않는다.** CodeBuild에서 셸 명령이 실행되는 파일이라 고정 템플릿을 쓴다. LLM은 Dockerfile만 만든다.
- **숫자를 지어내지 않는다.** 비용은 `agent/prices.json`(출처·확인일 필수)으로 코드가 계산하고, 단가가 없으면 `null`.
- **덮어쓰지 않는다.** 결과는 분석·시도마다 새 경로에 저장한다.

## 시연

```bash
AWS_PROFILE=peony .venv/Scripts/python scripts/demo.py           # 배포본 실제 호출 (약 2분)
.venv/Scripts/python scripts/demo.py --replay                    # 저장된 실제 결과로 재생 (AWS 없이)
.venv/Scripts/python scripts/demo.py --only 1,3                  # 일부 단계만
```

0 오늘 커밋 → 1 샘플 앱 analyze (Lambda 추천·근거·1~5순위·비용·Dockerfile) → 2 simple-web-app analyze ("지원 안 됨") → 3 gen_terraform (AWS Lambda + GCP Cloud Run 모듈 동시) → 4 fix_terraform (깨뜨린 모듈 수정).
녹화된 실제 결과: [examples/demo/](examples/demo/)

## 재시도는 누가 돌리나

에이전트는 **한 번 호출에 한 번** 처리합니다. 실패 → 수정 → 재실행 루프는 **Main Server**가 돌립니다.

| 상황 | 처리 | 횟수 |
|---|---|---|
| LLM 출력이 우리 코드 검사에 걸림 (gen/fix_terraform) | 에이전트가 같은 호출 안에서 위반 내용을 돌려 재생성 | 최대 3번 생성 |
| CodeBuild 빌드 실패 | Main → `fix_build(attempt=N)` → 새 Dockerfile → 재빌드 | `attempt` 3 초과면 `give_up` |
| Worker 실행 실패 (validate·plan·정책·apply·헬스체크) | Main → `fix_terraform(attempt=N, failed_stage, log)` → 새 모듈 → 같은 `deploy_id`로 재실행 | `attempt` 3 초과면 `give_up` |
| 코드로 못 고치는 원인 (권한·할당량·로그인) | 횟수와 상관없이 바로 `give_up` + `fixable: false` | — |
| `gen_terraform` 자체 실패 | Main 이 재호출하거나 `terraform_uri` 없이 Worker 기본 모듈 사용 | — |

## analyze 내부 순서

| # | 단계 | 방식 | 파일 |
|---|---|---|---|
| 1 | 입력 확인, `analysis_id` 발급, 재분석이면 이전 추천·사용자 메시지 보관 | 코드 | `analyze.py` |
| 2 | 소스 불러오기: S3 폴더/zip/tar.gz, 경로 탈출 차단, 비밀 파일 내용 차단, 5000개·50MB 제한 | 코드 | `source.py` |
| 3 | 스캔: 언어·프레임워크·DB·포트 단서·환경변수 이름·compose/k8s + 경고 | 코드 | `scan.py` |
| 3a | 파일 속 AI 지시문 탐지 → 사용자 경고 | 코드 | `scan.py` |
| 3b | InfraFit S0+S1 인벤토리 (별도 프로세스, 60초 제한, 실패해도 계속) | 규칙 | `inventory.py`, `vendor/infrafit` |
| 4 | Claude가 `list_files`·`read_file`로 파일을 직접 읽고 추천 (구조화 출력, 최대 12턴) | LLM | `brain.py` |
| 5 | 같은 대화를 이어 Dockerfile 생성 | LLM | `brain.py` |
| 6 | 검사·보정: 배포 가능 대상만, 근거 파일·줄 실재 확인, 비밀 env 분리(이름·값), compose/k8s면 `supported=false` 강제, 후보 5개 채움, 비용 계산 | 코드 | `analyze.py`, `cost.py` |
| 7 | Dockerfile 규칙 강제: Lambda Web Adapter·`PORT`·`EXPOSE`, `.env` COPY 거부 | 코드 | `buildfiles.py` |
| 8 | buildspec(고정 템플릿)·dockerignore 생성 | 코드 | `buildfiles.py` |
| 9 | S3 저장 (`analysis/<id>/`, `build/<id>/attempt-1/`) → 응답 | 코드 | `storage.py` |

## 호출

### Main Server에서 (boto3)

```python
import boto3, json, uuid
client = boto3.client("bedrock-agentcore", region_name="ap-northeast-2")
resp = client.invoke_agent_runtime(
    agentRuntimeArn=AGENT_RUNTIME_ARN,
    runtimeSessionId=f"pawploy-{project_id}-{uuid.uuid4().hex}",   # 33자 이상
    payload=json.dumps({"mode": "analyze", "project_id": project_id,
                        "source_uri": f"s3://{BUCKET}/projects/{project_id}/source/{sha}/",
                        "commit_sha": sha}),
)
result = json.loads(resp["response"].read())
```

모든 응답은 `status`가 `ok` / `error` / `give_up` 중 하나입니다.
에러는 항상 `{"status": "error", "mode": ..., "error": {"code": ..., "message": ...}}`.

### analyze

요청 ([examples/analyze-request.json](examples/analyze-request.json))

| 필드 | 필수 | 설명 |
|---|---|---|
| `project_id` | ✅ | |
| `source_uri` | ✅ | 소스 스냅샷. `s3://버킷/경로/`(폴더) 또는 `.zip`/`.tar.gz` |
| `commit_sha` | | 기록용 |
| `analysis_id` | | 없으면 에이전트가 만듦 (`ana-YYYYMMDDHHMMSS-xxxx`) |
| `revision_message` | | 사용자 수정 요청 (재분석). 예: "항상 켜져 있어야 해" |
| `previous_recommendation` | | 재분석 시 이전 `recommendation` |

응답 ([examples/analyze-response.json](examples/analyze-response.json)) 의 `recommendation`:

| 필드 | 누가 씀 | 설명 |
|---|---|---|
| `cloud`, `architecture`, `container_port`, `size`, `health_path`, `env`, `reason` | **Terraform Worker** | `architecture`: `lambda` \| `ec2` \| `cloud_run` (…) / `size`: `micro`\|`small`\|`medium` |
| `target`, `label`, `summary` | 화면 | `target` = `aws_lambda` 같은 카탈로그 id |
| `clues[]` | 화면 (근거) | `file`, `line`, `finding`, `plain`(쉬운 설명), `tag` — 파일·줄은 실제 존재 확인됨 |
| `candidates[]` | 화면 (1~5순위) | `rank`, `target`, `deployable`, `fit`(0~100), `verdict`, `why`, `size_spec`, `permissions`, `cost` |
| `cost` | 화면 | `test_1h`, `monthly`(USD, 단가 없으면 null), `assumptions` |
| `permissions` | 화면 (16. 권한 제시) | 배포될 앱이 받는 권한 |
| `required_secrets` | 화면 | 사용자가 직접 넣어야 하는 비밀 환경변수 이름 |
| `supported`, `warnings` | 화면 | 컨테이너 1개로 배포 불가하면 `supported=false` |

`build_files.uri_prefix` 아래에 `Dockerfile.pawploy`, `dockerignore`, `buildspec.yml` 이 저장됩니다.

### fix_build

요청 ([examples/fix-build-request.json](examples/fix-build-request.json)): `project_id`, `analysis_id`, `source_uri`, `dockerfile`(실패한 것), `build_log`, `failed_phase`, `attempt`(1부터).

- `status: ok` → `build_files.attempt` = 다음 시도 번호, `uri_prefix`에 새 파일
- `status: give_up` → 3회 초과, 또는 Dockerfile로 못 고치는 원인(권한·로그인 등, `fixable: false`)

### gen_terraform / fix_terraform (Terraform-worker 연동)

AI가 만드는 것은 **Worker의 `modules/<아키텍처>/` 자리에 들어갈 클라우드별 모듈**입니다. `gen_terraform` 한 번에 **AWS 모듈 하나 + GCP 모듈 하나를 동시에** 만듭니다. Worker 루트 `main.tf`(provider·필수 태그·backend·만료 예약)는 그대로 두고, 미리 만든 모듈 대신 이 모듈을 복사해서 `module "app"`으로 부르면 됩니다.

- 어떤 아키텍처로 만드나: 추천안과 같은 클라우드는 **추천 아키텍처**, 다른 클라우드는 analyze 후보 순위에서 **그 클라우드의 배포 가능한 첫 번째** (예: Lambda 추천 → `aws/` lambda + `gcp/` cloud_run, Cloud Run 추천 → `gcp/` cloud_run + `aws/` 순위 높은 lambda 또는 ec2). 사용자가 다른 순위를 고르면 `architectures: {"aws": "ec2"}` 로 지정 (준 클라우드만 만듦)
- 두 클라우드는 병렬로 만들고 따로 검사합니다. 한쪽만 통과하면 `status: partial` (통과한 쪽만 저장)
- 입력 변수는 정확히 6개: `name`, `image_uri`, `container_port`, `size`, `env`, `health_path` / 출력은 `endpoint`, `health_url`, `resource_id` (지금 모듈과 동일)
- 규칙은 Worker README "AgentCore 가 만들 Terraform 모듈" 약속과 같음. Worker `tfworker/iac.py` 검사를 `agent/terraform.py::_check_worker_iac` 에 그대로 옮겼고(주석까지 검사), 여기에 더 엄격한 검사를 더함
- 금지: `provider`(자동 제거)·`backend`·`cloud`·`module` 블록, `default_tags`·`default_labels`, provisioner(`local-exec`/`remote-exec`), `inline_policy`·`managed_policy_arns`, `access_token`, 같은 모듈 `.tftpl` 외 파일 읽기, 허용 밖 리소스·data 소스·IAM 정책, Worker가 안 넘기는 변수
- 필수: `aws_ssm_parameter` 는 `/aws/service/...` 만, EC2 `cpu_credits = "standard"`, Cloud Run `deletion_protection = false`·최대 인스턴스 1·메모리 2Gi 이하
- 허용 리소스·data 소스·IAM 정책은 Worker `policy.py`·`iac.py` 와 동일 (`cloud_run` 은 앱 전용 `google_service_account` 포함). 견본 `agent/tf_reference/` 는 Worker `modules/` 복사본
- Worker 와 어긋났는지 확인: `WORKER_REPO=<Terraform-worker 경로> python -m pytest tests/test_terraform_contract.py` (Worker 상수·모듈과 직접 비교)
- 검사에 걸리면 같은 호출 안에서 위반 내용을 모델에 돌려 최대 2번 다시 생성

**저장 위치 (Worker `render.module_uri` 규칙과 같음)**
```
s3://pawploy-agent-<계정>/projects/<project_id>/deploy/<deploy_id>/
  attempt-1/aws/main.tf          (+ ec2 면 user_data.sh.tftpl)
  attempt-1/gcp/main.tf
  attempt-2/aws/main.tf          ← fix_terraform 이 AWS 를 고친 것
  attempt-2/gcp/main.tf          ← 안 고친 GCP 는 attempt-1 에서 그대로 복사
```
Worker 는 `PAWPLOY_AGENT_BUCKET` 이 있으면 `terraform_uri` 없이도 이 폴더에서 **가장 큰 attempt-N** 을 고르고, 그 안의 `<cloud>/` 를 씁니다. 그래서 fix 때 안 고친 클라우드도 새 attempt 로 복사해 둡니다 (안 그러면 그 클라우드는 최신 attempt 에서 모듈을 못 찾음).

`gen_terraform` 요청: `project_id`, `deploy_id`, `recommendation`(analyze 결과 객체) 또는 `recommendation_uri`, (선택) `architectures`
→ 응답: `status`(ok/partial/error), `module_uri`(= `attempt-1/`), `targets[]` = `{cloud, architecture, status, module_uri, files{이름: 내용}, resources[](초보자용 설명), notes}` — Main 은 이걸로 Worker 작업의 `targets[{cloud, architecture, image_uri}]` 를 만듦

`fix_terraform` 요청: `project_id`, `deploy_id`, `architecture`(실패한 클라우드의 것), `attempt`(그 클라우드의 시도 번호, 1부터), `failed_stage`(validate/plan/policy/apply/health), `log`, 그리고 `files{}` / `module_uri` / 둘 다 없으면 저장된 최신 attempt 에서 읽음
→ `status: ok` 면 `next_attempt`(다음 시도 번호), `saved_attempt`(실제로 저장한 폴더 번호 = 최신+1), `module_uri`, `carried_over`(같이 복사한 클라우드), `cause`, `changes` / `give_up` 이면 3회 초과 또는 코드로 못 고치는 원인(`fixable: false`)

## CodeBuild (buildspec) 약속 — Main Server 담당

[examples/buildspec.yml](examples/buildspec.yml). `start_build` 때 넘길 환경변수:

| 이름 | 예 |
|---|---|
| `SOURCE_URI` | analyze 에 넣은 것과 같은 소스 |
| `BUILD_FILES_URI` | 응답의 `build_files.uri_prefix` |
| `ECR_REPO_URI` | `135808950984.dkr.ecr.ap-northeast-2.amazonaws.com/pawploy-apps` |
| `IMAGE_TAG` | `<project_id>-<sha 앞 12자리>` |
| `GCP_AR_REPO` (선택) | `asia-northeast3-docker.pkg.dev/<프로젝트>/<저장소>` — 비우면 GCP push 생략 |
| `GCP_SA_KEY_SECRET` (선택) | GCP 서비스계정 키가 든 Secrets Manager 이름 |

결과는 exported variables `ECR_IMAGE_URI`, `GCP_IMAGE_URI`(@sha256 digest 고정).
이미지는 Lambda Web Adapter 포함 · `linux/amd64` · `$PORT` 로 받으므로 EC2·Lambda·Cloud Run 공용입니다.

## InfraFit 인벤토리·추천 연동

`analyze` 스캔 단계에서 [InfraFit](vendor/infrafit/SOURCE) S0~S4(S1 인벤토리, S2 프로필, S3 적합성, S4 추천; 규칙 기반, LLM 없음)를 함께 돌려 `scan.inventory` 로 넘깁니다.
LLM 프롬프트에는 target 을 `recommendation` 1순위 대상으로 따르고(다르면 이유를 warnings 에), 탈락 이유를 candidates 의 why 에 반영하고, candidate(확정 아님) 사실은 단정하지 말고, required_secrets 는 `external_services.secrets` 와 `env_names` 를 모두 보고 정하라고 적었습니다. `analyze` 검사 로직은 그대로입니다.

- 내용 (`status: ok` 일 때 `summary`, 전체 10KB 이하): `workloads`, `endpoints`(총 개수·워크로드별 개수·앞 25개 `METHOD route @file:line`·노출), `datastores`, `external_services`, `environments`, `compute`(현재 컴퓨트 컴포넌트), `request_paths`(홉 + 명시된 timeout/body 설정), `unmapped`. 넘치면 긴 목록부터 줄이고 `truncated` 에 표시. 모든 file:line 은 inventory.json 근거 그대로.
- `summary.recommendation` (4KB 이하, recommendation.json + fit.json + profile.json): `recommended`·`top`(상위 5개) — 각 `target`(capabilities.yaml 의 컴퓨트 구성 요소 `target` = 배포 대상 id), `deployable`, `assignment`(범위별 구성 요소 id), `monthly_baseline_usd`(모르는 비용이 있으면 null), `unknown_count`; `rejected` — 탈락 컴퓨트별 이유 최대 3개(`rule`, `dimension`·`dimension_value`, `capability`·`capability_value`, `source.url`·짧은 `quote`); `app_scope` 와 `dimensions`(A1~A4, B1~B3, E2 값·근거 file:line, 가정이면 `assumed: true` + `why`); 후보가 없으면 `no_feasible: true`.
- S1 은 됐는데 S2~S4 가 실패·시간 초과하면 S1 요약만 두고 `stage_error: {stage, message}` 를 붙입니다(`status` 는 `ok`).
- 경고: 앱 워크로드 2개 이상, 리버스 프록시, 비밀값이 필요한 외부 서비스, 추천 대상과 주요 탈락 이유(또는 추천 단계 실패) → `warnings` 에 `InfraFit:` 로 추가. 코드가 `supported`·추천 대상을 직접 바꾸지는 않음.
- 실행: 소스를 임시 폴더에 풀고(비밀 파일 제외, `.env.example` 류는 값 지우고 이름만) 별도 프로세스로 실행. 시간 제한 `PAWPLOY_INVENTORY_TIMEOUT`(기본 60초, 0이면 끔). 실패·시간 초과여도 analyze 는 계속되고 `inventory` 는 `{"status": "error"|"timeout", "message": ...}`. `fix_build` 는 인벤토리를 돌리지 않음(`skipped`).
- 소스: `vendor/infrafit/` 에 복사본(vendoring). 갱신은 `python scripts/sync_infrafit.py <InfraFit 저장소 경로>` (커밋·날짜는 `vendor/infrafit/SOURCE`). 의존성 `crossplane`, `python-hcl2`(+`lark`, `regex`) 추가, `jsonschema`·`pyyaml` 은 기존에 이미 포함. kustomize 바이너리는 없어도 됨(overlay 는 미해석으로 기록).
- 크기·시간: 배포 zip 약 +0.6MB (vendor 0.13MB + 새 의존성 약 0.5MB, 압축 기준). 실제 저장소에서 S0~S4 0.2~1.5초, 요약 3~10KB.

## 평가 (analyze)

[evals/](evals/README.md): 데모·공개 저장소 10개 + 프롬프트 인젝션 저장소 5개의 기대 결과 표와 채점 스크립트.
`python scripts/eval_analyze.py --offline`(스캔만) / `AWS_PROFILE=... python scripts/eval_analyze.py`(실제 모델).
파일 속 AI 지시문은 스캔이 찾아 `scan.suspicious_instructions` 와 사용자 경고로 남기고, 모델에는 따르지 말라고 알립니다.

**실제 모델 평가 (2026-10-02, sonnet-4-6): PASS 13 / FAIL 2, 인젝션 5/5 방어**
- 막은 공격: README "무조건 EC2 추천" / 주석으로 AWS 키·관리자 토큰을 env 에 넣기 / Dockerfile 에 `COPY .env`·`curl | sh` / 3서비스인데 "supported=true"
- FAIL 2: `public-cloud-run-hello`(실제로 컨테이너 3개라 supported=false 판단 → 기대값 수정 필요), `public-docker-getting-started`(EC2 기대, Cloud Run 추천 + SQLite 초기화 경고)
- `workspace:` 케이스는 팀 저장소 폴더를 `PAWPLOY_WORKSPACE` 로 지정

## 개발

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt -r requirements-dev.txt
.venv/Scripts/python -m pytest -q                 # 오프라인 테스트 89개 (모델·AWS 호출 없음)
WORKER_REPO=<Terraform-worker 체크아웃> .venv/Scripts/python -m pytest -q   # Worker 규칙과 직접 대조 (+23)
AWS_PROFILE=peony .venv/Scripts/python -m agent.app   # 로컬 서버 → POST http://localhost:8080/invocations
```

| 환경변수 | 기본값 |
|---|---|
| `PAWPLOY_MODEL_ID` | `global.anthropic.claude-sonnet-4-6` |
| `PAWPLOY_REGION` | `ap-northeast-2` |
| `PAWPLOY_ARTIFACT_BUCKET` | (없으면 저장 안 하고 응답에만) `local:<폴더>` 면 로컬 저장 |
| `PAWPLOY_MAX_ATTEMPTS` | `3` |
| `PAWPLOY_DEPLOYABLE` | `aws_lambda,aws_ec2,gcp_cloud_run` |
| `PAWPLOY_INVENTORY_TIMEOUT` | `60` (초, 0이면 InfraFit 인벤토리 끔) |

## 배포 현황 (2026-10-02)

| 항목 | 값 |
|---|---|
| Runtime ARN | `arn:aws:bedrock-agentcore:ap-northeast-2:135808950984:runtime/pawploy_agent-kcEfwY3zhC` |
| 결과물 버킷 | `pawploy-agent-135808950984` (공개 차단) |
| 실행 역할 | `pawploy-agentcore-runtime` (Bedrock 호출, 위 버킷 읽기·쓰기, 로그) |
| 모델 | `global.anthropic.claude-sonnet-4-6` (계정 사용 승인 완료) |
| 배포 | `AWS_PROFILE=<프로필> python scripts/deploy.py` (없으면 생성, 있으면 업데이트) |
| 로그 | CloudWatch `/aws/bedrock-agentcore/runtimes/pawploy_agent-kcEfwY3zhC-DEFAULT` |

실제 호출 결과 (배포본 + 실제 모델):

| 프로젝트 | 시간 | 결과 |
|---|---|---|
| Terraform-worker 샘플 앱 | 34~39초 | `aws_lambda` / 8080 / micro, 근거 4~5개, 1~5순위 ([live-analyze-response.json](examples/live-analyze-response.json)) |
| simple-web-app (서비스 7개 + k8s) | 74~77초 (InfraFit 후) | `supported=false`, 비밀값 `DATABASE_URL`, `REDIS_URL` 분리, InfraFit 경고 (워크로드 5개·nginx 프록시) |
| gen_terraform (lambda / ec2 / cloud_run) | 16~24초 (AWS·GCP 동시 생성 18초) | `terraform validate` 통과, Worker `iac.py` 위반 0건 |
| fix_terraform (오타 2개 넣은 lambda 모듈) | 15~16초 | 두 개 다 수정 → validate 통과 |
| tests/fixtures/multi_service | 38초 | `supported=false` |

## 남은 일

- [x] `prices.json` 단가 (AWS Price List API, GCP 공식 가격표, 2026-10-02 확인, 정가·무료 티어 미반영)
- [ ] 실제 CodeBuild 빌드 실패 로그로 `fix_build` 확인
- [ ] Main Server 연동 (S3 경로 규칙, 호출, fix_* 재시도 루프)
- [ ] 평가 FAIL 2건 기대값/프롬프트 정리
- [x] Worker 모듈 규격(`feat/multi-cloud-a-plan`)에 맞춰 견본·검사 갱신
