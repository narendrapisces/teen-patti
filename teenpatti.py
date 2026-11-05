"""
Teen Patti game engine (pure Python, no dependencies) — solo and multiplayer.

Built to the MVP blueprint ruleset (version "bp-1"):
- Standard 52-card deck, no jokers; cryptographically secure server-side shuffle.
- Boot (ante) 100. Chaal (stake) starts at 200 and can rise to 400:
  blind pays half the stake (100-200), seen pays the full stake (200-400).
- Raise doubles the stake up to the 400 cap.
- 30-second turn timer for human players; on timeout the player auto-folds.
- Side show: a seen player may ask the previous seen player to compare;
  the loser packs; the asked player may refuse; on a tie the requester packs.
- Show: only when exactly 2 players remain. Tied hands SPLIT the pot evenly;
  any indivisible remainder goes to the earliest-seated tied player.
- Last player standing wins the pot.

Hand rankings (high -> low): Trail, Pure Sequence, Sequence, Color, Pair, High Card.
Sequences: A-K-Q > A-2-3 > K-Q-J > ... > 4-3-2.
"""

import random
import time

RANKS = "23456789TJQKA"
SUITS = "shdc"
RANK_VALUE = {r: i + 2 for i, r in enumerate(RANKS)}
_RNG = random.SystemRandom()  # cryptographically secure

CATEGORY_NAMES = {
    6: "Trail (Trio)",
    5: "Pure Sequence",
    4: "Sequence",
    3: "Color (Flush)",
    2: "Pair",
    1: "High Card",
}

RULESET_VERSION = "bp-1"


def new_deck():
    deck = [r + s for r in RANKS for s in SUITS]
    _RNG.shuffle(deck)  # Fisher-Yates via SystemRandom
    return deck


def evaluate_hand(cards):
    ranks = sorted((RANK_VALUE[c[0]] for c in cards), reverse=True)
    suits = [c[1] for c in cards]
    is_flush = len(set(suits)) == 1
    seq_high = None
    if len(set(ranks)) == 3:
        if ranks[0] - ranks[2] == 2:
            seq_high = ranks[0]
        elif ranks == [14, 3, 2]:
            seq_high = 13.5  # A-2-3 sits just below A-K-Q
    if ranks[0] == ranks[1] == ranks[2]:
        return (6, (ranks[0],))
    if seq_high is not None and is_flush:
        return (5, (seq_high,))
    if seq_high is not None:
        return (4, (seq_high,))
    if is_flush:
        return (3, tuple(ranks))
    if ranks[0] == ranks[1] or ranks[1] == ranks[2]:
        pair_rank = ranks[1]
        kicker = ranks[0] if ranks[0] != pair_rank else ranks[2]
        return (2, (pair_rank, kicker))
    return (1, tuple(ranks))


def compare_hands(a, b):
    ea, eb = evaluate_hand(a), evaluate_hand(b)
    return 1 if ea > eb else (-1 if ea < eb else 0)


def hand_strength(cards):
    cat, tb = evaluate_hand(cards)
    base = {6: 0.97, 5: 0.90, 4: 0.78, 3: 0.58, 2: 0.38, 1: 0.12}[cat]
    return base + min(sum(tb) / 42.0, 1.0) * 0.08


class Player:
    def __init__(self, name, chips, is_human=False):
        self.name = name
        self.chips = chips
        self.is_human = is_human
        self.cards = []
        self.status = "active"   # active | packed
        self.seen = False
        self.total_bet = 0
        self.blind_rounds = 0


