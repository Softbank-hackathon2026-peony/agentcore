"""여러 컨테이너 배포 단위 (InfraFit deploy_units, 다중 컨테이너 계약 §1~2).

InfraFit 이 S1 에서 찾은 "같이 떠야 하는 컨테이너 묶음"을 코드로 검사·보완해서 추천안 `deploy_units` 로 만든다.
- check: id·이미지·빌드 컨텍스트·Dockerfile 이 실제로 있는지, 포트가 정수인지, entry 가 있는지 (LLM 없음)
- apply_fixes: LLM 이 채운 빈 값(포트·운영용 명령·빌드 단계·entry)을 코드가 다시 검사해서 반영하고 warnings 에 남긴다
- prepare_run: 운영 배포용 변환 (코드). compose 원본에서 named volume·헬스체크·depends_on 조건을 읽고,
  저장소 경로 bind mount 는 버리고, DB 비밀번호는 배포 때 만드는 값(passwords)으로, 사용자 비밀값은 이름만 남긴다.
  결과는 각 서비스의 `run` 에 들어가고, compose.py 가 이것만 보고 compose.yaml.tftpl 을 만든다 (소스 없이 다시 렌더 가능).

사용자 compose 파일은 데이터로만 읽는다 (yaml.safe_load). 값은 그대로 실행하지 않고 compose.py 가 따옴표·이스케이프해서 쓴다.
"""
import posixpath
import re
import shlex

from . import analyze as rules   # 환경변수 이름·비밀값 규칙 (analyze 와 같은 것을 씀)
from .source import SourceTree, normalize

# Worker job.IMAGE_ID_RE 와 같음 (images 키 = 이미지 id). compose 서비스 이름도 같은 규칙으로 받는다
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
STAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
REGISTRY_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")
VOLUME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
MAX_CONTAINERS = 12
MAX_COMMAND_CHARS = 1000
MAX_ENV_VALUE_CHARS = 500
# 개발용 실행 명령·빌드 단계 (경고만. 바꾸는 것은 LLM 보완 + 코드 검사)
DEV_COMMAND = re.compile(r"--reload\b|--debug\b|\bnodemon\b|--inspect\b|\b(npm|yarn|pnpm) (run )?dev\b|\bflask run\b"
                         r"|\brunserver\b|--watch\b|\bng serve\b|\bwebpack-dev-server\b|\bvite\b(?!\s+(build|preview))", re.I)
DEV_TARGET = re.compile(r"^(dev|devel|develop|development|debug|test|testing)$", re.I)
# LLM 이 주는 실행 명령에는 셸 연산자를 받지 않는다 (명령 하나만)
UNSAFE_COMMAND = re.compile(r"[;&|`<>\n\r]|\$\(")
PASSWORD_KEY = re.compile(r"PASSWORD|PASSWD|_PASS$|_PWD$", re.I)
# scheme://user:password@host — host 가 이 묶음의 데이터 저장소 이름이면 비밀번호만 배포 값으로 바꾼다
URL_PASSWORD = re.compile(r"(?P<head>[A-Za-z][A-Za-z0-9+.-]*://[^:@/\s]*:)(?P<pw>[^@/\s]+)(?P<tail>@(?P<host>[A-Za-z0-9_.-]+)\b)")
INTERPOLATION_DEFAULT = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*:?-([^}$]*)\}$")
CODE_EXTS = {".py", ".js", ".mjs", ".cjs", ".ts", ".go", ".java", ".kt", ".rb", ".php", ".cs", ".rs"}
MAX_CODE_FILES = 400


def from_scan(scan: dict) -> dict | None:
    inv = scan.get("inventory") or {}
    return inv.get("deploy_units") if inv.get("status") == "ok" else None


def is_multi(du: dict | None) -> bool:
    """컨테이너 2개 이상이거나 데이터 저장소 컨테이너가 있으면 여러 컨테이너 배포 (ec2_compose)."""
    return bool(du) and (len(du.get("containers") or []) > 1 or bool(du.get("datastores")))


# ---------------- 검사 ----------------

