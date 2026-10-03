"""knowledge/ 형식 검사."""

from __future__ import annotations

import re
from fnmatch import fnmatchcase

from infrafit import kb

COMPONENT_ID = re.compile(r"^(cp|ds|ca|qu|sc|rt|fs|nw):[a-z0-9._-]+/[a-z0-9._-]+/[a-z0-9._-]+$")
ROLES = {"primary-db", "cache", "session", "queue", "scheduler", "realtime", "file-storage", "search", "other"}
STATUSES = {"confirmed", "candidate"}
ARTIFACTS = {"dockerfile", "k8s", "terraform", "hop"}
IMAGE_ROLES = {"reverse-proxy", "datastore", "cache", "queue", "infra", "dev-tool"}
IMAGE_KEYS = {"match", "role", "component", "hosting_hint", "port"}
# 이미지 role → component가 가져야 하는 family(infra·dev-tool은 정하지 않는다)
IMAGE_ROLE_FAMILIES = {"datastore": "ds", "cache": "ca", "queue": "qu", "reverse-proxy": "nw"}


def _lint_catalog(entries: list[dict] | None = None) -> list[str]:
    issues: list[str] = []
    seen: set[str] = set()
    for c in entries if entries is not None else kb._load("components/catalog.yaml")["components"]:
        cid = c.get("id", "")
        if not COMPONENT_ID.match(cid):
            issues.append(f"catalog: 잘못된 ID 형식 {cid!r}")
        if cid in seen:
            issues.append(f"catalog: 중복 ID {cid}")
        seen.add(cid)
        if c.get("family") != cid.split(":")[0]:
            issues.append(f"catalog: {cid}의 family가 접두어와 다름")
        if not c.get("name") or not c.get("source"):
            issues.append(f"catalog: {cid}에 name 또는 source 없음")
        if "recommendable" in c and not isinstance(c["recommendable"], bool):
            issues.append(f"catalog: {cid}의 recommendable이 불리언이 아님")
        if c.get("recommendable") is False and not c.get("reason"):
            issues.append(f"catalog: {cid}가 recommendable: false인데 reason 없음")
    return issues


def _lint_condition(sig_id: str, cond: dict, allow_env: bool = False) -> list[str]:
    issues: list[str] = []
    for leaf in kb.iter_conditions(cond):
        if allow_env and "env" in leaf:
            try:
                re.compile(leaf["env"])
            except (re.error, TypeError) as e:
                issues.append(f"{sig_id}: env 정규식 오류 {e}")
        elif "dependency" in leaf:
            if not isinstance(leaf["dependency"], str) or not leaf["dependency"]:
                issues.append(f"{sig_id}: dependency가 비었음")
        elif "code" in leaf:
            code = leaf["code"]
            if not code.get("glob") or not code.get("regex"):
                issues.append(f"{sig_id}: code에 glob 또는 regex 없음")
                continue
            if isinstance(code["glob"], list) and not all(isinstance(g, str) and g for g in code["glob"]):
                issues.append(f"{sig_id}: code glob 목록에 빈 값")
            try:
                re.compile(code["regex"])
            except re.error as e:
                issues.append(f"{sig_id}: 정규식 오류 {e}")
            if code.get("flags") not in (None, "i"):
                issues.append(f"{sig_id}: flags는 i만 허용")
            if code.get("multiline") not in (None, True, False):
                issues.append(f"{sig_id}: multiline은 true·false만 허용")
        else:
            issues.append(f"{sig_id}: 알 수 없는 조건 {leaf}")
    return issues


def _lint_signatures() -> list[str]:
    issues: list[str] = []
    catalog = kb.catalog()
    seen: set[str] = set()
    for s in kb.signatures():
        sid = s.get("id", "?")
        if sid in seen:
            issues.append(f"signature: 중복 ID {sid}")
        seen.add(sid)
        if s.get("component") not in catalog:
            issues.append(f"{sid}: catalog에 없는 구성 요소 {s.get('component')}")
        if s.get("role") not in ROLES:
            issues.append(f"{sid}: 잘못된 role {s.get('role')}")
        if s.get("status") not in STATUSES:
            issues.append(f"{sid}: 잘못된 status {s.get('status')}")
        if not s.get("when"):
            issues.append(f"{sid}: when 없음")
        else:
            issues += _lint_condition(sid, s["when"])
        for r in s.get("refine", []):
            if r.get("component") not in catalog:
                issues.append(f"{sid}: refine의 구성 요소가 catalog에 없음 {r.get('component')}")
            issues += _lint_condition(sid, r.get("when", {}))
    return issues


