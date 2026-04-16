"""
game_engine.py — Phase 2
Core poker logic: deck, hand evaluation, full game loop.
"""
import random
from enum import Enum
from itertools import combinations
from collections import Counter


SUITS = ['♠', '♥', '♦', '♣']
RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
RANK_VALUES = {
    '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7, '8': 8,
    '9': 9, '10': 10, 'J': 11, 'Q': 12, 'K': 13, 'A': 14
}


class Card:
    def __init__(self, rank: str, suit: str):
        self.rank = rank
        self.suit = suit
        self.value = RANK_VALUES[rank]

    def __repr__(self):
        return f'{self.rank}{self.suit}'

    def to_dict(self):
        return {'rank': self.rank, 'suit': self.suit}


class Deck:
    def __init__(self):
        self.cards = [Card(r, s) for s in SUITS for r in RANKS]
        random.shuffle(self.cards)

    def deal(self, n: int = 1) -> list:
        if len(self.cards) < n:
            raise ValueError('Not enough cards in deck')
        dealt = self.cards[:n]
        self.cards = self.cards[n:]
        return dealt


class GameState(Enum):
    WAITING   = 'waiting'
    PRE_FLOP  = 'pre_flop'
    FLOP      = 'flop'
    TURN      = 'turn'
    RIVER     = 'river'
    SHOWDOWN  = 'showdown'


# ── Hand Evaluation ──────────────────────────────────────────────────────────

def _eval_five(cards) -> tuple:
    """Evaluate exactly 5 cards. Returns a comparable tuple (higher = better)."""
    vals = sorted([c.value for c in cards], reverse=True)
    suits = [c.suit for c in cards]
    is_flush = len(set(suits)) == 1

    is_straight = (vals == list(range(vals[0], vals[0] - 5, -1)))
    # Wheel: A-2-3-4-5
    if not is_straight and set(vals) == {14, 2, 3, 4, 5}:
        is_straight = True
        vals = [5, 4, 3, 2, 1]

    cnt = Counter(vals)
    groups = sorted(cnt.items(), key=lambda x: (x[1], x[0]), reverse=True)
    freq = [g[1] for g in groups]
    rank_vals = [g[0] for g in groups]

    if is_flush and is_straight:
        return (8, vals[0])
    if freq[0] == 4:
        return (7, rank_vals[0], rank_vals[1])
    if freq[:2] == [3, 2]:
        return (6, rank_vals[0], rank_vals[1])
    if is_flush:
        return (5, *vals)
    if is_straight:
        return (4, vals[0])
    if freq[0] == 3:
        kickers = sorted([g[0] for g in groups[1:]], reverse=True)
        return (3, rank_vals[0], *kickers)
    if freq[:2] == [2, 2]:
        pairs = sorted([rank_vals[0], rank_vals[1]], reverse=True)
        return (2, *pairs, rank_vals[2])
    if freq[0] == 2:
        kickers = sorted([g[0] for g in groups[1:]], reverse=True)
        return (1, rank_vals[0], *kickers)
    return (0, *vals)


def best_hand_value(cards: list) -> tuple:
    """Best 5-card hand value from 5-7 cards."""
    if len(cards) < 5:
        return (0,)
    return max(_eval_five(combo) for combo in combinations(cards, 5))


def best_hand_cards(cards: list) -> list:
    """Returns the 5 Card objects that form the best hand from 5-7 cards."""
    if len(cards) < 5:
        return list(cards)
    return list(max(combinations(cards, 5), key=_eval_five))


HAND_NAMES = {
    8: 'Straight Flush', 7: 'Four of a Kind', 6: 'Full House',
    5: 'Flush', 4: 'Straight', 3: 'Three of a Kind',
    2: 'Two Pair', 1: 'Pair', 0: 'High Card'
}


# ── Game ─────────────────────────────────────────────────────────────────────

