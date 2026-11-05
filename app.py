"""Teen Patti — Flask web app: solo vs bots + multiplayer rooms.

Implements the MVP blueprint's product rules in a single-file Python service:
- Wallet: 10,000-coin new-player bonus, +500 daily reward, refill to 1,000
  when broke (once per 24h). Coins are free, non-transferable, non-redeemable.
- Rooms: private, 3-6 players, host controls, optional bot fill.
- Round history per table and a simple leaderboard.
State is in-memory (single worker) — suitable for a private friends/family game;
a restart resets tables and wallets. See README for the production roadmap.

Run locally:  python app.py      On Render:  gunicorn app:app
"""

import os
import re
import secrets
import string
import time
import uuid
from datetime import date
from flask import Flask, jsonify, request, session, send_from_directory

from teenpatti import Game

app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("SECRET_KEY", "teen-patti-diwali-dev-key")

BOT_NAMES = ["Ramesh", "Priya", "Arjun", "Kavita", "Vikram"]
START_BALANCE = 10000
DAILY_REWARD = 500
REFILL_AMOUNT = 1000
REFILL_COOLDOWN = 24 * 3600
MIN_PLAYERS = 3
MAX_SEATS = 6
HISTORY_KEEP = 20

solo_games = {}   # session id -> {"game": Game, "history": [...]}
rooms = {}        # code -> room dict
wallets = {}      # player_id -> {"name", "balance", "last_daily", "last_refill"}


def clean_name(raw):
    name = re.sub(r"[^\w\s.\-']", "", str(raw or "")).strip()[:16]
    return name or "Player"


# ============================== WALLET ==============================

def get_wallet(player_id, name=None):
    if not player_id:
        return None
    w = wallets.get(player_id)
    today = date.today().isoformat()
    if w is None:
        w = {"name": name or "Player", "balance": START_BALANCE,
             "last_daily": today, "last_refill": 0}
        wallets[player_id] = w
    else:
        if name:
            w["name"] = name
        if w["last_daily"] != today:
            w["balance"] += DAILY_REWARD
            w["last_daily"] = today
    return w


def wallet_view(player_id):
    w = wallets.get(player_id) if player_id else None
    if not w:
        return None
    return {
        "balance": w["balance"],
        "can_refill": w["balance"] <= 0 and (time.time() - w["last_refill"] >= REFILL_COOLDOWN),
    }


@app.route("/api/wallet")
def wallet_get():
    pid = request.args.get("player_id")
    name = clean_name(request.args.get("name")) if request.args.get("name") else None
    get_wallet(pid, name)
    return jsonify(wallet_view(pid) or {"balance": START_BALANCE, "can_refill": False})


@app.route("/api/wallet/refill", methods=["POST"])
def wallet_refill():
    data = request.get_json(force=True) or {}
    pid = data.get("player_id")
    w = wallets.get(pid) if pid else None
    if not w:
        return jsonify({"error": "No wallet found."}), 404
    if w["balance"] > 0:
        return jsonify({"error": "Refill is only available at zero balance."}), 400
    if time.time() - w["last_refill"] < REFILL_COOLDOWN:
        return jsonify({"error": "Refill already used — available again after 24 hours."}), 400
    w["balance"] = REFILL_AMOUNT
    w["last_refill"] = time.time()
    return jsonify(wallet_view(pid))


@app.route("/api/leaderboard")
def leaderboard():
    rows = sorted(wallets.values(), key=lambda w: -w["balance"])[:10]
    return jsonify([{"name": w["name"], "balance": w["balance"]} for w in rows])


# ============================== SOLO ==============================

def solo_specs(balance):
    return [{"name": session.get("player_name") or "You", "is_human": True, "chips": balance}] + [
        {"name": b, "is_human": False, "chips": START_BALANCE} for b in BOT_NAMES
    ]


def get_solo():
    sid = session.get("sid")
    if not sid or sid not in solo_games:
        sid = uuid.uuid4().hex
        session["sid"] = sid
        w = get_wallet(session.get("pid"), session.get("player_name"))
        balance = w["balance"] if w else START_BALANCE
        solo_games[sid] = {"game": Game(solo_specs(balance)), "history": []}
    return solo_games[sid]


def solo_state():
    entry = get_solo()
    game = entry["game"]
    record_result(entry["history"], game)
    sync_solo_wallet(game)
    st = game.state(0)
    st["history"] = entry["history"][-8:]
    st["wallet"] = wallet_view(session.get("pid"))
    return st


def record_result(history, game):
    if game.phase == "finished" and game.last_result and \
            (not history or history[-1] is not game.last_result):
        history.append(game.last_result)
        del history[:-HISTORY_KEEP]


def sync_solo_wallet(game):
    pid = session.get("pid")
    if pid and pid in wallets:
        wallets[pid]["balance"] = game.players[0].chips


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/api/state")
def solo_get_state():
    return jsonify(solo_state())


@app.route("/api/new_hand", methods=["POST"])
def solo_new_hand():
    entry = get_solo()
    if entry["game"].players[0].chips <= 0:
        return jsonify({"error": "You are out of coins — use the refill (once per 24h) or come back tomorrow for the daily reward."}), 400
    entry["game"].start_hand()
    return jsonify(solo_state())


@app.route("/api/reset", methods=["POST"])
def solo_reset():
    data = request.get_json(silent=True) or {}
    if data.get("name"):
        session["player_name"] = clean_name(data["name"])
    if data.get("player_id"):
        session["pid"] = str(data["player_id"])[:64]
    sid = session.get("sid") or uuid.uuid4().hex
    session["sid"] = sid
    w = get_wallet(session.get("pid"), session.get("player_name"))
    balance = w["balance"] if w else START_BALANCE
    entry = {"game": Game(solo_specs(balance)), "history": solo_games.get(sid, {}).get("history", [])}
    solo_games[sid] = entry
    entry["game"].start_hand()
    return jsonify(solo_state())