def _lint_defaults() -> list[str]:
    issues: list[str] = []
    for d in kb.defaults():
        name = f"default {d.get('artifact')}:{d.get('key')}"
        if d.get("artifact") not in ARTIFACTS:
            issues.append(f"{name}: 잘못된 artifact")
        if "key" not in d or "value" not in d:
            issues.append(f"{name}: key 또는 value 없음")
        if not (d.get("source") or {}).get("ref"):
            issues.append(f"{name}: source.ref 없음")
        if d.get("artifact") == "hop" and d.get("component") not in kb.catalog():
            issues.append(f"{name}: catalog에 없는 구성 요소")
        if d.get("artifact") == "k8s" and not d.get("match_kind"):
            issues.append(f"{name}: match_kind 없음")
        if d.get("artifact") == "terraform" and not d.get("resource"):
            issues.append(f"{name}: resource 없음")
    return issues


def _shadowed_by(pattern: str, earlier: str) -> bool:
    """앞 패턴 earlier가 pattern에 맞는 이미지를 모두 먼저 잡는가(pattern의 와일드카드는 글자로 보고 맞춰 본다).
    분류는 마지막 이름과 `저장소/이름`을 함께 보므로 `저장소/이름` 패턴은 마지막 이름만으로도 가려진다."""
    keys = [pattern] + ([pattern.rsplit("/", 1)[1]] if "/" in pattern else [])
    return any(fnmatchcase(k, earlier) for k in keys)


def _lint_images(entries: list[dict] | None = None) -> list[str]:
    issues: list[str] = []
    catalog = kb.catalog()
    seen: list[str] = []  # 앞 항목들의 패턴(분류가 보는 순서)
    for i, e in enumerate(kb.images() if entries is None else entries):
        name = f"image {i}"
        match = e.get("match") if isinstance(e, dict) else None
        if not isinstance(match, list) or not match or not all(isinstance(m, str) and m for m in match):
            issues.append(f"{name}: match가 비었거나 문자열 목록이 아님")
            if not isinstance(e, dict):
                continue
            match = []
        else:
            name = f"image {match[0]}"
        for key in sorted(set(e) - IMAGE_KEYS):
            issues.append(f"{name}: 알 수 없는 키 {key}")
        for pattern in (m.lower() for m in match):
            if pattern in seen:
                issues.append(f"{name}: 중복 패턴 {pattern}")
            elif shadow := next((x for x in seen if _shadowed_by(pattern, x)), None):
                issues.append(f"{name}: 패턴 {pattern}이 앞 패턴 {shadow}에 가려짐")
            seen.append(pattern)
        if e.get("role") not in IMAGE_ROLES:
            issues.append(f"{name}: 잘못된 role {e.get('role')}")
        for key in ("component", "hosting_hint"):
            if key in e and e[key] not in catalog:
                issues.append(f"{name}: {key}가 catalog에 없음 {e[key]}")
        if "port" in e and (not isinstance(e["port"], int) or isinstance(e["port"], bool)
                            or not 0 < e["port"] < 65536):
            issues.append(f"{name}: port는 1~65535 정수여야 함 {e['port']!r}")
        family = IMAGE_ROLE_FAMILIES.get(e.get("role"))
        if family and isinstance(e.get("component"), str) and e["component"].split(":")[0] != family:
            issues.append(f"{name}: role {e['role']}의 component family는 {family}여야 함 {e['component']}")
    return issues


def _lint_secrets(data: dict | None = None) -> list[str]:
    issues: list[str] = []
    data = kb.secrets() if data is None else data
    for part in ("key", "value"):
        patterns = data.get(part)
        if not patterns:
            issues.append(f"secrets: {part} 정규식 목록이 비었음")
            continue
        for rx in patterns:
            try:
                re.compile(rx)
            except (re.error, TypeError) as e:
                issues.append(f"secrets: {part} 정규식 오류 {rx!r}: {e}")
    return issues


def _lint_unmapped_signatures() -> list[str]:
    issues: list[str] = []
    seen = {s.get("id") for s in kb.signatures()}
    for s in kb.unmapped_signatures():
        sid = s.get("id", "?")
        if sid in seen:
            issues.append(f"unmapped signature: 중복 ID {sid}")
        seen.add(sid)
        if not isinstance(s.get("label"), str) or not s["label"]:
            issues.append(f"{sid}: label 없음")
        if s.get("role") not in ROLES:
            issues.append(f"{sid}: 잘못된 role {s.get('role')}")
        issues += _lint_condition(sid, s["when"]) if s.get("when") else [f"{sid}: when 없음"]
    return issues