class Game:
    """Manages a full game session (multiple hands of Texas Hold'em)."""

    def __init__(self, players: list, small_blind: int = 10, big_blind: int = 20):
        self.players = players       # list of Player objects (duck-typed)
        self.small_blind = small_blind
        self.big_blind = big_blind
        self.dealer_index = 0

        # Hand state
        self.deck = None
        self.community_cards = []
        self.pot = 0
        self.state = GameState.WAITING
        self.current_bet = 0
        self.min_raise = big_blind
        self.to_act = []             # indices of players yet to act this round
        self.raise_reopened_for = set()
        self._winners = []
        self._pot_results = []
        self.last_action_error = ''

        # Position markers (set in start_hand)
        self.small_blind_index = 0
        self.big_blind_index = 1

    # ── Hand lifecycle ───────────────────────────────────────────────────────

    def start_hand(self):
        """Shuffle, deal hole cards, post blinds. Returns first player to act."""
        self.deck = Deck()
        self.community_cards = []
        self.pot = 0
        self.state = GameState.PRE_FLOP
        self._winners = []
        self._pot_results = []
        self.last_action_error = ''
        self.current_bet = 0
        self.min_raise = self.big_blind

        for p in self.players:
            p.hand = self.deck.deal(2)
            p.bet = 0
            p.round_bet = 0
            p.folded = False

        n = len(self.players)
        if n == 2:
            # Heads-up: dealer = SB, other = BB
            self.small_blind_index = self.dealer_index
            self.big_blind_index = (self.dealer_index + 1) % n
        else:
            self.small_blind_index = (self.dealer_index + 1) % n
            self.big_blind_index = (self.dealer_index + 2) % n

        self._post_blind(self.small_blind_index, self.small_blind)
        self._post_blind(self.big_blind_index, self.big_blind)
        self.current_bet = max(
            self.players[self.small_blind_index].round_bet,
            self.players[self.big_blind_index].round_bet,
        )

        # Pre-flop: action starts left of BB
        start = (self.big_blind_index + 1) % n
        self.to_act = self._action_order_from(start)
        self.raise_reopened_for = set(self.to_act)
        return self._current_player()

    def next_hand(self):
        """Remove busted players, advance dealer button, start new hand."""
        self.players = [p for p in self.players if p.chips > 0]
        self.dealer_index = (self.dealer_index + 1) % len(self.players)
        return self.start_hand()

    def get_call_amount(self, player) -> int:
        return max(0, self.current_bet - getattr(player, 'round_bet', 0))

    def get_max_total(self, player) -> int:
        return getattr(player, 'round_bet', 0) + player.chips

    def get_min_raise_total(self, player) -> int:
        if player.chips <= 0:
            return getattr(player, 'round_bet', 0)
        if self.current_bet == 0:
            return max(self.big_blind, getattr(player, 'round_bet', 0) + self.big_blind)
        return self.current_bet + self.min_raise

    def can_check(self, player) -> bool:
        return self.get_call_amount(player) == 0

    def can_call(self, player) -> bool:
        return self.get_call_amount(player) > 0 and player.chips > 0

    def can_full_raise(self, player) -> bool:
        idx = self.players.index(player)
        return idx in self.raise_reopened_for and self.get_max_total(player) >= self.get_min_raise_total(player)

    def can_short_all_in(self, player) -> bool:
        idx = self.players.index(player)
        max_total = self.get_max_total(player)
        return (
            idx in self.raise_reopened_for
            and max_total > self.current_bet
            and max_total < self.get_min_raise_total(player)
        )

    def legal_actions_for(self, player) -> dict:
        call_amount = self.get_call_amount(player)
        max_total = self.get_max_total(player)
        min_raise_total = self.get_min_raise_total(player)
        can_raise = self.can_full_raise(player)
        can_short_all_in = self.can_short_all_in(player)
        if can_raise:
            aggressive_action = 'raise'
        elif can_short_all_in:
            aggressive_action = 'all_in'
        else:
            aggressive_action = 'none'
        return {
            'call_amount': call_amount,
            'current_bet': self.current_bet,
            'min_raise': self.min_raise,
            'min_raise_total': min_raise_total,
            'max_total': max_total,
            'can_check': self.can_check(player),
            'can_call': self.can_call(player),
            'can_raise': can_raise,
            'can_short_all_in': can_short_all_in,
            'aggressive_action': aggressive_action,
        }

    def classify_raise(self, player, amount: int) -> tuple[str, int]:
        if player.chips <= 0:
            return 'invalid', getattr(player, 'round_bet', 0)

        amount = min(amount, self.get_max_total(player))
        if amount <= self.current_bet:
            return 'invalid', amount

        min_raise_total = self.get_min_raise_total(player)
        if self.can_full_raise(player) and amount >= min_raise_total:
            return 'full_raise', amount
        if self.can_short_all_in(player) and amount == self.get_max_total(player):
            return 'short_all_in', amount
        return 'invalid', amount

    # ── Action processing ────────────────────────────────────────────────────

    def apply_action(self, sid, action: str, amount: int = 0) -> tuple:
        """
        Process one player action. sid=None bypasses the turn check (for bots).
        Returns (next_player_or_None, event_str).
        event_str: 'continue' | 'street_end' | 'game_over' | 'not_your_turn' | 'invalid_action'
        """
        player = self._current_player()
        if player is None:
            return None, 'error'

        self.last_action_error = ''

        # sid=None is used for bot/auto actions
        if sid is not None and hasattr(player, 'sid') and player.sid != sid:
            return None, 'not_your_turn'

        action = action.lower()
        player_idx = self.to_act[0]

        if action == 'fold':
            player.folded = True
            self._remove_current_actor(player_idx)

        elif action == 'check':
            if not self.can_check(player):
                self.last_action_error = 'Cannot check while facing a bet.'
                return None, 'invalid_action'
            self._remove_current_actor(player_idx)

        elif action == 'call':
            call_amount = self.get_call_amount(player)
            if call_amount <= 0:
                self._remove_current_actor(player_idx)
            else:
                actual = min(call_amount, player.chips)
                self._commit_bet(player, actual)
                self._remove_current_actor(player_idx)

        elif action == 'raise':
            classification, amount = self.classify_raise(player, amount)
            if classification == 'invalid':
                self.last_action_error = 'Illegal raise amount.'
                return None, 'invalid_action'

            previous_bet = self.current_bet
            cost = amount - player.round_bet
            self._commit_bet(player, cost)
            self.current_bet = player.round_bet

            if classification == 'full_raise':
                self.min_raise = amount - previous_bet
                self._reopen_action_from(player_idx)
            else:
                self._remove_current_actor(player_idx)

        else:
            self.last_action_error = 'Unknown action.'
            return None, 'invalid_action'

        # Only one player remaining?
        alive = [p for p in self.players if not p.folded]
        if len(alive) == 1:
            self._award_pots(alive)
            self.state = GameState.SHOWDOWN
            return None, 'game_over'

        # Betting round over?
        if not self.to_act:
            return self._advance_street()

        return self._current_player(), 'continue'

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _post_blind(self, idx: int, amount: int):
        p = self.players[idx]
        actual = min(amount, p.chips)
        self._commit_bet(p, actual)

    def _commit_bet(self, player, amount: int):
        if amount <= 0:
            return
        player.chips -= amount
        player.round_bet += amount
        player.bet += amount
        self.pot += amount

    def _action_order_from(self, start_idx: int) -> list:
        """Indices of players who can still act (not folded, not all-in), from start_idx."""
        n = len(self.players)
        order = []
        for i in range(n):
            idx = (start_idx + i) % n
            p = self.players[idx]
            if not p.folded and p.chips > 0:
                order.append(idx)
        return order

    def _current_player(self):
        return self.players[self.to_act[0]] if self.to_act else None

    def _remove_current_actor(self, player_idx: int):
        if self.to_act and self.to_act[0] == player_idx:
            self.to_act.pop(0)
        else:
            self.to_act = [idx for idx in self.to_act if idx != player_idx]
        self.raise_reopened_for.discard(player_idx)

    def _reopen_action_from(self, player_idx: int):
        self.to_act = [
            idx for idx in self._action_order_from((player_idx + 1) % len(self.players))
            if idx != player_idx
        ]
        self.raise_reopened_for = set(self.to_act)

    def _advance_street(self) -> tuple:
        transitions = {
            GameState.PRE_FLOP: (GameState.FLOP,  3),
            GameState.FLOP:     (GameState.TURN,   1),
            GameState.TURN:     (GameState.RIVER,  1),
        }

        while True:
            for p in self.players:
                p.round_bet = 0
            self.current_bet = 0
            self.min_raise = self.big_blind

            if self.state not in transitions:
                return self._resolve_showdown()

            next_state, n_cards = transitions[self.state]
            self.state = next_state
            deck = self.deck
            if deck is None:
                raise ValueError('Cannot advance street without an active deck')
            self.community_cards.extend(deck.deal(n_cards))
            start = (self.dealer_index + 1) % len(self.players)
            self.to_act = self._action_order_from(start)
            self.raise_reopened_for = set(self.to_act)

            if len(self.to_act) >= 2:
                return self._current_player(), 'street_end'
            # Everyone is all-in — deal next street automatically

    def _resolve_showdown(self) -> tuple:
        self.state = GameState.SHOWDOWN
        alive = [p for p in self.players if not p.folded]
        if len(alive) == 1:
            self._award_pots(alive)
            return None, 'game_over'

        self._award_pots()
        return None, 'game_over'

    def _build_pots(self) -> list:
        contribution_levels = sorted({p.bet for p in self.players if p.bet > 0})
        if not contribution_levels:
            return []

        pots = []
        previous_level = 0
        for level in contribution_levels:
            contributors = [idx for idx, player in enumerate(self.players) if player.bet >= level]
            layer = level - previous_level
            amount = layer * len(contributors)
            if amount <= 0:
                previous_level = level
                continue

            eligible = [
                idx for idx in contributors
                if not self.players[idx].folded
            ]
            pots.append({
                'amount': amount,
                'eligible': eligible,
                'contributors': contributors,
            })
            previous_level = level
        return pots

    def _best_winner_indices(self, eligible_indices: list[int]) -> list[int]:
        best = {
            idx: best_hand_value(self.players[idx].hand + self.community_cards)
            for idx in eligible_indices
        }
        max_val = max(best.values())
        return [idx for idx in eligible_indices if best[idx] == max_val]

    def _award_pots(self, forced_winners: list | None = None):
        self._winners = []
        self._pot_results = []

        pots = self._build_pots()
        overall_winner_indices = []
        forced_indices = None
        if forced_winners is not None:
            forced_indices = {self.players.index(player) for player in forced_winners}

        for pot in pots:
            eligible = pot['eligible']
            if not eligible:
                continue

            if forced_indices is not None:
                winner_indices = [idx for idx in eligible if idx in forced_indices]
                if not winner_indices:
                    winner_indices = eligible[:1]
            elif len(eligible) == 1:
                winner_indices = eligible[:1]
            else:
                winner_indices = self._best_winner_indices(eligible)

            share = pot['amount'] // len(winner_indices)
            remainder = pot['amount'] % len(winner_indices)
            for idx in winner_indices:
                self.players[idx].chips += share
            self.players[min(winner_indices)].chips += remainder

            for idx in winner_indices:
                if idx not in overall_winner_indices:
                    overall_winner_indices.append(idx)

            self._pot_results.append({
                'amount': pot['amount'],
                'winners': [self.players[idx].nickname for idx in winner_indices],
                'eligible': [self.players[idx].nickname for idx in eligible],
            })

        self.pot = 0
        self._winners = [self.players[idx] for idx in overall_winner_indices]

    # ── State serialization ──────────────────────────────────────────────────

    def current_player(self):
        return self._current_player()

    def get_winners(self) -> list:
        return self._winners

    def get_pot_results(self) -> list:
        return self._pot_results

    def winner_hand_names(self) -> dict:
        if self.state != GameState.SHOWDOWN or sum(1 for p in self.players if not p.folded) <= 1:
            return {}
        result = {}
        for p in self._winners:
            if p.hand and self.community_cards:
                val = best_hand_value(p.hand + self.community_cards)
                result[p.nickname] = HAND_NAMES.get(val[0], 'High Card')
            elif p.hand:
                result[p.nickname] = 'Unknown'
        return result

    def winner_hand_details(self) -> dict:
        """For each winner: hand name + the 5 cards that make the best hand."""
        if self.state != GameState.SHOWDOWN or sum(1 for p in self.players if not p.folded) <= 1:
            return {}
        result = {}
        for p in self._winners:
            all_cards = (p.hand or []) + self.community_cards
            if len(all_cards) >= 5:
                best_5 = best_hand_cards(all_cards)
                hand_name = HAND_NAMES.get(_eval_five(best_5)[0], 'High Card')
            else:
                best_5 = all_cards
                hand_name = 'Unknown'
            result[p.nickname] = {
                'hand_name': hand_name,
                'cards': [c.to_dict() for c in best_5],
            }
        return result

    def to_dict(self, for_sid=None) -> dict:
        """Serialize game state. Hide opponents' hole cards except at showdown."""
        current = self._current_player()
        players_out = []
        showdown_players_count = (
            sum(1 for p in self.players if not p.folded) if self.state == GameState.SHOWDOWN else 0
        )

        for i, p in enumerate(self.players):
            entry = {
                'nickname': p.nickname,
                'chips': p.chips,
                'bet': p.bet,
                'round_bet': getattr(p, 'round_bet', 0),
                'folded': p.folded,
                'all_in': p.chips == 0 and not p.folded,
                'is_connected': getattr(p, 'is_connected', True),
                'is_dealer': i == self.dealer_index,
                'is_sb': i == self.small_blind_index,
                'is_bb': i == self.big_blind_index,
                'is_current': current is not None and p is current,
            }

            show_hand = (
                self.state == GameState.SHOWDOWN and showdown_players_count > 1 and not p.folded
            ) or (
                for_sid is not None and hasattr(p, 'sid') and p.sid == for_sid
            )
            if show_hand:
                entry['hand'] = [c.to_dict() for c in p.hand] if p.hand else []
                if p.hand and self.community_cards and self.state == GameState.SHOWDOWN:
                    val = best_hand_value(p.hand + self.community_cards)
                    entry['hand_name'] = HAND_NAMES.get(val[0], 'High Card')

            players_out.append(entry)

        out = {
            'state': self.state.value,
            'community_cards': [c.to_dict() for c in self.community_cards],
            'pot': self.pot,
            'pots': [
                {
                    'amount': pot['amount'],
                    'eligible_count': len(pot['eligible']),
                }
                for pot in self._build_pots()
            ],
            'current_bet': self.current_bet,
            'min_raise': self.min_raise,
            'players': players_out,
            'current_player': current.nickname if current else None,
            'dealer': self.players[self.dealer_index].nickname,
        }
        if self._winners:
            out['winners'] = [p.nickname for p in self._winners]
            out['winner_hands'] = self.winner_hand_names()
        return out
