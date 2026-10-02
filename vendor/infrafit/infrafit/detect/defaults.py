"""설정이 없을 때 적용되는 기본값을 사실로 기록한다(설계 §6.5)."""

from __future__ import annotations

import json

from infrafit.detect.artifacts import ParsedArtifact, as_dict, pod_spec


def _is_full_object(doc: dict) -> bool:
    """kustomize 패치가 아니라 완전한 객체인가. 패치는 보통 이미지가 없다."""
    if doc.get("kind") == "CronJob":
        return "schedule" in as_dict(doc.get("spec"))
    containers = pod_spec(doc).get("containers")
    return (isinstance(containers, list) and bool(containers)
            and bool(as_dict(containers[0]).get("image")))


def _target_keys(art: ParsedArtifact, entry: dict) -> list[str]:
    if art.kind == "dockerfile":
        return [entry["key"]]
    if art.kind == "k8s":
        return [f"{d['kind']}/{as_dict(d.get('metadata')).get('name', '?')}.{entry['key']}"
                for d in art.objects if d.get("kind") == entry["match_kind"] and _is_full_object(d)]
    if art.kind == "terraform":
        return [f"{rtype}.{rname}.{entry['key']}" for (rtype, rname, _) in art.objects
                if rtype == entry["resource"]]
    return []


def apply_defaults(artifacts: list[ParsedArtifact], entries) -> None:
    for art in artifacts:
        present = {s["key"] for s in art.settings}
        for entry in entries:
            if entry["artifact"] != art.kind:
                continue
            for key in _target_keys(art, entry):
                if key not in present:
                    art.settings.append({"key": key, "value": entry["value"], "defaulted": True,
                                         "default_source": entry["source"]})
                    present.add(key)


def hop_settings(component: str, explicit: dict, entries) -> list[dict]:
    out = [{"key": k, "value": v, "defaulted": False} for k, v in explicit.items()]
    for entry in entries:
        if entry["artifact"] == "hop" and entry["component"] == component and entry["key"] not in explicit:
            out.append({"key": entry["key"], "value": entry["value"], "defaulted": True,
                        "default_source": entry["source"]})
    return sorted(out, key=lambda s: s["key"])


def fact_settings(component: str, groups: list[list[dict]], entries) -> list[dict]:
    """근거가 달린 설정 사실 묶음들(route·server마다 하나) → 구간 설정.
    묶음마다 그 묶음에 없는 키는 기본값으로 채운 뒤, 같은 (키, 값)은 하나로 합치고(명시한 것이 우선)
    값이 다르면 값마다 따로 둔다. 정렬은 (키, 값의 문자열)."""
    explicit, defaulted = [], []
    for facts in groups:
        keys = {f["key"] for f in facts}
        explicit += facts
        defaulted += [d for d in hop_settings(component, {}, entries) if d["key"] not in keys]
    merged: dict[tuple[str, str], dict] = {}
    for f in explicit + defaulted:
        merged.setdefault((f["key"], json.dumps(f["value"], sort_keys=True)), dict(f))
    return sorted(merged.values(), key=lambda s: (s["key"], str(s["value"])))