def _str_list(v) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, str) and x for x in v)


def _regex_issue(name: str, rx, groups: int = 0) -> str | None:
    if not isinstance(rx, str) or not rx:
        return f"{name}: regex 없음"
    try:
        compiled = re.compile(rx)
    except re.error as e:
        return f"{name}: 정규식 오류 {e}"
    return f"{name}: regex에 묶음이 없음" if compiled.groups < groups else None


def _lint_implicit_route(name: str, r) -> list[str]:
    if not isinstance(r, dict):
        return [f"{name}: 라우트가 객체가 아님"]
    issues: list[str] = []
    if not isinstance(r.get("method"), str) or not r["method"]:
        issues.append(f"{name}: method 없음")
    path = r.get("path")
    if not isinstance(path, str) or not path or path.count("{}") > 1:
        issues.append(f"{name}: path가 없거나 `{{}}`가 여럿")
        return issues
    for key in ("off_with", "only_with"):
        if key in r and not _str_list(r[key]):
            issues.append(f"{name}: {key}는 문자열 목록이어야 함")
    if "{}" not in path:
        if "override" in r or "default" in r:
            issues.append(f"{name}: `{{}}` 없는 path에 override·default")
        return issues
    if not isinstance(r.get("default"), str) or not r["default"].startswith("/"):
        issues.append(f"{name}: default는 `/`로 시작하는 경로여야 함")
    ov = r.get("override")
    keys = sorted(set(ov) & {"keyword", "property", "code"}) if isinstance(ov, dict) else []
    if len(keys) != 1 or len(ov) != 1:
        issues.append(f"{name}: override는 keyword·property·code 중 하나")
    elif keys[0] == "code":
        code = ov["code"] if isinstance(ov["code"], dict) else {}
        if not _str_list(code.get("globs")):
            issues.append(f"{name}: code.globs 없음")
        if issue := _regex_issue(name, code.get("regex"), 1):
            issues.append(issue)
    elif not isinstance(ov[keys[0]], str) or not ov[keys[0]]:
        issues.append(f"{name}: override {keys[0]}가 비었음")
    return issues


def _lint_implicit_routes(entries: list[dict] | None = None) -> list[str]:
    issues: list[str] = []
    seen: set[str] = set()
    for i, e in enumerate(kb.implicit_routes() if entries is None else entries):
        if not isinstance(e, dict):
            issues.append(f"implicit route {i}: 객체가 아님")
            continue
        name = str(e.get("id") or f"implicit route {i}")
        if name in seen:
            issues.append(f"implicit route: 중복 ID {name}")
        seen.add(name)
        if not isinstance(e.get("framework"), str) or not e["framework"]:
            issues.append(f"{name}: framework 없음")
        trigger = e.get("trigger") if isinstance(e.get("trigger"), dict) else {}
        if sorted(trigger) == ["dependency"]:
            if not _str_list(trigger["dependency"]):
                issues.append(f"{name}: trigger.dependency는 문자열 목록이어야 함")
        elif sorted(trigger) == ["call"] and isinstance(trigger["call"], dict):
            call = trigger["call"]
            if not _str_list(call.get("globs")):
                issues.append(f"{name}: call.globs 없음")
            if not isinstance(call.get("dependency"), str) or not call["dependency"]:
                issues.append(f"{name}: call.dependency 없음")
            if issue := _regex_issue(name, call.get("regex")):
                issues.append(issue)
            elif not call["regex"].endswith("\\("):
                issues.append(f"{name}: call.regex는 `\\(`로 끝나야 함")
        else:
            issues.append(f"{name}: trigger는 dependency 또는 call 하나")
        routes = e.get("routes")
        if not isinstance(routes, list) or not routes:
            issues.append(f"{name}: routes 없음")
            continue
        for r in routes:
            issues += _lint_implicit_route(name, r)
    return issues


EXTERNAL_KINDS = {"llm-api", "auth", "payments", "messaging", "email", "push", "maps", "video", "webhook", "other"}
EXTERNAL_ID = re.compile(r"^ext:[a-z0-9-]+$")
DEPLOY_TARGETS = {"source", "image", "job-build", "workflow-build", "compose", "cwd", "manifest", "name"}


