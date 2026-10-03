"""데이터 범위와 현재 구성 요소 매핑, 미매핑 의존성."""

from __future__ import annotations

from pathlib import PurePosixPath

from infrafit import kb
from infrafit.detect.artifacts import ParsedArtifact, as_dict, flatten
from infrafit.detect.images import ImageService
from infrafit.detect.manifests import Manifests
from infrafit.detect.signatures import Match, is_aux_path
from infrafit.detect.workloads import WorkloadInfo, slug
from infrafit.evidence import evidence
from infrafit.repo import Snapshot, parent_dir

PLATFORM_COMPUTE = {
    "vercel.json": "cp:vercel/functions/unspecified-plan",
    "netlify.toml": "cp:netlify/functions/unspecified-plan",
    "fly.toml": "cp:fly/machines/default",
    "render.yaml": "cp:render/web/unspecified-plan",
    "railway.json": "cp:railway/service/unspecified-plan",
    "railway.toml": "cp:railway/service/unspecified-plan",
    "railway.ts": "cp:railway/service/unspecified-plan",
}
# 정적 프런트엔드에는 함수 컴퓨트로 매핑하지 않는 플랫폼 설정(정적 호스팅 구성 요소는 아직 능력 표에 없다)
STATIC_HOSTING_FILES = ("vercel.json", "netlify.toml")
GENERIC_PROVIDERS = {"local", "unspecified", "lib"}
# 이미지 분류 role → 데이터 범위 role. infra는 구성 요소가 있을 때만 범위(other)를 만든다
IMAGE_SCOPE_ROLES = {"datastore": "primary-db", "cache": "cache", "queue": "queue", "infra": "other"}
NON_BACKEND_KINDS = ("static-frontend", "reverse-proxy")


def scope_id(component: str, role: str) -> str:
    provider, product, _ = component.split(":", 1)[1].split("/")
    name = product if provider in GENERIC_PROVIDERS else f"{provider}-{product}"
    prefix = "ds" if role in ("primary-db", "search") else "svc"
    return f"{prefix}-{slug(name)}"


def _terraform_objects(artifacts: list[ParsedArtifact]):
    for art in artifacts:
        if art.kind == "terraform":
            for obj in art.objects:
                if isinstance(obj, (tuple, list)) and len(obj) == 3 and isinstance(obj[0], str):
                    yield obj[0], obj[1], obj[2], art.path


def _single_terraform_root(artifacts: list[ParsedArtifact]) -> bool:
    return len({parent_dir(a.path) for a in artifacts if a.kind == "terraform"}) == 1


def _refined(component: str, rtype: str, flat: dict) -> str | None:
    if component == "ds:unspecified/postgresql/default":
        if rtype == "aws_db_instance" and str(flat.get("engine", "")).startswith("postgres"):
            multi = flat.get("multi_az") in (True, "true")
            return "ds:aws/rds-postgres/multi-az-instance" if multi else "ds:aws/rds-postgres/single-az"
        if rtype == "google_sql_database_instance" and str(flat.get("database_version", "")).startswith("POSTGRES"):
            ha = flat.get("settings.availability_type") == "REGIONAL"
            return "ds:gcp/cloudsql-postgres/ha" if ha else "ds:gcp/cloudsql-postgres/single"
    if component == "ca:unspecified/redis/default":
        if rtype.startswith("aws_elasticache_"):
            return "ca:aws/elasticache/node-based"
        if rtype == "google_redis_instance":
            return "ca:gcp/memorystore/redis"
    return None


def _refine_hosting(component: str, artifacts: list[ParsedArtifact]) -> tuple[str, str | None, bool]:
    """Terraform 자원으로 호스팅을 구체화한다. (구성 요소, 근거 경로, 확정 여부).

    Terraform 디렉터리는 보통 앱과 떨어져 있어 위치로 연결할 수 없다. 그래서 저장소 전체를 보되,
    Terraform 루트가 하나이고 맞는 자원이 하나일 때만 확정으로 본다."""
    found = [(refined, path) for rtype, _, attrs, path in _terraform_objects(artifacts)
             if (refined := _refined(component, rtype, flatten(attrs)))]
    if not found:
        return component, None, True
    return found[0][0], found[0][1], len(found) == 1 and _single_terraform_root(artifacts)


def _ep(w: WorkloadInfo) -> str | None:
    path = as_dict(w.entrypoint).get("path")
    return path if isinstance(path, str) else None


