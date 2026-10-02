"""관찰 → 차원 값(DimensionValue). 엔드포인트 → 워크로드 → 앱 집계(설계 §7.3).

값 모양(knowledge/profile_detectors.yaml `dimensions[].shape`):
- set: 값 목록(어휘 순서), 합집합
- ordered: 어휘 순서의 최댓값
- flag: "없음" | "있음"
- kinds: {"value": "없음"|"있음", "kinds": [종류...]}, 종류 합집합
"""

from __future__ import annotations

from infrafit.profile.detectors import Observation

MAX_EVIDENCE = 10
CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def _dedupe(evs) -> list[dict]:
    out, seen = [], set()
    for e in evs:
        key = (e["path"], e.get("line"), e["snippet"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def _best(confs) -> str:
    return max(confs, key=lambda c: CONF_RANK[c])


def _combine(dim: str, spec: dict, obs: list[Observation]) -> tuple[object, list[Observation]] | None:
    """관찰들의 값과 그 값을 정한 관찰. 관찰이 없으면 None."""
    if not obs:
        return None
    shape = spec["shape"]
    vocab = spec.get("values", [])
    if shape == "set":
        vals = {o.value for o in obs}
        return [v for v in vocab if v in vals], obs
    if shape == "ordered":
        top = max(vocab.index(o.value) for o in obs)
        return vocab[top], [o for o in obs if vocab.index(o.value) == top]
    if shape == "flag":
        return "있음", obs
    if shape == "kinds":
        return {"value": "있음", "kinds": sorted({o.kind for o in obs})}, obs
    raise ValueError(f"{dim}: 알 수 없는 shape {shape}")


def _default_value(spec: dict) -> object:
    v = spec["default"]["value"]
    return {"value": v, "kinds": []} if spec["shape"] == "kinds" else v


def _row(dim, scope, value, source, confidence, evidence=(), aggregated_from=(), assumption_key=None) -> dict:
    row = {"dimension": dim, "scope": scope, "value": value, "source": source, "confidence": confidence,
           "evidence": _dedupe(evidence)[:MAX_EVIDENCE]}
    if assumption_key:
        row["assumption_key"] = assumption_key
    if aggregated_from:
        row["aggregated_from"] = sorted(aggregated_from)
    return row


def _rank(spec: dict, value) -> int:
    shape = spec["shape"]
    if shape == "ordered":
        return spec["values"].index(value)
    if shape == "flag":
        return int(value == "있음")
    if shape == "kinds":
        return int(value["value"] == "있음")
    return len(value)


def workload_rows(dims: dict, workloads: list[dict], endpoints: list[dict],
                  obs: list[Observation]) -> tuple[list[dict], list[dict]]:
    """(엔드포인트 행, 워크로드 행). 엔드포인트 행은 탐지된 값이 있는 엔드포인트·차원만."""
    ep_of = {e["id"]: e["workload"] for e in endpoints}
    by: dict[tuple[str, str], list[Observation]] = {}
    for o in obs:
        by.setdefault((o.scope, o.dimension), []).append(o)
    ep_rows: list[dict] = []
    ep_vals: dict[tuple[str, str], list[dict]] = {}
    for (scope, dim), items in sorted(by.items()):
        if scope not in ep_of:
            continue
        value, used = _combine(dim, dims[dim], items)
        row = _row(dim, scope, value, "detector", _best(o.confidence for o in used),
                   [e for o in used for e in o.evidence])
        ep_rows.append(row)
        ep_vals.setdefault((ep_of[scope], dim), []).append(row)
    w_rows: list[dict] = []
    for w in workloads:
        wid = w["id"]
        for dim, spec in dims.items():
            if spec.get("applies_to") and w["kind"] not in spec["applies_to"]:
                continue
            own = list(by.get((wid, dim), []))
            if spec["shape"] == "set" and w["kind"] in spec.get("from_kind", {}):
                own.append(Observation(wid, dim, spec["from_kind"][w["kind"]], None, (w["entrypoint"],),
                                       "high"))
            eps = ep_vals.get((wid, dim), [])
            # 엔드포인트 행도 관찰로 되돌려 같은 규칙으로 합친다(값을 정한 엔드포인트를 aggregated_from에 남긴다)
            if spec["shape"] == "kinds":
                ep_obs = [Observation(r["scope"], dim, None, k, tuple(r["evidence"]), r["confidence"])
                          for r in eps for k in r["value"]["kinds"]]
            elif spec["shape"] == "set":
                ep_obs = [Observation(r["scope"], dim, v, None, tuple(r["evidence"]), r["confidence"])
                          for r in eps for v in r["value"]]
            else:
                ep_obs = [Observation(r["scope"], dim, r["value"], None, tuple(r["evidence"]), r["confidence"])
                          for r in eps]
            got = _combine(dim, spec, own + ep_obs)
            if got is None:
                d = spec["default"]
                src = d["source"]
                w_rows.append(_row(dim, wid, _default_value(spec), src,
                                   "low" if src == "assumption" else "medium",
                                   assumption_key=dim if src == "assumption" else None))
                continue
            value, used = got
            from_eps = sorted({o.scope for o in used if o.scope in ep_of})
            w_rows.append(_row(dim, wid, value, "detector", _best(o.confidence for o in used),
                               [e for o in used for e in o.evidence], aggregated_from=from_eps))
    return ep_rows, w_rows


def app_rows(dims: dict, app_scope: str, w_rows: list[dict]) -> list[dict]:
    """앱 워크로드 집계 범위의 행: aggregated_from = 그 차원 행이 있는 앱 워크로드 전부."""
    out: list[dict] = []
    for dim, spec in dims.items():
        rows = [r for r in w_rows if r["dimension"] == dim]
        if not rows:
            continue
        detected = [r for r in rows if r["source"] != "assumption" and r["evidence"]]
        if spec["shape"] == "set":
            vals = {v for r in rows for v in r["value"]}
            value = [v for v in spec["values"] if v in vals]
            used = rows
        elif spec["shape"] == "kinds":
            kinds = sorted({k for r in rows for k in r["value"]["kinds"]})
            value = {"value": "있음" if kinds else "없음", "kinds": kinds}
            used = [r for r in rows if r["value"]["kinds"]] or rows
        else:
            top = max(_rank(spec, r["value"]) for r in rows)
            used = [r for r in rows if _rank(spec, r["value"]) == top]
            value = used[0]["value"]
        assumed = all(r["source"] == "assumption" for r in used)
        out.append(_row(dim, app_scope, value, "assumption" if assumed else "detector",
                        _best(r["confidence"] for r in used),
                        [e for r in used if r in detected for e in r["evidence"]],
                        aggregated_from=[r["scope"] for r in rows],
                        assumption_key=dim if assumed else None))
    return out
