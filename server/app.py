#!/usr/bin/env python3
from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO, emit
import os
import socket

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC_DIR = os.path.join(BASE_DIR, 'public')

app = Flask(__name__)
app.config['SECRET_KEY'] = 'royaltest-dev-secret'
socketio = SocketIO(app, cors_allowed_origins='*')

# ── State ──────────────────────────────────────────────────────────────────────

connected_players = {}   # sid -> {nickname, chips}
current_game = None      # Game instance (active during a game session)
sid_to_player = {}       # sid -> HumanPlayer (during game)
game_active = False
join_queue = []          # [{sid, nickname, chips}] players waiting to join next hand


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return '<h2>RoyalTest</h2><a href="/host">Host Game</a> | <a href="/join">Join Game</a>'

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
    print(f'[connect] {request.sid}')

@socketio.on('disconnect')
def on_disconnect():
    global join_queue
    player = connected_players.pop(request.sid, None)
    prev_len = len(join_queue)
    join_queue = [p for p in join_queue if p['sid'] != request.sid]
    if len(join_queue) != prev_len:
        _broadcast_queue()
    if player:
        print(f'[disconnect] {player["nickname"]}')
        emit('player_left', {'nickname': player['nickname']}, broadcast=True)
        _broadcast_lobby()

    # If a game is running and this player was in it, auto-fold on their turn
    if current_game and request.sid in sid_to_player:
        p = sid_to_player.pop(request.sid, None)
        if p and not p.folded and current_game.current_player() is p:
            _apply_and_advance(request.sid, 'fold', 0)

@socketio.on('host_connected')
def on_host_connected():
    emit('lobby_update', _lobby_snapshot())

@socketio.on('join_game')
def on_join_game(data):
    nickname = (data.get('nickname') or '').strip()
    if not nickname:
        emit('join_error', {'message': 'Nickname cannot be empty.'})
        return
    if len(nickname) > 20:
        emit('join_error', {'message': 'Nickname must be 20 characters or less.'})
        return
    if (any(p['nickname'] == nickname for p in connected_players.values()) or
            any(p['nickname'] == nickname for p in join_queue)):
        emit('join_error', {'message': f'"{nickname}" is already taken. Choose another.'})
        return

    if game_active:
        join_queue.append({'sid': request.sid, 'nickname': nickname, 'chips': 1000})
        print(f'[queued] {nickname}')
        emit('join_queued', {'nickname': nickname, 'chips': 1000, 'position': len(join_queue)})
        _broadcast_queue()
        return

    connected_players[request.sid] = {'nickname': nickname, 'chips': 1000}
    print(f'[join] {nickname}')
    emit('join_success', {'nickname': nickname, 'chips': 1000})
    _broadcast_lobby()

@socketio.on('start_game')
def on_start_game():
    global current_game, sid_to_player, game_active

    players_list = _lobby_snapshot()
    if len(players_list) < 2:
        emit('start_error', {'message': 'Need at least 2 players to start.'})
        return

    from game_engine import Game
    from bot_player import HumanPlayer

    players = []
    sid_to_player = {}
    for sid, info in connected_players.items():
        p = HumanPlayer(info['nickname'], sid, info['chips'])
        players.append(p)
        sid_to_player[sid] = p

    current_game = Game(players)
    game_active = True
    current_game.start_hand()

    print(f'[start_game] {len(players)} players')
    socketio.emit('game_starting', {})
    _broadcast_game_state()
    _send_private_hands()
    _notify_current_player()
    _process_bot_actions()


# ── Socket.IO — Game ──────────────────────────────────────────────────────────

@socketio.on('player_action')
def on_player_action(data):
    if not current_game or not game_active:
        return
    action = data.get('action', '')
    amount  = int(data.get('amount', 0))
    _apply_and_advance(request.sid, action, amount)

@socketio.on('next_hand')
def on_next_hand():
    global current_game, game_active

    if not current_game:
        return

    _flush_queue()

    alive = [p for p in current_game.players if p.chips > 0]
    if len(alive) < 2:
        socketio.emit('game_finished', {
            'winner': alive[0].nickname if alive else None
        })
        game_active = False
        return

    current_game.next_hand()
    _broadcast_game_state()
    _send_private_hands()
    _notify_current_player()
    _process_bot_actions()