def _lint_external(entries: list[dict] | None = None) -> list[str]:
    """외부 서비스: id 형식·중복, label, kind, when.any(dependency·code·env)."""
    issues: list[str] = []
    seen: set[str] = set()
    for i, e in enumerate(kb.external() if entries is None else entries):
        if not isinstance(e, dict):
            issues.append(f"external {i}: 항목이 매핑이 아님")
            continue
        eid = str(e.get("id", f"external {i}"))
        if not EXTERNAL_ID.match(eid):
            issues.append(f"{eid}: 잘못된 외부 서비스 ID 형식")
        if eid in seen:
            issues.append(f"external: 중복 ID {eid}")
        seen.add(eid)
        if not e.get("label"):
            issues.append(f"{eid}: label 없음")
        if e.get("kind") not in EXTERNAL_KINDS:
            issues.append(f"{eid}: 잘못된 kind {e.get('kind')}")
        when = e.get("when")
        if not isinstance(when, dict) or set(when) != {"any"} or not isinstance(when["any"], list) or not when["any"]:
            issues.append(f"{eid}: when은 any 목록이어야 함")
        else:
            issues += _lint_condition(eid, when, allow_env=True)
    return issues


def _lint_deploy(entries: list[dict] | None = None) -> list[str]:
    """CI 배포 패턴: match(run 정규식 또는 uses), compute(카탈로그 ID 또는 unmapped + label), target."""
    issues: list[str] = []
    catalog = kb.catalog()
    seen: set[str] = set()
    for i, d in enumerate(kb.deploy() if entries is None else entries):
        if not isinstance(d, dict):
            issues.append(f"deploy {i}: 항목이 매핑이 아님")
            continue
        did = str(d.get("id", f"deploy {i}"))
        if did in seen:
            issues.append(f"deploy: 중복 ID {did}")
        seen.add(did)
        match = d.get("match")
        if not isinstance(match, dict) or len(match) != 1 or not set(match) <= {"run", "uses"}:
            issues.append(f"{did}: match는 run 또는 uses 하나")
        elif "run" in match:
            try:
                re.compile(match["run"])
            except (re.error, TypeError) as e:
                issues.append(f"{did}: run 정규식 오류 {e}")
        compute = d.get("compute")
        if compute == "unmapped":
            if not d.get("label"):
                issues.append(f"{did}: compute가 unmapped이면 label 필요")
        elif compute not in catalog:
            issues.append(f"{did}: catalog에 없는 compute {compute}")
        target = d.get("target")
        if not isinstance(target, list) or not target or not set(target) <= DEPLOY_TARGETS:
            issues.append(f"{did}: 잘못된 target {target}")
    return issues


RESEARCH_DIR = kb.KB_DIR.parent / "docs" / "research"
CLOUDS = {"aws", "gcp", "azure", "local"}
TARGETS = {"aws_lambda", "gcp_cloud_run", "aws_ecs_fargate", "aws_ec2", "gcp_compute_engine"}
OPS_BURDEN = {"low", "medium", "high"}
DS_ENGINES = {"postgres", "redis", "sqlite"}
# 능력 키(계획 2 MVP 고정 형식) → 값 검사
CAPABILITY_KEYS = {
    "CP.max_request_seconds": lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0,
    "CP.websocket": lambda v: isinstance(v, bool),
    "CP.cpu_after_response": lambda v: isinstance(v, bool),
    "CP.cpu_after_response_config": lambda v: isinstance(v, str) and bool(v),
    "CP.persistent_local_disk": lambda v: isinstance(v, bool),
    "CP.scale_to_zero": lambda v: isinstance(v, bool),
    "CP.single_instance_config": lambda v: isinstance(v, str) and bool(v),
    "CP.always_on": lambda v: isinstance(v, bool),
    "CP.always_on_config": lambda v: isinstance(v, str) and bool(v),
    "DS.engine": lambda v: v in DS_ENGINES,
    "COST.monthly_floor_usd": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0,
    # 인스턴스를 고정(1개 상시)했을 때 서울 리전 월 비용. scale-to-zero 플랫폼에서 고정 설정이 필요한 후보에 쓴다
    "COST.monthly_pinned_usd": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0,
}
# 조사 문서 줄에 붙은 이 표시가 있으면 그 줄은 근거로 쓸 수 없다(README §3)
BAD_MARKERS = ("⚠️근거없음", "⚠️출처부적격", "⚠️출처확인필요")
# 적격(A) 발행처 호스트(README §3, source-audit.md §1). 능력 값에 쓰는 것만 둔다.
ELIGIBLE_HOSTS = ("docs.aws.amazon.com", "aws.amazon.com", "pricing.us-east-1.amazonaws.com",
                  "cloud.google.com", "docs.cloud.google.com", "www.sqlite.org", "docs.docker.com")
