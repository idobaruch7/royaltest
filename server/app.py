#!/usr/bin/env python3
from flask import Flask, send_from_directory, request
from flask_socketio import SocketIO, emit
import os
import socket
from sqlalchemy import Boolean, Integer, String, create_engine, delete, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC_DIR = os.path.join(BASE_DIR, 'public')

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('ROYALTEST_SECRET_KEY', 'royaltest-dev-secret')
socketio = SocketIO(app, cors_allowed_origins='*')

# ── State ──────────────────────────────────────────────────────────────────────

session_players = {}      # session_id -> {nickname, chips, sid, is_connected, state}
sid_to_session = {}       # sid -> session_id
current_game = None       # Game instance (active during a game session)
session_to_player = {}    # session_id -> HumanPlayer (during game)
game_active = False
join_queue = []           # [session_id] players waiting to join next hand


class Base(DeclarativeBase):
    pass


class RuntimeState(Base):
    __tablename__ = 'runtime_state'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    current_game: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    game_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text('false'), index=True)


class SessionPlayerState(Base):
    __tablename__ = 'session_players_state'
    session_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    nickname: Mapped[str] = mapped_column(String(20), nullable=False, unique=True, index=True)
    chips: Mapped[int] = mapped_column(Integer, nullable=False)
    sid: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    is_connected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text('false'), index=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, index=True)


class SidSessionState(Base):
    __tablename__ = 'sid_session_state'
    sid: Mapped[str] = mapped_column(String(100), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)


class SessionToPlayerState(Base):
    __tablename__ = 'session_to_player_state'
    session_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    player_data: Mapped[dict] = mapped_column(JSONB, nullable=False)


class JoinQueueState(Base):
    __tablename__ = 'join_queue_state'
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)


DATABASE_URL = os.getenv(
    'ROYALTEST_DATABASE_URL',
    'postgresql+psycopg://postgres@localhost:5432/royaltest',
)
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def _serialize_session_player(player) -> dict:
    return {
        'nickname': player.nickname,
        'chips': player.chips,
        'sid': getattr(player, 'sid', None),
        'is_connected': getattr(player, 'is_connected', True),
        'session_id': getattr(player, 'session_id', None),
    }


def _load_runtime_state():
    global session_players, sid_to_session, current_game, session_to_player, game_active, join_queue
    from game_engine import Game

    with SessionLocal() as db:
        session_players = {
            row.session_id: {
                'session_id': row.session_id,
                'nickname': row.nickname,
                'chips': row.chips,
                'sid': row.sid,
                'is_connected': row.is_connected,
                'state': row.state,
            }
            for row in db.execute(select(SessionPlayerState)).scalars().all()
        }
        sid_to_session = {
            row.sid: row.session_id
            for row in db.execute(select(SidSessionState)).scalars().all()
        }
        join_queue = [
            row.session_id
            for row in db.execute(select(JoinQueueState).order_by(JoinQueueState.position)).scalars().all()
        ]
        runtime = db.get(RuntimeState, 1)
        if runtime and runtime.current_game:
            current_game = Game.from_persisted_dict(runtime.current_game)
            session_to_player = {
                p.session_id: p
                for p in current_game.players
                if hasattr(p, 'session_id')
            }
        else:
            current_game = None
            session_to_player = {}
        game_active = bool(runtime.game_active) if runtime else False


def _persist_runtime_state():
    with SessionLocal.begin() as db:
        runtime = db.get(RuntimeState, 1)
        if runtime is None:
            runtime = RuntimeState(id=1)
            db.add(runtime)

        runtime.game_active = game_active
        runtime.current_game = current_game.to_persisted_dict() if current_game else None

        db.execute(delete(SessionPlayerState))
        db.add_all([
            SessionPlayerState(
                session_id=session_id,
                nickname=info['nickname'],
                chips=info['chips'],
                sid=info.get('sid'),
                is_connected=info.get('is_connected', False),
                state=info.get('state', 'lobby'),
            )
            for session_id, info in session_players.items()
        ])

        db.execute(delete(SidSessionState))
        db.add_all([
            SidSessionState(sid=sid, session_id=session_id)
            for sid, session_id in sid_to_session.items()
        ])

        db.execute(delete(SessionToPlayerState))
        db.add_all([
            SessionToPlayerState(session_id=session_id, player_data=_serialize_session_player(player))
            for session_id, player in session_to_player.items()
        ])

        db.execute(delete(JoinQueueState))
        db.add_all([
            JoinQueueState(position=index, session_id=session_id)
            for index, session_id in enumerate(join_queue)
        ])


def _init_persistence():
    Base.metadata.create_all(engine)
    with SessionLocal.begin() as db:
        if db.get(RuntimeState, 1) is None:
            db.add(RuntimeState(id=1, game_active=False, current_game=None))
    _load_runtime_state()