# ── Game helpers ──────────────────────────────────────────────────────────────

def _flush_queue():
    """Promote queued players into the active game before the next hand."""
    global join_queue
    if not join_queue or not current_game:
        return
    from bot_player import HumanPlayer
    for entry in join_queue:
        connected_players[entry['sid']] = {'nickname': entry['nickname'], 'chips': entry['chips']}
        p = HumanPlayer(entry['nickname'], entry['sid'], entry['chips'])
        current_game.players.append(p)
        sid_to_player[entry['sid']] = p
        socketio.emit('game_starting', {}, to=entry['sid'])
        print(f'[queue->game] {entry["nickname"]}')
    join_queue = []
    _broadcast_queue()

def _apply_and_advance(sid, action: str, amount: int):
    """Apply one action and handle all follow-up (bots, street transitions, game over)."""
    next_player, event = current_game.apply_action(sid, action, amount)
    _broadcast_game_state()

    if event == 'game_over':
        _broadcast_hand_over()
    elif event in ('continue', 'street_end'):
        _notify_current_player()
        _process_bot_actions()

def _process_bot_actions():
    """Run all consecutive bot turns until a human needs to act (or game ends)."""
    from bot_player import BotPlayer

    while current_game and current_game.state.value not in ('waiting', 'showdown'):
        player = current_game.current_player()
        if player is None or not isinstance(player, BotPlayer):
            break

        call_amount = current_game.current_bet - getattr(player, 'round_bet', 0)
        game_state_for_bot = {
            'community_cards_objects': current_game.community_cards,
            'call_amount': call_amount,
            'pot': current_game.pot,
            'big_blind': current_game.big_blind,
            'current_bet': current_game.current_bet,
            'min_raise': current_game.min_raise,
        }
        action_dict = player.get_action(game_state_for_bot)
        action = action_dict.get('action', 'fold')
        amount = action_dict.get('amount', 0)

        _, event = current_game.apply_action(None, action, amount)
        _broadcast_game_state()

        if event == 'game_over':
            _broadcast_hand_over()
            return
        elif event in ('continue', 'street_end'):
            _notify_current_player()

def _broadcast_game_state():
    if not current_game:
        return
    socketio.emit('game_state', current_game.to_dict())

def _send_private_hands():
    """Send each human player their hole cards privately."""
    if not current_game:
        return
    for sid, player in sid_to_player.items():
        if player.hand:
            socketio.emit('your_hand', {
                'hand': [c.to_dict() for c in player.hand]
            }, to=sid)

def _notify_current_player():
    """Emit 'your_turn' to whoever needs to act next."""
    if not current_game:
        return
    player = current_game.current_player()
    if player is None or not hasattr(player, 'sid') or player.sid is None:
        return
    call_amount = current_game.current_bet - getattr(player, 'round_bet', 0)
    socketio.emit('your_turn', {
        'call_amount': call_amount,
        'min_raise': current_game.min_raise,
        'pot': current_game.pot,
        'current_bet': current_game.current_bet,
    }, to=player.sid)

def _broadcast_hand_over():
    winners = current_game.get_winners()
    socketio.emit('hand_over', {
        'winners': [p.nickname for p in winners],
        'winner_hands': current_game.winner_hand_names(),
        'winner_details': current_game.winner_hand_details(),
        'game_state': current_game.to_dict(),
    })


# ── Lobby helpers ─────────────────────────────────────────────────────────────

def _lobby_snapshot():
    return list(connected_players.values())

def _broadcast_lobby():
    socketio.emit('lobby_update', _lobby_snapshot())

def _broadcast_queue():
    socketio.emit('queue_update', [{'nickname': p['nickname'], 'chips': p['chips']} for p in join_queue])

def _get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    local_ip = _get_local_ip()
    print()
    print(f'  Host page : http://localhost:5000/host')
    print(f'  Player URL: http://{local_ip}:5000/join')
    print()
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)
