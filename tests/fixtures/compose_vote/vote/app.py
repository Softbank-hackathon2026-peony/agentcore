from flask import Flask, render_template, request, make_response, g
from redis import Redis
import os
import json

option_a = os.getenv('OPTION_A', "Cats")
option_b = os.getenv('OPTION_B', "Dogs")

app = Flask(__name__)


def get_redis():
    if not hasattr(g, 'redis'):
        g.redis = Redis(host="redis", db=0, socket_timeout=5)
    return g.redis


@app.route("/", methods=['POST', 'GET'])
def hello():
    vote = None
    if request.method == 'POST':
        redis = get_redis()
        vote = request.form['vote']
        data = json.dumps({'voter_id': 'x', 'vote': vote})
        redis.rpush('votes', data)
    return make_response(json.dumps({'a': option_a, 'b': option_b, 'vote': vote}))


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=80, debug=True, threaded=True)
