from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str
    redis_url: str
    queue_max_len: int = 50000
    log_level: str = "INFO"

    page_size: int = 20
    list_cache_ttl_seconds: int = 3
    stale_cache_ttl_seconds: int = 60
    detail_cache_ttl_seconds: int = 30
    idempotency_ttl_seconds: int = 600
    status_ttl_seconds: int = 3600

    worker_batch_size: int = 100
    worker_block_ms: int = 1000
    claim_idle_ms: int = 30000
    heartbeat_path: str = "/tmp/worker-heartbeat"
    worker_metrics_port: int = 9100
