# Terraform 생성·수정 시간 단축 — speed-terraform

2026-10-03 로컬 실측. 구현 커밋: `652d218`.

표준 단일 컨테이너 모듈을 LLM 없이 검증된 견본으로 반환하고, Terraform 수정은 전체 파일 대신 최소 교체를 받도록 구현했다. 생성 평균 **20.029초 → 0.021초**, 수정 평균 **14.000초 → 6.652초**. 운영 GitHub URL → 공개 URL E2E를 재측정한 숫자는 아니다.

## 변경

- `agent/terraform.py`: `standard_module()`은 저장소의 EC2·Lambda·Cloud Run 견본을 그대로 읽는다. 이미지·포트·크기·환경변수·헬스체크는 이미 Worker 입력 변수이므로 생성 시 값 치환도 필요 없다. 리소스 설명은 코드에서 반환한다.
- `agent/brain.py`: `StrandsBrain.gen_terraform()`은 기본적으로 표준 모듈을 반환한다. 기존 AWS+GCP 병렬 처리, 아키텍처 선택, 저장 경로, 응답 구조는 유지한다. 기존 ec2_compose 렌더러도 유지한다.
- `fix_terraform()`은 `TerraformPatch`(파일 이름·old·new)를 받아 코드에서 순서대로 적용하고 기존 `TerraformFix`로 반환한다. old가 정확히 한 곳에 있을 때만 적용한다. 파일 추가·삭제를 허용하지 않으며 ec2_compose는 main.tf만 수정한다. 모호한 교체는 원본을 사용한 기존 전체 파일 LLM 수정으로 돌아간다.
- 수정 호출의 `TF_SYSTEM_PROMPT + 견본`을 고정 system prefix에 넣고 Bedrock cache point를 둔다. 현재 코드·로그·검사 오류는 그 뒤에 둔다.
- 두 경로 모두 **기존 `check_files`를 수정하지 않고** 통과한 결과만 저장한다. 다른 클라우드 복사·attempt 증가·give_up 규칙도 유지한다.
- `scripts/profile_terraform.py`: peony 프로필과 계정 135808950984를 검사하고 승인된 로컬 examples로 실제 경로를 실행한다. 저장은 `LocalStore`만 사용한다. 시간·LLM 호출 횟수·입출력 토큰·캐시 토큰·검사 결과를 기록한다.
- 보안 위반 견본/교체 차단, 모호한 교체 fallback, 외부 원인 give_up, 다른 클라우드 보존 등 회귀 테스트 12개를 추가했다.

## 측정 조건

- Windows / Python 3.13 / Strands 1.57.2 / Bedrock `global.anthropic.claude-sonnet-4-6`, `ap-northeast-2`.
- 모든 AWS 호출은 `AWS_PROFILE=peony`. STS 읽기로 계정 확인, 이후 Bedrock만 호출했다.
- 추천안: `examples/demo/1-analyze-sample.json`, `examples/demo/judge/1-analyze.json`, `examples/demo/2-analyze-swa.json`의 `output.recommendation`.
- 수정 입력: `examples/demo/4-fix-terraform.json`, `examples/demo/judge/5-fix-terraform.json`의 `payload`. 두 사례 모두 Lambda의 `package_typ`, `local.memory_mb` 오타를 포함한다. 수정 사례가 동일한 종류이므로 일반적인 apply 실패 성능으로 확대 해석하면 안 된다.
- sample/judge는 Lambda+Cloud Run, swa는 **10/2 저장된 EC2+Cloud Run 추천안**이다. 현재 multi-container swa의 ec2_compose 경로는 원래 LLM 없이 생성되므로 이번 측정의 swa 숫자와 다르다.
- 각 사례 2회. 변경 전 `--legacy`는 원래 전체 파일 생성/수정 호출을 사용한다. 변경 후도 동일 추천안·수정 파일을 사용한다. 생성 시간은 두 클라우드 병렬 생성부터 로컬 파일 저장까지, 수정 시간은 로드·LLM·검사·저장·다른 클라우드 복사까지 포함한다.
- 일부 전후 측정과 테스트가 동시에 실행됐다. 네트워크 및 호스트 부하 변동이 있는 작은 표본이다. p95나 운영 E2E 이득은 측정하지 않았다.
- 원시 숫자: `docs/terraform_profile.json`. 생성·수정 전체 파일과 호출별 상세는 커밋하지 않은 `work/terraform-profile/{before,after,patch-no-cache}/`에 있다.

## 전후 실측

단위: 초. 아래 숫자는 각 실행의 측정값이며 마지막 열은 두 실행 평균이다.

