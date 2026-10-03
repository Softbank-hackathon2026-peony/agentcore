from flask import Flask
from tasks import add
app = Flask(__name__)

@app.get("/")
def index():
    add.delay(1, 2)
    return "ok"
