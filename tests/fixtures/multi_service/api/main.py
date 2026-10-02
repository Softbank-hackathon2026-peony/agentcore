import os

from fastapi import FastAPI

app = FastAPI()
DATABASE_URL = os.environ.get("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")


@app.get("/")
def root():
    return {"ok": True}
