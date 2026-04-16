#!/usr/bin/env python3
from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import os
import socket
import string
import secrets

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC_DIR = os.path.join(BASE_DIR, 'public')

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('ROYALTEST_SECRET_KEY', 'royaltest-dev-secret')
socketio = SocketIO(app, cors_allowed_origins='*')

# ── State ──────────────────────────────────────────────────────────────────────

GAME_CODE_LEN = 6
VALID_GAME_CODE_CHARS = string.ascii_uppercase + string.digits

games = {}             # game_code -> game state dict
sid_to_game = {}       # sid -> game_code


def _new_game_state(game_code: str):
    return {
        'game_code': game_code,
        'host_sids': set(),
        'session_players': {},      # session_id -> {nickname, chips, sid, is_connected, state}
        'sid_to_session': {},       # sid -> session_id
        'current_game': None,       # Game instance (active during a game session)
        'session_to_player': {},    # session_id -> HumanPlayer (during game)
        'game_active': False,
        'join_queue': [],           # [session_id] players waiting to join next hand
    }


def _get_or_create_game(game_code: str):
    game = games.get(game_code)
    if game is None:
        game = _new_game_state(game_code)
        games[game_code] = game
    return game


def _game_room(game_code: str) -> str:
    return f'game:{game_code}'


def _normalize_game_code(raw: str) -> str:
    code = ''.join(ch for ch in (raw or '').upper() if ch in VALID_GAME_CODE_CHARS)
    return code[:GAME_CODE_LEN]


def _generate_game_code() -> str:
    max_attempts = 2048
    for _ in range(max_attempts):
        code = ''.join(secrets.choice(VALID_GAME_CODE_CHARS) for _ in range(GAME_CODE_LEN))
        if code not in games:
            return code
    raise RuntimeError('Unable to allocate a unique game code.')


def _lookup_game_for_sid(sid: str):
    game_code = sid_to_game.get(sid)
    if not game_code:
        return None, None
    return game_code, games.get(game_code)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory(PUBLIC_DIR, 'index.html')


@app.route('/host')
def host():
    return send_from_directory(os.path.join(PUBLIC_DIR, 'host'), 'index.html')


@app.route('/join')
def join():
    return send_from_directory(os.path.join(PUBLIC_DIR, 'player'), 'index.html')


@app.route('/public/<path:filename>')
def public_files(filename):
    return send_from_directory(PUBLIC_DIR, filename)


# ── Socket.IO — Lobby ─────────────────────────────────────────────────────────

@socketio.on('connect')
def on_connect():
    print(f'[connect] {_request_sid()}')


@socketio.on('disconnect')
def on_disconnect():
    sid = _request_sid()
    game_code = sid_to_game.pop(sid, None)
    if not game_code:
        return

    game = games.get(game_code)
    if not game:
        return

    if sid in game['host_sids']:
        game['host_sids'].discard(sid)
        leave_room(_game_room(game_code))
        print(f'[host_disconnect] {game_code}')

    session_id = game['sid_to_session'].pop(sid, None)
    if not session_id:
        return

    info = game['session_players'].get(session_id)
    if not info:
        return

    info['sid'] = None
    info['is_connected'] = False
    print(f'[disconnect] {game_code} {info["nickname"]}')

    player = game['session_to_player'].get(session_id)
    if player:
        player.sid = None
        player.is_connected = False

    _broadcast_lobby(game_code, game)
    _broadcast_queue(game_code, game)

    if game['current_game']:
        if _finish_game_if_too_few_connected(game_code, game):
            return
        _process_automatic_turns(game_code, game)
        _broadcast_game_state(game_code, game)


@socketio.on('host_connected')
def on_host_connected(data=None):
    requested_code = _normalize_game_code((data or {}).get('game_code', ''))
    try:
        game_code = requested_code or _generate_game_code()
    except RuntimeError:
        emit('host_error', {'message': 'Unable to create a new table right now. Please try again.'})
        return
    game = _get_or_create_game(game_code)

    sid = _request_sid()
    sid_to_game[sid] = game_code
    game['host_sids'].add(sid)
    join_room(_game_room(game_code))

    emit('host_ready', {
        'game_code': game_code,
        'join_url': f'{request.host_url.rstrip("/")}/join?code={game_code}',
    })
    emit('lobby_update', _lobby_snapshot(game))
    emit('queue_update', _queue_snapshot(game))
    if game['current_game']:
        emit('game_starting', {})
        emit('game_state', game['current_game'].to_dict())