class Game:
    BOOT = 100
    INITIAL_STAKE = 200
    MAX_STAKE = 400
    TURN_SECONDS = 30
    MAX_BET_ROUNDS = 60

    def __init__(self, player_specs):
        self.players = [Player(s["name"], int(s.get("chips", 10000)), bool(s.get("is_human", False)))
                        for s in player_specs]
        self.pot = 0
        self.stake = self.INITIAL_STAKE
        self.turn = 0
        self.dealer = -1
        self.phase = "idle"      # idle | betting | finished
        self.log = []
        self.winner = None
        self.bet_rounds = 0
        self.pending_sideshow = None   # {"from_idx": int, "to_idx": int}
        self.last_result = None
        self._deadline = None

    # ---------- helpers ----------
    def active_players(self):
        return [p for p in self.players if p.status == "active"]

    def _say(self, msg):
        self.log.append(msg)
        self.log = self.log[-80:]

    def _pay(self, player, amount):
        amount = min(amount, player.chips)
        player.chips -= amount
        player.total_bet += amount
        self.pot += amount
        return amount

    def cost_for(self, player):
        return self.stake // 2 if not player.seen else self.stake

    def current_player(self):
        return self.players[self.turn] if self.phase == "betting" else None

    # ---------- turn timer ----------
    def _refresh_deadline(self):
        """Set/clear the 30s deadline; auto-fold a human who runs out of time."""
        cur = self.current_player()
        if self.phase != "betting" or self.pending_sideshow or cur is None or not cur.is_human:
            self._deadline = None
            return
        now = time.time()
        if self._deadline is None:
            self._deadline = now + self.TURN_SECONDS
            return
        if now > self._deadline:
            self._deadline = None
            cur.status = "packed"
            self._say(f"⏱ {cur.name} ran out of time — auto-folded.")
            if not self._finish_if_done():
                self.bet_rounds += 1
                self._advance_turn()
                self.run_bots()

    # ---------- hand lifecycle ----------
    def start_hand(self):
        self.dealer = (self.dealer + 1) % len(self.players)
        deck = new_deck()
        self.pot = 0
        self.stake = self.INITIAL_STAKE
        self.bet_rounds = 0
        self.winner = None
        self.pending_sideshow = None
        self.last_result = None
        self._deadline = None
        for p in self.players:
            p.cards = [deck.pop() for _ in range(3)]
            p.status = "active" if p.chips > 0 else "packed"
            p.seen = False
            p.total_bet = 0
            p.blind_rounds = 0
        for p in self.players:
            if p.status == "active":
                self._pay(p, min(self.BOOT, p.chips))
        self.turn = self.dealer
        self._advance_turn()
        self.phase = "betting"
        self._say(f"New hand! Boot {self.BOOT} each. Pot: {self.pot}. Chaal starts at {self.stake}.")
        self.run_bots()

    def _advance_turn(self):
        for _ in range(len(self.players)):
            self.turn = (self.turn + 1) % len(self.players)
            if self.players[self.turn].status == "active":
                return

    def _finish_if_done(self):
        active = self.active_players()
        if len(active) == 1:
            self._settle([active[0]], "everyone else packed")
            return True
        if self.bet_rounds >= self.MAX_BET_ROUNDS and len(active) >= 2:
            best = max(evaluate_hand(p.cards) for p in active)
            winners = [p for p in active if evaluate_hand(p.cards) == best]
            self._settle(winners, "stake limit reached — best hand")
            return True
        return False

    def _settle(self, winners, reason):
        """Split the pot evenly among winners; remainder to the earliest seat."""
        share = self.pot // len(winners)
        remainder = self.pot - share * len(winners)
        for i, w in enumerate(winners):
            w.chips += share + (remainder if i == 0 else 0)
        names = [w.name for w in winners]
        self.winner = " & ".join(names)
        self.phase = "finished"
        hand_names = {p.name: CATEGORY_NAMES[evaluate_hand(p.cards)[0]]
                      for p in self.players if p.cards}
        self.last_result = {
            "winners": names,
            "pot": self.pot,
            "reason": reason,
            "hands": hand_names,
            "ruleset": RULESET_VERSION,
        }
        if len(winners) == 1:
            self._say(f"🏆 {names[0]} wins {self.pot} chips ({reason}) with {hand_names[names[0]]}!")
        else:
            self._say(f"🤝 Tie! {self.winner} split the pot of {self.pot} ({reason}).")

    # ---------- actions ----------
    def legal_actions(self, player):
        if (self.phase != "betting" or self.pending_sideshow
                or self.players[self.turn] is not player or player.status != "active"):
            return []
        acts = ["pack"]
        if not player.seen:
            acts.append("see")
        acts.append("chaal")
        if self.stake < self.MAX_STAKE:
            acts.append("raise")
        if len(self.active_players()) == 2:
            acts.append("show")
        prev = self._previous_active(player)
        if player.seen and prev is not None and prev.seen:
            acts.append("sideshow")
        return acts

    def _previous_active(self, player):
        idx = self.players.index(player)
        for step in range(1, len(self.players)):
            cand = self.players[(idx - step) % len(self.players)]
            if cand.status == "active" and cand is not player:
                return cand
        return None

    def act(self, player, action):
        self._deadline = None
        if action == "see":
            player.seen = True
            self._say(f"{player.name} looks at their cards (seen).")
            return True
        if action == "pack":
            player.status = "packed"
            self._say(f"{player.name} packs.")
        elif action == "chaal":
            paid = self._pay(player, self.cost_for(player))
            self._say(f"{player.name} {'(blind) ' if not player.seen else ''}chaal — pays {paid}. Pot: {self.pot}.")
        elif action == "raise":
            self.stake = min(self.stake * 2, self.MAX_STAKE)
            paid = self._pay(player, self.cost_for(player))
            self._say(f"{player.name} RAISES the stake to {self.stake} — pays {paid}. Pot: {self.pot}.")
        elif action == "show":
            paid = self._pay(player, self.cost_for(player))
            other = [p for p in self.active_players() if p is not player][0]
            self._say(f"{player.name} pays {paid} and calls a SHOW against {other.name}!")
            cmp = compare_hands(player.cards, other.cards)
            if cmp > 0:
                self._settle([player], "show")
            elif cmp < 0:
                self._settle([other], "show")
            else:
                self._settle([player, other], "show — tied hands split the pot")
            return True
        elif action == "sideshow":
            prev = self._previous_active(player)
            if prev is None or not prev.seen:
                return False
            self._pay(player, self.cost_for(player))
            if prev.is_human:
                self.pending_sideshow = {
                    "from_idx": self.players.index(player),
                    "to_idx": self.players.index(prev),
                }
                self._say(f"{player.name} asks {prev.name} for a SIDE SHOW… waiting for an answer.")
                return "pending"
            if not self._bot_sideshow_answer(prev):
                self._say(f"{prev.name} refuses the side show.")
            else:
                cmp = compare_hands(player.cards, prev.cards)
                loser = prev if cmp >= 0 else player  # tie -> requester packs
                loser.status = "packed"
                self._say(f"Side show: {player.name} vs {prev.name} — {loser.name} has the weaker hand and packs.")
        else:
            return False

        if player.status == "active" and not player.seen:
            player.blind_rounds += 1
        if self._finish_if_done():
            return True
        self.bet_rounds += 1
        self._advance_turn()
        self.run_bots()
        return True

    def resolve_sideshow(self, accept):
        pend = self.pending_sideshow
        if not pend:
            return False
        self.pending_sideshow = None
        self._deadline = None
        requester = self.players[pend["from_idx"]]
        target = self.players[pend["to_idx"]]
        if not accept:
            self._say(f"{target.name} refuses the side show.")
        else:
            cmp = compare_hands(requester.cards, target.cards)
            loser = target if cmp >= 0 else requester  # tie -> requester packs
            loser.status = "packed"
            self._say(f"Side show accepted: {requester.name} vs {target.name} — "
                      f"{loser.name} has the weaker hand and packs.")
        if self._finish_if_done():
            return True
        self.turn = pend["from_idx"]
        self.bet_rounds += 1
        self._advance_turn()
        self.run_bots()
        return True

    # ---------- bots ----------
    def _bot_sideshow_answer(self, bot):
        return hand_strength(bot.cards) >= 0.35 or _RNG.random() < 0.25

    def run_bots(self):
        guard = 0
        while (self.phase == "betting" and not self.pending_sideshow
               and self.players[self.turn].status == "active"
               and not self.players[self.turn].is_human and guard < 300):
            guard += 1
            self._bot_move(self.players[self.turn])
            if self.phase != "betting":
                break

    def _bot_move(self, bot):
        strength = hand_strength(bot.cards)
        if not bot.seen and bot.blind_rounds >= _RNG.choice([1, 2]):
            self.act(bot, "see")
            if self.phase != "betting" or self.current_player() is not bot:
                return
        active = self.active_players()
        if len(active) == 2 and (strength > 0.55 or self.pot > 2000) and bot.seen:
            self.act(bot, "show")
            return
        prev = self._previous_active(bot)
        if bot.seen and prev is not None and prev.seen and strength > 0.62 and _RNG.random() < 0.4:
            self.act(bot, "sideshow")
            return
        bluff = _RNG.random() < 0.06
        if strength < 0.22 and bot.seen and not bluff and self.stake >= self.BOOT * 2:
            self.act(bot, "pack")
        elif strength > 0.72 and bot.seen and _RNG.random() < 0.5:
            self.act(bot, "raise")
        else:
            self.act(bot, "chaal")

    # ---------- state ----------
    def state(self, viewer_idx=0):
        self._refresh_deadline()
        reveal = self.phase == "finished"
        viewer = self.players[viewer_idx] if 0 <= viewer_idx < len(self.players) else None
        players_out = []
        for i, p in enumerate(self.players):
            show_cards = reveal or (i == viewer_idx and p.seen)
            players_out.append({
                "name": p.name,
                "chips": p.chips,
                "is_human": p.is_human,
                "is_you": i == viewer_idx,
                "status": p.status,
                "seen": p.seen,
                "total_bet": p.total_bet,
                "cards": p.cards if show_cards else [],
                "hand_name": CATEGORY_NAMES[evaluate_hand(p.cards)[0]] if (reveal and p.cards) else None,
            })
        pend = None
        if self.pending_sideshow and self.pending_sideshow["to_idx"] == viewer_idx:
            pend = {"from": self.players[self.pending_sideshow["from_idx"]].name,
                    "to": self.players[self.pending_sideshow["to_idx"]].name}
        seconds_left = None
        if self._deadline is not None:
            seconds_left = max(0, int(self._deadline - time.time()))
        return {
            "phase": self.phase,
            "pot": self.pot,
            "stake": self.stake,
            "boot": self.BOOT,
            "turn": self.players[self.turn].name if self.phase == "betting" else None,
            "turn_idx": self.turn if self.phase == "betting" else None,
            "seconds_left": seconds_left,
            "winner": self.winner,
            "last_result": self.last_result,
            "log": self.log[-14:],
            "players": players_out,
            "viewer": viewer_idx,
            "your_actions": self.legal_actions(viewer) if viewer else [],
            "pending_sideshow": pend,
            "your_hand": (CATEGORY_NAMES[evaluate_hand(viewer.cards)[0]]
                          if viewer and (viewer.seen or reveal) and viewer.cards else None),
        }
