"""
Teen Patti game engine (pure Python, no dependencies).

Rules implemented (classic Teen Patti):
- 6 players (1 human + 5 bots in the web app, but the engine is generic).
- Boot (ante) from everyone, 3 cards each, betting clockwise.
- Blind players pay half the current stake; seen players pay the full stake.
- Raise doubles the current stake (capped).
- Side show: a seen player may ask the previous seen player to compare;
  the loser packs. The asked player may refuse. On a tie the requester packs.
- Show: only when exactly 2 players remain; better hand wins the pot.
- Last player standing wins the pot.

Hand rankings (high -> low): Trail, Pure Sequence, Sequence, Color, Pair, High Card.
Sequences: A-K-Q > A-2-3 > K-Q-J > ... > 4-3-2.
"""

import random

RANKS = "23456789TJQKA"
SUITS = "shdc"  # spades, hearts, diamonds, clubs
RANK_VALUE = {r: i + 2 for i, r in enumerate(RANKS)}

CATEGORY_NAMES = {
    6: "Trail (Trio)",
    5: "Pure Sequence",
    4: "Sequence",
    3: "Color (Flush)",
    2: "Pair",
    1: "High Card",
}


def new_deck():
    deck = [r + s for r in RANKS for s in SUITS]
    random.shuffle(deck)
    return deck


def evaluate_hand(cards):
    """Return (category, tiebreak_tuple) — bigger tuple = stronger hand."""
    ranks = sorted((RANK_VALUE[c[0]] for c in cards), reverse=True)
    suits = [c[1] for c in cards]
    is_flush = len(set(suits)) == 1

    # sequence check: A-2-3 counts as a sequence (second-highest after A-K-Q)
    seq_high = None
    if len(set(ranks)) == 3:
        if ranks[0] - ranks[2] == 2:
            seq_high = ranks[0]
        elif ranks == [14, 3, 2]:
            seq_high = 13.5  # A-2-3 sits just below A-K-Q (14)

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
    """1 if a wins, -1 if b wins, 0 tie."""
    ea, eb = evaluate_hand(a), evaluate_hand(b)
    if ea > eb:
        return 1
    if ea < eb:
        return -1
    return 0


def hand_strength(cards):
    """Rough 0..1 strength used by bots (category weight + tiebreak + noise)."""
    cat, tb = evaluate_hand(cards)
    base = {6: 0.97, 5: 0.90, 4: 0.78, 3: 0.58, 2: 0.38, 1: 0.12}[cat]
    tb_norm = min(sum(tb) / 42.0, 1.0) * 0.08
    return base + tb_norm


class Player:
    def __init__(self, name, chips, is_human=False):
        self.name = name
        self.chips = chips
        self.is_human = is_human
        self.cards = []
        self.status = "active"  # active | packed
        self.seen = False
        self.total_bet = 0
        self.blind_rounds = 0

    def to_dict(self, reveal=False):
        return {
            "name": self.name,
            "chips": self.chips,
            "is_human": self.is_human,
            "status": self.status,
            "seen": self.seen,
            "total_bet": self.total_bet,
            "cards": self.cards if (reveal or (self.is_human and self.seen)) else [],
            "hand_name": CATEGORY_NAMES[evaluate_hand(self.cards)[0]] if (reveal and self.cards) else None,
        }