def check(du: dict, src: SourceTree) -> tuple[dict, list[str], list[str]]:
    """InfraFit deploy_units 를 실제 소스와 대조해 고친다 → (units, notes, problems).
    problems 는 배포할 수 없는 이유 (있으면 supported=false)."""
    notes, problems = [], []
    seen: set[str] = set()

    images = []
    for i in du.get("images") or []:
        iid = str(i.get("id") or "")
        if not ID_RE.match(iid) or iid in {x["id"] for x in images}:
            notes.append(f"이미지 id 형식 오류·중복으로 제외: {iid!r}")
            continue
        ctx = _context(i.get("context"), src)
        if ctx is None:
            problems.append(f"이미지 {iid} 의 빌드 컨텍스트 {i.get('context')!r} 가 소스에 없습니다")
            continue
        item = {"id": iid, "context": ctx}
        df = normalize(str(i.get("dockerfile") or "")) if i.get("dockerfile") else None
        if df and src.exists(df) and not df.startswith("-"):
            item["dockerfile"] = df
        elif i.get("dockerfile"):
            notes.append(f"이미지 {iid} 의 Dockerfile {i.get('dockerfile')} 이 소스에 없어 새로 만듦")
        target = i.get("target")
        if target:
            if not STAGE_RE.match(str(target)) or (item.get("dockerfile") and target not in stages(src, item["dockerfile"])):
                notes.append(f"이미지 {iid} 의 빌드 단계 {target!r} 가 Dockerfile 에 없어 지움 (마지막 단계로 빌드)")
            else:
                item["target"] = str(target)
        if i.get("evidence"):
            item["evidence"] = _evidence(i["evidence"])
        images.append(item)
    image_ids = {i["id"] for i in images}

    containers = []
    for c in (du.get("containers") or [])[:MAX_CONTAINERS]:
        cid = str(c.get("id") or "")
        if not ID_RE.match(cid) or cid in seen:
            notes.append(f"컨테이너 id 형식 오류·중복으로 제외: {cid!r}")
            continue
        seen.add(cid)
        item = {"id": cid}
        if c.get("workload"):
            item["workload"] = str(c["workload"])
        if c.get("image") in image_ids:
            item["image"] = c["image"]
        elif c.get("registry_image") and REGISTRY_IMAGE_RE.match(str(c["registry_image"])):
            item["registry_image"] = str(c["registry_image"])
        else:
            problems.append(f"컨테이너 {cid} 를 실행할 이미지가 없습니다 (빌드 정보도 레지스트리 이미지도 없음)")
        for key in ("command", "entrypoint"):
            if isinstance(c.get(key), str) and c[key].strip():
                if len(c[key]) > MAX_COMMAND_CHARS or not _splittable(c[key]):
                    notes.append(f"{cid}.{key} 를 읽을 수 없어 지움 (이미지 기본 실행)")
                else:
                    item[key] = c[key]
        item.update(_common(c, cid, notes))
        item["depends_on"] = [str(d) for d in c.get("depends_on") or [] if isinstance(d, str)]
        item["one_shot"] = bool(c.get("one_shot"))
        containers.append(item)
    if len(du.get("containers") or []) > MAX_CONTAINERS:
        problems.append(f"컨테이너가 {len(du['containers'])}개로 한 서버에 올리기엔 너무 많습니다 (최대 {MAX_CONTAINERS})")

    stores = []
    for d in du.get("datastores") or []:
        sid = str(d.get("id") or "")
        if not ID_RE.match(sid) or sid in seen:
            notes.append(f"데이터 저장소 id 형식 오류·중복으로 제외: {sid!r}")
            continue
        seen.add(sid)
        item = {"id": sid}
        if d.get("datastore"):
            item["datastore"] = str(d["datastore"])
        if d.get("build_image") in image_ids:
            item["build_image"] = d["build_image"]
        elif d.get("image") and REGISTRY_IMAGE_RE.match(str(d["image"])):
            item["image"] = str(d["image"])
        else:
            problems.append(f"데이터 저장소 {sid} 를 실행할 이미지가 없습니다")
        item.update(_common(d, sid, notes))
        stores.append(item)

    for c in containers:                       # 묶음 밖 서비스에 기대는 depends_on 은 뺀다
        missing = [x for x in c["depends_on"] if x not in seen or x == c["id"]]
        if missing:
            notes.append(f"{c['id']}.depends_on 에서 묶음에 없는 서비스 제외: {', '.join(missing)}")
        c["depends_on"] = [x for x in dict.fromkeys(c["depends_on"]) if x not in missing]

    used = {c.get("image") for c in containers} | {d.get("build_image") for d in stores}  # 저장소 image 는 레지스트리 이미지
    for i in images:
        if i["id"] not in used:
            notes.append(f"쓰는 컨테이너가 없는 이미지 제외: {i['id']}")
    images = [i for i in images if i["id"] in used]

    entry = None
    e = du.get("entry") or {}
    if e.get("container") in {c["id"] for c in containers} and _port(e.get("port")):
        entry = {"container": e["container"], "port": _port(e["port"]), "why": str(e.get("why") or "")[:200]}
    elif e:
        notes.append(f"entry {e.get('container')!r} 가 컨테이너 목록에 없거나 포트가 없어 지움")

    unresolved = [{"field": str(u.get("field") or ""), "why": str(u.get("why") or "")[:300]}
                  for u in du.get("unresolved") or [] if isinstance(u, dict)]
    src_info = du.get("source") or {}
    units = {"source": {"kind": src_info.get("kind"), "path": src_info.get("path")},
             "images": images, "containers": containers, "datastores": stores, "entry": entry,
             "unresolved": unresolved}
    return units, notes, problems


