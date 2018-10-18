import asyncio
import base64
import hashlib
import os
import random
import subprocess
from asyncio import sleep
from collections import defaultdict

import aiohttp_jinja2
import aiomysql
import jinja2
import socketio
from aiohttp import web
from aiohttp_session import setup, get_session
from aiohttp_session.cookie_storage import EncryptedCookieStorage
from cryptography import fernet

import mail

pool = None
verify_map = {}
downinfo = {'can_play': True, 'message': ""}

class Http_handler:
    @aiohttp_jinja2.template('index.html')
    async def index(self, request):
        session = await get_session(request)
        if 'sid' in session:
            return {'sid': session['sid']}
        else:
            return aiohttp_jinja2.render_template('login.html', request, {})
    
    @aiohttp_jinja2.template('login.html')
    async def logout(self, request):
        session = await get_session(request)
        if 'sid' in session:
            del session['sid']
        return {}
        
    @aiohttp_jinja2.template('login.html')
    async def login(self, request):
        if request._method == "GET":
            return {}
            
        data = await request.post()
        session = await get_session(request)
        if data['pwd'] == str(hashlib.md5('123'.encode()).hexdigest()):
            return {'error': "Password too weak, please reset it."}

        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT * FROM users where sid='{}'".format(data['sid']))
                row = await cursor.fetchone()
        if row and row[1] != data['pwd']:
            return {'error': "Password wrong"}
        elif not row:
            return {'error': "Student Id not exist"}
        # elif not row:
        #     await db.execute("insert into users values({}, '{}', 0, 0, 0)".format(data['sid'], data['pwd']))
        #     await db.commit()
        else:
            session['sid'] = data['sid']
            return aiohttp_jinja2.render_template('index.html', request, {'sid': data['sid']})

    async def send_email (self, request):
        data = await request.post()
        sid = data['sid']
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT * FROM users where sid='{}'".format(sid))
                row = await cursor.fetchone()
        if not row:
            return web.Response(text="Error: StudentId not exist")
        verify_code = str(random.randint(100000, 1000000))
        verify_map[sid] = verify_code
        mail.send_verify_code(sid, verify_code)
        return web.Response(text="ok, please check your student email.")
    
    @aiohttp_jinja2.template('resetpwd.html')
    async def resetpwd(self, request):
        if request._method == "GET":
            return {}
        data = await request.post()
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT * FROM users where sid='{}'".format(data['sid']))
                row = await cursor.fetchone()
                if not row:
                    return {'error': "Error: StudentId not exist."}
                if data['sid'] in verify_map and data['verify_code'] == verify_map[data['sid']]:
                    await cursor.execute("UPDATE users set password='{}' where sid='{}'".format(data['newpwd'], data['sid']))
                    del verify_map[data['sid']]
                else:
                    return {'error': "Error: Wrong verify code."}
                raise web.HTTPFound('/login')
    
    @aiohttp_jinja2.template('index.html')
    async def upload(self, request):
        session = await get_session(request)
        if 'sid' not in session:
            raise web.HTTPFound('/login')
        sid = session['sid']
        reader = await request.multipart()
        field = await reader.next()
        assert field.name == 'code'
        size = 0
        if not os.path.exists("user_code/"):
            os.mkdir("user_code/")
        if not os.path.exists("tem_code/"):
            os.mkdir("tem_code/")
        with open(os.path.join('tem_code/{}.py'.format(sid)), 'wb') as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                f.write(chunk)
                if size > 10 * 1024 ** 2:
                    return {'sid': sid, 'error': "Your code can not excess 1M."}

        #test code
        subprocess.Popen("python code_check_test.py tem_code {}".format(sid), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=True)
        raise web.HTTPFound('/')

        

    @aiohttp_jinja2.template('full_rank.html')
    async def full_rank (self, request):
        return {'rank': rank_info}
    
sio = socketio.AsyncServer()
app = web.Application()
sio.attach(app)
aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader('template'))

