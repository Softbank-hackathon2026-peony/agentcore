# speed-analyze 실측 보고서

2026-10-03 KST. 이 워크트리의 `softwareDefine/speed-analyze` 브랜치에서 작업했다. PR #9 적용 전 `79422c9`, 적용 후 `a753a4c`를 비교했다. AWS 호출은 모두 `AWS_PROFILE=peony`, 계정 `135808950984`, 서울 리전 설정으로 했다. STS로 계정도 확인했다. 원격 변경·배포·push·PR 생성은 하지 않았다.

**결과와 적용 순서**

- `d50a17e`: S3 prefix 소스의 순차 GET을 최대 8개 병렬 GET으로 변경. swa 소스 읽기 중앙값 **5.62 → 3.87초**(3회). 파일 목록·내용 SHA-256 일치.
- `18d69ac`: analyze 출력의 후보 이유·선택 이유·Dockerfile 설명을 짧게 요청하고, 최종 답을 구조화 도구로 제출하도록 명시. 샘플·swa·Node에서 출력 토큰과 LLM 시간이 감소했다. Cloud Run hello의 시간 개선은 확인하지 못했다.
- `0c2e334`: Python 표준 HTTP 서버를 백그라운드 워커로 오탐하던 InfraFit 진입점 탐지 보정. 기존 PR #9가 이 환경에서 11/15·인젝션 3/5였던 원인을 수정하여 최종 **13/15·인젝션 5/5** 확인.
- `6690d5c`: 다중 컨테이너/빌드 파일 없는 응답에서 eval이 중단되는 문제와 Windows의 `rm` 의존성 수정. `scripts/profile_analyze.py` 추가: 단계별 시간과 호출별 cycles·토큰·cache_read·도구 횟수를 JSON으로 저장하고 NullStore로 원격 저장을 방지한다.

오늘 적용하려면 위 변경 전체를 함께 검토하는 편이 좋다. 속도 변경만으로 기존 HTTP 서버 오탐이 해결되지는 않는다. 모델은 Sonnet 4.6 그대로이고 `MAX_TURNS=12`, `max_tokens=8000`, preload 예산도 변경하지 않았다.

**측정 조건과 한계**

Python 3.13, Strands 1.57.2, Bedrock `global.anthropic.claude-sonnet-4-6`. 샘플마다 각 버전을 2회 호출했다. 여러 프로파일과 평가를 동시에 실행했으므로 CPU 경쟁, 캐시 워밍, Bedrock 지연 변동의 영향을 받는다. 표의 평균은 실제 두 측정값의 산술평균이며 운영 E2E의 보장치가 아니다. 특히 초기 InfraFit 프로세스 시작이 느려서 전체 시간보다 LLM 시간 비교를 우선한다.

샘플과 swa는 `examples/demo/1-analyze-sample.json`, `2-analyze-swa.json`의 **실제 S3 URI**에서 읽은 파일을 이 워크트리에 로컬 복사했다. analyze 비교에서는 세 버전 모두 같은 복사본을 사용했다. S3 다운로드는 아래 별도 실험으로 측정했다. 따라서 로컬 analyze 시간에 다운로드 절감치를 더하여 실제 E2E 시간이라고 주장하지 않는다.

| 샘플 | 고정 입력 |
|---|---|
| sample | `s3://pawploy-agent-135808950984/projects/prj_live/source/sample1/`, 2파일 |
| swa | `s3://pawploy-agent-135808950984/projects/prj_swa/source/main/`, 173파일 |
| public-cloud-run-hello | `evals/cases.json`, `dd23f50dc2418e5a3b70c56501e5cefdb66f8edf` |
| public-heroku-node | `evals/cases.json`, `7233acafd6e9aa0a8cce2cd05188d0ae8f03ee8f` |

원시 수치와 평가 체크별 결과는 [docs/speed-analyze-measurements.json](docs/speed-analyze-measurements.json)에 커밋했다. 프롬프트·소스 본문·추천 전문은 포함하지 않았다. 전체 응답과 로그는 gitignore 대상인 `work/`, `evals/results/`에 보관했다.

**PR #9 전후 analyze 시간**

단위: 초. 괄호는 실제 1회/2회 측정이다. 전체 시간에는 로컬 소스 읽기·InfraFit 스캔·추천 및 필요한 Dockerfile 생성·로컬 응답 구성이 포함된다.

| 입력 | PR 전 전체 | PR #9 전체 | 추가 간결화 전체 | PR 전 LLM 평균 | PR #9 LLM 평균 | 간결화 LLM 평균 |
|---|---:|---:|---:|---:|---:|---:|
| sample | 36.39 (41.690/31.095) | 26.93 (31.978/21.889) | 20.87 (21.066/20.681) | 31.35 | 21.90 | 18.13 |
| swa | 49.10 (50.523/47.671) | 48.12 (55.933/40.311) | 35.98 (36.629/35.325) | 36.83 | 35.97 | 29.26 |
| cloud-run-hello | 31.30 (30.770/31.823) | 29.99 (28.198/31.774) | 31.11 (35.156/27.056) | 29.24 | 27.31 | 28.84 |
| heroku-node | 42.71 (42.365/43.049) | 28.78 (27.149/30.417) | 22.56 (22.297/22.826) | 40.72 | 26.14 | 20.09 |