def final_problems(units: dict) -> list[str]:
    """보완까지 끝난 뒤에도 남은, 배포를 막는 문제."""
    out = []
    if not units["containers"]:
        out.append("실행할 앱 컨테이너가 없습니다")
    if not units.get("entry"):
        out.append("외부에서 접속할 컨테이너(entry)를 정하지 못했습니다 (호스트 포트를 연 web/proxy 컨테이너가 없음)")
    return out


def _context(raw, src: SourceTree) -> str | None:
    """빌드 컨텍스트: 저장소 루트는 "", 아니면 실제로 있는 폴더."""
    raw = str(raw or "").strip().strip("/")
    if raw in ("", "."):
        return ""
    ctx = normalize(raw)
    if not ctx or ctx.startswith("-") or not any(p.startswith(ctx + "/") for p in src.paths()):
        return None
    return ctx


def _port(v) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 65535 and str(v).strip().isdigit() else None


def _splittable(text: str) -> bool:
    try:
        return bool(shlex.split(text))
    except ValueError:
        return False


def _evidence(ev) -> dict | None:
    if not isinstance(ev, dict) or not ev.get("path"):
        return None
    return {"path": str(ev["path"]), "line": ev.get("line") if isinstance(ev.get("line"), int) else None}


def _common(c: dict, cid: str, notes: list[str]) -> dict:
    ports = []
    for p in c.get("ports") or []:
        n = p if isinstance(p, int) and not isinstance(p, bool) and 1 <= p <= 65535 else None
        if n is None:
            notes.append(f"{cid} 의 포트 {p!r} 는 정수가 아니어서 제외")
        elif n not in ports:
            ports.append(n)
    env = {}
    for k, v in (c.get("env") or {}).items():
        if not rules.ENV_KEY_RE.match(str(k)) or not isinstance(v, str) or len(v) > MAX_ENV_VALUE_CHARS:
            notes.append(f"{cid} 의 환경변수 {k} 를 쓸 수 없어 제외")
        elif rules.SECRETISH.search(k) or rules.SECRET_VALUE.search(v):
            pass                                   # 비밀로 보이는 값은 이름만 (env_names 에 남아 있음)
        else:
            env[str(k)] = v
    names = sorted({str(n) for n in c.get("env_names") or [] if rules.ENV_KEY_RE.match(str(n))} | set(env))
    out = {"ports": ports, "env": env, "env_names": names}
    ev = _evidence(c.get("evidence"))
    if ev:
        out["evidence"] = ev
    return out