def platform_config_for(w: WorkloadInfo, artifacts: list[ParsedArtifact]) -> ParsedArtifact | None:
    """코드에서 찾은 워크로드에 적용되는 플랫폼 설정: 코드 루트를 품는 가장 깊은 디렉터리의 설정.

    k8s·compose 워크로드는 배포 방식이 이미 정해져 있으므로 플랫폼 설정을 붙이지 않는다.
    코드 루트를 모르면 저장소 루트의 설정만 쓴다."""
    if w.source != "code":
        return None
    configs = [a for a in artifacts if a.kind == "platform-config"
               and PurePosixPath(a.path).name in PLATFORM_COMPUTE
               and (parent_dir(a.path) == "" or (w.code_root is not None and _under(w.code_root, parent_dir(a.path))))]
    if not configs:
        return None
    return min(configs, key=lambda a: (-len(PurePosixPath(parent_dir(a.path)).parts), a.path))


def _compute_for(w: WorkloadInfo, artifacts: list[ParsedArtifact]) -> tuple[str, str | None, str]:
    config = platform_config_for(w, artifacts)
    if config is not None:
        name = PurePosixPath(config.path).name
        if w.kind == "static-frontend" and name in STATIC_HOSTING_FILES:  # 정적 호스팅이지 함수 컴퓨트가 아니다
            return "unmapped", config.path, "confirmed"
        return PLATFORM_COMPUTE[name], config.path, "confirmed"
    if w.source == "k8s":
        clusters = [rtype for rtype, _, _, _ in _terraform_objects(artifacts)
                    if rtype in ("aws_eks_cluster", "google_container_cluster")]
        status = "confirmed" if len(clusters) == 1 and _single_terraform_root(artifacts) else "candidate"
        if "aws_eks_cluster" in clusters:
            return "cp:aws/eks/unspecified", _ep(w), status
        if "google_container_cluster" in clusters:
            return "cp:gcp/gke/unspecified", _ep(w), status
        return "cp:k8s/deployment/unspecified-cluster", _ep(w), "confirmed"
    if w.source == "compose":
        return "cp:local/compose/default", _ep(w), "confirmed"
    # 저장소 루트 Dockerfile로 대신 정하는 규칙은 정적 프런트엔드에 쓰지 않는다(그 이미지는 다른 앱의 것이다)
    for target in (w.app_dir,) if w.kind == "static-frontend" else (w.app_dir, ""):
        for art in artifacts:
            if art.kind == "dockerfile" and parent_dir(art.path) == target:
                return "cp:docker/container/unspecified-host", art.path, "confirmed"
    return "unmapped", None, "confirmed"


def _under(path: str, root: str) -> bool:
    return root == "" or path == root or path.startswith(root + "/")


def _users(evidence_items: list[dict], workloads: list[WorkloadInfo]) -> tuple[list[str], bool]:
    """(사용하는 워크로드, 코드 위치로 정했는가). 못 정하면 모든 백엔드 워크로드로 추측한다.
    정적 프런트엔드·리버스 프록시는 사용 주체가 아니다."""
    backend = [w for w in workloads if w.kind not in NON_BACKEND_KINDS]
    paths = {e["path"] for e in evidence_items}
    users = sorted(w.id for w in backend
                   if w.code_root is not None and any(_under(p, w.code_root) for p in paths))
    if users:
        return users, True
    return sorted(w.id for w in backend), False


def _add_image_services(scopes: dict[str, dict], services: list[ImageService], workloads: list[WorkloadInfo]) -> None:
    """구성 요소가 있는 분류된 compose 서비스: 같은 구성 요소의 범위가 있으면 근거에 compose 줄을 더하고,
    없으면 후보 범위를 만든다. 새 범위의 사용 주체는 그 서비스를 depends_on하는 앱 워크로드다."""
    by_name = {w.name: w for w in workloads if w.source == "compose" and w.kind not in NON_BACKEND_KINDS}
    for svc in services:
        role = IMAGE_SCOPE_ROLES.get(svc.role)
        if not role or not svc.component:
            continue
        sid = next((k for k in sorted(scopes) if scopes[k]["component"] == svc.component), None)
        if sid is None:
            sid = scope_id(svc.component, role)
            scopes.setdefault(sid, {"role": role, "component": svc.component, "status": "candidate",
                                    "signatures": [], "evidence": [], "label": f"image:{svc.label}", "users": []})
        entry = scopes[sid]
        if svc.evidence not in entry["evidence"]:
            entry["evidence"].append(svc.evidence)
        if "users" in entry:
            entry["users"] += [by_name[n].id for n in svc.dependents if n in by_name]


