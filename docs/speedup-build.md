**영역 ② — CodeBuild 준비 시간과 첫 빌드 성공률 (2026-10-03, KST)**

수정 브랜치는 `softwareDefine/speed-build`이며 `main(cb5eba4)` 위로 rebase했다. 코드 커밋은 `1ed2dbb`와 `103ed74`다. PR #9의 추천·Dockerfile 통합 호출, 파일 preload, 프롬프트 캐싱, 다중 이미지 Dockerfile 병렬 생성을 유지했다.

`linux_tweet_app`의 원본 Dockerfile을 보존하도록 수정한 뒤, **실제 Bedrock analyze → 첫 CodeBuild를 독립적으로 3회 실행하여 3/3 성공**했다. `fix_build`는 호출하지 않았다. PRE_BUILD의 가장 큰 비용은 소스 크기가 아니라 **첫 AWS CLI 실행 비용**이었다. 설정 변경 없이 소스 전송과 레지스트리 로그인을 겹치고 push를 병렬화해, 같은 Dockerfile을 사용한 비교에서 빌드 서비스 시간이 **39.401 → 35.871초**로 줄었다.

**범위와 실행 조건**

- 모든 AWS 호출은 `AWS_PROFILE=peony`, 계정 `135808950984`, 리전 `ap-northeast-2`로 실행했다. STS로 계정을 확인했다.
- 기존 `pawploy-build`에서만 빌드했다. 조회한 설정은 `BUILD_GENERAL1_SMALL`, `aws/codebuild/amazonlinux-x86_64-standard:5.0`, `NO_CACHE`였다. 컴퓨트·캐시·프로젝트 설정을 변경하지 않았다.
- AWS 조회, Bedrock 호출, 허용된 기존 프로젝트 빌드, 로컬 파일·테스트·커밋만 수행했다. S3에 새 소스나 build 파일을 업로드하지 않았다. 인프라 배포·리소스 설정 변경·git push·PR·외부 코멘트는 수행하지 않았다.
- 소스는 `dockersamples/linux_tweet_app`, 커밋 `23747d9b1faf5562529e10e28369dc3a661db614`의 기존 S3 스냅샷이다. 압축 크기는 기존 로그에서 14.1KiB로 관측했다.

**PRE_BUILD 분해**

CloudWatch 로그의 `Running command` 사이 타임스탬프를 빼서 계산했다. 명령 시간에는 다음 명령을 시작하는 CodeBuild 오버헤드도 포함된다. 기존 성공 빌드 `a96d3c0d-9b87-4ca5-aee1-d977b8f7786b` 기준:

| 구간 | 실측 시간 |
|---|---:|
| mkdir·작업 디렉터리 진입 | 0.628초 |
| 소스 S3 다운로드 + tar 해제 | 15.105초 |
| 소스 공통 루트 처리 | 0.045초 |
| Dockerfile 다운로드 | 0.591초 |
| dockerignore 다운로드 | 0.586초 |
| 작업 디렉터리 기록 | 0.007초 |
| ECR 로그인 | 0.573초 |
| GCP 로그인 → PRE_BUILD 종료 | 1.535초 |
| PRE_BUILD phase 전체 | 19.150초 |

첫 AWS CLI 실행을 분리한 진단 빌드 `682c6a56-b460-43b3-b8cf-7e23fa6d3eb8`에서 `time aws --version`은 **14.772초**, 그 다음 `aws s3api get-object`는 **1.102초**, `tar`는 **0.102초**였다. 버전 조회에도 같은 지연이 발생했으므로 전송량·tar 비용을 주 병목으로 설명할 수 없다. CLI 내부에서 무엇을 기다리는지(실행 파일 로딩, 파일시스템 등)는 확인하지 못했다.

시도 후 제외한 방식도 있다. 단순 `s3 cp → s3api get-object` 교체와 로그인 병렬화 후 Python 인증 병합은 PRE_BUILD **19.787초**로 개선되지 않았다(`71e0f655`). Python SDK 단일 준비 프로세스도 유효한 개선을 보이지 않아 제거했다(`d1ab11e3`). 최종 코드는 기존 AWS CLI 전송을 유지한다.

**최종 변경**

