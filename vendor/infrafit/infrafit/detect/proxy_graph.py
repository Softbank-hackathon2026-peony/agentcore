"""프록시 그래프: 환경 하나의 route들로 워크로드에 요청을 넘기는 프록시 체인을 찾는다.
요청 경로(paths)와 exposure(endpoints)가 같은 체인 규칙과 상한을 쓴다."""

from __future__ import annotations

from collections.abc import Callable, Container

from infrafit.detect.environments import Environment, env_scopes
from infrafit.detect.nginx import ProxyRoute, ProxyServer
from infrafit.detect.workloads import WorkloadInfo

MAX_CHAIN = 5  # 체인에 넣는 프록시 수의 상한
MAX_CHAINS = 64  # (워크로드, 환경)마다 따라가는 체인 수의 상한(조밀한 프록시 망에서 조합이 폭증하지 않게)


def fronted_proxies(workloads: list[WorkloadInfo], environments: list[Environment], servers: list[ProxyServer],
                    has_front: Callable[[WorkloadInfo, Environment | None], bool]) -> set[tuple[str, str | None]]:
    """자기 앞 구간(엣지·로드밸런서)이 있는 (프록시 워크로드 id, 환경 이름). 체인은 이런 프록시에서 끝난다."""
    ids = {s.proxy for s in servers}
    return {(w.id, e.name if e else None) for w in workloads if w.id in ids
            for e in env_scopes(w, environments) if has_front(w, e)}


def fronted_in(fronted: set[tuple[str, str | None]], env: str | None) -> set[str]:
    """환경 env에서 앞 구간이 있는 프록시 id들."""
    return {p for p, e in fronted if e == env}


def upstream_chains(routes: list[ProxyRoute], fronted: Container[str], target: str, bottom: str | None = None,
                    below: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """target에 요청을 넘기는 프록시 체인들(위→아래, 맨 끝 프록시가 맨 아래 워크로드 bottom으로 넘긴다).
    routes는 한 환경의 route들이다. 갈래마다 프록시 id 순으로 따라 올라가며, 앞 구간이 있는 프록시, 자기에게 넘기는
    다른 프록시가 없는 프록시, 프록시 MAX_CHAIN개에서 끝난다. 이미 체인에 있는 프록시와 bottom으로 되돌아가는
    프록시(순환)는 빼고 다음 id로 넘어간다. 맨 아래 단계에서 bottom 자신에게 넘기는 프록시는 그것만으로 체인 하나다.
    체인은 MAX_CHAINS개까지 모은다."""
    bottom = target if bottom is None else bottom
    out: list[tuple[str, ...]] = []
    for p in sorted({r.proxy for r in routes if r.target == target}):
        if len(out) >= MAX_CHAINS:
            break
        if p in below or (below and p == bottom):
            continue
        chain = (p,) + below
        ups = ([] if p == bottom or p in fronted or len(chain) >= MAX_CHAIN
               else upstream_chains(routes, fronted, p, bottom, chain))
        out += (ups or [chain])[:MAX_CHAINS - len(out)]
    return out


def first_chain(routes: list[ProxyRoute], fronted: Container[str], target: str) -> list[tuple[str, list[ProxyRoute]]]:
    """요청 경로에 쓰는 체인 하나: upstream_chains의 첫 번째(단계마다 프록시 id 순 첫 번째), 단 target 자신에게
    넘기는 route만으로 된 체인은 뺀다. (프록시 id, 그 프록시에서 아래 단계로 가는 route들) 목록, 위→아래."""
    chain = next((c for c in upstream_chains(routes, fronted, target) if c != (target,)), ())
    nexts = chain[1:] + (target,)
    return [(p, [r for r in routes if r.proxy == p and r.target == n]) for p, n in zip(chain, nexts)]
