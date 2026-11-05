"""Teen Patti — Flask web app: solo vs bots + multiplayer rooms (share a code).

Run locally:  python app.py
On Render:    gunicorn app:app  (see render.yaml)
State is in-memory (single worker) — fine for a family game night.
"""

import os
import re
import secrets
import string
import uuid
from flask import Flask, jsonify, request, session, send_from_directory

from teenpatti import Game

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "teen-patti-diwali-dev-key")

BOT_NAMES = ["Ramesh", "Priya", "Arjun", "Kavita", "Vikram"]
START_CHIPS = 1000
MAX_SEATS = 6

solo_games = {}   # session id -> Game
rooms = {}        # code -> room dict


def clean_name(raw):
    name = re.sub(r"[^\w\s.\-']", "", str(raw or "")).strip()[:16]
    return name or "Player"


# ============================== SOLO ==============================

def solo_specs(name):
    return [{"name": name, "is_human": True, "chips": START_CHIPS}] + [
        {"name": b, "is_human": False, "chips": START_CHIPS} for b in BOT_NAMES
    ]


def get_solo():
    sid = session.get("sid")
    if not sid or sid not in solo_games:
        sid = uuid.uuid4().hex
        session["sid"] = sid
        solo_games[sid] = Game(solo_specs(session.get("player_name") or "You"))
    return solo_games[sid]


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/api/state")
def solo_state():
    return jsonify(get_solo().state(0))


@app.route("/api/new_hand", methods=["POST"])
def solo_new_hand():
    game = get_solo()
    if game.players[0].chips <= 0:
        return jsonify({"error": "You are out of chips — reset the game for a fresh 1,000."}), 400
    game.start_hand()
    return jsonify(game.state(0))


@app.route("/api/reset", methods=["POST"])
def solo_reset():
    data = request.get_json(silent=True) or {}
    if data.get("name"):
        session["player_name"] = clean_name(data["name"])
    sid = session.get("sid") or uuid.uuid4().hex
    session["sid"] = sid
    solo_games[sid] = Game(solo_specs(session.get("player_name") or "You"))
    solo_games[sid].start_hand()
    return jsonify(solo_games[sid].state(0))


@app.route("/api/action", methods=["POST"])
def solo_action():
    game = get_solo()
    human = game.players[0]
    data = request.get_json(force=True) or {}
    act = data.get("action")
    if game.pending_sideshow and act in ("sideshow_accept", "sideshow_refuse"):
        game.resolve_sideshow(accept=(act == "sideshow_accept"))
        return jsonify(game.state(0))
    if act not in ("see", "chaal", "raise", "pack", "show", "sideshow"):
        return jsonify({"error": "unknown action"}), 400
    if act not in game.legal_actions(human):
        return jsonify({"error": f"action '{act}' is not available right now"}), 400
    game.act(human, act)
    return jsonify(game.state(0))


# ============================== ROOMS ==============================

def new_code():
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if code not in rooms:
            return code


def room_state(room, member):
    """Personalised state for one room member (or lobby state)."""
    sync_chips(room)
    base = {
        "mode": "room",
        "code": room["code"],
        "is_host": member["token"] == room["host_token"],
        "fill_bots": room["fill_bots"],
        "members": [{"name": m["name"], "chips": m["chips"],
                     "is_you": m["token"] == member["token"],
                     "is_host": m["token"] == room["host_token"]} for m in room["members"]],
        "room_phase": room["phase"],   # lobby | betting | finished
    }
    game = room["game"]
    if game is None:
        base.update({"phase": "lobby", "players": [], "pot": 0, "stake": Game.BOOT,
                     "boot": Game.BOOT, "turn": None, "winner": None, "log": room["log"][-14:],
                     "your_actions": [], "pending_sideshow": None, "your_hand": None, "viewer": -1})
        return base
    seat = room["seat_of"].get(member["token"])
    if seat is None:
        # spectator (joined mid-hand): watch with cards hidden
        st = game.state(0)
        st["players"] = [dict(p, cards=[], is_you=False) for p in st["players"]]
        st["your_actions"] = []
        st["your_hand"] = None
        st["pending_sideshow"] = None
        st["viewer"] = -1
        st["spectating"] = True
    else:
        st = game.state(seat)
        st["spectating"] = False
    base.update(st)
    base["room_phase"] = room["phase"]
    return base


def sync_chips(room):
    game = room["game"]
    if game is None:
        return
    for token, seat in room["seat_of"].items():
        m = next(x for x in room["members"] if x["token"] == token)
        m["chips"] = game.players[seat].chips
    if game.phase == "finished" and room["phase"] != "finished":
        room["phase"] = "finished"


def get_room_member(code, token):
    room = rooms.get((code or "").upper())
    if not room:
        return None, None, (jsonify({"error": "Room not found. Check the code."}), 404)
    member = next((m for m in room["members"] if m["token"] == token), None)
    if not member:
        return None, None, (jsonify({"error": "You are not in this room — join again."}), 403)
    return room, member, None


