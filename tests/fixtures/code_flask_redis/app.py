import os

import redis
from flask import Flask

app = Flask(__name__)
cache = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))


@app.get("/health")
def health():
    return "ok"


@app.get("/")
def index():
    return str(cache.incr("hits"))