def stages(src: SourceTree, dockerfile: str) -> list[str]:
    """Dockerfile 의 `FROM ... AS <이름>` 단계 이름들."""
    try:
        lines = src.lines(dockerfile)
    except Exception:  # noqa: BLE001 — 못 읽으면 단계 없음으로
        return []
    return [m.group(1) for l in lines if (m := re.match(r"^\s*FROM\s+\S+\s+AS\s+([A-Za-z0-9_.-]+)", l, re.I))]


# ---------------- LLM 보완 ----------------

def apply_fixes(units: dict, fixes, src: SourceTree) -> tuple[list[str], list[str]]:
    """LLM 의 unit_fixes 를 코드로 검사해서 반영한다 → (반영한 것 = warnings 로, 거절한 것 = notes 로)."""
    applied, rejected = [], []
    services = {c["id"]: c for c in units["containers"]} | {d["id"]: d for d in units["datastores"]}
    containers = {c["id"]: c for c in units["containers"]}
    images = {i["id"]: i for i in units["images"]}
    for f in list(fixes or [])[:20]:
        field, fid, value, why = f.field, str(f.id), str(f.value).strip(), str(f.why)[:200]
        label = f"{field} {fid}"
        if field == "port":
            port, svc = _port(value), services.get(fid)
            if svc is None or port is None:
                rejected.append(f"AI 보완 거절 ({label}): 없는 서비스이거나 포트가 숫자가 아님: {value!r}")
            elif svc["ports"] and port not in svc["ports"]:
                rejected.append(f"AI 보완 거절 ({label}): 이미 정해진 포트 {svc['ports']} 를 바꿀 수 없음")
            elif not svc["ports"]:
                svc["ports"] = [port]
                applied.append(f"AI 보완 (코드 확인): {fid} 포트 → {port} — {why}")
                _resolve(units, f"containers.{fid}.ports", f"datastores.{fid}.ports")
        elif field in ("command", "entrypoint"):
            c = containers.get(fid)
            problem = None if c else "없는 컨테이너"
            if c and value:
                if len(value) > 300 or UNSAFE_COMMAND.search(value) or not _splittable(value):
                    problem = "명령 하나만 쓸 수 있음 (셸 연산자·줄바꿈 불가)"
                elif rules.SECRET_VALUE.search(value):
                    problem = "비밀값처럼 보이는 문자열이 있음"
                elif DEV_COMMAND.search(value):
                    problem = "여전히 개발용 명령"
            elif c and field == "command":
                problem = "빈 명령"
            if problem:
                rejected.append(f"AI 보완 거절 ({label}): {problem}: {value!r}")
                continue
            old = c.get(field)
            if value:
                c[field] = value
            else:
                c.pop(field, None)
            applied.append(f"AI 보완 (코드 확인): {fid} {field} {old or '(이미지 기본값)'!r} → "
                           f"{value or '(이미지 기본값)'!r} — {why}")
        elif field == "build_target":
            img = images.get(fid)
            if img is None or not img.get("dockerfile"):
                rejected.append(f"AI 보완 거절 ({label}): 프로젝트 Dockerfile 이 있는 이미지가 아님")
            elif value not in stages(src, img["dockerfile"]) or DEV_TARGET.match(value):
                rejected.append(f"AI 보완 거절 ({label}): {img['dockerfile']} 에 없는 단계이거나 개발용 단계: {value!r}")
            else:
                old = img.get("target")
                img["target"] = value
                applied.append(f"AI 보완 (코드 확인): 이미지 {fid} 빌드 단계 {old or '(마지막)'!r} → {value!r} — {why}")
        elif field == "entry":
            c, port = containers.get(fid), _port(value) if value else None
            if c is None:
                rejected.append(f"AI 보완 거절 ({label}): 없는 컨테이너")
                continue
            port = port or (c["ports"][0] if c["ports"] else None)
            if port is None or port not in c["ports"]:
                rejected.append(f"AI 보완 거절 ({label}): {fid} 가 듣는 포트 {c['ports']} 중 하나여야 함: {value!r}")
                continue
            old = units.get("entry")
            units["entry"] = {"container": fid, "port": port, "why": f"AI 보완: {why}"}
            applied.append(f"AI 보완 (코드 확인): entry {(old or {}).get('container')}→{fid}:{port} — {why}")
            _resolve(units, "entry")
        else:
            rejected.append(f"AI 보완 거절: 모르는 항목 {field}")
    return applied, rejected


