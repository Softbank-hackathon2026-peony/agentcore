from prometheus_client import Gauge

_cache_counts = {"hit": 0, "miss": 0}


def record_cache(hit: bool) -> None:
    _cache_counts["hit" if hit else "miss"] += 1


def _cache_hit_ratio() -> float:
    total = _cache_counts["hit"] + _cache_counts["miss"]
    return _cache_counts["hit"] / total if total else 0.0


CACHE_HIT_RATIO = Gauge("cache_hit_ratio", "Redis 읽기 캐시 적중률 (프로세스 시작 이후 누적)")
CACHE_HIT_RATIO.set_function(_cache_hit_ratio)
