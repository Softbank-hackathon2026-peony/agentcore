# InfraFit 연동 코드 정리 (agent/infrafit 패키지) — 설계

날짜: 2026-10-03 · 브랜치: `refactor/infrafit-package` (기준 `feat/analyze-speedup-main` a753a4c)

## 1. 문제

InfraFit(규칙 기반 S0~S4 분석, `vendor/infrafit/`)을 쓰는 우리 쪽 코드가 읽기 어렵다.

- **흩어짐**: InfraFit 관련 코드가 5개 파일에 나뉘어 있다.
  `agent/inventory.py`(실행 + S1 요약 + S2~S4 추천 요약 + Worker 상한 대체), `agent/scan.py`(경고 문장 ~130줄),
  `agent/analyze.py`(`_infrafit_view`·`_attach_infrafit`·`_infrafit_compute_warnings`·`_engine`·`_websocket_*` 등),
  `agent/units.py`·`agent/brain.py`(`scan["inventory"]` 를 직접 파고듦).
- **타입 없는 dict**: `((scan.get("inventory") or {}).get("summary") or {}).get("recommendation") or {}` 같은 체인이
  여러 파일에 반복된다. 무엇이 들어 있는지 만든 곳을 읽어야 안다.
- **이름**: `rec`·`reco`·`recommendation` 이 서로 다른 것(LLM 출력 / InfraFit 요약 / 응답)이다. `_candidate()` 가
  `inventory.py`·`analyze.py` 에 다른 뜻으로 두 개. `run_inventory` 는 S1 만이 아니라 S0~S4 를 돌린다.
- **종속성**: `scan` 이 서브프로세스를 띄우고, `units` ↔ `analyze` 가 서로 import 한다 (`units.py:16`).

`vendor/infrafit/` 자체는 `scripts/sync_infrafit.py` 로 덮어쓰는 복사본이라 **범위 밖**이다.

## 2. 목표 / 비목표

목표
- InfraFit 연동 코드를 `agent/infrafit/` 한 패키지에 모으고, 밖에서는 이 패키지의 공개 API 만 쓴다.
- InfraFit 결과를 dataclass 로 표현한다 (필드·메서드가 IDE 에 보이게).
- 종속성을 한 방향으로 만든다 (`infrafit/` 은 `analyze`·`scan`·`units`·`brain` 을 import 하지 않음, `units`↔`analyze` 순환 제거).
- 헷갈리는 이름을 바꾼다.

비목표 (동작은 그대로)
- 응답 JSON·저장되는 `recommendation.json`·LLM 프롬프트에 들어가는 JSON 의 모양과 값은 **바이트 단위로 같다**.
- `deploy_units` 는 dict 그대로 둔다 (`units.py` 가 제자리에서 고치고 응답·compose·Terraform 계약에 그대로 나감).
- 프롬프트용으로 줄인 데이터를 코드가 판단에 쓰는 지금 동작도 그대로 둔다 (§7 후속 과제).

## 3. 파일 구조

```
agent/infrafit/
  __init__.py      공개 API + 흐름 그림 docstring. 밖에서는 여기서만 import
                     run(src, timeout_s) -> InfraFitResult
                     skipped(reason) -> InfraFitResult
                     of(scan) -> InfraFitResult            # scan["inventory"] 접근자
  runner.py        별도 프로세스로 InfraFit S0~S4 실행 → 원본 JSON 4개(inventory/profile/fit/recommendation) 읽기
                     (구 inventory.py: run_inventory, _materialize, _names_only, _RUNNER, _progress, _commit)
  models.py        dataclass: InfraFitResult, StageError, InventorySummary, Recommendation, Candidate,
                     Rejection, RejectReason, Dimension, Ranking, WorkerOverride  (+ to_dict)
  inventory.py     S1 원본 → InventorySummary (summarize, deploy_units_summary)
  recommend.py     S2~S4 원본 → Recommendation, Worker 상한으로 1순위 대체
                     (recommendation_summary, worker_limit, _app_scope, Candidate 생성)
  trim.py          프롬프트 바이트 상한에 맞게 객체를 줄임 (_bound, _bound_recommendation, MAX_*_BYTES)
  knowledge.py     vendor knowledge/*.yaml 읽기 (_targets → component_targets, ranking_labels)
  messages.py      사용자에게 보이는 경고 문장
                     (scan._inventory_warnings·_recommendation_warnings, analyze._infrafit_compute_warnings·_engine)
  screen.py        응답의 "infrafit" 필드와 candidates[].infrafit (analyze._infrafit_view, _attach_infrafit)
agent/envrules.py  환경변수 이름·비밀값 규칙 (analyze 의 ENV_KEY_RE·SECRETISH·SECRET_VALUE·RESERVED_ENV)
```

삭제: `agent/inventory.py`. `scripts/deploy.py` 주석의 경로를 고친다.

## 4. 종속성

```
analyze ─┬─> scan ──> infrafit.run
         ├─> units ──> envrules
         ├─> infrafit (of, messages, screen)
         └─> envrules
brain ───> infrafit (of(scan).to_prompt_dict())
units ───> infrafit (of(scan).deploy_units)

infrafit/* ─> catalog, cost, source  (analyze·scan·units·brain 은 import 하지 않음)
```