def _resolve(units: dict, *fields: str) -> None:
    units["unresolved"] = [u for u in units["unresolved"] if u["field"] not in fields]


def review_warnings(units: dict) -> list[str]:
    """사용자에게 알릴 것: 개발용 명령·빌드 단계, 공개 포트가 있는 다른 web 컨테이너, InfraFit 미해결 항목."""
    out = []
    for c in units["containers"]:
        for key in ("command", "entrypoint"):
            if c.get(key) and DEV_COMMAND.search(c[key]):
                out.append(f"{c['id']} 의 {key} 가 개발용 실행 명령으로 보입니다: {c[key][:120]} "
                           "(운영에서는 코드 자동 재시작·디버거가 필요 없음)")
    for i in units["images"]:
        if i.get("target") and DEV_TARGET.match(i["target"]):
            out.append(f"이미지 {i['id']} 를 개발용 빌드 단계 {i['target']!r} 로 빌드합니다")
    entry = units.get("entry")
    if entry:
        out.append(f"외부 접속(80번)은 {entry['container']}:{entry['port']} 하나로만 받습니다 ({entry['why'][:100]})")
    for u in units["unresolved"]:
        out.append(f"InfraFit 미해결: {u['field']} — {u['why'][:160]}")
    return out


# ---------------- 운영 배포용 변환 ----------------

