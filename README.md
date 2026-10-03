# Pawploy AgentCore 에이전트

사용자 프로젝트 소스를 읽고 **배포 방식 추천 + 빌드 파일 생성**을 하는 AI 에이전트입니다.
Amazon Bedrock AgentCore Runtime(서울)에 올라가고, Main Server가 `mode`를 정해서 호출합니다.

| mode | 그림 단계 | 담당 | 상태 |
|---|---|---|---|
| `analyze` | 05~08 분석·추천 + Dockerfile·buildspec 생성 (수정 요청 재분석 포함). 컨테이너 여러 개면 `deploy_units` + 이미지별 빌드 파일 | 이주호 (InfraFit 연결·인젝션 방어·여러 컨테이너: 고준서) | ✅ 배포본 실제 호출, 평가 13/15 · 인젝션 5/5 / ⚠️ 여러 컨테이너는 오프라인 테스트만 |
| `fix_build` | 11~13 빌드 실패 → Dockerfile 수정 (최대 3회). 여러 컨테이너면 `image_id` 하나만 | 이주호 | ⚠️ 오프라인 테스트만 (CodeBuild 생기면 실제 로그로 확인) |
| `gen_terraform` | 18~20 승인안 → AWS·GCP Terraform 모듈 동시 생성. 여러 컨테이너는 AWS `ec2_compose` (견본 + 코드가 렌더한 compose) | 이주호 (Worker 규격 맞춤: 고준서) | ✅ ec2·lambda·cloud_run 실제 생성 → `terraform validate` 통과 + Worker `iac.py` 위반 0건 / `ec2_compose` 는 Worker `iac.find_violations` 0건 (오프라인) |
| `fix_terraform` | 23~25 Terraform 실패 → 수정 (최대 3회). `ec2_compose` 는 main.tf 만 | 이주호 | ✅ 실제 validate 에러 수정 (로그에 없던 오타까지) → validate 통과 |

## 설계 원칙

- **판단은 LLM, 검사는 코드.** LLM 출력은 외부 입력처럼 다룬다. 허용 목록·범위·실제 파일 대조를 코드로 다시 한다.
- **사용자 코드는 데이터.** 파일 안의 지시는 따르지 않는다(프롬프트 인젝션). 에이전트 도구는 `list_files`, `read_file`(읽기 전용) 두 개뿐이다.
- **buildspec은 LLM이 만들지 않는다.** CodeBuild에서 셸 명령이 실행되는 파일이라 고정 템플릿을 쓴다. LLM은 Dockerfile만 만든다. 여러 컨테이너의 compose 파일도 코드가 만든다.
- **숫자를 지어내지 않는다.** 비용은 `agent/prices.json`(출처·확인일 필수)으로 코드가 계산하고, 단가가 없으면 `null`.
- **덮어쓰지 않는다.** 결과는 분석·시도마다 새 경로에 저장한다.

## 시연

```bash
AWS_PROFILE=peony .venv/Scripts/python scripts/demo.py           # 배포본 실제 호출 (약 2분)
.venv/Scripts/python scripts/demo.py --replay                    # 저장된 실제 결과로 재생 (AWS 없이)
.venv/Scripts/python scripts/demo.py --only 1,3                  # 일부 단계만
```