_init_persistence()


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
    session_id = sid_to_session.pop(_request_sid(), None)
    if not session_id:
        return

    info = session_players.get(session_id)
    if not info:
        return

    info['sid'] = None
    info['is_connected'] = False
    print(f'[disconnect] {info["nickname"]}')

    player = session_to_player.get(session_id)
    if player:
        player.sid = None
        player.is_connected = False

    _persist_runtime_state()
    _broadcast_lobby()
    _broadcast_queue()

    if current_game:
        _process_automatic_turns()
        _broadcast_game_state()


@socketio.on('host_connected')
def on_host_connected():
    emit('lobby_update', _lobby_snapshot())
    emit('queue_update', _queue_snapshot())
    if current_game:
        emit('game_starting', {})
        emit('game_state', current_game.to_dict())


@socketio.on('join_game')
def on_join_game(data):
    nickname = (data.get('nickname') or '').strip()
    session_id = (data.get('session_id') or '').strip()

    if not session_id:
        emit('join_error', {'message': 'Missing browser session. Refresh and try again.'})
        return
    if len(session_id) > 100:
        emit('join_error', {'message': 'Invalid browser session.'})
        return

    existing = session_players.get(session_id)
    if existing:
        _attach_session_to_sid(session_id, _request_sid())
        _sync_player_connection(session_id)
        _persist_runtime_state()
        print(f'[rejoin] {existing["nickname"]}')
        _emit_session_state(session_id)
        _broadcast_lobby()
        _broadcast_queue()
        if current_game:
            _broadcast_game_state()
        return

    if not nickname:
        emit('join_error', {'message': 'Nickname cannot be empty.'})
        return
    if len(nickname) > 20:
        emit('join_error', {'message': 'Nickname must be 20 characters or less.'})
        return
    if any(p['nickname'] == nickname for p in session_players.values()):
        emit('join_error', {'message': f'"{nickname}" is already taken. Choose another.'})
        return

    session_players[session_id] = {
        'session_id': session_id,
        'nickname': nickname,
        'chips': 1000,
        'sid': None,
        'is_connected': False,
        'state': 'lobby',
    }
    _attach_session_to_sid(session_id, _request_sid())

    if game_active:
        session_players[session_id]['state'] = 'queued'
        join_queue.append(session_id)
        _persist_runtime_state()
        print(f'[queued] {nickname}')
        emit('join_queued', {'nickname': nickname, 'chips': 1000, 'position': len(join_queue)})
        _broadcast_queue()
        return

    _persist_runtime_state()
    print(f'[join] {nickname}')
    emit('join_success', {'nickname': nickname, 'chips': 1000})
    _broadcast_lobby()


@socketio.on('start_game')
def on_start_game():
    global current_game, session_to_player, game_active

    players_list = _connected_lobby_players()
    if len(players_list) < 2:
        emit('start_error', {'message': 'Need at least 2 players to start.'})
        return

    from game_engine import Game
    from bot_player import HumanPlayer

    players = []
    session_to_player = {}
    for session_id, info in session_players.items():
        if info['state'] != 'lobby' or not info['is_connected']:
            continue
        info['state'] = 'game'
        p = HumanPlayer(info['nickname'], session_id, info['sid'], info['chips'])
        p.is_connected = info['is_connected']
        players.append(p)
        session_to_player[session_id] = p

    current_game = Game(players)
    game_active = True
    current_game.start_hand()
    _persist_runtime_state()

    print(f'[start_game] {len(players)} players')
    socketio.emit('game_starting', {})
    _broadcast_lobby()
    _broadcast_game_state()
    _send_private_hands()
    _process_automatic_turns()


# ── Socket.IO — Game ──────────────────────────────────────────────────────────

@socketio.on('player_action')
def on_player_action(data):
    if not current_game or not game_active:
        return
    action = data.get('action', '')
    amount = int(data.get('amount', 0))
    _apply_and_advance(_request_sid(), action, amount)


@socketio.on('next_hand')
def on_next_hand():
    global current_game, game_active

    if not current_game:
        return

    _flush_queue()

    current_game.next_hand()
    _sync_all_game_player_chips()
    _persist_runtime_state()
    _broadcast_game_state()
    _send_private_hands()
    _process_automatic_turns()


# ── Game helpers ──────────────────────────────────────────────────────────────

def _flush_queue():
    """Promote queued players into the active game before the next hand."""
    global join_queue
    if not join_queue or not current_game:
        return

    from bot_player import HumanPlayer

    remaining_queue = []
    for session_id in join_queue:
        info = session_players.get(session_id)
        if not info:
            continue
        if not info['is_connected']:
            remaining_queue.append(session_id)
            continue
        info['state'] = 'game'
        p = HumanPlayer(info['nickname'], session_id, info['sid'], info['chips'])
        p.is_connected = info['is_connected']
        current_game.players.append(p)
        session_to_player[session_id] = p
        if info['sid']:
            socketio.emit('game_starting', {}, to=info['sid'])
        print(f'[queue->game] {info["nickname"]}')

    join_queue = remaining_queue
    _persist_runtime_state()
    _broadcast_queue()
    _broadcast_lobby()