@socketio.on('join_game')
def on_join_game(data):
    nickname = (data.get('nickname') or '').strip()
    session_id = (data.get('session_id') or '').strip()
    game_code = _normalize_game_code(data.get('game_code') or '')

    if not game_code:
        emit('join_error', {'message': 'Enter a table code.'})
        return

    game = games.get(game_code)
    if not game:
        emit('join_error', {'message': 'Table code not found. Check code and try again.'})
        return

    if not session_id:
        emit('join_error', {'message': 'Missing browser session. Refresh and try again.'})
        return
    if len(session_id) > 100:
        emit('join_error', {'message': 'Invalid browser session.'})
        return

    sid = _request_sid()
    sid_to_game[sid] = game_code
    join_room(_game_room(game_code))

    existing = game['session_players'].get(session_id)
    if existing:
        _attach_session_to_sid(game_code, game, session_id, sid)
        _sync_player_connection(game, session_id)
        print(f'[rejoin] {game_code} {existing["nickname"]}')
        _emit_session_state(game_code, game, session_id)
        _broadcast_lobby(game_code, game)
        _broadcast_queue(game_code, game)
        if game['current_game']:
            _broadcast_game_state(game_code, game)
        return

    if not nickname:
        emit('join_error', {'message': 'Nickname cannot be empty.'})
        return
    if len(nickname) > 20:
        emit('join_error', {'message': 'Nickname must be 20 characters or less.'})
        return
    if any(p['nickname'] == nickname for p in game['session_players'].values()):
        emit('join_error', {'message': f'"{nickname}" is already taken. Choose another.'})
        return

    game['session_players'][session_id] = {
        'session_id': session_id,
        'nickname': nickname,
        'chips': 1000,
        'sid': None,
        'is_connected': False,
        'state': 'lobby',
    }
    _attach_session_to_sid(game_code, game, session_id, sid)

    if game['game_active']:
        game['session_players'][session_id]['state'] = 'queued'
        game['join_queue'].append(session_id)
        print(f'[queued] {game_code} {nickname}')
        emit('join_queued', {'nickname': nickname, 'chips': 1000, 'position': len(game['join_queue']), 'game_code': game_code})
        _broadcast_queue(game_code, game)
        return

    print(f'[join] {game_code} {nickname}')
    emit('join_success', {'nickname': nickname, 'chips': 1000, 'game_code': game_code})
    _broadcast_lobby(game_code, game)


@socketio.on('start_game')
def on_start_game():
    sid = _request_sid()
    game_code, game = _lookup_game_for_sid(sid)
    if not game_code or not game:
        emit('start_error', {'message': 'No active table.'})
        return
    if sid not in game['host_sids']:
        emit('start_error', {'message': 'Only the host can start the game.'})
        return

    players_list = _connected_lobby_players(game)
    if len(players_list) < 2:
        emit('start_error', {'message': 'Need at least 2 players to start.'})
        return

    from game_engine import Game
    from bot_player import HumanPlayer

    players = []
    game['session_to_player'] = {}
    for session_id, info in game['session_players'].items():
        if info['state'] != 'lobby' or not info['is_connected']:
            continue
        info['state'] = 'game'
        p = HumanPlayer(info['nickname'], session_id, info['sid'], info['chips'])
        p.is_connected = info['is_connected']
        players.append(p)
        game['session_to_player'][session_id] = p

    game['current_game'] = Game(players)
    game['game_active'] = True
    game['current_game'].start_hand()

    print(f'[start_game] {game_code} {len(players)} players')
    socketio.emit('game_starting', {}, to=_game_room(game_code))
    _broadcast_lobby(game_code, game)
    _broadcast_game_state(game_code, game)
    _send_private_hands(game)
    _process_automatic_turns(game_code, game)


# ── Socket.IO — Game ──────────────────────────────────────────────────────────

@socketio.on('player_action')
def on_player_action(data):
    sid = _request_sid()
    game_code, game = _lookup_game_for_sid(sid)
    if not game_code or not game or not game['current_game'] or not game['game_active']:
        return
    action = data.get('action', '')
    amount = int(data.get('amount', 0))
    _apply_and_advance(game_code, game, sid, action, amount)


@socketio.on('next_hand')
def on_next_hand():
    sid = _request_sid()
    game_code, game = _lookup_game_for_sid(sid)
    if not game_code or not game:
        return

    current_game = game['current_game']
    if not current_game:
        return

    _flush_queue(game_code, game)

    if _finish_game_if_too_few_connected(game_code, game):
        return

    current_game.next_hand()
    _sync_all_game_player_chips(game)
    _broadcast_game_state(game_code, game)
    _send_private_hands(game)
    _process_automatic_turns(game_code, game)