handler = Http_handler()
app.router.add_static('/static', 'template')
app.add_routes([web.get('/', handler.index, name='index'),
                web.get('/login', handler.login, name='login'),
                web.post('/login', handler.login, name='login'),
                web.get('/logout', handler.logout, name='logout'),
                web.get('/full_rank', handler.full_rank, name='full_rank'),
                web.post('/upload', handler.upload, name='upload'),
                web.post('/resetpwd', handler.resetpwd, name='reset'),
                web.get('/resetpwd', handler.resetpwd, name='reset'),
                web.post('/send_email', handler.send_email, name='send_email')])
fernet_key = fernet.Fernet.generate_key()
secret_key = base64.urlsafe_b64decode(fernet_key)
setup(app, EncryptedCookieStorage(secret_key))

rank_info = []
score_info = {}
max_game_id = 0
games = defaultdict(dict)
watching_room = set()
players = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))

def score(row):
    sid = row[0]
    score_info = row[1]*10-row[2]*10
    return {'score': score_info, 'rand': random.random(), 'sid': sid}

def find_rank(sid):
    idx = -1
    for i, info in enumerate(rank_info):
        if sid == info['sid']:
            idx = i
            break
    return idx

async def add_game_log (white, black):
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("insert into game_log(white_sid, black_sid, start_time, end_time, winner, loser) "
                                 "values(%s, %s, current_timestamp, null, 0, 0)" % (white, black))
            return cursor.lastrowid

async def update_game_log (game_id, winner, loser):
    # print("update_game_log", game_id, winner, loser)
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "update game_log set winner=%s, loser=%s, end_time=current_timestamp where id=%d" % (winner, loser, game_id))
    # print("update_game_log success")

async def update_chess_log (game_id):
    # print("update_chess_log", games[game_id]['chess_log'])
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.executemany("insert into chess_log values(%d,%d,%d,%d)" % (tuple(games[game_id]['chess_log'])))
    # print("update_chess_log success")


async def push_game (player, tag, soid=None):
    if soid or (player + str(tag) in watching_room):
        await sio.emit('push_game', games[players[player][tag]['id']], room=soid if soid else player + str(tag))


@sio.on('connect')
async def connect(soid, environ):
    print("connect ", soid)

@sio.on('message')
async def message (soid, msg):
    print(soid, "msg ", msg)


@sio.on('upload_test')
async def upload_test (soid, data):
    sid, info, is_pass = str(data['sid']), data['info'], int(data['is_pass'])
    print(data)
    if is_pass:
        subprocess.Popen('mv tem_code/{}.py user_code/{}.py'.format(sid, sid), shell=True)
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT update_times FROM users where sid='{}' and submit_time is not null".format(sid))
                row = await cursor.fetchone()
                if row:
                    await cursor.execute("update users set last_update=current_timestamp, update_times={} where sid='{}'".format(int(row[0]) + 1, sid))
                else:
                    await cursor.execute("update users set submit_time=current_timestamp, last_update=current_timestamp where sid='{}'".format(sid))
                    global score_info
                    score_info[sid]['score'] = -10
        await update_all_list()
    await sio.emit('error', {'type': 3, 'info': info}, room=sid + str(1))
    await sio.emit('error', {'type': 3, 'info': info}, room=sid + str(-1))

@sio.on('watch')
async def watch (soid, data):
    player = str(data['player'])
    tag = int(data['tag'])
    new_room = player + str(tag)
    while len(sio.rooms(soid)) > 1:
        old_room = sio.rooms(soid)[1]
        sio.leave_room(soid, old_room)
        if old_room in watching_room:
            watching_room.remove(old_room)
    sio.enter_room(soid, new_room)
    watching_room.add(new_room)
    await push_game(player, tag)

@sio.on('self_play')
async def self_play(soid, data):
    if not downinfo['can_play']:
        await sio.emit('error', {'type': 3, 'info': downinfo['message']}, soid)
        return
    player = str(data['player'])
    AI = str(data['AI'])
    tag = int(data['tag'])
    print(player, 'self_play', AI)
    if players[player][tag]['status'] and players[player][tag]['id'] >= 10 ** 10:
        await error_finish(soid, {'player': player, 'tag': tag, 'new_game': 1})

    idx = find_rank(AI)
    if rank_info[idx]['score'] == -20:
        await sio.emit('error', {'type': 3, 'info': "Please upload your code again."}, soid)
        return
    if tag > 0:
        await self_begin(player, tag, AI, 'human-' + player)
    else:
        await self_begin(player, tag, 'human-' + player, AI)