- `agent/buildfiles.py`: 소스 다운로드와 ECR·GCP 로그인을 동시에 시작한다. 두 빌드 파일은 include 필터가 있는 recursive S3 복사 한 번으로 받는다.
- 두 `docker login`은 서로 다른 임시 config 디렉터리를 사용한다. `jq`로 기존 config와 인증을 병합하고, 권한 0600으로 파일을 원자적으로 교체한다. 같은 config에 동시 쓰기하여 인증 하나가 사라지는 문제를 피한다.
- 로그인 파이프라인은 `pipefail`로 AWS 인증 조회 실패를 전파한다. 두 로그인과 두 push는 모든 PID를 기다리고, 하나라도 실패하면 해당 phase가 실패한다.
- ECR·GCP push는 병렬이다. digest는 두 push가 끝난 뒤 **해당 레지스트리의 RepoDigests 항목**을 각각 선택한다. 첫 항목을 ECR이라고 가정하지 않는다. export 값이 비면 실패한다. 레지스트리별 digest가 달랐던 보존 빌드에서도 각각 실제 push 출력의 digest와 일치했다.
- `buildspec_images()`의 다중 이미지 빌드 흐름은 변경하지 않았다. 이번 실측 대상은 단일 이미지다.
- `scripts/build.py`: `--buildspec` 로컬 override와 `--result` 결과 JSON 저장을 추가했다. CodeBuild 설정 또는 S3 파일 변경 없이 전후 비교할 수 있다. ID와 phase의 정확한 시간을 출력한다.

**동일 소스·동일 수정 Dockerfile로 비교한 빌드**

기존 S3 `attempt-2`의 Dockerfile·dockerignore를 양쪽 모두 사용했다. baseline은 S3의 기존 buildspec을, 변경 후는 로컬 최종 buildspec을 `scripts/build.py --buildspec`으로 전달했다. 태그는 운영 태그와 구분했다.

| 항목 | 변경 전 `b5a57c8f` | 변경 후 `4b16354c` | 차이 |
|---|---:|---:|---:|
| PROVISIONING | 8.340 | 9.350 | +1.010초 |
| PRE_BUILD | 19.162 | 16.501 | -2.661초 |
| BUILD | 4.230 | 4.229 | -0.001초 |
| POST_BUILD | 5.701 | 3.810 | -1.891초 |
| startTime → endTime | 39.401 | 35.871 | -3.530초 |

baseline 반복 `01517e94-f1f3-4f9d-af84-c4aeaa349faf`도 성공했고, PRE_BUILD 19.942초·POST_BUILD 5.638초·전체 41.057초였다. 서비스 시간은 CodeBuild `batch_get_builds`의 타임스탬프로 계산했다. CLI에서 표시하는 전체 소요 시간은 AWS API 요청·5초 polling을 포함하므로 이 표와 다르다. 적은 표본이므로 일반적인 절감 폭을 보장하지 않는다.

**첫 빌드 실패 원인과 수정**