# ── Game helpers ──────────────────────────────────────────────────────────────

def _flush_queue(game_code: str, game):
    """Promote queued players into the active game before the next hand."""
    current_game = game['current_game']
    if not game['join_queue'] or not current_game:
        return

    from bot_player import HumanPlayer

    remaining_queue = []
    for session_id in game['join_queue']:
        info = game['session_players'].get(session_id)
        if not info:
            continue
        if not info['is_connected']:
            remaining_queue.append(session_id)
            continue
        info['state'] = 'game'
        p = HumanPlayer(info['nickname'], session_id, info['sid'], info['chips'])
        p.is_connected = info['is_connected']
        current_game.players.append(p)
        game['session_to_player'][session_id] = p
        if info['sid']:
            socketio.emit('game_starting', {}, to=info['sid'])
        print(f'[queue->game] {game_code} {info["nickname"]}')

    game['join_queue'] = remaining_queue
    _broadcast_queue(game_code, game)
    _broadcast_lobby(game_code, game)


def _apply_and_advance(game_code: str, game, sid, action: str, amount: int):
    """Apply one action and handle all follow-up (bots, auto-folds, street transitions)."""
    current_game = game['current_game']
    if not current_game:
        return

    _, event = current_game.apply_action(sid, action, amount)
    _sync_all_game_player_chips(game)
    _broadcast_game_state(game_code, game)

    if event == 'invalid_action':
        session_id = game['sid_to_session'].get(sid)
        info = game['session_players'].get(session_id) if session_id else None
        if info and info.get('sid'):
            socketio.emit('action_error', {'message': current_game.last_action_error or 'Illegal action.'}, to=info['sid'])
        _notify_current_player(game)
        return

    if event == 'game_over':
        _broadcast_hand_over(game_code, game)
    elif event in ('continue', 'street_end'):
        _process_automatic_turns(game_code, game)


def _process_automatic_turns(game_code: str, game):
    """Run bot turns and disconnected human turns until a connected human needs to act."""
    from bot_player import BotPlayer, HumanPlayer

    current_game = game['current_game']
    while current_game and current_game.state.value not in ('waiting', 'showdown'):
        player = current_game.current_player()
        if player is None:
            return

        if isinstance(player, BotPlayer):
            game_state_for_bot = {
                'community_cards_objects': current_game.community_cards,
                'pot': current_game.pot,
                'big_blind': current_game.big_blind,
                **current_game.legal_actions_for(player),
            }
            action_dict = player.get_action(game_state_for_bot)
            action = action_dict.get('action', 'fold')
            amount = action_dict.get('amount', 0)

            _, event = current_game.apply_action(None, action, amount)
            _sync_all_game_player_chips(game)
            _broadcast_game_state(game_code, game)

            if event == 'game_over':
                _broadcast_hand_over(game_code, game)
                return
            continue

        if isinstance(player, HumanPlayer) and not player.is_connected:
            call_amount = current_game.current_bet - getattr(player, 'round_bet', 0)
            action = 'check' if call_amount <= 0 else 'fold'
            print(f'[auto_{action}] {game_code} {player.nickname}')
            _, event = current_game.apply_action(None, action, 0)
            _sync_all_game_player_chips(game)
            _broadcast_game_state(game_code, game)

            if event == 'game_over':
                _broadcast_hand_over(game_code, game)
                return
            continue

        _notify_current_player(game)
        return


def _broadcast_game_state(game_code: str, game):
    current_game = game['current_game']
    if not current_game:
        return
    socketio.emit('game_state', current_game.to_dict(), to=_game_room(game_code))


def _send_private_hands(game):
    """Send each connected human player their hole cards privately."""
    if not game['current_game']:
        return
    for player in game['session_to_player'].values():
        _send_private_hand(player)


def _send_private_hand(player):
    if not player.sid or not player.hand:
        return
    socketio.emit('your_hand', {
        'hand': [c.to_dict() for c in player.hand]
    }, to=player.sid)


def _notify_current_player(game):
    """Emit 'your_turn' to whoever needs to act next."""
    current_game = game['current_game']
    if not current_game:
        return
    player = current_game.current_player()
    if player is None or not hasattr(player, 'sid') or player.sid is None:
        return
    socketio.emit('your_turn', {
        **current_game.legal_actions_for(player),
        'big_blind': current_game.big_blind,
        'pot': current_game.pot,
    }, to=player.sid)