# [PL] = 문서 머리말에 적은 AWS Price List 오퍼 파일
PRICE_LIST_PREFIX = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/"
DIMENSION_ROW = re.compile(r"^\|\s*([A-G][0-9]+)\s*\|", re.M)
# schemas/infrafit.schema.json $defs.RuleId
RULE_ID = re.compile(r"^(CMP|CAP|TIM|CMB|OVR|HYG|COST)-[A-Z]+-[0-9]{3}$")
WHEN_OPS = {"equals", "in"}
REQUIRE_OPS = {"equals", "gte", "exists"}


def _doc_lines(doc: str, research_dir, cache: dict) -> list[str] | None:
    if doc not in cache:
        path = research_dir / doc
        ok = (isinstance(doc, str) and doc.startswith("capabilities/") and doc.endswith(".md")
              and ".." not in doc and path.is_file())
        cache[doc] = path.read_text(encoding="utf-8").splitlines() if ok else None
    return cache[doc]


def _lint_source(name: str, src, research_dir, cache: dict) -> list[str]:
    """source {doc, line, url, quote}: 필드 필수, 인용 문구가 doc:line에 그대로 있고, url도 그 줄에 있고, 표시 없음."""
    if not isinstance(src, dict):
        return [f"{name}: source 없음"]
    missing = [k for k in ("doc", "line", "url", "quote") if not src.get(k)]
    if missing:
        return [f"{name}: source에 {', '.join(missing)} 없음"]
    lines = _doc_lines(src["doc"], research_dir, cache)
    if lines is None:
        return [f"{name}: 조사 문서 없음 {src['doc']} (docs/research/capabilities/*.md 이어야 함)"]
    line = src["line"]
    if not isinstance(line, int) or isinstance(line, bool) or not 1 <= line <= len(lines):
        return [f"{name}: 줄 번호가 범위 밖 {src['doc']}:{line}"]
    text = lines[line - 1]
    issues: list[str] = []
    if str(src["quote"]) not in text:
        issues.append(f"{name}: 인용 문구가 {src['doc']}:{line}에 없음")
    if marker := next((m for m in BAD_MARKERS if m in text), None):
        issues.append(f"{name}: {src['doc']}:{line}에 {marker} 표시")
    url = str(src["url"])
    host = url.split("://", 1)[-1].split("/", 1)[0]
    if not url.startswith("https://") or host not in ELIGIBLE_HOSTS:
        issues.append(f"{name}: 적격 발행처가 아닌 url {url}")
    on_line = url in text or url.rstrip("/") in text
    via_price_list = ("[PL]" in text and url.startswith(PRICE_LIST_PREFIX)
                      and any(PRICE_LIST_PREFIX in ln for ln in lines))
    if not (on_line or via_price_list):
        issues.append(f"{name}: url이 {src['doc']}:{line}의 인용이 아님 {url}")
    return issues


def _lint_capabilities(entries: list[dict] | None = None, catalog: dict | None = None,
                       research_dir=None) -> list[str]:
    """능력 값: 형식, 카탈로그 소속, 키·값 형식, source 필수와 인용 문구 위치."""
    issues: list[str] = []
    catalog = kb.catalog() if catalog is None else catalog
    research_dir = RESEARCH_DIR if research_dir is None else research_dir
    entries = kb._load("capabilities.yaml").get("components") or [] if entries is None else entries
    cache: dict = {}
    seen: set[str] = set()
    for i, c in enumerate(entries):
        if not isinstance(c, dict):
            issues.append(f"capability {i}: 항목이 매핑이 아님")
            continue
        cid = str(c.get("id", f"capability {i}"))
        if cid in seen:
            issues.append(f"capabilities: 중복 ID {cid}")
        seen.add(cid)
        if cid not in catalog:
            issues.append(f"{cid}: catalog에 없는 구성 요소")
        elif catalog[cid].get("recommendable") is False:
            issues.append(f"{cid}: recommendable: false 구성 요소에 능력 값")
        family = cid.split(":")[0]
        if c.get("family") != family:
            issues.append(f"{cid}: family가 ID 접두어와 다름")
        if c.get("cloud") not in CLOUDS:
            issues.append(f"{cid}: 잘못된 cloud {c.get('cloud')}")
        if family == "cp":
            if c.get("target") not in TARGETS:
                issues.append(f"{cid}: 잘못된 target {c.get('target')}")
        elif "target" in c:
            issues.append(f"{cid}: 컴퓨트가 아닌데 target 있음")
        if "ops_burden" in c:
            ob = c.get("ops_burden_source")
            if c["ops_burden"] not in OPS_BURDEN:
                issues.append(f"{cid}: 잘못된 ops_burden {c['ops_burden']}")
            if not isinstance(ob, dict) or not ob.get("reason"):
                issues.append(f"{cid}: ops_burden에 ops_burden_source.reason 없음")
            else:
                issues += _lint_source(f"{cid}#ops_burden", {k: v for k, v in ob.items() if k != "reason"},
                                       research_dir, cache)
        caps = c.get("capabilities")
        if not isinstance(caps, dict):
            issues.append(f"{cid}: capabilities가 매핑이 아님")
            continue
        for key, entry in caps.items():
            name = f"{cid}#{key}"
            if key not in CAPABILITY_KEYS:
                issues.append(f"{name}: 알 수 없는 능력 키")
                continue
            if not isinstance(entry, dict) or "value" not in entry:
                issues.append(f"{name}: value 없음")
                continue
            if not CAPABILITY_KEYS[key](entry["value"]):
                issues.append(f"{name}: 잘못된 값 {entry['value']!r}")
            issues += _lint_source(name, entry.get("source"), research_dir, cache)
    return issues