0 오늘 커밋 → 1 샘플 앱 analyze (Lambda 추천·근거·1~5순위·비용·Dockerfile) → 2 simple-web-app analyze ("지원 안 됨") → 3 gen_terraform (AWS Lambda + GCP Cloud Run 모듈 동시) → 4 fix_terraform (깨뜨린 모듈 수정).
녹화된 실제 결과: [examples/demo/](examples/demo/) (10/2 녹화. 10/3 부터 simple-web-app 은 `aws_ec2_compose` 로 지원 — 아래 [여러 컨테이너](#여러-컨테이너-ec2_compose))

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
| 3b | InfraFit S0~S4 인벤토리·추천 + `deploy_units` (별도 프로세스, 60초 제한, 실패해도 계속) | 규칙 | `inventory.py`, `vendor/infrafit` |
| 4 | Claude가 `list_files`·`read_file`로 파일을 직접 읽고 추천 (구조화 출력, 최대 12턴). 여러 컨테이너면 `unit_fixes`(빈 포트·운영용 명령·빌드 단계·entry)도 | LLM | `brain.py` |
| 5 | 같은 대화를 이어 Dockerfile 생성 (여러 컨테이너면 Dockerfile 이 없는 이미지만 따로) | LLM | `brain.py` |
| 6 | 검사·보정: 배포 가능 대상만, 근거 파일·줄 실재 확인, 비밀 env 분리(이름·값), 후보 5개 채움, 비용 계산 | 코드 | `analyze.py`, `cost.py` |
| 6a | 여러 컨테이너: deploy_units 를 소스와 대조, LLM 보완을 다시 검사, 운영 배포용 변환(마운트·DB 비밀번호·헬스체크), compose 렌더 확인 | 코드 | `units.py`, `compose.py` |
| 7 | Dockerfile 규칙 강제: Lambda Web Adapter·`PORT`·`EXPOSE`, `.env` COPY 거부 (여러 컨테이너 이미지는 어댑터 없음) | 코드 | `buildfiles.py` |
| 8 | buildspec(고정 템플릿)·dockerignore 생성 (여러 컨테이너면 이미지 목록 `images.tsv` 를 도는 템플릿) | 코드 | `buildfiles.py` |
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
| `cloud`, `architecture`, `container_port`, `size`, `health_path`, `env`, `reason` | **Terraform Worker** | `architecture`: `lambda` \| `ec2` \| `cloud_run` \| `ec2_compose`(컨테이너 여러 개) / `size`: `micro`\|`small`\|`medium`. `ec2_compose` 의 `container_port` 는 entry 컨테이너 포트(정보용), `env` 는 쓰지 않음 (환경변수는 compose 안에) |
| `target`, `label`, `summary` | 화면 | `target` = `aws_lambda` 같은 카탈로그 id |
| `clues[]` | 화면 (근거) | `file`, `line`, `finding`, `plain`(쉬운 설명), `tag` — 파일·줄은 실제 존재 확인됨 |
| `candidates[]` | 화면 (1~5순위) | `rank`, `target`, `deployable`, `fit`(0~100), `verdict`, `why`, `size_spec`, `permissions`, `cost`. InfraFit 요약 `top` 에 같은 대상이 있으면 `infrafit {rank, decided_by, unverified, worker_limit}` (InfraFit 순위·갈린 기준·능력 확인 필요·우리 Worker 상한으로 안 되는 이유). `aws_ec2_compose` 는 InfraFit `aws_ec2` 항목을 받고, 그때 `aws_ec2` 후보에는 붙이지 않음. 워크로드마다 다른 컴퓨트인 조합(`mixed`)은 붙이지 않음 |
| `infrafit` | 화면 | InfraFit 판단 요약: `service_type`, `label`, `coverage`, `unprioritized`, `criteria_order`, `why`, `recommended_target`, `outcome`, `worker_override`(우리 Worker 상한 때문에 InfraFit 1순위 대신 다른 후보를 고른 이유), `unknown_capabilities`(outcome=unverified 일 때). InfraFit 요약이 없으면 없음 |
| `cost` | 화면 | `test_1h`, `monthly`(USD, 단가 없으면 null), `assumptions` |
| `permissions` | 화면 (16. 권한 제시) | 배포될 앱이 받는 권한 |
| `required_secrets` | 화면 | 사용자가 직접 넣어야 하는 비밀 환경변수 이름 |
| `supported`, `warnings` | 화면 | 배포 가능한 대상으로 실행할 수 없는 컨테이너가 있을 때만 `supported=false` (여러 컨테이너: 이미지 없는 컨테이너, entry 없음, InfraFit 후보 없음 등). `AI 보완 (코드 확인): …` 은 LLM 이 바꾼 값 |
| `deploy_units` | gen_terraform·화면 | 컨테이너가 여러 개일 때만. InfraFit `deploy_units` 를 검사·보완한 것 + 서비스마다 `run`(운영 배포용 env·volumes·healthcheck·depends_on). 아래 [여러 컨테이너](#여러-컨테이너-ec2_compose) |

컨테이너 1개: `build_files.uri_prefix` 아래에 `Dockerfile.pawploy`, `dockerignore`, `buildspec.yml` 이 저장되고 응답 `build_files` 는 `attempt`, `uri_prefix`, `dockerfile`, `buildspec` (예전과 같음).
컨테이너 여러 개: `build_files.images = {이미지 id: {dockerfile, generated, context, target, port}}` (`dockerfile` 없음). 프로젝트 Dockerfile 이 있으면 소스 경로 그대로(`generated: false`), 없을 때만 LLM 이 만든 `Dockerfile.pawploy.<이미지 id>`(`generated: true`, 같은 폴더에 저장). 폴더에는 `images.json`, `images.tsv`, `buildspec.yml`(여러 이미지용), `dockerignore`.

### fix_build

요청 ([examples/fix-build-request.json](examples/fix-build-request.json)): `project_id`, `analysis_id`, `source_uri`, `dockerfile`(실패한 것), `build_log`, `failed_phase`, `attempt`(1부터).

- `status: ok` → `build_files.attempt` = 다음 시도 번호, `uri_prefix`에 새 파일
- `status: give_up` → 3회 초과, 또는 Dockerfile로 못 고치는 원인(권한·로그인 등, `fixable: false`)
- 여러 컨테이너: `image_id`(실패한 이미지, CodeBuild 로그의 `[pawploy] build <id>` 줄)를 넣으면 그 이미지만 고칩니다. `dockerfile` 은 생략 가능(저장된 `images.json` 또는 요청의 `images` 로 찾음). 프로젝트 Dockerfile 이었으면 소스는 그대로 두고 고친 사본 `Dockerfile.pawploy.<id>` 로 바꿔 빌드합니다(`override_of`). 새 attempt 폴더에 다른 이미지의 생성 Dockerfile 도 옮겨 두므로 `uri_prefix` 하나로 다시 빌드하면 됩니다. 응답 `build_files.images` 는 새 목록

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
(`architecture: ec2_compose` 추천안은 AWS 모듈 하나만 만들고 LLM 을 부르지 않음 — 아래 [여러 컨테이너](#여러-컨테이너-ec2_compose))
→ 응답: `status`(ok/partial/error), `module_uri`(= `attempt-1/`), `targets[]` = `{cloud, architecture, status, module_uri, files{이름: 내용}, resources[](초보자용 설명), notes}` — Main 은 이걸로 Worker 작업의 `targets[{cloud, architecture, image_uri}]` 를 만듦

`fix_terraform` 요청: `project_id`, `deploy_id`, `architecture`(실패한 클라우드의 것), `attempt`(그 클라우드의 시도 번호, 1부터), `failed_stage`(validate/plan/policy/apply/health), `log`, 그리고 `files{}` / `module_uri` / 둘 다 없으면 저장된 최신 attempt 에서 읽음
→ `status: ok` 면 `next_attempt`(다음 시도 번호), `saved_attempt`(실제로 저장한 폴더 번호 = 최신+1), `module_uri`, `carried_over`(같이 복사한 클라우드), `cause`, `changes` / `give_up` 이면 3회 초과 또는 코드로 못 고치는 원인(`fixable: false`)

## 이미지 빌드 (CodeBuild → ECR / Artifact Registry)

하나의 Dockerfile 로 한 번 빌드해서 **AWS 는 ECR, GCP 는 Artifact Registry** 에 같은 이미지를 올립니다.

| 만든 것 | 이름 | 비고 |
|---|---|---|
| CodeBuild 프로젝트 | `pawploy-build` | buildspec = `agent/buildfiles.py` 고정 템플릿 (컨테이너 1개 [examples/buildspec.yml](examples/buildspec.yml), 여러 개 [examples/buildspec-images.yml](examples/buildspec-images.yml) — 응답 `build_files.buildspec` 이 그 앱에 맞는 것), amd64·privileged, 20분 제한 |
| ECR 저장소 | `pawploy-apps` | 푸시 때 스캔, 최근 50개 보관 |
| IAM 역할 | `ppw-codebuild` | 소스 버킷(`pawploy-agent-*`, `fawploy-source-*`) 읽기, `pawploy-apps` 푸시, GCP 키 읽기, 로그 |
| GCP AR | `asia-northeast3-docker.pkg.dev/softbankhackathon2026-peony/pawploy/pawploy-apps` | 이미 있는 저장소 `pawploy`. 키는 워커 키 `pawploy/gcp-worker-key` (저장소 단위 작성자 권한 추가됨) |

만들기·갱신: `AWS_PROFILE=peony python scripts/setup_build.py [--gcp-ar-repo <AR 주소> --gcp-key-secret <Secrets Manager 이름>]`
(GCP 옵션을 주면 모든 빌드가 ECR + AR 양쪽에 올림. 키는 AR **쓰기** 권한 `roles/artifactregistry.writer` 가 있어야 함)

Main Server 는 `start_build(projectName="pawploy-build", buildspecOverride=<응답 build_files.buildspec>)` 에 환경변수 세 개를 넘기면 됩니다 (`scripts/build.py` 가 같은 호출).
**`buildspecOverride` 는 꼭 넘겨야 합니다.** 프로젝트에 박힌 buildspec 은 컨테이너 1개용이라, 여러 컨테이너 앱은 이걸 안 넘기면 이미지 하나만 빌드됩니다. 값은 analyze·fix_build 응답의 `build_files.buildspec` (= 빌드 파일 폴더의 `buildspec.yml`) 그대로:

| 이름 | 예 |
|---|---|
| `SOURCE_URI` | analyze 에 넣은 것과 같은 소스 |
| `BUILD_FILES_URI` | 응답의 `build_files.uri_prefix` (fix_build 후엔 새 attempt 폴더) |
| `IMAGE_TAG` | `<project_id>-<sha 앞 12자리>` |

`ECR_REPO_URI`·`GCP_AR_REPO`·`GCP_SA_KEY_SECRET` 은 프로젝트 기본값으로 들어 있음 (필요하면 override).
결과는 exported variables `ECR_IMAGE_URI`, `GCP_IMAGE_URI`(@sha256 digest 고정) → Worker `targets[].image_uri`.

**`.dockerignore`**: 생성한 Dockerfile 은 `Dockerfile.pawploy.dockerignore`(BuildKit 이 프로젝트 `.dockerignore` 대신 읽는 파일)로 **우리 규칙만** 씁니다 (`.git`·`node_modules`·`.env`·키 파일 등, 하위 폴더 포함). 프로젝트 `.dockerignore` 가 생성 Dockerfile 이 COPY 하는 경로를 빼서 빌드가 깨지던 문제 때문입니다 (예: Terraform-worker 저장소는 `examples`·`*.json` 을 뺌 → `COPY examples/sample-app/app.py` 가 `not found`). 같은 소스·Dockerfile 로 예전 buildspec 은 실패, 지금 buildspec 은 성공 확인 (10/3).
빌드 실패 시 `scripts/build.py` 가 로그 마지막 부분을 출력 → 그대로 `fix_build` 의 `build_log` 로.

지금 설정: `setup_build.py --gcp-ar-repo asia-northeast3-docker.pkg.dev/softbankhackathon2026-peony/pawploy/pawploy-apps --gcp-key-secret pawploy/gcp-worker-key`

실제 확인: `prj_test` 샘플 앱 빌드 → ECR + Artifact Registry 둘 다 푸시 성공 (47초), `ECR_IMAGE_URI`·`GCP_IMAGE_URI` 둘 다 digest 고정으로 나옴.
여러 컨테이너 실제 확인 (10/3, `buildspecOverride` 사용): simple-web-app 분석(68초, `ec2_compose`, 컨테이너 5개·이미지 3개) → CodeBuild 이미지 3개 빌드·푸시 성공 (88초), `IMAGE_DIGESTS` 3개 digest 고정. 같은 방식으로 샘플 앱(컨테이너 1개)도 ECR + AR 성공 (47초).
이미지는 Lambda Web Adapter 포함 · `linux/amd64` · `$PORT` 로 받으므로 EC2·Lambda·Cloud Run 공용입니다.

여러 컨테이너 (`buildspec-images.yml`, `env.shell: bash`):
- `BUILD_FILES_URI` 폴더를 통째로 받아 `images.tsv`(코드가 형식 검사: `id \t 컨텍스트 \t Dockerfile \t 단계(-=마지막) \t 생성 여부`)를 한 줄씩 빌드: `docker build -f <Dockerfile> [--target <단계>] -t $ECR_REPO_URI:$IMAGE_TAG-<id> <컨텍스트>`. 생성 Dockerfile 은 소스 루트로 복사하고 `<Dockerfile>.dockerignore` 로 우리 규칙만 씀. 프로젝트 Dockerfile 이면 컨텍스트의 `.dockerignore`(+ `<Dockerfile>.dockerignore` 가 있으면 거기도)에 비밀 파일 규칙 추가
- 결과는 exported variable `IMAGE_DIGESTS` = `{"<이미지 id>": "<ECR_REPO_URI>@sha256:…", …}` (JSON 한 줄). Main 은 이것을 Worker 작업의 `targets[].images` 에 그대로 넣음
- GCP push 없음 (`ec2_compose` 는 AWS 만). 빌드 실패 시 로그의 마지막 `[pawploy] build <id>` 가 실패한 이미지 → `fix_build(image_id=<id>)`

## 여러 컨테이너 (`ec2_compose`)

팀 계약 `one-click-deploy-agent/docs/superpowers/specs/2026-10-03-multi-container-contract.md` 2~4절. InfraFit `deploy_units`(compose → k8s → 코드 순)에 **앱 컨테이너 2개 이상, 또는 compose·k8s 에 적힌 데이터 저장소 컨테이너**가 있으면 이 경로로 갑니다. 코드 경로(`source.kind: code`)에서 InfraFit 이 만든 저장소 컨테이너는 앱 컨테이너가 2개 이상일 때만 셉니다 — 컨테이너 1개 앱(예: Flask + 코드만 Redis)은 단일 경로(Lambda·Cloud Run·EC2) 그대로이고 저장소 주소는 `required_secrets` 로 받습니다 (`units.is_multi`).

| 단계 | 방식 | 내용 |
|---|---|---|
| 검사 | 코드 (`units.check`) | id 형식(Worker `IMAGE_ID_RE` 와 같음)·중복, 빌드 컨텍스트 폴더·Dockerfile·빌드 단계가 소스에 실제로 있는지, 포트는 정수, depends_on 은 묶음 안 서비스만, entry 는 포트가 있는 컨테이너 |
| 보완 | LLM → 코드 재검사 (`units.apply_fixes`) | `unit_fixes`: 빈 포트 채우기(이미 정해진 포트는 못 바꿈), 개발용 명령(`--reload`·`nodemon`·`--inspect` …) 대신 운영용 명령(셸 연산자·비밀값·여전히 개발용이면 거절), `dev` 대신 Dockerfile 에 실제로 있는 운영 단계, entry. 반영한 것은 `warnings` 에 `AI 보완 (코드 확인)`, 거절한 것은 `validation_notes` |
| 경고 | 코드 | 남은 개발용 명령·빌드 단계, entry 하나만 80번으로 열림(공개 web 이 여럿이면 InfraFit 미해결 항목도), 코드가 쓰는 저장소(InfraFit 인벤토리)가 묶음에 컨테이너로 없으면 그 사실(접속 실패 위험), InfraFit 이 관리형 DB(묶음에 있는 저장소만)·다른 컴퓨트를 추천했으면 그 사실, AI 가 채운 포트가 코드·Dockerfile 에 없으면 "근거 없음, 확인 필요", 코드에 적힌 DB 비밀번호(`file:line`, 이 경우 무작위 비밀번호 대신 프로젝트 값) |
| 운영 변환 | 코드 (`units.prepare_run`) | compose 원본을 데이터로 읽어(yaml.safe_load) 저장소 경로 bind mount 제거·named volume 유지 (단, 빌드 없이 레지스트리 이미지를 쓰는 서비스 — `nginx:1.27-alpine` + `./nginx/nginx.conf` 같은 — 는 마운트하던 저장소 파일을 `COPY` 한 이미지 `<서비스>-baked` 를 코드가 만들어 씀. 저장소에 없거나 비밀 파일이면 빼고 경고), 실행 명령의 `$PORT`·`${X}` 는 compose 가 서버 환경변수로 바꿔 빈 값이 되므로 듣는 포트·compose 에 값이 있는 환경변수로 바꿈 (모르는 변수는 경고), 마운트한 파일에 기대는 헬스체크 제거(그 서비스를 `service_healthy` 로 기다리던 곳은 `service_started`), 한 번 실행(one_shot) 서비스는 `service_completed_successfully`, DB 비밀번호 env(`*_PASSWORD`)와 접속 URL 의 비밀번호(`postgresql://app:app@postgres` 의 `app`)는 `passwords["<저장소 id>"]` (단, 코드에 그 비밀번호가 직접 적혀 있으면 앱이 접속하지 못하므로 그 저장소는 프로젝트 값을 그대로 쓰고 경고 — 저장소 포트는 서버 안에서만 열림), `${X:-기본값}` 은 기본값, 나머지 비밀값은 이름만 (`required_secrets`, compose 에 값이 이미 들어가는 이름은 빼고) |
| 렌더 | 코드 (`compose.render`) | `deploy_units` 만 보고 `compose.yaml.tftpl` (같은 입력 → 같은 결과). 서비스 이름 = id 그대로, 빌드한 이미지 `${images["<id>"]}`·레지스트리 이미지 그대로, entry 만 `ports: ["80:<포트>"]`, 사용자 값은 모두 큰따옴표 문자열 + `${`→`$${`·`%{`→`%%{`, 사용자 비밀값은 `""` + 주석. Worker 금지 설정(`privileged`·host 네트워크·`docker.sock`·`cap_add`·`devices`·`build`·파일 함수)은 렌더 단계에서 다시 검사 |
| 모듈 | 코드 (`terraform.compose_module`) | Worker `modules/ec2_compose` 견본(`agent/tf_reference/ec2_compose.tf`, `ec2_compose_user_data.sh.tftpl`) + 렌더한 템플릿. 입력 `name`, `images`, `size`, `health_path` / 출력 `endpoint`, `health_url`, `resource_id`. 응답 target 에 `images`(필요한 이미지 id), `passwords`(모듈이 만들 비밀번호 id), `required_secrets` |
| 수정 | LLM (main.tf 만) | `fix_terraform(architecture=ec2_compose)`: LLM 결과에서 main.tf 만 쓰고 user_data 는 지금 것, compose 는 `recommendation`(또는 `recommendation_uri`)을 주면 다시 렌더, 안 주면 gen 때 코드가 만든 것 그대로 |

- 데이터 저장소는 v1 에서 같은 서버의 컨테이너입니다(관리형 RDS·ElastiCache 아님). 서버를 지우면 데이터도 사라집니다
- 사용자 비밀값(외부 API 키 등)은 아직 Worker 작업으로 넘길 길이 없어 빈 값으로 둡니다 → `required_secrets`·`warnings` 에 이름
- 비용은 같은 인스턴스 타입의 EC2 단가(`cost.SAME_PRICE_AS`), 컨테이너가 많으면 medium 권장 (t3.micro 에 3개 넘으면 경고)

## InfraFit 인벤토리·추천 연동

`analyze` 스캔 단계에서 [InfraFit](vendor/infrafit/SOURCE) S0~S4(S1 인벤토리, S2 프로필, S3 적합성, S4 추천; 규칙 기반, LLM 없음)를 함께 돌려 `scan.inventory` 로 넘깁니다.
LLM 프롬프트에는 target 을 `recommendation` 1순위 대상으로 따르고(다르면 이유를 warnings 에), 탈락 이유를 candidates 의 why 에 반영하고, candidates 의 why 는 `ranking` 의 기준 순서로 설명하고, `unverified: true` 후보는 확정적으로 추천하지 말고 warnings 에 확인 필요로 적고(1순위가 unverified 면 target 은 그대로 따르되 warnings 에 확인 필요; `outcome: unverified` 면 top 1순위를 따르고 `unknown_capabilities` 를 확인 필요로), 비용 숫자는 쓰지 말고, candidate(확정 아님) 사실은 단정하지 말고, required_secrets 는 `external_services.secrets` 와 `env_names` 를 모두 보고 정하라고 적었습니다. `analyze` 검사 로직은 그대로입니다.

- 코드 경로 저장소 (029e195): compose·k8s 가 없어도 코드가 쓰는 Redis 는 `redis:7-alpine` 저장소 컨테이너(id `redis`)로 묶음에 들어가고, 앱 컨테이너는 `depends_on: [redis]`, 코드가 loopback 기본값으로 읽는 환경변수(`os.environ.get("REDIS_URL", "redis://localhost:6379/0")`)에 `redis://redis:6379/0` 을 넣음(못 넣으면 `unresolved containers.<id>.env`). 비밀번호가 필요한 저장소(postgres 등)는 컨테이너 없이 `unresolved datastores.<범위>` → 묶음에 없는 저장소 경고. 이 저장소 컨테이너로 여러 컨테이너 경로가 되는 것은 앱 컨테이너가 2개 이상일 때뿐 (예: Procfile web·worker·beat). 여러 컨테이너 경로에서는 스캔 경고 `외부 데이터베이스/캐시가 필요해 보입니다: …` 에서 묶음에 컨테이너로 든 저장소(이미지 이름, 없으면 InfraFit 범위 id)를 빼고, 다 들어 있으면 그 경고를 지움. Procfile `beat`·`celery … beat` 는 `scheduled` 컨테이너
- `deploy_units`: 결과 최상위 `inventory.deploy_units` 에 InfraFit 것 **전체**(근거 포함, 코드가 검사·렌더에 씀), 프롬프트용 `summary.deploy_units` 는 이름·구조만(3KB 이하, 전체 10KB 안에 포함). 프롬프트에는 전체를 넣지 않음
- 내용 (`status: ok` 일 때 `summary`, 전체 10KB 이하): `workloads`, `endpoints`(총 개수·워크로드별 개수·앞 25개 `METHOD route @file:line`·노출), `datastores`, `external_services`, `environments`, `compute`(현재 컴퓨트 컴포넌트), `request_paths`(홉 + 명시된 timeout/body 설정), `unmapped`. 넘치면 긴 목록부터 줄이고 `truncated` 에 표시. 모든 file:line 은 inventory.json 근거 그대로.
- `summary.recommendation` (4.5KB 이하 = `MAX_RECOMMENDATION_BYTES` 4500, recommendation.json + fit.json + profile.json): `recommended`·`top`(상위 5개) — 각 `target`(capabilities.yaml 의 컴퓨트 구성 요소 `target` = 배포 대상 id, 호환용으로 첫 대상), `targets`(placement 순서의 서로 다른 대상), `mixed: true`(워크로드마다 다른 컴퓨트, 예: api=Lambda + worker=Fargate), `deployable`, `assignment`(범위별 구성 요소 id), `topology`, `decided_by`(바로 아래 후보와 갈린 기준), `unverified: true`(탐지한 요구에 대한 플랫폼 능력을 모름, 확인 필요) + `unknown_capabilities`(InfraFit 후보 `unknown` 의 능력 이름, 중복 없이 최대 3), `worker_limit`(우리 Worker 상한 Lambda 30초·Cloud Run 60초로는 안 되는 이유, 1순위가 그러면 `worker_override` 와 함께 다음 후보로 바꿈 — unverified 후보로는 바꾸지 않고, 바꿀 후보가 없으면 1순위에 `worker_limit` 표시), `unknown_count`; `ranking` — `service_type`, `label`, `coverage`, `unprioritized`, `criteria_order`, `why` (InfraFit 서비스 유형별 비교 기준 순서); `rejected` — 탈락 컴퓨트별 이유 최대 3개(`rule`, `dimension`·`dimension_value`, `scope`(위반한 워크로드 범위, 값도 그 범위의 것), `capability`·`capability_value`, `source.url`·짧은 `quote`); `app_scope`(앱 집계 범위, 보통 `w-app`) 와 `dimensions`(A1~A4, B1~B3, E2 값·근거 file:line, 가정이면 `assumed: true` + `why`); 후보가 없으면 `no_feasible: true`; `outcome`·`outcome_detail`(문장); 모든 후보가 능력 확인 필요면 InfraFit 은 `recommended: null`, `outcome: "unverified"` → 요약 `unknown_capabilities`(확인 못 한 능력, 별도 키) + 경고 `InfraFit: 조건 충족을 확인한 후보가 없습니다 (확인 못 한 능력: …)`. 넘치면 탈락 근거 인용 → 후보 수 → 탈락 수 → 차원 `why` → `ranking.why`(120자) 순으로 줄임.
- S1 은 됐는데 S2~S4 가 실패·시간 초과하면 S1 요약만 두고 `stage_error: {stage, message}` 를 붙입니다(`status` 는 `ok`).
- 경고: 앱 워크로드 2개 이상, 리버스 프록시, 비밀값이 필요한 외부 서비스, `InfraFit: <유형> 유형으로 판단 — <기준 순서> 순으로 비교`, 추천 대상·갈린 기준(동률이면 `동률 (이름순)`·`동률 (모름 수)`)과 주요 탈락 이유(워크로드가 여럿이면 `w-worker A1=워커` 처럼 위반한 범위 표시) 또는 추천 단계 실패 → `warnings` 에 `InfraFit:` 로 추가. 컨테이너 1개 앱은 코드가 `supported`·추천 대상을 직접 바꾸지 않음 (예외: 웹소켓(A3, 가정 아님)을 근거와 함께 탐지했는데 대상이 Lambda 면 코드가 다음 배포 가능 대상으로 바꿈 — InfraFit 순위 → LLM 후보 → `aws_ec2`, 경고·`reason` 머리말·Lambda 후보 `부적합` 표시) (여러 컨테이너면 대상은 `aws_ec2_compose`, InfraFit 후보가 없으면 `supported=false`).
- 실행: 소스를 임시 폴더에 풀고(비밀 파일 제외, `.env.example` 류는 값 지우고 이름만) 별도 프로세스로 실행. 시간 제한 `PAWPLOY_INVENTORY_TIMEOUT`(기본 60초, 0이면 끔). 실패·시간 초과여도 analyze 는 계속되고 `inventory` 는 `{"status": "error"|"timeout", "message": ...}`. `fix_build` 는 인벤토리를 돌리지 않음(`skipped`).
- 소스: `vendor/infrafit/` 에 복사본(vendoring). 갱신은 `python scripts/sync_infrafit.py <InfraFit 저장소 경로>` (커밋·날짜는 `vendor/infrafit/SOURCE`, 지금 029e195). 의존성 `crossplane`, `python-hcl2`(+`lark`, `regex`) 추가, `jsonschema`·`pyyaml` 은 기존에 이미 포함. kustomize 바이너리는 없어도 됨(overlay 는 미해석으로 기록).
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

QA(실제 런타임 시나리오 검증) 방법·시나리오 목록·결과: [docs/qa.md](docs/qa.md)

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt -r requirements-dev.txt
.venv/Scripts/python -m pytest -q                 # 오프라인 테스트 153개 (모델·AWS 호출 없음)
WORKER_REPO=<Terraform-worker 체크아웃> .venv/Scripts/python -m pytest -q   # Worker 규칙과 직접 대조 (+33, Worker main)
AWS_PROFILE=peony .venv/Scripts/python -m agent.app   # 로컬 서버 → POST http://localhost:8080/invocations
```

| 환경변수 | 기본값 |
|---|---|
| `PAWPLOY_MODEL_ID` | `global.anthropic.claude-sonnet-4-6` |
| `PAWPLOY_REGION` | `ap-northeast-2` |
| `PAWPLOY_ARTIFACT_BUCKET` | (없으면 저장 안 하고 응답에만) `local:<폴더>` 면 로컬 저장 |
| `PAWPLOY_MAX_ATTEMPTS` | `3` |
| `PAWPLOY_DEPLOYABLE` | `aws_lambda,aws_ec2,aws_ec2_compose,gcp_cloud_run` (`aws_ec2_compose` 를 빼면 여러 컨테이너 앱은 `supported=false`) |
| `PAWPLOY_INVENTORY_TIMEOUT` | `60` (초, 0이면 InfraFit 인벤토리 끔) |

## 배포 현황 (2026-10-02)

| 항목 | 값 |
|---|---|
| Runtime ARN | `arn:aws:bedrock-agentcore:ap-northeast-2:135808950984:runtime/pawploy_agent-kcEfwY3zhC` |
| 결과물 버킷 | `pawploy-agent-135808950984` (공개 차단) |
| 실행 역할 | `ppw-agentcore-runtime` (Bedrock 호출, 위 버킷 읽기·쓰기, 로그) |
| 모델 | `global.anthropic.claude-sonnet-4-6` (계정 사용 승인 완료) |
| 배포 | `AWS_PROFILE=<프로필> python scripts/deploy.py` (없으면 생성, 있으면 업데이트) |
| 로그 | CloudWatch `/aws/bedrock-agentcore/runtimes/pawploy_agent-kcEfwY3zhC-DEFAULT` |

실제 호출 결과 (배포본 + 실제 모델):

| 프로젝트 | 시간 | 결과 |
|---|---|---|
| Terraform-worker 샘플 앱 | 34~39초 | `aws_lambda` / 8080 / micro, 근거 4~5개, 1~5순위 ([live-analyze-response.json](examples/live-analyze-response.json)) |
| simple-web-app (서비스 7개 + k8s) | 74~77초 (InfraFit 후) | `supported=false`, 비밀값 `DATABASE_URL`, `REDIS_URL` 분리, InfraFit 경고 (워크로드 5개·nginx 프록시) — 10/2 결과, 10/3 부터 `aws_ec2_compose` |
| gen_terraform (lambda / ec2 / cloud_run) | 16~24초 (AWS·GCP 동시 생성 18초) | `terraform validate` 통과, Worker `iac.py` 위반 0건 |
| fix_terraform (오타 2개 넣은 lambda 모듈) | 15~16초 | 두 개 다 수정 → validate 통과 |
| tests/fixtures/multi_service | 38초 | `supported=false` — 10/2 결과, 10/3 부터 `aws_ec2_compose` |

## 남은 일

- [x] `prices.json` 단가 (AWS Price List API, GCP 공식 가격표, 2026-10-02 확인, 정가·무료 티어 미반영)
- [ ] 실제 CodeBuild 빌드 실패 로그로 `fix_build` 확인
- [ ] Main Server 연동 (S3 경로 규칙, 호출, fix_* 재시도 루프)
- [ ] 평가 FAIL 2건 기대값/프롬프트 정리
- [x] Worker 모듈 규격(`feat/multi-cloud-a-plan`)에 맞춰 견본·검사 갱신
- [x] 여러 컨테이너: deploy_units 검사·보완 → 이미지별 빌드 파일 → `ec2_compose` 모듈 (Worker `feat/multi-container` 066d88d 규격, 오프라인 테스트)
- [ ] 여러 컨테이너 실제 모델 호출·평가 (simple-web-app, example-voting-app) + 실제 CodeBuild·Worker 한 바퀴
- [ ] 사용자 비밀값을 compose 로 넘기는 길 (지금은 빈 값) — Worker 작업 입력 계약 필요
- [ ] 여러 컨테이너 analyze 응답 예시 (`examples/`)
