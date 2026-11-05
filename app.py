"""Teen Patti — Flask web app. Run locally:  python app.py
On Render: gunicorn app:app  (see render.yaml)"""

import os
import uuid
from flask import Flask, jsonify, request, session, send_from_directory

from teenpatti import Game

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "teen-patti-diwali-dev-key")

BOT_NAMES = ["Ramesh", "Priya", "Arjun", "Kavita", "Vikram"]
START_CHIPS = 1000

games = {}  # session id -> Game (single-instance demo store)


def get_game():
    sid = session.get("sid")
    if not sid or sid not in games:
        sid = uuid.uuid4().hex
        session["sid"] = sid
        games[sid] = Game(["You"] + BOT_NAMES, start_chips=START_CHIPS)
    return games[sid]


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/state")
def state():
    return jsonify(get_game().state())


@app.route("/api/new_hand", methods=["POST"])
def new_hand():
    game = get_game()
    human = game.players[0]
    if human.chips <= 0:
        return jsonify({"error": "You are out of chips — reset the game for a fresh 1,000."}), 400
    game.start_hand()
    return jsonify(game.state())


@app.route("/api/reset", methods=["POST"])
def reset():
    sid = session.get("sid") or uuid.uuid4().hex
    session["sid"] = sid
    games[sid] = Game(["You"] + BOT_NAMES, start_chips=START_CHIPS)
    games[sid].start_hand()
    return jsonify(games[sid].state())


@app.route("/api/action", methods=["POST"])
def action():
    game = get_game()
    human = game.players[0]
    data = request.get_json(force=True) or {}
    act = data.get("action")
    if game.pending_sideshow and act in ("sideshow_accept", "sideshow_refuse"):
        game.resolve_sideshow(accept=(act == "sideshow_accept"))
        return jsonify(game.state())
    if act not in ("see", "chaal", "raise", "pack", "show", "sideshow"):
        return jsonify({"error": "unknown action"}), 400
    if act not in game.legal_actions(human):
        return jsonify({"error": f"action '{act}' is not available right now"}), 400
    game.act(human, act)
    return jsonify(game.state())


@app.route("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