def prepare_run(units: dict, src: SourceTree) -> tuple[list[str], list[str]]:
    """각 서비스에 `run`(env·volumes·healthcheck·depends_on) 을 채운다 → (notes = warnings 로, required_secrets)."""
    raw = _compose_services(units, src)
    notes, secrets = [], []
    stores = {d["id"]: d for d in units["datastores"]}
    pw_stores = {sid for sid, d in stores.items() if any(PASSWORD_KEY.search(n) for n in d["env_names"])}
    one_shot = {c["id"] for c in units["containers"] if c["one_shot"]}
    named: set[str] = set()
    runs: dict[str, dict] = {}

    # 코드에 저장소 비밀번호가 직접 적혀 있으면(예: 'postgres://postgres:postgres@db') 무작위 비밀번호로 바꾸면 앱이 접속하지 못한다.
    # 그 저장소는 프로젝트 값을 그대로 쓴다 (저장소 포트는 서버 안에서만 열리고 외부는 entry 80번뿐)
    dev_passwords: dict[str, set[str]] = {}
    for svc in [*units["containers"], *units["datastores"]]:
        for name, value in _raw_env((raw.get(svc["id"]) or {}).get("environment")).items():
            if svc["id"] in stores and PASSWORD_KEY.search(name) and value:
                dev_passwords.setdefault(svc["id"], set()).add(value)
            elif (m := URL_PASSWORD.search(value)) and m.group("host") in pw_stores:
                dev_passwords.setdefault(m.group("host"), set()).add(m.group("pw"))
    hardcoded = {sid: where for sid, pws in dev_passwords.items() if (where := _hardcoded(src, sid, pws))}
    for sid, where in hardcoded.items():
        notes.append(f"코드에 {sid} 비밀번호가 직접 적혀 있어({', '.join(where)}) 무작위 비밀번호 대신 프로젝트 값을 그대로 씁니다. "
                     f"{sid} 는 서버 안에서만 접속되고 외부에는 80번(entry)만 열립니다. "
                     "무작위 비밀번호를 쓰려면 코드가 환경변수에서 읽도록 고치세요.")

    for svc in [*units["containers"], *units["datastores"]]:
        sid = svc["id"]
        r = raw.get(sid) or {}
        raw_env = _raw_env(r.get("environment"))
        env: dict[str, list] = {}
        credential: list[str] = []            # 저장소 비밀번호(무작위 또는 프로젝트 값)로 채운 이름 — 사용자에게 묻지 않음
        for name in svc["env_names"]:
            literal = name in svc["env"]
            value = svc["env"][name] if literal else raw_env.get(name)
            if literal and not URL_PASSWORD.search(value):
                env[name] = [value]
                continue
            m = URL_PASSWORD.search(value or "")
            if sid in hardcoded and PASSWORD_KEY.search(name) and value or m and m.group("host") in hardcoded:
                env[name] = [value]
                credential.append(name)
                continue
            if sid in stores and PASSWORD_KEY.search(name):
                env[name] = [{"password": sid}]
                credential.append(name)
                notes.append(f"{sid}.{name}: 개발용 비밀번호 대신 배포 때 만드는 무작위 비밀번호(passwords[\"{sid}\"])를 씁니다")
                continue
            if m and m.group("host") in pw_stores:
                env[name] = [value[:m.start("pw")], {"password": m.group("host")}, value[m.end("pw"):]]
                credential.append(name)
                notes.append(f"{sid}.{name}: 접속 주소의 개발용 비밀번호를 {m.group('host')} 배포 비밀번호로 바꿨습니다")
                continue
            d = INTERPOLATION_DEFAULT.match(value or "")
            if d and not rules.SECRETISH.search(name) and not rules.SECRET_VALUE.search(d.group(1)) and not PASSWORD_KEY.search(name):
                env[name] = [d.group(1)]          # ${X:-기본값} → 서버에는 X 가 없으므로 기본값
                continue
            env[name] = [{"secret": name}]
            secrets.append(name)
        volumes, bind_targets = [], []
        for v in r.get("volumes") or []:
            kept, target = _volume(v)
            if kept:
                volumes.append(kept)
                if ":" in kept and not kept.startswith("/"):
                    named.add(kept.split(":", 1)[0])
            elif target:
                bind_targets.append(target)
                notes.append(f"{sid}: 저장소 폴더 마운트({target})는 운영 배포에서 뺍니다 (이미지 안의 파일을 씀)")
            else:
                notes.append(f"{sid}: 읽을 수 없는 volumes 항목을 뺐습니다")
        hc = _healthcheck(r.get("healthcheck"), bind_targets)
        if r.get("healthcheck") and hc is None:
            notes.append(f"{sid}: 저장소 파일(마운트)에 기대는 헬스체크라 뺐습니다")
        runs[sid] = {"env": env, "credential_env": credential, "volumes": volumes, "healthcheck": hc,
                     "_raw_depends": r.get("depends_on")}

    for svc in [*units["containers"], *units["datastores"]]:
        run = runs[svc["id"]]
        raw_dep = run.pop("_raw_depends")
        deps = {}
        for dep in svc.get("depends_on") or []:
            cond = (raw_dep.get(dep) or {}).get("condition") if isinstance(raw_dep, dict) else None
            if dep in one_shot:
                deps[dep] = "service_completed_successfully"
            elif cond == "service_healthy" and runs[dep]["healthcheck"]:
                deps[dep] = "service_healthy"
            elif cond == "service_healthy":
                deps[dep] = "service_started"
                notes.append(f"{svc['id']} → {dep}: 헬스체크를 뺐으므로 '시작됨'까지만 기다립니다")
            else:
                deps[dep] = "service_started"
        run["depends_on"] = deps
        svc["run"] = run
    units["volumes"] = sorted(named)
    return list(dict.fromkeys(notes)), sorted(set(secrets))


def _compose_services(units: dict, src: SourceTree) -> dict:
    """compose 원본의 services (데이터로만 읽음). compose 가 아니거나 못 읽으면 빈 dict."""
    s = units.get("source") or {}
    if s.get("kind") != "compose" or not s.get("path"):
        return {}
    try:
        import yaml
        data = yaml.safe_load(src.read_text(s["path"]))
    except Exception:  # noqa: BLE001 — 못 읽으면 원본 정보 없이 (헬스체크·볼륨 없음)
        return {}
    services = data.get("services") if isinstance(data, dict) else None
    return {str(k): v for k, v in services.items() if isinstance(v, dict)} if isinstance(services, dict) else {}


def _raw_env(raw) -> dict[str, str]:
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if v is not None and not isinstance(v, (dict, list)):
                out[str(k)] = str(v).lower() if isinstance(v, bool) else str(v)
    elif isinstance(raw, list):
        for item in raw:
            k, sep, v = str(item).partition("=")
            if sep:
                out[k.strip()] = v
    return out