def _dimension_ids(research_dir=None) -> set[str]:
    path = (RESEARCH_DIR if research_dir is None else research_dir) / "dimensions.md"
    return set(DIMENSION_ROW.findall(path.read_text(encoding="utf-8")))


def _lint_require(name: str, cond) -> list[str]:
    if not isinstance(cond, dict):
        return [f"{name}: require가 매핑이 아님"]
    if set(cond) in ({"any"}, {"all"}):
        children = cond[next(iter(cond))]
        if not isinstance(children, list) or not children:
            return [f"{name}: require any/all이 비었음"]
        return [i for child in children for i in _lint_require(name, child)]
    issues: list[str] = []
    if cond.get("capability") not in CAPABILITY_KEYS:
        issues.append(f"{name}: 알 수 없는 능력 키 {cond.get('capability')}")
    ops = set(cond) - {"capability"}
    if len(ops) != 1 or not ops <= REQUIRE_OPS:
        issues.append(f"{name}: require 연산자는 {sorted(REQUIRE_OPS)} 중 하나")
    elif "gte" in ops and (not isinstance(cond["gte"], (int, float)) or isinstance(cond["gte"], bool)):
        issues.append(f"{name}: gte는 숫자여야 함")
    return issues


def _lint_rules(entries: list[dict] | None = None, research_dir=None) -> list[str]:
    """규칙: id, when의 차원 ID, require의 능력 키, otherwise ∈ {infeasible, config}, config일 때 config_from."""
    issues: list[str] = []
    dims = _dimension_ids(research_dir)
    seen: set[str] = set()
    for i, r in enumerate(kb.rules() if entries is None else entries):
        if not isinstance(r, dict):
            issues.append(f"rule {i}: 항목이 매핑이 아님")
            continue
        rid = str(r.get("id", f"rule {i}"))
        if not RULE_ID.match(rid):
            issues.append(f"{rid}: 규칙 ID가 스키마 RuleId 형식이 아님")
        if rid in seen:
            issues.append(f"rules: 중복 ID {rid}")
        seen.add(rid)
        when = r.get("when")
        if not isinstance(when, dict) or when.get("dimension") not in dims:
            issues.append(f"{rid}: when.dimension이 dimensions.md의 차원 ID가 아님 "
                          f"{when.get('dimension') if isinstance(when, dict) else when}")
        elif len(set(when) - {"dimension"}) != 1 or not set(when) - {"dimension"} <= WHEN_OPS:
            issues.append(f"{rid}: when 연산자는 {sorted(WHEN_OPS)} 중 하나")
        elif "in" in when and (not isinstance(when["in"], list) or not when["in"]):
            issues.append(f"{rid}: when.in은 비지 않은 목록이어야 함")
        issues += _lint_require(rid, r.get("require"))
        otherwise = r.get("otherwise")
        if otherwise not in ("infeasible", "config"):
            issues.append(f"{rid}: otherwise는 infeasible 또는 config")
        cfg = r.get("config_from")
        if otherwise == "config" and cfg not in CAPABILITY_KEYS:
            issues.append(f"{rid}: otherwise가 config인데 config_from이 능력 키가 아님 {cfg}")
        if otherwise == "infeasible" and cfg is not None:
            issues.append(f"{rid}: otherwise가 infeasible이면 config_from은 null")
        if not r.get("message"):
            issues.append(f"{rid}: message 없음")
    return issues