def _apply_and_advance(sid, action: str, amount: int):
    """Apply one action and handle all follow-up (bots, auto-folds, street transitions)."""
    if not current_game:
        return

    _, event = current_game.apply_action(sid, action, amount)
    _sync_all_game_player_chips()
    _persist_runtime_state()
    _broadcast_game_state()

    if event == 'invalid_action':
        session_id = sid_to_session.get(sid)
        info = session_players.get(session_id) if session_id else None
        if info and info.get('sid'):
            socketio.emit('action_error', {'message': current_game.last_action_error or 'Illegal action.'}, to=info['sid'])
        _notify_current_player()
        return

    if event == 'game_over':
        _broadcast_hand_over()
    elif event in ('continue', 'street_end'):
        _process_automatic_turns()


def _process_automatic_turns():
    """Run bot turns until a human player needs to act."""
    from bot_player import BotPlayer, HumanPlayer

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
            _sync_all_game_player_chips()
            _persist_runtime_state()
            _broadcast_game_state()

            if event == 'game_over':
                _broadcast_hand_over()
                return
            continue

        _notify_current_player()
        return


def _broadcast_game_state():
    if not current_game:
        return
    socketio.emit('game_state', current_game.to_dict())


def _send_private_hands():
    """Send each connected human player their hole cards privately."""
    if not current_game:
        return
    for player in session_to_player.values():
        _send_private_hand(player)


def _send_private_hand(player):
    if not player.sid or not player.hand:
        return
    socketio.emit('your_hand', {
        'hand': [c.to_dict() for c in player.hand]
    }, to=player.sid)


def _notify_current_player():
    """Emit 'your_turn' to whoever needs to act next."""
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


def _broadcast_hand_over():
    if not current_game:
        return

    _sync_all_game_player_chips()
    winners = current_game.get_winners()
    socketio.emit('hand_over', {
        'winners': [p.nickname for p in winners],
        'winner_hands': current_game.winner_hand_names(),
        'winner_details': current_game.winner_hand_details(),
        'pot_results': current_game.get_pot_results(),
        'game_state': current_game.to_dict(),
    })


# ── Lobby helpers ─────────────────────────────────────────────────────────────

def _lobby_players():
    return [info for info in session_players.values() if info['state'] == 'lobby']


def _connected_lobby_players():
    return [info for info in _lobby_players() if info['is_connected']]


def _lobby_snapshot():
    return [
        {
            'nickname': info['nickname'],
            'chips': info['chips'],
            'is_connected': info['is_connected'],
        }
        for info in _lobby_players()
    ]


def _broadcast_lobby():
    socketio.emit('lobby_update', _lobby_snapshot())


def _queue_snapshot():
    out = []
    for session_id in join_queue:
        info = session_players.get(session_id)
        if not info:
            continue
        out.append({
            'nickname': info['nickname'],
            'chips': info['chips'],
            'is_connected': info['is_connected'],
        })
    return out


def _broadcast_queue():
    socketio.emit('queue_update', _queue_snapshot())


def _attach_session_to_sid(session_id: str, sid: str):
    info = session_players[session_id]
    old_sid = info.get('sid')
    if old_sid and old_sid != sid:
        sid_to_session.pop(old_sid, None)

    sid_to_session[sid] = session_id
    info['sid'] = sid
    info['is_connected'] = True


def _sync_player_connection(session_id: str):
    player = session_to_player.get(session_id)
    info = session_players.get(session_id)
    if player and info:
        player.sid = info['sid']
        player.is_connected = info['is_connected']


def _sync_all_game_player_chips():
    for session_id, player in session_to_player.items():
        info = session_players.get(session_id)
        if info:
            info['chips'] = player.chips


def _emit_session_state(session_id: str):
    info = session_players.get(session_id)
    if not info or not info.get('sid'):
        return

    payload = {
        'nickname': info['nickname'],
        'chips': info['chips'],
        'reconnected': True,
    }

    if info['state'] == 'queued':
        emit('join_queued', {
            **payload,
            'position': _queue_position(session_id),
        })
    else:
        emit('join_success', payload)

    if current_game and session_id in session_to_player:
        player = session_to_player[session_id]
        emit('game_starting', {})
        emit('game_state', current_game.to_dict(for_sid=player.sid))
        _send_private_hand(player)
        if current_game.current_player() is player:
            _notify_current_player()


def _queue_position(session_id: str) -> int:
    try:
        return join_queue.index(session_id) + 1
    except ValueError:
        return 0


def _end_game_session():
    global current_game, session_to_player, game_active, join_queue

    game_active = False
    current_game = None
    session_to_player = {}
    join_queue = []

    for info in session_players.values():
        info['state'] = 'lobby'

    _persist_runtime_state()
    _broadcast_queue()
    _broadcast_lobby()


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
    print(f'  Player URL: http://{local_ip}:{port}/join')
    print()
    socketio.run(app, host=bind_host, port=port, debug=debug)