class Game:
    BOOT = 10
    MAX_STAKE_MULT = 16          # stake cap = BOOT * 16
    MAX_BET_ROUNDS = 40          # safety valve -> forced showdown

    def __init__(self, player_names, start_chips=1000):
        self.players = [Player(n, start_chips, is_human=(i == 0)) for i, n in enumerate(player_names)]
        self.pot = 0
        self.stake = self.BOOT
        self.turn = 0
        self.dealer = -1
        self.phase = "idle"      # idle | betting | finished
        self.log = []
        self.winner = None
        self.bet_rounds = 0
        self.last_raiser = None
        self.pending_sideshow = None

    # ---------- helpers ----------
    def active_players(self):
        return [p for p in self.players if p.status == "active"]

    def _say(self, msg):
        self.log.append(msg)
        self.log = self.log[-60:]

    def _pay(self, player, amount):
        amount = min(amount, player.chips)
        player.chips -= amount
        player.total_bet += amount
        self.pot += amount
        return amount

    def cost_for(self, player):
        return self.stake // 2 if not player.seen else self.stake

    # ---------- hand lifecycle ----------
    def start_hand(self):
        self.dealer = (self.dealer + 1) % len(self.players)
        deck = new_deck()
        self.pot = 0
        self.stake = self.BOOT
        self.bet_rounds = 0
        self.winner = None
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
        self._advance_turn()  # first player after dealer
        self.phase = "betting"
        self.pending_sideshow = None
        self._say(f"New hand! Everyone pays boot of {self.BOOT}. Pot: {self.pot}.")
        if not self.players[self.turn].is_human:
            self.run_bots()

    def _advance_turn(self):
        for _ in range(len(self.players)):
            self.turn = (self.turn + 1) % len(self.players)
            if self.players[self.turn].status == "active":
                return

    def _finish_if_done(self):
        active = self.active_players()
        if len(active) == 1:
            self._award(active[0], "everyone else packed")
            return True
        if self.bet_rounds >= self.MAX_BET_ROUNDS and len(active) >= 2:
            best = max(active, key=lambda p: evaluate_hand(p.cards))
            self._award(best, "stake limit reached — best hand")
            return True
        return False

    def _award(self, winner, reason):
        winner.chips += self.pot
        self.winner = winner.name
        self.phase = "finished"
        self._say(f"🏆 {winner.name} wins {self.pot} chips ({reason}) with "
                  f"{CATEGORY_NAMES[evaluate_hand(winner.cards)[0]]}!")

    # ---------- actions ----------
    def legal_actions(self, player):
        if self.phase != "betting" or self.players[self.turn] is not player or player.status != "active":
            return []
        acts = ["pack"]
        if not player.seen:
            acts.append("see")
        acts.append("chaal")
        if self.stake < self.BOOT * self.MAX_STAKE_MULT:
            acts.append("raise")
        active = self.active_players()
        if len(active) == 2:
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

    def act(self, player, action, sideshow_answer=None):
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
            self.stake = min(self.stake * 2, self.BOOT * self.MAX_STAKE_MULT)
            paid = self._pay(player, self.cost_for(player))
            self.last_raiser = player.name
            self._say(f"{player.name} RAISES the stake to {self.stake} — pays {paid}. Pot: {self.pot}.")
        elif action == "show":
            paid = self._pay(player, self.cost_for(player))
            other = [p for p in self.active_players() if p is not player][0]
            self._say(f"{player.name} pays {paid} and calls a SHOW against {other.name}!")
            cmp = compare_hands(player.cards, other.cards)
            winner = player if cmp >= 0 else other  # tie -> show caller wins (house rule, stated in UI)
            self._award(winner, "show")
            return True
        elif action == "sideshow":
            prev = self._previous_active(player)
            if prev is None or not prev.seen:
                return False
            self._pay(player, self.cost_for(player))
            if prev.is_human and sideshow_answer is None:
                self.pending_sideshow = {"from": player.name, "to": prev.name}
                self._say(f"{player.name} asks {prev.name} for a SIDE SHOW… waiting for an answer.")
                return "pending"
            accept = sideshow_answer if prev.is_human else self._bot_sideshow_answer(prev, player)
            if not accept:
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
        if not self.players[self.turn].is_human:
            self.run_bots()
        return True

    def resolve_sideshow(self, accept):
        """Human answers a bot's side-show request (payment already taken)."""
        pend = self.pending_sideshow
        if not pend:
            return False
        self.pending_sideshow = None
        requester = next(p for p in self.players if p.name == pend["from"])
        human = self.players[0]
        if not accept:
            self._say(f"{human.name} refuses the side show.")
        else:
            cmp = compare_hands(requester.cards, human.cards)
            loser = human if cmp >= 0 else requester  # tie -> requester packs
            loser.status = "packed"
            self._say(f"Side show accepted: {requester.name} vs {human.name} — "
                      f"{loser.name} has the weaker hand and packs.")
        if self._finish_if_done():
            return True
        # the requester has had their turn; move on
        self.turn = self.players.index(requester)
        self.bet_rounds += 1
        self._advance_turn()
        if self.phase == "betting" and not self.players[self.turn].is_human:
            self.run_bots()
        return True

    # ---------- bots ----------
    def _bot_sideshow_answer(self, bot, requester):
        # accept if bot thinks its hand is at least decent
        return hand_strength(bot.cards) >= 0.35 or random.random() < 0.25

    def run_bots(self):
        guard = 0
        while (self.phase == "betting" and not self.pending_sideshow
               and not self.players[self.turn].is_human
               and self.players[self.turn].status == "active" and guard < 200):
            guard += 1
            bot = self.players[self.turn]
            self._bot_move(bot)
            if self.phase != "betting":
                break

    def _bot_move(self, bot):
        strength = hand_strength(bot.cards)
        active = self.active_players()
        # go seen after a blind round or two
        if not bot.seen and bot.blind_rounds >= random.choice([1, 2]):
            self.act(bot, "see")
            if self.phase != "betting" or self.players[self.turn] is not bot:
                return
        # showdown when only 2 remain and hand is decent, or pot is huge
        if len(active) == 2 and (strength > 0.55 or self.pot > 400) and bot.seen:
            self.act(bot, "show")
            return
        # side show sometimes with a strong-ish seen hand
        prev = self._previous_active(bot)
        if bot.seen and prev is not None and prev.seen and strength > 0.62 and random.random() < 0.4:
            self.act(bot, "sideshow")
            return
        cost = self.cost_for(bot)
        bluff = random.random() < 0.06
        if strength < 0.22 and bot.seen and not bluff and self.stake >= self.BOOT * 2:
            self.act(bot, "pack")
        elif strength > 0.72 and bot.seen and random.random() < 0.5:
            self.act(bot, "raise")
        elif bot.chips <= cost:
            self.act(bot, "chaal")  # all-in chaal
        else:
            self.act(bot, "chaal")

    # ---------- state ----------
    def state(self):
        reveal = self.phase == "finished"
        human = self.players[0]
        return {
            "phase": self.phase,
            "pot": self.pot,
            "stake": self.stake,
            "boot": self.BOOT,
            "turn": self.players[self.turn].name if self.phase == "betting" else None,
            "winner": self.winner,
            "log": self.log[-12:],
            "players": [p.to_dict(reveal=reveal) for p in self.players],
            "your_actions": self.legal_actions(human),
            "pending_sideshow": self.pending_sideshow,
            "your_hand": CATEGORY_NAMES[evaluate_hand(human.cards)[0]] if human.seen or reveal else None,
        }