원본 [Dockerfile](https://github.com/dockersamples/linux_tweet_app/blob/23747d9b1faf5562529e10e28369dc3a661db614/Dockerfile)은 nginx의 기본 설정·실행 명령을 사용하고 `EXPOSE 80 443`을 선언한다. 기존 "환경변수 PORT를 읽어야 한다" 규칙 때문에 모델이 nginx 설정을 재작성했고, 없는 `/etc/nginx/templates`에 쓰면서 실패했다. 실제 `5493c718`와 운영 v16의 `b99fd853-7bf5-42df-bc27-b3932e6eea82` 로그에서 `can't create /etc/nginx/templates/default.conf.template: nonexistent directory`를 확인했다. 참고로 브리프의 `examples/demo/judge/fix-build-input.json`은 nginx가 아니라 `requirements-missing.txt` 실패 예시다.

`buildfiles.existing_dockerfile()`은 루트 Dockerfile에 최종 stage의 명확한 literal TCP EXPOSE가 있으면 원문을 선택한다. HTTP·HTTPS의 일반적인 `80 443` 쌍은 모델 추천 포트에 관계없이 HTTP 80을 선택한다. ARG·변수 포트·UDP·불명확한 복수 포트·탐지한 개발 실행 옵션이 있으면 기존 모델 작성 흐름을 유지한다. 일반적인 모든 Dockerfile의 런타임을 증명하는 파서는 아니다.

`brain.py`는 PR #9의 통합 모델 답변을 받은 다음, 이 조건에 맞으면 모델이 작성한 Dockerfile을 **코드로 대체**하고 `container_port`를 실제 선택 포트로 맞춘다. 모델이 Dockerfile을 비워도 원본을 사용하므로 두 번째 Dockerfile 호출이 필요 없다. 따라서 nginx 템플릿 재작성은 최종 빌드에 들어가지 않는다. 생성 프롬프트도 기존 고정 포트를 유지하도록 수정했다.

이후 기존 정규화를 그대로 실행해 고정 버전 LWA COPY·ENV PORT·EXPOSE·secret COPY 검사를 유지한다. CMD·ENTRYPOINT·nginx 설정은 수정하지 않는다. 기존 Dockerfile의 `.dockerignore` 우선순위도 유지하며 Pawploy의 비밀 파일 제외 규칙을 뒤에 붙인다.

**main(cb5eba4) + 수정에서 실제 analyze → 첫 CodeBuild 3회**

각 회마다 새 Bedrock analyze를 실행했고, 결과의 포트 80·원본 CMD 보존·템플릿 재작성 없음·보존 노트를 확인했다. 생성한 Dockerfile와 ignore를 로컬 buildspec의 고정 base64 복원 명령으로 전달했다. 기존 S3 build 파일 폴더를 다운로드한 뒤 로컬 분석 결과로 덮어쓰므로 S3 쓰기는 없었다. `attempt-2` 폴더는 다운로드 운송에 사용했을 뿐, 각 검증은 새 분석 결과의 **첫 빌드**이며 retry 또는 fix_build가 아니다.

| 회차 | 전체 build ID | analyze | PRE_BUILD | BUILD | POST_BUILD | 서비스 전체 | 결과 |
|---|---|---:|---:|---:|---:|---:|---|
| 1 | `pawploy-build:128c69c7-0f18-4441-bd42-b9e13cefc283` | 27.018 | 16.645 | 4.903 | 4.307 | 40.602 | SUCCEEDED |
| 2 | `pawploy-build:f1536cf6-bb2c-4e4b-9e1a-04eaf2842efe` | 25.047 | 16.014 | 4.688 | 4.436 | 35.891 | SUCCEEDED |
| 3 | `pawploy-build:241c4dfe-bbee-4499-908e-18fa4da4a5d5` | 26.997 | 16.211 | 4.617 | 4.358 | 36.237 | SUCCEEDED |

단위는 초다. 관측 성공률은 **3/3**이다. 다른 앱의 성공률 또는 공개 URL까지의 전체 E2E 시간은 측정하지 않았다. 첫 실패→fix_build→재빌드를 피하는 효과는 이 3회에서 수정 사이클이 발생하지 않았다는 범위로 확인했다.

**품질과 남은 위험**

`PYTHONUTF8=1 python -m pytest -q`를 final main 기반 코드에서 실행했다. Worker 저장소를 `WORKER_REPO`로 지정해 상호 규격 검사도 실행했다. 결과는 **212 passed, 2 failed**다. 실패는 기존 Windows 문제인 `test_buildspec_images_loop_with_fake_docker`의 bash 경로 처리와 `test_preload_picks_run_and_dependency_files_not_docs_or_secrets`의 CRLF 비교다. 추가한 보존·불확실한 포트 fallback·비밀 파일 차단·병렬 실패 전파 검사와 조정한 통합 호출/fallback 검사는 별도 실행에서 **23 passed**였다.

최종 `AWS_PROFILE=peony python scripts/eval_analyze.py`는 **11/15, injection 3/5**였다. 브리프 기준선 13/15·5/5를 달성했다고 주장할 수 없다. 실패는 public-cloud-run-hello, public-docker-getting-started, inject-clean-memo, inject-readme-ec2의 추천 대상 검사다. 키 유출·Dockerfile 명령 주입 관련 검사는 통과했다. inject-clean-memo와 inject-readme-ec2에서는 InfraFit의 `ThreadingHTTPServer.serve_forever()` 워커 분류를 따른 EC2 추천이 관측됐다. 이 변경은 추천 분류 로직을 수정하지 않지만, 통합 모델 프롬프트도 변경되므로 추천 품질 무영향을 단정하지 않는다.

같은 입력과 환경으로 수정 전 main을 추가 평가하던 중, 효헌이가 Sonnet 4.6의 하루 토큰 할당량 초과를 알리고 **추가 Bedrock 호출 금지**를 지시했다. 해당 평가 프로세스를 즉시 종료했다. 비교 평가는 미완료이며, 기존 main과의 품질 차이를 확정하지 못했다. 지시 이후 analyze·eval·profile 모델 호출을 추가로 실행하지 않았다. 따라서 추천 품질 기준선 미충족은 남은 확인 사항이며, 이 결과를 승인된 품질 회귀 없음으로 취급하면 안 된다.

모든 AWS Lambda·EC2·Cloud Run 런타임 health 또는 공개 URL까지 배포한 것은 아니다. EXPOSE는 실제 리슨의 완전한 증명이 아니며, 개발용 파일이나 잘못된 EXPOSE가 있는 앱은 별도 확인이 필요하다. 원본 nginx `latest` 태그도 보존하므로 upstream 이미지가 바뀔 수 있다. 이것은 이번 변경이 새로 만드는 조건은 아니다.

**고정 포트 전달 근거 — Worker는 읽기만 수행**

조직의 `Terraform-worker`를 읽기 조회했고 확인한 SHA는 `2533b1521b85b974a2f5191ba7421752c1e3913e`다. 브리프에서 언급한 `agent/terraform` 디렉터리는 없으며, 이 저장소의 실제 견본 경로는 `agent/tf_reference`다. Worker의 아래 모듈과 로컬 견본에서 같은 포트 전달을 확인했다.

| 대상 | Worker의 실제 전달 | 로컬 견본 | 판정 |
|---|---|---|---|
| Lambda LWA | `modules/lambda/main.tf:60-61`: `PORT`와 `AWS_LWA_PORT`에 `var.container_port` 문자열 | `agent/tf_reference/lambda.tf:60-61` | 앱은 고정 80으로 리슨하고 LWA가 80으로 전달 가능 |
| EC2 | `modules/ec2/main.tf:35`가 template에 port 전달; `user_data.sh.tftpl:35,45`가 `PORT=...`, `-p 80:<container_port>` | `agent/tf_reference/ec2.tf`, `user_data.sh.tftpl` | 외부 80 → 앱 고정 80 매핑 가능 |
| Cloud Run | `modules/cloud_run/main.tf:62`: `ports.container_port = var.container_port` | `agent/tf_reference/cloud_run.tf:62` | 플랫폼이 지정한 앱 포트로 라우팅; 앱에 PORT 해석을 강제할 이유 없음 |
| 앱 ECS Fargate | Worker `tfworker/job.py`의 AWS 지원 아키텍처는 ec2·ec2_compose·lambda. Fargate 앱 모듈 없음 | `catalog.py`에서도 Fargate는 기본 배포 가능 목록에 없음 | 고정 포트 전달을 검증할 앱 모듈이 없음. 지원한다고 주장하지 않음 |

`tfworker/recommendation.py`는 `container_port`를 전달 필드에 포함하고, `tfworker/job.py`가 포트를 정수로 검사하며, `tfworker/render.py:49`의 루트 모듈 호출도 `container_port`를 넘긴다. `infra/setup_fargate.py`는 **Terraform Worker 자체를 SQS 소비자로 ECS에 올리는 인프라**다. 사용자 앱의 Fargate 포트 전달 모듈과 구별해야 한다.

직접 근거: [Lambda](https://github.com/Softbank-hackathon2026-peony/Terraform-worker/blob/2533b1521b85b974a2f5191ba7421752c1e3913e/modules/lambda/main.tf), [EC2 포트 매핑](https://github.com/Softbank-hackathon2026-peony/Terraform-worker/blob/2533b1521b85b974a2f5191ba7421752c1e3913e/modules/ec2/user_data.sh.tftpl), [Cloud Run](https://github.com/Softbank-hackathon2026-peony/Terraform-worker/blob/2533b1521b85b974a2f5191ba7421752c1e3913e/modules/cloud_run/main.tf), [Worker 입력 규격](https://github.com/Softbank-hackathon2026-peony/Terraform-worker/blob/2533b1521b85b974a2f5191ba7421752c1e3913e/tfworker/job.py).

컴퓨트 상향·캐시 변경의 효과는 측정하지 않았다. 추정으로는 CLI 첫 실행 비용과 프로비저닝 비용을 별도로 조사하고, 여러 시연에서 레이어를 재사용하는 경우에만 Docker 캐시를 비교할 가치가 있다. 오늘 넣을 변경에는 프로젝트 설정 변경을 포함하지 않는다.

로컬 원본 로그·build metadata·분석 결과·평가 summary·검증 harness는 `work/speed-build-evidence/`에 보관한다. 재현 시 AWS 프로필을 반드시 peony로 고정하고, `scripts/build.py --buildspec examples/buildspec.yml --result <로컬 JSON>`을 사용하면 기존 프로젝트 설정 변경 없이 최종 buildspec을 실행할 수 있다. 운영 태그와 구분된 새 태그가 필요하다.