@app.route("/api/room/create", methods=["POST"])
def room_create():
    data = request.get_json(force=True) or {}
    name = clean_name(data.get("name"))
    code = new_code()
    token = uuid.uuid4().hex
    rooms[code] = {
        "code": code,
        "host_token": token,
        "members": [{"token": token, "name": name, "chips": START_CHIPS}],
        "game": None,
        "seat_of": {},
        "phase": "lobby",
        "fill_bots": False,
        "log": [f"Room {code} created by {name}. Share the code with friends!"],
    }
    return jsonify({"code": code, "token": token})


@app.route("/api/room/join", methods=["POST"])
def room_join():
    data = request.get_json(force=True) or {}
    code = str(data.get("code") or "").strip().upper()
    name = clean_name(data.get("name"))
    room = rooms.get(code)
    if not room:
        return jsonify({"error": "Room not found. Check the code."}), 404
    # rejoin with an existing name in this room (page refresh) -> new token, same seat record
    existing = next((m for m in room["members"] if m["name"].lower() == name.lower()), None)
    if existing:
        old = existing["token"]
        existing["token"] = uuid.uuid4().hex
        if room["host_token"] == old:
            room["host_token"] = existing["token"]
        if old in room["seat_of"]:
            room["seat_of"][existing["token"]] = room["seat_of"].pop(old)
        return jsonify({"code": code, "token": existing["token"], "rejoined": True})
    if len(room["members"]) >= MAX_SEATS:
        return jsonify({"error": "This room is full (6 players)."}), 400
    token = uuid.uuid4().hex
    room["members"].append({"token": token, "name": name, "chips": START_CHIPS})
    room["log"].append(f"{name} joined the room.")
    return jsonify({"code": code, "token": token})


@app.route("/api/room/<code>/state")
def room_get_state(code):
    room, member, err = get_room_member(code, request.args.get("token"))
    if err:
        return err
    return jsonify(room_state(room, member))


@app.route("/api/room/<code>/start", methods=["POST"])
def room_start(code):
    data = request.get_json(force=True) or {}
    room, member, err = get_room_member(code, data.get("token"))
    if err:
        return err
    if member["token"] != room["host_token"]:
        return jsonify({"error": "Only the host can start the hand."}), 403
    if room["phase"] == "betting":
        return jsonify({"error": "A hand is already in progress."}), 400
    sync_chips(room)
    players = [m for m in room["members"] if m["chips"] > 0]
    if len(players) < 2 and not room["fill_bots"]:
        return jsonify({"error": "Need at least 2 players with chips — or turn on 'Fill empty seats with bots'."}), 400
    specs, seat_of = [], {}
    for m in players:
        seat_of[m["token"]] = len(specs)
        specs.append({"name": m["name"], "is_human": True, "chips": m["chips"]})
    if room["fill_bots"]:
        used = {m["name"] for m in room["members"]}
        for b in BOT_NAMES:
            if len(specs) >= MAX_SEATS:
                break
            if b not in used:
                specs.append({"name": b, "is_human": False, "chips": START_CHIPS})
    if len(specs) < 2:
        return jsonify({"error": "Not enough players to start."}), 400
    room["game"] = Game(specs)
    room["seat_of"] = seat_of
    room["phase"] = "betting"
    room["log"] = []
    room["game"].log = room["log"]
    room["game"].start_hand()
    sync_chips(room)
    return jsonify(room_state(room, member))


@app.route("/api/room/<code>/fill_bots", methods=["POST"])
def room_fill_bots(code):
    data = request.get_json(force=True) or {}
    room, member, err = get_room_member(code, data.get("token"))
    if err:
        return err
    if member["token"] != room["host_token"]:
        return jsonify({"error": "Only the host can change this."}), 403
    room["fill_bots"] = bool(data.get("fill"))
    return jsonify(room_state(room, member))


@app.route("/api/room/<code>/action", methods=["POST"])
def room_action(code):
    data = request.get_json(force=True) or {}
    room, member, err = get_room_member(code, data.get("token"))
    if err:
        return err
    game = room["game"]
    if game is None:
        return jsonify({"error": "No hand in progress."}), 400
    act = data.get("action")
    if game.pending_sideshow and act in ("sideshow_accept", "sideshow_refuse"):
        if game.pending_sideshow["to_idx"] != room["seat_of"].get(member["token"]):
            return jsonify({"error": "That side show is not for you."}), 403
        game.resolve_sideshow(accept=(act == "sideshow_accept"))
        sync_chips(room)
        return jsonify(room_state(room, member))
    seat = room["seat_of"].get(member["token"])
    if seat is None:
        return jsonify({"error": "You are spectating this hand."}), 403
    player = game.players[seat]
    if act not in ("see", "chaal", "raise", "pack", "show", "sideshow"):
        return jsonify({"error": "unknown action"}), 400
    if act not in game.legal_actions(player):
        return jsonify({"error": "Not your turn, or that action is not available."}), 400
    game.act(player, act)
    sync_chips(room)
    return jsonify(room_state(room, member))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
