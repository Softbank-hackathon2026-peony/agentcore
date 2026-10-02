# Pawploy AgentCore 에이전트

사용자 프로젝트 소스를 읽고 **배포 방식 추천 + 빌드 파일 생성**을 하는 AI 에이전트입니다.
Amazon Bedrock AgentCore Runtime(서울)에 올라가고, Main Server가 `mode`를 정해서 호출합니다.

| mode | 그림 단계 | 담당 | 상태 |
|---|---|---|---|
| `analyze` | 05~08 분석·추천 + Dockerfile·buildspec 생성 (수정 요청 재분석 포함) | 이주호 | ✅ 배포본 실제 호출 확인 |
| `fix_build` | 11~13 빌드 실패 → Dockerfile 수정 (최대 3회) | 이주호 | ✅ 오프라인 테스트 |
| `gen_terraform` | 18~20 승인안 → Terraform 모듈 생성 | 이주호 | ✅ 실제 모델 + `terraform validate` 통과 (ec2·lambda·cloud_run) |
| `fix_terraform` | 23~25 Terraform 실패 → 수정 (최대 3회) | 이주호 | ✅ 실제 에러 로그로 수정 → validate 통과 |

## 설계 원칙

- **판단은 LLM, 검사는 코드.** LLM 출력은 외부 입력처럼 다룬다. 허용 목록·범위·실제 파일 대조를 코드로 다시 한다.
- **사용자 코드는 데이터.** 파일 안의 지시는 따르지 않는다(프롬프트 인젝션). 에이전트 도구는 `list_files`, `read_file`(읽기 전용) 두 개뿐이다.
- **buildspec은 LLM이 만들지 않는다.** CodeBuild에서 셸 명령이 실행되는 파일이라 고정 템플릿을 쓴다. LLM은 Dockerfile만 만든다.
- **숫자를 지어내지 않는다.** 비용은 `agent/prices.json`(출처·확인일 필수)으로 코드가 계산하고, 단가가 없으면 `null`.
- **덮어쓰지 않는다.** 결과는 분석·시도마다 새 경로에 저장한다.

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

AI가 만드는 것은 **Worker의 `modules/<아키텍처>/` 자리에 들어갈 모듈 하나**입니다. Worker 루트 `main.tf`(provider·필수 태그·backend·만료 예약)는 그대로 두고, 미리 만든 모듈 대신 이 모듈을 복사해서 `module "app"`으로 부르면 됩니다.

- 입력 변수는 정확히 6개: `name`, `image_uri`, `container_port`, `size`, `env`, `health_path` / 출력은 `endpoint`, `health_url`, `resource_id` (지금 모듈과 동일)
- 규칙은 Worker README "AgentCore 가 만들 Terraform 모듈" 약속과 같음. Worker `tfworker/iac.py` 검사를 `agent/terraform.py::_check_worker_iac` 에 그대로 옮겼고(주석까지 검사), 여기에 더 엄격한 검사를 더함
- 금지: `provider`(자동 제거)·`backend`·`cloud`·`module` 블록, `default_tags`·`default_labels`, provisioner(`local-exec`/`remote-exec`), `inline_policy`·`managed_policy_arns`, `access_token`, 같은 모듈 `.tftpl` 외 파일 읽기, 허용 밖 리소스·data 소스·IAM 정책, Worker가 안 넘기는 변수
- 필수: `aws_ssm_parameter` 는 `/aws/service/...` 만, EC2 `cpu_credits = "standard"`, Cloud Run `deletion_protection = false`·최대 인스턴스 1·메모리 2Gi 이하
- 허용 리소스·data 소스·IAM 정책은 Worker `policy.py`·`iac.py` 와 동일 (`cloud_run` 은 앱 전용 `google_service_account` 포함). 견본 `agent/tf_reference/` 는 Worker `modules/` 복사본
- Worker 와 어긋났는지 확인: `WORKER_REPO=<Terraform-worker 경로> python -m pytest tests/test_terraform_contract.py` (Worker 상수·모듈과 직접 비교)
- 검사에 걸리면 같은 호출 안에서 위반 내용을 모델에 돌려 최대 2번 다시 생성
- 저장: `projects/<project_id>/deploy/<deploy_id>/attempt-N/` (`main.tf`, 필요하면 `user_data.sh.tftpl`)

`gen_terraform` 요청: `project_id`, `deploy_id`, `recommendation`(analyze 결과 객체) 또는 `recommendation_uri`
→ 응답: `module_uri`, `files{이름: 내용}`, `resources[]`(초보자용 설명)

`fix_terraform` 요청: `project_id`, `deploy_id`, `architecture`, `attempt`(1부터), `failed_stage`(validate/plan/policy/apply/health), `log`, `files{}` 또는 `module_uri`
→ `status: ok` 면 `next_attempt`, `module_uri`, `cause`, `changes` / `give_up` 이면 3회 초과 또는 코드로 못 고치는 원인(`fixable: false`)

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

## 개발

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt pytest
.venv/Scripts/python -m pytest -q                 # 오프라인 테스트 (모델·AWS 호출 없음)
AWS_PROFILE=peony .venv/Scripts/python -m agent.app   # 로컬 서버 → POST http://localhost:8080/invocations
```

| 환경변수 | 기본값 |
|---|---|
| `PAWPLOY_MODEL_ID` | `global.anthropic.claude-sonnet-4-6` |
| `PAWPLOY_REGION` | `ap-northeast-2` |
| `PAWPLOY_ARTIFACT_BUCKET` | (없으면 저장 안 하고 응답에만) `local:<폴더>` 면 로컬 저장 |
| `PAWPLOY_MAX_ATTEMPTS` | `3` |
| `PAWPLOY_DEPLOYABLE` | `aws_lambda,aws_ec2,gcp_cloud_run` |

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
| Terraform-worker 샘플 앱 | 37초 | `aws_lambda` / 8080 / micro, 근거 5개, 1~5순위 ([live-analyze-response.json](examples/live-analyze-response.json)) |
| simple-web-app (서비스 7개 + k8s) | 91초 | `supported=false`, 비밀값 `DATABASE_URL`, `REDIS_URL` 분리 |
| tests/fixtures/multi_service | 38초 | `supported=false` |

## 남은 일

- [x] `prices.json` 단가 (AWS Price List API, GCP 공식 가격표, 2026-10-02 확인, 정가·무료 티어 미반영)
- [ ] 실제 CodeBuild 빌드 실패 로그로 `fix_build` 확인
- [ ] Main Server 연동 (S3 경로 규칙, 호출)
- [x] Worker 모듈 규격(`feat/multi-cloud-a-plan`)에 맞춰 견본·검사 갱신