def _absorb(scopes: dict[str, dict], matches: list[Match], services: list[ImageService],
            artifacts: list[ParsedArtifact]) -> None:
    """시그니처의 `absorbs`(예: Supabase DB 접속 URL → 일반 Postgres): 흡수할 구성 요소의 범위를 맞은 시그니처의
    범위로 합친다(시그니처·근거를 옮기고 그 범위는 지운다). 그 구성 요소를 compose·k8s 이미지로 띄우거나
    Terraform이 호스팅을 정하면(RDS·Cloud SQL) 별개 저장소일 수 있어 합치지 않는다."""
    absorbs = {s["id"]: s.get("absorbs") or [] for s in kb.signatures()}
    image_components = {svc.component for svc in services if svc.component}
    for m in matches:
        target = scopes.get(scope_id(m.component, m.role))
        if target is None:
            continue
        for comp in absorbs.get(m.signature, []):
            if comp in image_components or _refine_hosting(comp, artifacts)[1] is not None:
                continue
            for sid in sorted(k for k, v in scopes.items() if v["component"] == comp and v is not target):
                src = scopes.pop(sid)
                target["signatures"] += [x for x in src["signatures"] if x not in target["signatures"]]
                target["evidence"] += [e for e in src["evidence"] if e not in target["evidence"]]
                if src["status"] == "confirmed":
                    target["status"] = "confirmed"


def map_components(snap: Snapshot, matches: list[Match], workloads: list[WorkloadInfo],
                   artifacts: list[ParsedArtifact],
                   services: list[ImageService] | None = None) -> tuple[list[dict], list[dict], dict[str, str]]:
    scopes: dict[str, dict] = {}
    # 근거가 모두 보조 코드에 있는 매치는 범위를 만들지 않고, 다른 근거로 만든 범위에 근거만 더한다
    aux_only = [m for m in matches if m.evidence and all(is_aux_path(e["path"]) for e in m.evidence)]
    for m in matches:
        if m in aux_only:
            continue
        sid = scope_id(m.component, m.role)
        entry = scopes.setdefault(sid, {"role": m.role, "component": m.component, "status": m.status,
                                        "signatures": [], "evidence": []})
        entry["signatures"].append(m.signature)
        entry["evidence"].extend(m.evidence)
        if m.status == "confirmed":
            entry["status"] = "confirmed"
    _add_image_services(scopes, services or [], workloads)
    for m in aux_only:
        entry = scopes.get(scope_id(m.component, m.role))
        if entry is not None:
            entry["signatures"].append(m.signature)
            entry["evidence"].extend(e for e in m.evidence if e not in entry["evidence"])
    _absorb(scopes, [m for m in matches if m not in aux_only], services or [], artifacts)

    datastores: list[dict] = []
    comps: list[dict] = []
    for sid in sorted(scopes):
        s = scopes[sid]
        component, tf_path, sure = _refine_hosting(s["component"], artifacts)
        ev = list(s["evidence"])
        if tf_path:
            ev.append(evidence(snap, tf_path))
        used_by, located = _users(s["evidence"], workloads)
        if s.get("users"):  # compose 이미지로 만든 범위: depends_on한 앱 워크로드
            used_by = sorted(set(s["users"]))
        datastores.append({"id": sid, "role": s["role"], "used_by": used_by,
                           "evidence": s["evidence"], "status": s["status"] if located else "candidate"})
        comps.append({"scope": sid, "component": component, "label": s.get("label") or ",".join(s["signatures"]),
                      "settings": [], "evidence": ev, "status": s["status"] if sure else "candidate"})

    compute: dict[str, str] = {}
    for w in sorted(workloads, key=lambda w: w.id):
        comp, path, status = _compute_for(w, artifacts)
        compute[w.id] = comp
        entry = {"scope": w.id, "component": comp, "settings": [], "status": status,
                 "evidence": [evidence(snap, path)] if path else []}
        if comp == "unmapped":
            entry["label"] = f"static hosting ({PurePosixPath(path).name})" if path else "no deployment config"
        comps.append(entry)
    return datastores, comps, compute


def image_unmapped(services: list[ImageService]) -> list[dict]:
    """구성 요소가 없는 분류된 compose 서비스는 `image:<이름>`, 호스팅 후보가 있으면 `hosting-hint:<id>`로
    compose 줄 근거와 함께 남긴다(호스팅 판단은 S2·S3 몫). 라벨 순, 같은 라벨은 근거를 모은다."""
    found: dict[str, list[dict]] = {}
    for svc in services:
        labels = [] if svc.component else [f"image:{svc.label}"]
        if svc.hosting_hint:
            labels.append(f"hosting-hint:{svc.hosting_hint}")
        for label in labels:
            evs = found.setdefault(label, [])
            if svc.evidence not in evs:
                evs.append(svc.evidence)
    return [{"label": k, "evidence": sorted(v, key=lambda e: (e["path"], e["line"] or 0))}
            for k, v in sorted(found.items())]


def find_unmapped(snap: Snapshot, manifests: Manifests) -> list[dict]:
    known = {leaf["dependency"].lower() for s in kb.signatures()
             for cond in [s["when"]] + [r["when"] for r in s.get("refine", [])]
             for leaf in kb.iter_conditions(cond) if "dependency" in leaf}
    out = []
    for name in kb.watchlist():
        if name in manifests.deps and name not in known:
            rel, line = manifests.deps[name]
            out.append({"label": name, "evidence": [evidence(snap, rel, line)]})
    return out