@app.route("/api/action", methods=["POST"])
def solo_action():
    entry = get_solo()
    game = entry["game"]
    human = game.players[0]
    data = request.get_json(force=True) or {}
    act = data.get("action")
    game.state(0)  # lets the turn timer fire before we validate
    if game.pending_sideshow and act in ("sideshow_accept", "sideshow_refuse"):
        game.resolve_sideshow(accept=(act == "sideshow_accept"))
        return jsonify(solo_state())
    if act not in ("see", "chaal", "raise", "pack", "show", "sideshow"):
        return jsonify({"error": "unknown action"}), 400
    if act not in game.legal_actions(human):
        return jsonify({"error": f"action '{act}' is not available right now"}), 400
    game.act(human, act)
    return jsonify(solo_state())


# ============================== ROOMS ==============================

def new_code():
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if code not in rooms:
            return code


def room_state(room, member):
    sync_chips(room)
    record_result(room["history"], room["game"]) if room["game"] else None
    base = {
        "mode": "room",
        "code": room["code"],
        "is_host": member["token"] == room["host_token"],
        "fill_bots": room["fill_bots"],
        "max_players": room["max_players"],
        "members": [{"name": m["name"], "chips": m["chips"],
                     "is_you": m["token"] == member["token"],
                     "is_host": m["token"] == room["host_token"]} for m in room["members"]],
        "room_phase": room["phase"],
        "history": room["history"][-8:],
        "wallet": wallet_view(member.get("pid")),
    }
    game = room["game"]
    if game is None:
        base.update({"phase": "lobby", "players": [], "pot": 0, "stake": Game.INITIAL_STAKE,
                     "boot": Game.BOOT, "turn": None, "turn_idx": None, "seconds_left": None,
                     "winner": None, "last_result": None, "log": room["log"][-14:],
                     "your_actions": [], "pending_sideshow": None, "your_hand": None, "viewer": -1})
        return base
    seat = room["seat_of"].get(member["token"])
    if seat is None:
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
        if m.get("pid") and m["pid"] in wallets:
            wallets[m["pid"]]["balance"] = m["chips"]
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
    pid = str(data.get("player_id") or "")[:64] or None
    w = get_wallet(pid, name)
    max_players = max(MIN_PLAYERS, min(MAX_SEATS, int(data.get("max_players") or MAX_SEATS)))
    code = new_code()
    token = uuid.uuid4().hex
    rooms[code] = {
        "code": code,
        "host_token": token,
        "max_players": max_players,
        "members": [{"token": token, "name": name, "pid": pid,
                     "chips": w["balance"] if w else START_BALANCE}],
        "game": None,
        "seat_of": {},
        "phase": "lobby",
        "fill_bots": False,
        "history": [],
        "log": [f"Room {code} created by {name}. Share the code with friends!"],
    }
    return jsonify({"code": code, "token": token})


@app.route("/api/room/join", methods=["POST"])
def room_join():
    data = request.get_json(force=True) or {}
    code = str(data.get("code") or "").strip().upper()
    name = clean_name(data.get("name"))
    pid = str(data.get("player_id") or "")[:64] or None
    room = rooms.get(code)
    if not room:
        return jsonify({"error": "Room not found. Check the code."}), 404
    existing = None
    if pid:
        existing = next((m for m in room["members"] if m.get("pid") == pid), None)
    if existing is None:
        existing = next((m for m in room["members"] if m["name"].lower() == name.lower()), None)
    if existing:
        old = existing["token"]
        existing["token"] = uuid.uuid4().hex
        existing["name"] = name
        if pid:
            existing["pid"] = pid
        if room["host_token"] == old:
            room["host_token"] = existing["token"]
        if old in room["seat_of"]:
            room["seat_of"][existing["token"]] = room["seat_of"].pop(old)
        return jsonify({"code": code, "token": existing["token"], "rejoined": True})
    if len(room["members"]) >= room["max_players"]:
        return jsonify({"error": f"This room is full ({room['max_players']} players)."}), 400
    w = get_wallet(pid, name)
    token = uuid.uuid4().hex
    room["members"].append({"token": token, "name": name, "pid": pid,
                            "chips": w["balance"] if w else START_BALANCE})
    room["log"].append(f"{name} joined the room.")
    return jsonify({"code": code, "token": token})


@app.route("/api/room/<code>/state")
def room_get_state(code):
    room, member, err = get_room_member(code, request.args.get("token"))
    if err:
        return err
    if room["game"]:
        room["game"].state(0)  # fire the turn timer
        sync_chips(room)
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
    specs, seat_of = [], {}
    for m in players:
        seat_of[m["token"]] = len(specs)
        specs.append({"name": m["name"], "is_human": True, "chips": m["chips"]})
    if room["fill_bots"]:
        used = {m["name"] for m in room["members"]}
        for b in BOT_NAMES:
            if len(specs) >= room["max_players"]:
                break
            if b not in used:
                specs.append({"name": b, "is_human": False, "chips": START_BALANCE})
    if len(specs) < MIN_PLAYERS:
        return jsonify({"error": f"Need at least {MIN_PLAYERS} players at the table — invite more friends or turn on 'Fill empty seats with bots'."}), 400
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
    game.state(0)  # fire the turn timer before validating
    sync_chips(room)
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