def _lint_rule_vocabulary(entries: list[dict] | None = None, detectors: dict | None = None) -> list[str]:
    """규칙 when의 차원은 S2가 내는 차원이고, 비교 값은 그 차원의 어휘(profile_detectors.yaml values)에 있다."""
    issues: list[str] = []
    dims = (kb.profile_detectors() if detectors is None else detectors).get("dimensions") or {}
    for i, r in enumerate(kb.rules() if entries is None else entries):
        when = r.get("when") if isinstance(r, dict) else None
        if not isinstance(when, dict):
            continue
        rid = str(r.get("id", f"rule {i}"))
        dim = when.get("dimension")
        spec = dims.get(dim)
        if not isinstance(spec, dict):
            issues.append(f"{rid}: when.dimension {dim}은 profile_detectors.yaml에 정의되지 않은 차원(S2가 내지 않음)")
            continue
        vocab = spec.get("values") or []
        values = [when["equals"]] if "equals" in when else list(when.get("in") or [])
        for v in values:
            if v not in vocab:
                issues.append(f"{rid}: when 값 {v!r}이 {dim} 어휘 {vocab}에 없음")
    return issues

WORKLOAD_KINDS = {"web", "worker", "scheduled", "realtime", "batch", "static-frontend", "migration-job",
                  "reverse-proxy"}
DIM_SHAPES = {"set", "ordered", "flag", "kinds"}
DIM_ID = re.compile(r"^[A-G][0-9]+$")
PROFILE_SCOPES = {"handler", "request-path", "workload"}
SCOPE_ID = re.compile(r"^w-[a-z0-9._-]+$")


def _lint_dim_ref(name: str, entry: dict, dims: dict) -> list[str]:
    """탐지 항목의 dimension이 정의돼 있고, kinds 차원은 kind, 나머지는 어휘 안의 value를 가진다."""
    dim = entry.get("dimension")
    if dim not in dims or not isinstance(dims[dim], dict):
        return [f"{name}: 정의되지 않은 dimension {dim}"]
    spec = dims[dim]
    if spec.get("shape") == "kinds":
        if entry.get("value") is not None:
            return [f"{name}: kinds 차원 {dim}에는 value 대신 kind"]
        if not isinstance(entry.get("kind"), str) or not entry["kind"]:
            # external 항목은 서비스 ID를 종류로 쓴다
            return [] if "kinds" in entry else [f"{name}: kind 없음"]
        return []
    if entry.get("kind") is not None:
        return [f"{name}: {dim}은 kinds 차원이 아님(kind 불가)"]
    if entry.get("value") not in spec.get("values", []):
        return [f"{name}: {dim} 어휘에 없는 value {entry.get('value')!r}"]
    return []