def _volume(v) -> tuple[str | None, str | None]:
    """(남길 volume 문자열, 버린 bind mount 의 컨테이너 경로). named volume·익명 volume 만 남긴다."""
    if isinstance(v, dict):
        target = str(v.get("target") or "")
        if v.get("type") == "volume" and VOLUME_NAME_RE.match(str(v.get("source") or "")) and target.startswith("/"):
            return f"{v['source']}:{target}", None
        return None, target or None
    if not isinstance(v, str):
        return None, None
    parts = v.split(":")
    if len(parts) == 1:
        return (v, None) if v.startswith("/") and _plain_path(v) else (None, None)
    source, target = parts[0], parts[1]
    mode = parts[2] if len(parts) > 2 else ""
    if VOLUME_NAME_RE.match(source) and target.startswith("/") and _plain_path(target):
        return f"{source}:{target}" + (f":{mode}" if mode in ("ro", "rw") else ""), None
    return None, target if target.startswith("/") else None


def _plain_path(p: str) -> bool:
    return bool(re.fullmatch(r"/[A-Za-z0-9_./@+-]*", p)) and ".." not in p.split("/")


def _healthcheck(raw, bind_targets: list[str]) -> dict | None:
    if not isinstance(raw, dict):
        return None
    test = raw.get("test")
    if isinstance(test, list) and all(isinstance(x, (str, int)) for x in test):
        test = [str(x) for x in test]
        text = " ".join(test)
    elif isinstance(test, str):
        text = test
    else:
        return None
    if any(t and (text == t or t + "/" in text or re.search(rf"(^|\s){re.escape(t)}(\s|$)", text)) for t in bind_targets) \
            or re.search(r"(^|\s)\./", text) or len(text) > 500:
        return None
    out = {"test": test}
    for key in ("interval", "timeout", "start_period", "start_interval"):
        if isinstance(raw.get(key), str) and re.fullmatch(r"[0-9][0-9a-z.]{0,15}", raw[key]):
            out[key] = raw[key]
    if isinstance(raw.get("retries"), int) and 0 < raw["retries"] <= 1000:
        out["retries"] = raw["retries"]
    return out


def _hardcoded(src: SourceTree, store_id: str, passwords: set[str]) -> list[str]:
    """코드 파일에 `<비밀번호>@<저장소>` 또는 `Password=<비밀번호>` 가 있는 곳 (최대 3곳)."""
    found = []
    pats = [re.compile(rf":{re.escape(pw)}@{re.escape(store_id)}\b|Password={re.escape(pw)}\s*;", re.I)
            for pw in passwords if pw]
    files = [p for p in src.paths() if posixpath.splitext(p)[1].lower() in CODE_EXTS][:MAX_CODE_FILES]
    for p in files:
        for n, line in enumerate(src.lines(p), 1):
            if any(rx.search(line) for rx in pats):
                found.append(f"{p}:{n}")
                if len(found) >= 3:
                    return found
    return found


def _built(units: dict) -> list[tuple[str, dict]]:
    """(이미지 id, 서비스): 컨테이너는 image, 데이터 저장소는 build_image 가 이미지 id (image 는 레지스트리 이미지)."""
    return [(c["image"], c) for c in units["containers"] if c.get("image")] + \
           [(d["build_image"], d) for d in units["datastores"] if d.get("build_image")]


def image_ports(units: dict) -> dict[str, int | None]:
    """이미지 id → 그 이미지를 쓰는 서비스가 듣는 첫 포트 (Dockerfile ENV PORT·EXPOSE 용, 모르면 None)."""
    out: dict[str, int | None] = {}
    for iid, svc in _built(units):
        if out.get(iid) is None:
            out[iid] = svc["ports"][0] if svc["ports"] else None
    return out


def users_of(units: dict, image_id: str) -> list[dict]:
    """이 이미지를 쓰는 서비스 (Dockerfile 생성 프롬프트용, run 제외)."""
    return [{k: v for k, v in svc.items() if k not in ("run", "evidence")} for iid, svc in _built(units) if iid == image_id]