| 작업 | 변경 전 1회 / 2회 | 변경 후 1회 / 2회 | 평균 전 → 후 |
|---|---:|---:|---:|
| sample AWS+GCP 생성 | 19.064909 / 18.918445 | 0.037529 / 0.019335 | 18.992 → 0.028 |
| judge AWS+GCP 생성 | 17.796932 / 15.559179 | 0.024375 / 0.017291 | 16.678 → 0.021 |
| swa AWS+GCP 생성 | 26.096846 / 22.737522 | 0.015164 / 0.013153 | 24.417 → 0.014 |
| sample Lambda 수정 | 13.732044 / 13.311628 | 7.578926 / 6.205538 | 13.522 → 6.892 |
| judge Lambda 수정 | 14.202449 / 14.754529 | 7.168092 / 5.655147 | 14.478 → 6.412 |
| 전체 생성 평균 (각 6회) | 20.028972 | 0.021141 | 측정 평균 차이 20.008초 |
| 전체 수정 평균 (각 4회) | 14.000162 | 6.651926 | 측정 평균 차이 7.348초 |

생성의 LLM 호출은 변경 전 총 12회 → 변경 후 0회. 생성 한 요청의 출력 토큰(AWS+GCP 합산)은 평균 4,340.83 → 0개다. 변경 전 생성 6회·수정 4회 모두 내부 보안 검사 재생성이 없었다. 따라서 이 표본에서는 `INTERNAL_RETRIES`가 관측된 병목이 아니며, 운영 재생성 빈도와 검사 원인의 분포는 알 수 없다. 재시도 상한 2는 유지했다.

수정은 전체 파일 출력 평균 **1,823.5 → 384.25 토큰**(약 78.9% 감소). 최소 교체 4회 모두 한 호출에 성공했고 fallback 및 보안 재시도는 없었다. 수정 결과에서도 두 오타가 사라졌고 기존 보안·리소스 설정은 유지됐다.

## 프롬프트 크기·캐시 분리 측정

문자 수는 토큰 수가 아니다. `TF_SYSTEM_PROMPT` 1,255자, 견본(헤더 포함)은 EC2 5,181자 / Lambda 3,094자 / Cloud Run 2,542자 / ec2_compose 7,295자다.

변경 전 Lambda 수정 user prompt는 6,611자였고, 최소 교체 user prompt는 3,603자다. 견본을 system으로 옮겼으므로 이를 전체 입력 감소라고 표현하면 안 된다. 캐시 없는 최소 교체 입력은 4,658토큰으로 기존 전체 파일 수정의 4,490토큰보다 오히려 크다. 핵심은 출력 토큰 감소다.

- 캐시 첫 호출: `cacheWriteInputTokens=2704`, `cacheReadInputTokens=0`.
- 이후 3회: 각 `cacheReadInputTokens=2704`, `cacheWriteInputTokens=0`, 일반 입력 1,954토큰.
- 동일 최소 교체를 캐시 없이 4회 추가 측정: 4.714537 / 6.134945 / 5.616434 / 5.427635초, 평균 **5.473388초**. 출력 352 / 453 / 384 / 342토큰.
- 캐시를 켠 평균 6.651926초보다 캐시 없는 측정이 빠르다. **이번 표본에서는 캐시의 추가 속도 이득을 입증하지 못했다.** 캐시 hit 및 일반 입력 토큰 감소는 실측했고, 속도 단축은 최소 교체 방식에서 확인했다. 실행 순서·부하·모델 출력 길이의 영향을 분리하지 못했다.
- 견본 주석을 무리하게 잘라 프롬프트를 줄이지 않았다. 생성에서는 견본 입력·전체 HCL 출력을 모두 생략했고, 수정에서는 기존 파일과 비교하여 실제 수정 부분만 출력한다.

## 품질 검증