def _lint_profile_detectors(cfg: dict | None = None) -> list[str]:
    """S2 프로필 탐지 지식: 차원 정의(모양·어휘·기본값), 가정, 인벤토리 대응, 코드 탐지기(정규식·범위·값)."""
    cfg = kb.profile_detectors() if cfg is None else cfg
    issues: list[str] = []
    p = "profile_detectors"
    if not isinstance(cfg.get("app_kinds"), list) or not set(cfg["app_kinds"]) <= WORKLOAD_KINDS:
        issues.append(f"{p}: app_kinds는 워크로드 kind 목록")
    if not isinstance(cfg.get("app_scope"), str) or not SCOPE_ID.match(cfg["app_scope"]):
        issues.append(f"{p}: app_scope는 w- 범위 ID")
    if not isinstance(cfg.get("import_depth"), int) or cfg["import_depth"] < 0:
        issues.append(f"{p}: import_depth는 0 이상 정수")
    if not _str_list(cfg.get("source_globs")):
        issues.append(f"{p}: source_globs 없음")
    dims = cfg.get("dimensions")
    if not isinstance(dims, dict) or not dims:
        return issues + [f"{p}: dimensions 없음"]
    for dim, spec in dims.items():
        name = f"{p} {dim}"
        if not DIM_ID.match(str(dim)):
            issues.append(f"{name}: 잘못된 차원 ID")
        if not isinstance(spec, dict) or spec.get("shape") not in DIM_SHAPES:
            issues.append(f"{name}: shape는 {sorted(DIM_SHAPES)} 중 하나")
            continue
        vocab = spec.get("values")
        if not _str_list(vocab) or len(set(vocab)) != len(vocab):
            issues.append(f"{name}: values는 겹치지 않는 문자열 목록")
            continue
        if spec["shape"] in ("flag", "kinds") and vocab != ["없음", "있음"]:
            issues.append(f"{name}: flag·kinds 차원의 values는 [없음, 있음]")
        applies = spec.get("applies_to", [])
        if not isinstance(applies, list) or not set(applies) <= WORKLOAD_KINDS:
            issues.append(f"{name}: applies_to는 워크로드 kind 목록")
        for kind, v in (spec.get("from_kind") or {}).items():
            if kind not in WORKLOAD_KINDS or v not in vocab:
                issues.append(f"{name}: from_kind {kind}: {v!r}")
        default = spec.get("default")
        if spec["shape"] == "set":
            if default is not None:
                issues.append(f"{name}: set 차원은 default 없음(워크로드 kind에서 정함)")
            continue
        if not isinstance(default, dict) or default.get("value") not in vocab:
            issues.append(f"{name}: default.value가 어휘에 없음")
        elif default.get("source") not in ("detector", "assumption"):
            issues.append(f"{name}: default.source는 detector 또는 assumption")
        elif default["source"] == "assumption" and not default.get("reason"):
            issues.append(f"{name}: 가정 기본값에는 reason 필요")
    seen_keys: set[str] = set()
    for i, a in enumerate(cfg.get("assumptions") or []):
        key = a.get("key") if isinstance(a, dict) else None
        if not isinstance(key, str) or not DIM_ID.match(key):
            issues.append(f"{p} assumption {i}: key는 차원 ID")
            continue
        if key in seen_keys or key in dims:
            issues.append(f"{p} assumption {key}: 중복(차원 정의나 다른 가정과 겹침)")
        seen_keys.add(key)
        if not a.get("value") or not a.get("reason"):
            issues.append(f"{p} assumption {key}: value와 reason 필요")
    for i, c in enumerate(cfg.get("components") or []):
        name = f"{p} component {i}"
        if not isinstance(c.get("component"), str) or ":" not in c["component"]:
            issues.append(f"{name}: component 패턴 없음")
        elif not any(fnmatchcase(cid, c["component"]) for cid in kb.catalog()):
            issues.append(f"{name}: 카탈로그에 맞는 구성 요소가 없음 {c['component']}")
        if c.get("role") is not None and c["role"] not in ROLES:
            issues.append(f"{name}: 잘못된 role {c['role']}")
        issues += _lint_dim_ref(name, c, dims)
    for i, u in enumerate(cfg.get("unmapped") or []):
        name = f"{p} unmapped {i}"
        if not u.get("label"):
            issues.append(f"{name}: label 없음")
        issues += _lint_dim_ref(name, u, dims)
    for i, e in enumerate(cfg.get("endpoints") or []):
        name = f"{p} endpoint {i}"
        if not isinstance(e.get("method"), str) or not e["method"]:
            issues.append(f"{name}: method 없음")
        issues += _lint_dim_ref(name, e, dims)
    for i, x in enumerate(cfg.get("external") or []):
        name = f"{p} external {i}"
        if not _str_list(x.get("kinds")) or not set(x["kinds"]) <= EXTERNAL_KINDS:
            issues.append(f"{name}: kinds는 외부 서비스 kind 목록")
        if dims.get(x.get("dimension"), {}).get("shape") != "kinds":
            issues.append(f"{name}: dimension은 kinds 차원이어야 함(종류 = 외부 서비스 ID)")
    seen_ids: set[str] = set()
    for i, d in enumerate(cfg.get("code") or []):
        did = str(d.get("id", f"code {i}"))
        name = f"{p} {did}"
        if did in seen_ids:
            issues.append(f"{p}: 중복 ID {did}")
        seen_ids.add(did)
        if d.get("scope") not in PROFILE_SCOPES:
            issues.append(f"{name}: scope는 {sorted(PROFILE_SCOPES)} 중 하나")
        if not _str_list(d.get("glob")):
            issues.append(f"{name}: glob 목록 없음")
        issue = _regex_issue(name, d.get("regex"))
        if issue:
            issues.append(issue)
        if d.get("unless") is not None:
            issue = _regex_issue(f"{name} unless", d["unless"])
            if issue:
                issues.append(issue)
        issues += _lint_dim_ref(name, d, dims)
    return issues


def lint() -> list[str]:
    return (_lint_catalog() + _lint_signatures() + _lint_unmapped_signatures() + _lint_defaults() + _lint_images()
            + _lint_secrets()
            + _lint_implicit_routes() + _lint_external() + _lint_deploy() + _lint_capabilities() + _lint_rules()
            + _lint_rule_vocabulary() + _lint_profile_detectors())
