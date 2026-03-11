import sys
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT_DIR / 'server'
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from server import app as app_module


class GameStub:
    def __init__(self, players=None):
        self.players = players or []


def reset_app_state():
    app_module.session_players.clear()
    app_module.sid_to_session.clear()
    app_module.session_to_player.clear()
    app_module.join_queue.clear()
    app_module.current_game = None
    app_module.game_active = False


def test_finish_game_if_too_few_connected_picks_last_connected_winner():
    reset_app_state()

    class Player:
        def __init__(self, nickname, chips, is_connected):
            self.nickname = nickname
            self.chips = chips
            self.is_connected = is_connected

    app_module.current_game = GameStub([
        Player('Alice', 1000, True),
        Player('Bob', 1000, False),
    ])
    app_module.game_active = True

    with patch.object(app_module.socketio, 'emit') as emit_mock, patch.object(app_module, '_end_game_session') as end_mock:
        ended = app_module._finish_game_if_too_few_connected()

    assert ended is True
    emit_mock.assert_called_once_with('game_finished', {'winner': 'Alice'})
    end_mock.assert_called_once()


def test_finish_game_if_too_few_connected_keeps_game_with_two_connected():
    reset_app_state()

    class Player:
        def __init__(self, nickname, chips, is_connected):
            self.nickname = nickname
            self.chips = chips
            self.is_connected = is_connected

    app_module.current_game = GameStub([
        Player('Alice', 1000, True),
        Player('Bob', 1000, True),
    ])
    app_module.game_active = True

    with patch.object(app_module.socketio, 'emit') as emit_mock, patch.object(app_module, '_end_game_session') as end_mock:
        ended = app_module._finish_game_if_too_few_connected()

    assert ended is False
    emit_mock.assert_not_called()
    end_mock.assert_not_called()


def test_flush_queue_skips_disconnected_players():
    reset_app_state()

    app_module.session_players['connected'] = {
        'session_id': 'connected',
        'nickname': 'Alice',
        'chips': 1000,
        'sid': 'sid-alice',
        'is_connected': True,
        'state': 'queued',
    }
    app_module.session_players['offline'] = {
        'session_id': 'offline',
        'nickname': 'Bob',
        'chips': 1000,
        'sid': None,
        'is_connected': False,
        'state': 'queued',
    }
    app_module.join_queue[:] = ['connected', 'offline']
    app_module.current_game = GameStub([])

    with patch.object(app_module.socketio, 'emit') as emit_mock, patch.object(app_module, '_broadcast_queue'), patch.object(app_module, '_broadcast_lobby'):
        app_module._flush_queue()

    assert [player.nickname for player in app_module.current_game.players] == ['Alice']
    assert 'connected' in app_module.session_to_player
    assert app_module.join_queue == ['offline']
    assert app_module.session_players['connected']['state'] == 'game'
    assert app_module.session_players['offline']['state'] == 'queued'
    emit_mock.assert_called_once_with('game_starting', {}, to='sid-alice')