- `PYTHONUTF8=1 python -m pytest -q`: 변경 전 **154 passed, 33 skipped, 1 failed**, 변경 후 **166 passed, 33 skipped, 1 failed**. 같은 `test_buildspec_images_loop_with_fake_docker`의 Windows 가짜 Docker 로그 파일 누락만 실패했다. 실패 원문은 `work/tests-{before,after}.txt`.
- Terraform 관련 검사(알려진 Windows 실패 1개 제외): **143 passed, 33 skipped, 1 deselected**. 새 안전성 회귀 테스트만 재실행: **12 passed**.
- Worker 저장소가 로컬에 없어 Worker 직접 대조 테스트 33개는 건너뛰었다. 저장소 내부 contract 검사는 통과했다.
- 전후 프로파일 산출물은 모두 `check_files`를 통과했다. 생성은 견본과 파일 내용이 동일한지 테스트했다. 보안 검사 우회는 없다.
- Terraform **1.13.3**, AWS provider **6.67.0**, Google provider **8.5.0**으로 `init -backend=false -input=false`와 `validate -json`을 실행해 **전후 32/32 모듈이 모두 통과**했다(각 버전: 생성 12개 + 수정 4개). provider는 처음 공식 registry에서 받아 공유했고, 이후 로컬 설치 경로를 사용했다. 처음 로컬 mirror를 install cache와 같은 경로로 설정한 일부 init 실패는 cache 설정을 제거하고 재실행해 해결했다. 검증 결과는 `docs/terraform_profile.json`에 기록했다. plan/apply는 실행하지 않았다.
- 전체 `AWS_PROFILE=peony python scripts/eval_analyze.py`는 로컬 `../Terraform-worker/examples/sample-app`이 없어서 중단됐다. `--only inject-`도 unsupported 응답에 Dockerfile이 없는 경우 기존 평가 스크립트의 `KeyError: 'dockerfile'`로 중단됐다.
- 보조 로컬 실행에서 원래 `analyze.run`과 `eval_analyze.grade`를 사용하되 Dockerfile 부재만 `.get()`으로 허용해 인젝션 5개를 채점했다: **3/5**. clean-memo부터 `aws_ec2`가 나와 clean의 target_in과 readme의 target_not_in이 실패했다. readme/code-env의 비교 앱과 같은 추천 유지, 환경변수·Dockerfile 인젝션 차단, multi-container supported 케이스는 해당 검사를 통과했다. 전체 품질 기준 **13/15 및 인젝션 5/5를 이번 환경에서 재확인했다고 주장하지 않는다.**
- 이번 diff는 analyze·Dockerfile 프롬프트·추천 스키마·`_model`·`_run`을 바꾸지 않는다. 관측된 analyze 결과의 원인을 이 Terraform 변경 탓 또는 기존 문제로 확정할 전후 동일 조건 평가는 없다. 상위 세션의 통합 평가로 확인이 필요하다.

## 재현·롤백

PowerShell, 워크트리 루트에서:

```powershell
$env:AWS_PROFILE = 'peony'
$env:AWS_DEFAULT_REGION = 'ap-northeast-2'
$env:PYTHONUTF8 = '1'
.venv/Scripts/python scripts/profile_terraform.py --label rerun-before --runs 2 --legacy
.venv/Scripts/python scripts/profile_terraform.py --label rerun-after --runs 2
.venv/Scripts/python scripts/profile_terraform.py --label rerun-no-cache --runs 2 --no-cache
.venv/Scripts/python -m pytest -q
```

환경변수는 프로세스 시작 전에 지정한다. `PAWPLOY_TF_USE_REFERENCE=0`은 원래 전체 파일 LLM 생성을, `PAWPLOY_TF_PATCH_FIX=0`은 원래 전체 파일 LLM 수정을 사용한다. `PAWPLOY_TF_CACHE_PROMPT=0`으로 최소 교체를 유지하면서 캐시만 끌 수 있다. 모두 기본값은 1이다.

## 남은 위험·권고

- 구현·로컬 파일 저장·커밋만 했다. push, PR, AgentCore 배포, IAM/CodeBuild/S3 정책 변경, 리소스 생성·수정·삭제는 하지 않았다. 금지된 네트워크 경로에도 접근하지 않았다.
- 표준 생성은 세 아키텍처의 기존 견본이 처리하는 범위다. 분석 단계에서 새 아키텍처를 도입하거나 앱별 새 IaC 기능이 필요해지면 견본을 먼저 확장·검증해야 한다. 현재 지원 대상에서는 Worker 입력 변수로 값이 전달된다.
- 최소 교체의 정확 일치 실패는 전체 파일 fallback으로 한 호출을 더 쓸 수 있다. 복잡한 EC2/Cloud Run apply 실패는 Bedrock으로 실측하지 않았다. 기존 검사 재시도·권한/할당량 give_up 처리는 유지하지만 테스트를 운영 배포 성공으로 해석하면 안 된다.
- 프롬프트 캐시를 지원하지 않는 다른 모델로 바꾸면 캐시 플래그를 꺼야 한다. 현 설정 모델은 실제 cache write/read를 확인했다.
- Terraform validate는 리소스 생성 없이 문법·provider 스키마를 확인한다. Worker의 실제 lock 파일·루트 연결·plan 정책·apply·공개 URL health 검증은 상위 환경에서 해야 한다.
- 오늘 밤 적용 우선순위는 표준 생성 생략이다. 수정 최적화도 독립 플래그로 끌 수 있어 분리 적용 가능하다. 인젝션 기준선 재확인 한계와 Worker 직접 대조 미실행은 통합 담당자에게 함께 전달한다.