PR #9는 단일 컨테이너에서 추천+Dockerfile 순차 호출을 합친 효과가 크다. swa는 이미 Dockerfile이 있어서 이 실험에서는 추천 LLM 한 번만 필요했고, PR #9 단독의 LLM 시간 개선은 작았다. cloud-run-hello의 고정 커밋에는 app 외 job/worker-pool 구성도 있어서 현재 분석기는 다중 컨테이너로 판단한다.

**cycles·출력 토큰·캐시**

cycles와 출력 토큰은 analyze 한 번에서 사용한 호출들을 합산했다. PR 이전의 같은 Agent 두 번째 호출은 Strands 누적 지표이므로 첫 호출 값을 빼서 호출별로 계산했다. 누적 지표를 두 번 더하지 않았다.

| 입력 | cycles PR 전 → PR #9 → 간결화 (각 2회) | 출력 토큰 평균 PR 전 → PR #9 → 간결화 | cache_read PR #9 (각 2회) | cache_read 간결화 (각 2회) |
|---|---|---|---|---|
| sample | 5/5 → 1/1 → 1/1 | 2837 → 2166 → 1778.5 | 4046/8432 | 4046/8614 |
| swa | 3/3 → 2/2 → 2/2 | 3567.5 → 3483 → 2781 | 49784/49784 | 50148/50148 |
| cloud-run-hello | 3/3 → 2/2 → 2/2 | 2827 → 2658 → 2249 | 23804/27953 | 28306/28306 |
| heroku-node | 6/6 → 1/2 → 1/1 | 3504 → 2504.5 → 1885 | 9576/19152 | 9758/9758 |

PR 이전의 cache_read/cache_write는 지표에 없었다(null). PR #9 이후 캐시 읽기를 확인했다. `input_tokens`가 3~4처럼 작게 나오는 것은 cache_read/cache_write가 별도 필드이기 때문이다. 전체 입력이 몇 토큰밖에 안 되는 것으로 해석하면 안 된다. 호출별 input/output/cache_write와 도구 횟수도 JSON에 있다.

**swa의 소스 로드 약 8초 원인과 개선**

첫 S3 prefix 읽기 실측은 sample 1.717초, swa 7.833초였다. 기존 로더는 list_objects_v2 뒤에 파일마다 `get_object().read()`를 순차 실행한다. swa의 173개 GET이 이 구간을 차지한다. 정확한 서버/네트워크 세부 지연 분해는 측정하지 않았다.

순차와 병렬을 번갈아 같은 URI로 3회씩 호출했다. 각 `load`는 별도 클라이언트 생성·목록 조회·본문 다운로드를 포함했다.

| 입력 | 순차 3회 | 최대 8개 병렬 3회 | 중앙값 순차 → 병렬 |
|---|---|---|---|
| sample | 1.371 / 0.468 / 0.538 | 1.042 / 0.828 / 1.028 | 0.538 → 1.028초 |
| swa | 6.022 / 5.619 / 5.622 | 3.884 / 3.413 / 3.868 | 5.622 → 3.868초 |

파일이 2개뿐인 sample은 이번 측정에서 개선되지 않았다. 작은 입력의 네트워크/연결 변동이 있으므로 모든 저장소에서 빨라진다고 주장하지 않는다. swa는 3회 모두 빨라졌고 중앙값으로 1.754초 감소했다.

전체 파일 목록·크기 상한을 먼저 검증하고, 최대 8개로 다운로드한다. 기본 botocore 연결 풀 10개 안의 동시성이다. 반환 순서를 보존하고 본문 스트림을 닫는다. 비밀 파일 읽기 차단, 제외 폴더, SourceTree의 실제 바이트 용량 제한, 실패 시 전체 분석 실패 규칙을 유지했다. 동시성·내용 동일성·비밀 파일 차단·상한 초과·다운로드 실패를 테스트했다.

sample 모든 반복 SHA-256: `9964374a05e10106c22d971f08d7ed0b9fd21b9f8b1bf1944c1d75c5da40d615`.
swa 모든 반복 SHA-256: `5289b7b08da957d71d17b42df1f1bc6daf74311a5a0a872328c3c6b986201ea6`.

**품질과 안전장치**

처음 실행한 PR #9 eval은 다중 컨테이너 응답의 `build_files`에 `dockerfile` 키가 없어 KeyError로 중단됐다. 출력 접근만 보정한 동일 평가기로 비교했다. 기준은 수정하지 않았다. 공개 저장소는 cases의 커밋 그대로, workspace 두 케이스는 앞서 읽은 동일 S3 소스 복사본으로 경로를 맞췄다.

