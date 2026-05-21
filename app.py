import random
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit

app = Flask(__name__, template_folder='.')
app.config['SECRET_KEY'] = 'mahjong_secret!'
# 啟用執行緒異步模式，防止 WebSocket 雙向握手時卡死
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

players = []      
deck = []         
current_turn = 0  
game_started = False
river = []        

action_state = {
    'active': False,       
    'discarded_tile': None,
    'discarder_idx': None, 
    'waiting_players': {}  
}

FLOWER_LIST = ['春', '夏', '秋', '冬', '梅', '蘭', '竹', '菊']

def create_deck():
    new_deck = []
    for suit in ['萬', '筒', '條']:
        for val in range(1, 10):
            for _ in range(4):
                new_deck.append({'suit': suit, 'val': str(val), 'text': f"{val}{suit}"})
    for honor in ['東', '南', '西', '北', '中', '發', '白']:
        for _ in range(4):
            new_deck.append({'suit': '字', 'val': honor, 'text': honor})
    for flower in FLOWER_LIST:
        new_deck.append({'suit': '花', 'val': flower, 'text': flower})
    random.shuffle(new_deck)
    return new_deck

def sort_hand(hand):
    suit_order = { '萬': 1, '筒': 2, '條': 3, '字': 4, '花': 5 }
    honor_order = { '東': 1, '南': 2, '西': 3, '北': 4, '中': 5, '發': 6, '白': 7 }
    def get_sort_key(tile):
        s_pos = suit_order.get(tile['suit'], 99)
        if tile['suit'] == '字': v_pos = honor_order.get(tile['val'], 99)
        elif tile['suit'] == '花': v_pos = FLOWER_LIST.index(tile['val']) if tile['val'] in FLOWER_LIST else 99
        else: v_pos = int(tile['val'])
        return (s_pos, v_pos)
    return sorted(hand, key=get_sort_key)

def auto_draw_and_flower(player_index):
    global deck
    if player_index >= len(players): return None
    p = players[player_index]
    if not deck:
        socketio.emit('log', '🈄 牌庫已空，流局！', broadcast=True)
        return None
    new_tile = deck.pop()
    while new_tile and new_tile['suit'] == '花':
        p['flowers'].append(new_tile)
        socketio.emit('log', f"【{p['name']}】 摸到花牌 【{new_tile['text']}】！自動補花...", broadcast=True)
        if deck: new_tile = deck.pop()
        else: new_tile = None
    return new_tile

def check_and_flower_initial(player):
    global deck
    has_flower = True
    while has_flower:
        flower_idx = next((i for i, t in enumerate(player['hand']) if t['suit'] == '花'), None)
        if flower_idx is not None:
            flower_tile = player['hand'].pop(flower_idx)
            player['flowers'].append(flower_tile)
            if deck: player['hand'].append(deck.pop())
        else: has_flower = False
    player['hand'] = sort_hand(player['hand'])

def check_eat_pong_options(discarder_idx, tile):
    options_found = {}
    if tile['suit'] == '花' or len(players) < 2: return options_found
    for idx, p in enumerate(players):
        if idx == discarder_idx: continue 
        opts = []
        if len([t for t in p['hand'] if t['text'] == tile['text']]) >= 2: opts.append('碰')
        is_next = (idx == (discarder_idx + 1) % len(players))
        if is_next and tile['suit'] != '字' and tile['val'].isdigit():
            v, s, h_t = int(tile['val']), tile['suit'], [t['text'] for t in p['hand']]
            if f"{v-1}{s}" in h_t and f"{v+1}{s}" in h_t: opts.append('吃')
            if f"{v+1}{s}" in h_t and f"{v+2}{s}" in h_t: opts.append('吃')
            if f"{v-2}{s}" in h_t and f"{v-1}{s}" in h_t: opts.append('吃')
        if opts: options_found[p['id']] = opts
    return options_found

def send_game_state():
    global current_turn, deck, river, action_state, players
    active_player = players[current_turn] if current_turn < len(players) else None
    
    public_data = []
    for p in players:
        public_data.append({
            'name': p['name'],
            'id': p['id'],
            'flowers': p['flowers'],
            'melds': p['melds']
        })

    for i, p in enumerate(players):
        my_actions = action_state['waiting_players'].get(p['id'], []) if action_state['active'] else []
        socketio.emit('gameState', {
            'hand': p['hand'],
            'flowers': p['flowers'],
            'melds': p['melds'], 
            'drawnTile': p.get('drawnTile'),
            'river': river,
            'deckCount': len(deck),
            'isMyTurn': (i == current_turn) and not action_state['active'],
            'currentTurnName': active_player['name'] if active_player else "",
            'actionOptions': my_actions, 
            'interceptTile': action_state['discarded_tile']['text'] if action_state['active'] else "",
            'allPlayersPublic': public_data 
        }, to=p['id'])

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    global players, game_started
    sid = request.sid
    # 如果已經在打牌中，允許斷線重整的玩家直接重連，不要塞新座位
    if game_started:
        send_game_state()
        return
    if not any(p['id'] == sid for p in players) and len(players) < 4:
        players.append({'id': sid, 'name': f"牌友 {len(players) + 1}", 'hand': [], 'flowers': [], 'melds': [], 'drawnTile': None})
        emit('updatePlayers', players, broadcast=True)

@socketio.on('disconnect')
def handle_disconnect():
    global players, game_started
    sid = request.sid
    # 如果還沒開局，有人離開或重新整理就立刻把幽靈 ID 拔掉
    if not game_started:
        players = [p for p in players if p['id'] != sid]
        for idx, p in enumerate(players):
            p['name'] = f"牌友 {idx + 1}"
        emit('updatePlayers', players, broadcast=True)