## 5. 데이터 모델

필드 이름은 지금 JSON 키와 같게 한다. `to_dict()` 는 지금처럼 비어 있는 선택 키를 뺀다.

```python
@dataclass
class StageError:
    stage: str                     # "S2" | "S3" | "S4"
    message: str

@dataclass
class InfraFitResult:
    status: Literal["ok", "error", "timeout", "skipped"]
    message: str | None = None
    commit: str | None = None                     # JSON 키: infrafit_commit
    inventory: InventorySummary | None = None     # S1 요약 (JSON 키: summary, recommendation·deploy_units 제외)
    recommendation: Recommendation | None = None  # S2~S4 (JSON 키: summary.recommendation). 못 갔으면 None
    deploy_units: dict | None = None              # 전체, 손대지 않음 (units.py 담당)
    stage_error: StageError | None = None

    @property
    def ok(self) -> bool
    def to_prompt_dict(self) -> dict              # 지금 brain 이 프롬프트에 넣는 inventory JSON 과 같음

@dataclass
class InventorySummary:                           # 지금 summarize() 의 키 그대로
    workloads: list[dict]; endpoints: dict; datastores: list[dict]; external_services: list[dict]
    environments: list[dict]; compute: list[dict]; request_paths: dict; unmapped: list
    deploy_units: dict | None                     # 프롬프트용으로 줄인 것 (deploy_units_summary)
    truncated: list[str]

@dataclass
class Candidate:                                  # 구 inventory._candidate 결과
    id; rank; target; targets: list[str]; deployable: bool; assignment: dict; unknown_count: int
    mixed: bool; transforms; external_scopes; topology; decided_by; unverified: bool
    unknown_capabilities: list[str]; worker_limit: str | None
    def compute(self) -> str | None               # assignment 에서 cp:… 하나

@dataclass
class Recommendation:
    recommended: Candidate | None; top: list[Candidate]; rejected: list[Rejection]
    app_scope: str | None; dimensions: dict[str, Dimension]
    ranking: Ranking | None; worker_override: WorkerOverride | None
    outcome: str | None; outcome_detail: str | None; unknown_capabilities: list[str]; no_feasible: bool
    def websocket_evidence(self) -> list[str] | None   # 구 analyze._websocket_evidence
```

`InventorySummary` 의 목록 항목(워크로드·엔드포인트 등)은 프롬프트용 요약 행이라 dict 로 둔다.
코드가 판단에 쓰는 것(추천·후보·차원·탈락 이유)만 타입을 붙인다.

`scan["inventory"]` 에는 `InfraFitResult` 객체가 들어간다. 꺼낼 때는 `infrafit.of(scan)` 를 쓴다
(키가 없으면 `skipped`).

## 6. 이름 바꾸기

| 지금 | 바뀜 |
|---|---|
| `inventory.run_inventory` | `infrafit.run` |
| `analyze.py` 의 `rec` (LLM 출력) | `llm_rec` |
| `analyze.py`·`scan.py` 의 `reco` (InfraFit 추천 요약) | `infrafit_reco` (타입 `Recommendation`) |
| `scan.py` 의 `rec` (InfraFit 1순위) | `first` → `recommended` |
| `u`·`du` | `deploy_units` |
| `inventory._candidate` | `recommend.candidate_from_raw` |
| `analyze._candidate` | `analyze.screen_candidate` |
| `inventory._targets` | `knowledge.component_targets` |

## 7. 오류 처리

- `infrafit.run()` 은 예외를 던지지 않는다 (지금과 같음). 실패는 `InfraFitResult(status="error"|"timeout", message=…)`.
- S1 성공 후 S2~S4 실패·시간 초과: `status="ok"`, `inventory` 있음, `recommendation=None`, `stage_error` 있음 (지금과 같음).

후속 과제 (이번 범위 밖): 코드가 프롬프트용으로 줄인 `top`·`rejected` 로 판단한다 (예: 웹소켓 대체 대상 고르기).
큰 저장소에서 후보가 잘리면 판단이 달라질 수 있다.

## 8. 검증

1. 리팩터링 **전에** 스냅샷 테스트 `tests/test_infrafit_golden.py` + `tests/golden/*.json` 을 추가한다.
   `tests/fixtures/` 9개 저장소마다:
   (a) 프롬프트에 들어가는 inventory JSON, (b) scan 경고 목록,
   (c) `tests/fakes.FakeBrain` 으로 돌린 analyze 응답 (시각·id·uri 제외).
   지금 코드에서 통과하는 상태로 커밋한다.
2. 리팩터링 뒤 스냅샷이 그대로 같아야 한다.
3. 내부 함수를 직접 부르는 기존 테스트는 import 경로·호출 모양만 바꾸고 단언은 그대로 둔다.
4. 완료 기준: 기존 160 passed / 33 skipped + 스냅샷 테스트 전부 통과.

## 9. 커밋 순서

1. 스냅샷 테스트 추가
2. `agent/infrafit/` 패키지 + 모델 (구 inventory.py 이동)
3. scan·analyze·units·brain 을 새 API 로 옮기고 이름 바꾸기, `envrules` 분리
4. `agent/inventory.py` 삭제, 문서·주석 정리
