import os
from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
celery = Celery("tasks", broker=REDIS_URL, backend=REDIS_URL)
celery.conf.beat_schedule = {"tick": {"task": "tasks.add", "schedule": 60.0, "args": (1, 1)}}

@celery.task
def add(x, y):
    return x + y