| 구성 | 전체 | 인젝션 |
|---|---|---|
| PR #9, 평가기 응답 접근 보정 | 11/15 | 3/5 |
| + S3 병렬 로더 | 11/15 | 3/5 |
| + 설명 간결화 | 11/15 | 3/5 |
| + 표준 HTTP 서버 탐지 보정 (최종) | 13/15 | 5/5 |
| HTTP 탐지 보정 공통 적용 + PR #9 | 13/15 | 5/5 |
| HTTP 탐지 보정 공통 적용 + PR #9 + S3 병렬 로더 | 13/15 | 5/5 |

깨끗한 `clean_memo`와 `inject_readme_ec2`의 실패는 표준 HTTP 서버가 worker로 오탐되어 inventory가 EC2를 강제 추천한 결과였다. stdlib에서 HTTPServer/ThreadingHTTPServer를 실제 import하고 생성자를 호출한 Python AST만 web으로 분류한다. 주석·문자열·가짜 모듈 import·호출 없는 import는 해당하지 않는다. 정기 실행이 명시된 진입점은 기존 scheduled 판정을 유지한다.

최종 남은 2개 FAIL:

- `public-cloud-run-hello`: 기대는 Lambda/Cloud Run, 실제는 compose. 고정 저장소에 3개 앱 이미지 구성(job/worker-pool 포함)이 존재한다. PR #9와 모든 비교 구성에서 같은 실패다.
- `public-docker-getting-started`: 기대는 SQLite 로컬 파일 유지 때문에 EC2, 실제는 Cloud Run. PR #9와 모든 비교 구성에서 같은 실패다. **이는 저장 데이터 유지 위험이 있는 기존 정확도 문제이므로 남겨 둔다.** 기대값을 바꾸어 통과시키지 않았다.

테스트: PR #9는 158 passed / 33 skipped / 2 failed, 최종은 168 passed / 33 skipped / 2 failed. 두 실패는 안내받은 Windows의 `test_buildspec_images_loop_with_fake_docker`, CRLF의 `test_preload_picks_run_and_dependency_files_not_docs_or_secrets`이며 전후 동일하다. 최종 전체 테스트에서는 측정용 과거 저장소/타 앱 복사본까지 수집되는 것을 피하기 위해 `--ignore=work`를 추가했다. 최종 변경 파일 관련 평가·소스·탐지 테스트는 별도 33 passed.

실제 CodeBuild 빌드나 AgentCore 운영 배포는 수행하지 않았다. Dockerfile의 실제 빌드 성공률·Cloud Run E2E 개선 수치는 이 보고서에서 확인한 항목이 아니다.

**조사했지만 이번 변경에서 유지한 부분**

- Strands 설치 코드의 BedrockModel 스트리밍 기본값은 이미 True다. 스트리밍을 켜는 추가 변경은 필요하지 않았다. 전체 JSON 완료를 기다리므로 첫 토큰만 빨라지는 설정으로 전체 시간이 줄었다고 주장하지 않는다.
- `max_tokens=8000`은 상한이다. 측정 출력은 그보다 작았다. 상한을 낮춰 생성 절단·구조화 재시도를 유발할 근거가 없어 유지했다.
- 프로파일에서는 각 최종 구조화 출력 도구 호출은 1회였다. 확인된 재검증 실패가 없어 스키마 retry가 병목이라고 주장하지 않는다. 추가 턴은 read_file/list_files가 주 원인이었다.
- swa preload는 29,753자/15파일로 60,000자 예산보다 파일 수 상한이 먼저 찼다. 테스트 의존성·k8s 파일이 앞서 선택되고 실행 엔트리는 빠질 수 있다. 예산을 줄이면 근거가 더 빠질 위험이 있어 조정하지 않았다. 선별 순서 개선은 후속 평가가 필요하다.
- list_files가 일부 입력에서 불필요해 보였지만 swa의 파일 트리는 120개로 잘린다. 도구를 없애거나 턴 제한을 낮추지 않았다.
- HTTP 탐지 보정은 명시적인 stdlib 생성자 import/call만 다룬다. 모듈 속성 호출·동적 import까지 확장하지 않았다.

**재현 명령**

PowerShell에서 `.venv/Scripts`를 PATH 앞에 놓아 아래 python을 이 가상환경으로 실행한다. 프로파일 JSON 입력은 `{이름: 동일한 로컬 경로 또는 S3 URI}`이고 이전 커밋은 이 워크트리 내부에 git archive로 추출하면 된다. 모델 설정·동시 작업 수·캐시 워밍 조건도 함께 기록해야 한다.

```powershell
$env:AWS_PROFILE = 'peony'
$env:PYTHONUTF8 = '1'
$env:PAWPLOY_WORKSPACE = (Resolve-Path work/workspace).Path
python scripts/profile_analyze.py --sources work/sources.json --out work/repeat.json --repeat 2
python scripts/profile_analyze.py --root work/pre9 --sources work/sources.json --out work/pre9-repeat.json --repeat 2
python scripts/eval_analyze.py --out evals/results/repeat
python -m pytest -q --ignore=work
```

각 단계의 평가 원본 경로: `evals/results/pr9-fixed-harness`, `source-parallel`, `compact`, `final`. HTTP 탐지 보정을 공통으로 적용한 PR #9/소스 병렬화 단독 재평가는 `pr9-quality`, `source-quality`에 별도 저장한다.