# 💥 核心修正：完美重新開局與洗牌重置機制 💥
@socketio.on('startGame')
def handle_start_game():
    global deck, players, current_turn, game_started, river, action_state
    if len(players) == 0: return
    
    # 1. 徹底洗牌，清空公用河底，回合歸零
    deck = create_deck()
    river = []
    current_turn = 0 
    game_started = True
    
    # 2. 徹底清空所有吃碰攔截的暫停死結狀態
    action_state['active'] = False
    action_state['discarded_tile'] = None
    action_state['discarder_idx'] = None
    action_state['waiting_players'] = {}
    
    # 3. 核心大洗牌：將桌上「所有現存活人」的歷史手牌、花牌、吃碰區、摸牌全部強制歸零重置！
    for p in players:
        p['hand'] = []
        p['flowers'] = []
        p['melds'] = []
        p['drawnTile'] = None
        
        # 重新分發新一局的 16 張初始手牌
        for _ in range(16):
            if deck: p['hand'].append(deck.pop())
        # 執行新一局的初始補花
        check_and_flower_initial(p)
    
    # 4. 首家自動摸牌（第 17 張牌解鎖）
    players[current_turn]['drawnTile'] = auto_draw_and_flower(current_turn)
    
    socketio.emit('log', '🔄 【洗牌重開】桌面與狀態已全數清空，新對局正式開始！', broadcast=True)
    # 廣播最新乾淨的牌局資料給所有人
    send_game_state()

@socketio.on('discardTile')
def handle_discard(data):
    global players, current_turn, river, action_state
    sid, t_text, is_d = request.sid, data.get('tileText'), data.get('isDrawn', False)
    idx = next((i for i, p in enumerate(players) if p['id'] == sid), None)
    if idx != current_turn or action_state['active'] or idx is None: return 
    p, d_t = players[idx], None
    if is_d and p['drawnTile'] and p['drawnTile']['text'] == t_text:
        d_t, p['drawnTile'] = p['drawnTile'], None
    else:
        for i, t in enumerate(p['hand']):
            if t['text'] == t_text:
                d_t = p['hand'].pop(i)
                break
        if p['drawnTile']: p['hand'].append(p['drawnTile']); p['drawnTile'] = None
    if d_t:
        p['hand'] = sort_hand(p['hand'])
        opts = check_eat_pong_options(idx, d_t)
        if opts:
            action_state.update({'active': True, 'discarded_tile': d_t, 'discarder_idx': idx, 'waiting_players': opts})
        else:
            river.append(d_t)
            current_turn = (current_turn + 1) % len(players)
            players[current_turn]['drawnTile'] = auto_draw_and_flower(current_turn)
        send_game_state()

@socketio.on('playerAction')
def handle_player_action(data):
    global players, current_turn, river, action_state
    sid, act = request.sid, data.get('action')
    if not action_state['active']: return
    idx = next((i for i, p in enumerate(players) if p['id'] == sid), None)
    if idx is None: return
    p, target = players[idx], action_state['discarded_tile']
    
    if act == '過':
        if sid in action_state['waiting_players']:
            del action_state['waiting_players'][sid]
        if not action_state['waiting_players']:
            action_state['active'] = False
            river.append(target)
            current_turn = (action_state['discarder_idx'] + 1) % len(players)
            players[current_turn]['drawnTile'] = auto_draw_and_flower(current_turn)
            socketio.emit('log', f"大家都過，輪到 【{players[current_turn]['name']}】 摸牌。", broadcast=True)
            
    elif act in ['碰', '吃']:
        action_state['active'] = False
        action_state['waiting_players'] = {}
        
        if act == '碰':
            c, n_h = 0, []
            for t in p['hand']:
                if t['text'] == target['text'] and c < 2: c += 1
                else: n_h.append(t)
            p['hand'] = n_h
            p['melds'].append({'type': '碰', 'tile': target['text']})
            socketio.emit('log', f"🔥 【{p['name']}】 碰了 【{target['text']}】！", broadcast=True)
        else:
            v, s, h_t = int(target['val']), target['suit'], [t['text'] for t in p['hand']]
            pair = []
            if f"{v-1}{s}" in h_t and f"{v+1}{s}" in h_t: pair = [f"{v-1}{s}", f"{v+1}{s}"]
            elif f"{v+1}{s}" in h_t and f"{v+2}{s}" in h_t: pair = [f"{v+1}{s}", f"{v+2}{s}"]
            elif f"{v-2}{s}" in h_t and f"{v-1}{s}" in h_t: pair = [f"{v-2}{s}", f"{v-1}{s}"]
            for txt in pair:
                for i, t in enumerate(p['hand']):
                    if t['text'] == txt: p['hand'].pop(i); break
            
            all_three_vals = [int(target['val']), int(pair[0].replace(s,'')), int(pair[1].replace(s,''))]
            all_three_vals.sort()
            sorted_eat_text = f"{all_three_vals[0]}{s}-{all_three_vals[1]}{s}-{all_three_vals[2]}{s}"
            
            p['melds'].append({'type': '吃', 'tile': sorted_eat_text})
            socketio.emit('log', f"🍴 【{p['name']}】 吃了 【{target['text']}】！組合為：{sorted_eat_text}", broadcast=True)
            
        p['hand'] = sort_hand(p['hand'])
        current_turn = idx 
        p['drawnTile'] = None 

    send_game_state()

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000, debug=True)