async def self_begin (player, tag, white, black):
    old_game_id = players[player][tag]['id']
    if old_game_id in games:
        del games[old_game_id]

    game_id = 10 ** 10
    while game_id in games:
        game_id = random.randint(10 ** 10, 2 * 10 ** 10)
    players[player][tag]['id'] = game_id
    players[player][tag]['status'] = 1
    games[game_id] = {'white': white, 'black': black, "chess_log": [], 'game_id': game_id, "type": 2}
    await push_game(player, tag)
    print(white, black, player, tag)
    subprocess.Popen('python god.py user_code {} {} {} {} {} {}'.format(white, black, 15, 1, player, tag), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=True)
    
@sio.on('self_register')
async def self_register (soid, data):
    player = str(data[0])
    tag = int(data[1])
    print('register', player, tag, soid)
    if players[player][tag]['status']:
        game_id = players[player][tag]['id']
        games[game_id]['god'] = soid
        if player + str(tag) in watching_room:
            await sio.emit('register', 0, room=player + str(tag))

@sio.on('self_go')
async def self_go (soid, data):  # data[player1, tag, x, y, color]
    player = str(data[0])
    tag = int(data[1])
    if players[player][tag]['status']:
        # print(data)
        game_id = players[player][tag]['id']
        games[game_id]['chess_log'].append((game_id, data[2], data[3], data[4]))
        god = games[game_id]['god']
        await sio.emit('self_go', data[2:], god)
        if player + str(tag) in watching_room:
            await sio.emit('go', data[2:], room=player + str(tag))

@sio.on('self_finish')
async def self_finish (soid, data):  # data[player, tag, winner, loser]
    player = str(data[0])
    tag = int(data[1])
    if players[player][tag]['status']:
        game_id = players[player][tag]['id']
        games[game_id]['winner'] = data[2]
        players[player][tag]['status'] = 0
        if player + str(tag) in watching_room:
            await sio.emit('finish', {'winner': data[2], 'game_id': game_id}, room=player + str(tag))
        
@sio.on('play')
async def play (soid, data):
    if not downinfo['can_play']:
        await sio.emit('error', {'type': 3, 'info': downinfo['message']}, soid)
        return
    player = str(data['player'])
    tag = int(data['tag'])
    print(player, "play", tag)
    if players[player][tag]['status'] and players[player][tag]['id'] < 10 ** 10 or players[player][-tag]['status'] and players[player][-tag]['id'] < 10 ** 10:
        await sio.emit('error', {'type': 1, 'info': 'Another color is not finished yet.'}, soid)
        return
    else:
        await error_finish(soid, {'player': player, 'tag': tag, 'new_game': 1})
        await error_finish(soid, {'player': player, 'tag': -tag, 'new_game': 1})

    idx = find_rank(player)
    if rank_info[idx]['score'] == -20:
        await sio.emit('error', {'type': 3, 'info': "You have not uploaded user_code."}, soid)
        return
    elif idx == 0:
        player2 = player
    else:
        player2 = rank_info[idx - 1]['sid']
    player1 = player
    await begin(player, -tag, player1, player2)
    await begin(player, tag, player2, player1)
    # await begin(player2, player1)


async def begin (player, tag, white, black):
    old_game_id = players[player][tag]['id']
    if old_game_id in games:
        del games[old_game_id]
        
    game_id = await add_game_log(white, black)
    print("begin", white, black, game_id)
    players[player][tag]['id'] = game_id
    players[player][tag]['status'] = 1
    games[game_id] = {'white': white, 'black': black, "chess_log": [], 'game_id': game_id, "type": 1}
    await push_game(player, tag)
    subprocess.Popen('python god.py user_code {} {} {} {} {} {}'.format(white, black, 15, 1, player, tag), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=True)

