from server.game_engine import Card, Game, GameState
from server.bot_player import Player


class StubPlayer(Player):
    def __init__(self, nickname: str, chips: int = 1000):
        super().__init__(nickname, chips)
        self.sid = nickname
        self.is_connected = True


def make_game(chips):
    players = [StubPlayer(f'P{i + 1}', stack) for i, stack in enumerate(chips)]
    game = Game(players, small_blind=10, big_blind=20)
    game.start_hand()
    return game, players


def deal_showdown(game, hands, board):
    game.community_cards = [Card(rank, suit) for rank, suit in board]
    for player, cards in zip(game.players, hands):
        player.hand = [Card(rank, suit) for rank, suit in cards]
    game.state = GameState.RIVER


def test_short_all_in_does_not_reopen_betting():
    game, players = make_game([500, 60, 500])

    assert game.current_player() is players[0]
    _, event = game.apply_action(players[0].sid, 'raise', 50)
    assert event == 'continue'
    assert game.current_bet == 50
    assert game.min_raise == 30

    assert game.current_player() is players[1]
    _, event = game.apply_action(players[1].sid, 'raise', 60)
    assert event == 'continue'
    assert game.current_bet == 60
    assert game.min_raise == 30

    legal = game.legal_actions_for(players[2])
    assert legal['can_raise']
    assert legal['min_raise_total'] == 90
    assert game.can_full_raise(players[0]) is False

    _, event = game.apply_action(players[2].sid, 'call', 0)
    assert event == 'street_end'


def test_invalid_partial_raise_is_rejected():
    game, players = make_game([500, 500, 500])

    _, event = game.apply_action(players[0].sid, 'raise', 50)
    assert event == 'continue'

    _, event = game.apply_action(players[1].sid, 'raise', 70)
    assert event == 'invalid_action'
    assert game.last_action_error == 'Illegal raise amount.'
    assert game.current_player() is players[1]
    assert game.current_bet == 50


def test_side_pot_distribution_at_showdown():
    players = [StubPlayer('P1', 0), StubPlayer('P2', 0), StubPlayer('P3', 0)]
    game = Game(players, small_blind=10, big_blind=20)
    game.pot = 750
    game.state = GameState.RIVER
    players[0].bet = 120
    players[1].bet = 250
    players[2].bet = 380

    deal_showdown(
        game,
        hands=[
            [('A', '♠'), ('A', '♥')],
            [('K', '♠'), ('K', '♥')],
            [('Q', '♠'), ('Q', '♥')],
        ],
        board=[('2', '♣'), ('7', '♦'), ('9', '♠'), ('3', '♣'), ('4', '♦')],
    )

    _, event = game._resolve_showdown()
    assert event == 'game_over'

    assert players[0].chips == 360
    assert players[1].chips == 260
    assert players[2].chips == 130

    pot_results = game.get_pot_results()
    assert [pot['amount'] for pot in pot_results] == [360, 260, 130]
    assert pot_results[0]['winners'] == ['P1']
    assert pot_results[1]['winners'] == ['P2']
    assert pot_results[2]['winners'] == ['P3']