def _broadcast_hand_over(game_code: str, game):
    current_game = game['current_game']
    if not current_game:
        return

    _sync_all_game_player_chips(game)
    winners = current_game.get_winners()
    socketio.emit('hand_over', {
        'winners': [p.nickname for p in winners],
        'winner_hands': current_game.winner_hand_names(),
        'winner_details': current_game.winner_hand_details(),
        'pot_results': current_game.get_pot_results(),
        'game_state': current_game.to_dict(),
    }, to=_game_room(game_code))


# ── Lobby helpers ─────────────────────────────────────────────────────────────

def _lobby_players(game):
    return [info for info in game['session_players'].values() if info['state'] == 'lobby']


def _connected_lobby_players(game):
    return [info for info in _lobby_players(game) if info['is_connected']]


def _connected_game_players(game):
    current_game = game['current_game']
    if not current_game:
        return []
    return [
        player for player in current_game.players
        if getattr(player, 'is_connected', True) and player.chips > 0
    ]


def _finish_game_if_too_few_connected(game_code: str, game) -> bool:
    connected_players = _connected_game_players(game)
    if len(connected_players) >= 2:
        return False

    socketio.emit('game_finished', {
        'winner': connected_players[0].nickname if len(connected_players) == 1 else None
    }, to=_game_room(game_code))
    _end_game_session(game_code, game)
    return True


def _lobby_snapshot(game):
    return [
        {
            'nickname': info['nickname'],
            'chips': info['chips'],
            'is_connected': info['is_connected'],
        }
        for info in _lobby_players(game)
    ]


def _broadcast_lobby(game_code: str, game):
    socketio.emit('lobby_update', _lobby_snapshot(game), to=_game_room(game_code))


def _queue_snapshot(game):
    out = []
    for session_id in game['join_queue']:
        info = game['session_players'].get(session_id)
        if not info:
            continue
        out.append({
            'nickname': info['nickname'],
            'chips': info['chips'],
            'is_connected': info['is_connected'],
        })
    return out


def _broadcast_queue(game_code: str, game):
    socketio.emit('queue_update', _queue_snapshot(game), to=_game_room(game_code))


def _attach_session_to_sid(game_code: str, game, session_id: str, sid: str):
    info = game['session_players'][session_id]
    old_sid = info.get('sid')
    if old_sid and old_sid != sid:
        game['sid_to_session'].pop(old_sid, None)

    sid_to_game[sid] = game_code
    game['sid_to_session'][sid] = session_id
    info['sid'] = sid
    info['is_connected'] = True


def _sync_player_connection(game, session_id: str):
    player = game['session_to_player'].get(session_id)
    info = game['session_players'].get(session_id)
    if player and info:
        player.sid = info['sid']
        player.is_connected = info['is_connected']


def _sync_all_game_player_chips(game):
    for session_id, player in game['session_to_player'].items():
        info = game['session_players'].get(session_id)
        if info:
            info['chips'] = player.chips


def _emit_session_state(game_code: str, game, session_id: str):
    info = game['session_players'].get(session_id)
    if not info or not info.get('sid'):
        return

    payload = {
        'nickname': info['nickname'],
        'chips': info['chips'],
        'reconnected': True,
        'game_code': game_code,
    }

    if info['state'] == 'queued':
        emit('join_queued', {
            **payload,
            'position': _queue_position(game, session_id),
        })
    else:
        emit('join_success', payload)

    current_game = game['current_game']
    if current_game and session_id in game['session_to_player']:
        player = game['session_to_player'][session_id]
        emit('game_starting', {})
        emit('game_state', current_game.to_dict(for_sid=player.sid))
        _send_private_hand(player)
        if current_game.current_player() is player:
            _notify_current_player(game)


def _queue_position(game, session_id: str) -> int:
    try:
        return game['join_queue'].index(session_id) + 1
    except ValueError:
        return 0


def _end_game_session(game_code: str, game):
    game['game_active'] = False
    game['current_game'] = None
    game['session_to_player'] = {}
    game['join_queue'] = []

    for info in game['session_players'].values():
        info['state'] = 'lobby'

    _broadcast_queue(game_code, game)
    _broadcast_lobby(game_code, game)


def _get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def _request_sid() -> str:
    return str(getattr(request, 'sid', ''))


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    bind_host = os.getenv('ROYALTEST_HOST', '0.0.0.0')
    port = int(os.getenv('ROYALTEST_PORT', '5000'))
    debug = os.getenv('ROYALTEST_DEBUG', '0').lower() in {'1', 'true', 'yes', 'on'}
    local_ip = _get_local_ip()
    print()
    print(f'  Host page : http://localhost:{port}/host')
    print(f'  Player URL: http://{local_ip}:{port}/join?code=ABC123')
    print()
    socketio.run(app, host=bind_host, port=port, debug=debug)