@sio.on('go')
async def go (soid, data):  # data[player, tag, x, y, color]
    player = str(data[0])
    tag = int(data[1])
    if players[player][tag]['status']:
        game_id = players[player][tag]['id']
        games[game_id]['chess_log'].append((game_id, data[2], data[3], data[4]))
        if player + str(tag) in watching_room:
            await sio.emit('go', data[2:], room=player + str(tag))
        
@sio.on('finish')
async def finish (soid, data):  # data[player, tag, winner, loser]
    player = str(data[0])
    tag = int(data[1])
    if players[player][tag]['status']:
        game_id = players[player][tag]['id']
        await update_game_log(game_id, data[2], data[3])
        if 'god' in games[game_id]:
            await sio.emit('finish', 0, games[game_id]['god'])
        games[game_id]['winner'] = data[2]
        players[player][tag]['status'] = 0
        print('finish', {'player': player, 'winner': data[2], 'game_id': game_id})
        await sio.emit('finish', {'winner': data[2], 'game_id': game_id}, room=player + str(tag))
        await update_all_list(data[2], data[3])
        
@sio.on('error_finish')
async def d_error_finish (soid, data):
    player = str(data['player'])
    tag = int(data['tag'])
    await error_finish(soid, {'player': player, 'tag': tag})
    await error_finish(soid, {'player': player, 'tag': -tag})


async def error_finish (soid, data):
    player = str(data['player'])
    tag = int(data['tag'])
    if players[player][tag]['status']:
        print(data)
        game_id = players[player][tag]['id']
        if games[game_id]['type'] == 1:
            await update_game_log(game_id, 0, 0)
        if 'god' in games[game_id]:
            await sio.emit('finish', 0, games[game_id]['god'])
        games[game_id]['winner'] = 0
        players[player][tag]['status'] = 0
        if 'new_game' not in data:
            print('error')
            if player + str(tag) in watching_room:
                await sio.emit('error_finish', 0, room=player + str(tag))

@sio.on('error')
async def error (soid, data):  # data[player, tag, msg]
    sleep(0.3)
    await sio.emit('error', {'type': 2, 'info': data[2]}, room=str(data[0]) + str(data[1]))


@sio.on('order')
async def order (soid, data):
    order = data['order']
    params = data['params']
    if order == 'down':
        downinfo['can_play'] = params['can_play']
        downinfo['message'] = params['message']
        await sio.emit('error', {'type': 3, 'info': params['message']})
    elif order == 'check_games':
        await sio.emit('check_games', games, soid)
    elif order == 'check_players':
        await sio.emit('check_players', players, soid)
    elif order == 'update_rank':
        global max_game_id
        max_game_id = 0
        await init_list()


async def update_all_list (winner=0, loser=0):
    global rank_info
    if winner != loser:
        score_info[winner]['score'] += 5
        if score_info[loser]['score'] >= 5:
            score_info[loser]['score'] -= 5
    rank_info = [{'sid': k, 'name': v['name'], 'score': v['score']} for k, v in score_info.items()]
    rank_info.sort(key=lambda x: (x['score'], random.random()), reverse=True)
    await sio.emit('update_list', rank_info)


async def init_list ():
    global score_info
    score_info = {}
    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT sid, name, submit_time FROM users")
            users = await cursor.fetchall()
    for row in users:
        if row[0] not in score_info:
            score_info[row[0]] = {'name': row[1], 'score': -10 if row[2] else -20}

    async with pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT winner, loser FROM game_log")
            logs = await cursor.fetchall()
    for row in logs:
        winner = str(row[0])
        loser = str(row[1])
        if winner == loser:
            continue
        if winner in score_info:
            score_info[winner]['score'] += 5
        if loser in score_info:
            if score_info[loser]['score'] >= 5:
                score_info[loser]['score'] -= 5
    await update_all_list()

@sio.on('update_list')
async def update_one_list(soid, data):
    await sio.emit('update_list', rank_info, room=soid)
    
@sio.on('disconnect')
def disconnect(soid):
    print('disconnect ', soid)

async def init_pool ():
    global pool
    pool = await aiomysql.create_pool(host='127.0.0.1', port=3307, user='chess', password='chess123456', db='chess', loop=loop, autocommit=True, minsize=1, maxsize=100)
    await init_list()
    
if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(init_pool())
    web.run_app(app)
