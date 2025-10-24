import os
import hashlib
import hmac
import json
import time
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client
from contextlib import asynccontextmanager
from aiogram import Bot
from aiogram.types import Update, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton

# === Настройки из переменных окружения ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# === Хранилище WebSocket-соединений ===
active_connections: dict[str, list[WebSocket]] = {}

# === Валидация initData от Telegram ===
def validate_init_data(init_ str, bot_token: str) -> dict:
    try:
        pairs = [pair.split('=', 1) for pair in init_data.split('&')]
        data = {k: v for k, v in pairs if k != 'hash'}
        data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(data.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        h = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256)
        if h.hexdigest() != data.get('hash'):
            raise HTTPException(status_code=403, detail="Invalid hash")
        return json.loads(data["user"])
    except Exception:
        raise HTTPException(status_code=403, detail="Invalid init data")

# === FastAPI ===
@asynccontextmanager
async def lifespan(app: FastAPI):
    bot = Bot(token=BOT_TOKEN)
    await bot.set_webhook(f"{WEBHOOK_URL}/webhook")
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
)
app.mount("/mini", StaticFiles(directory="static"), name="mini")

# === WebSocket: обновления игры ===
@app.websocket("/ws/{game_id}")
async def game_websocket(websocket: WebSocket, game_id: str):
    await websocket.accept()
    if game_id not in active_connections:
        active_connections[game_id] = []
    active_connections[game_id].append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections[game_id].remove(websocket)
        if not active_connections[game_id]:
            del active_connections[game_id]

# === WebSocket: чат ===
@app.websocket("/ws/chat/{game_id}")
async def chat_websocket(websocket: WebSocket, game_id: str):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            user = validate_init_data(msg["initData"], BOT_TOKEN)
            # Сохраняем сообщение
            supabase.table("messages").insert({
                "game_id": game_id,
                "user_id": user["id"],
                "username": user["first_name"],
                "text": msg["text"][:100]
            }).execute()
            # Рассылаем
            full_msg = {
                "type": "chat",
                "username": user["first_name"],
                "text": msg["text"][:100],
                "timestamp": time.time()
            }
            if game_id in active_connections:
                for ws in active_connections[game_id][:]:
                    try:
                        await ws.send_text(json.dumps(full_msg))
                    except:
                        pass
    except WebSocketDisconnect:
        pass

# === Утилита: рассылка обновления игры ===
async def broadcast_game_update(game_id: str):
    game = supabase.table("games").select("*").eq("id", game_id).execute().data[0]
    message = json.dumps({"type": "game", **game})
    if game_id in active_connections:
        for ws in active_connections[game_id][:]:
            try:
                await ws.send_text(message)
            except:
                active_connections[game_id].remove(ws)

# === API: создать игру ===
@app.post("/api/create-game")
async def create_game(request: Request):
    data = await request.json()
    user = validate_init_data(data["initData"], BOT_TOKEN)
    game_id = hashlib.sha256(f"{user['id']}{time.time()}".encode()).hexdigest()[:8]
    supabase.table("games").insert({
        "id": game_id,
        "creator_id": user["id"],
        "creator_name": user["first_name"],
        "current_turn": user["id"]
    }).execute()
    invite_link = f"https://t.me/Alex_tictactoeBot?start={game_id}"
    return {"game_id": game_id, "invite_link": invite_link}

# === API: присоединиться ===
@app.post("/api/join-game")
async def join_game(request: Request):
    data = await request.json()
    user = validate_init_data(data["initData"], BOT_TOKEN)
    game_id = data["game_id"]
    game = supabase.table("games").select("*").eq("id", game_id).execute().data
    if not game or game[0]["opponent_id"] or game[0]["creator_id"] == user["id"]:
        raise HTTPException(400, "Невозможно присоединиться")
    supabase.table("games").update({
        "opponent_id": user["id"],
        "opponent_name": user["first_name"]
    }).eq("id", game_id).execute()
    await broadcast_game_update(game_id)
    return {"status": "ok"}

# === API: сделать ход ===
@app.post("/api/make-move")
async def make_move(request: Request):
    data = await request.json()
    user = validate_init_data(data["initData"], BOT_TOKEN)
    game_id = data["game_id"]
    row, col = data["row"], data["col"]
    game = supabase.table("games").select("*").eq("id", game_id).execute().data[0]
    if game["winner"] or game["current_turn"] != user["id"]:
        raise HTTPException(400, "Не ваш ход")
    symbol = "X" if user["id"] == game["creator_id"] else "O"
    board = game["board"]
    if board[row][col] != "":
        raise HTTPException(400, "Ячейка занята")
    board[row][col] = symbol

    def check_win(b, s):
        for i in range(3):
            if all(b[i][j] == s for j in range(3)): return True
            if all(b[j][i] == s for j in range(3)): return True
        if all(b[i][i] == s for i in range(3)): return True
        if all(b[i][2-i] == s for i in range(3)): return True
        return False

    winner = None
    if check_win(board, symbol):
        winner = symbol
    elif all(cell != "" for row in board for cell in row):
        winner = "draw"

    next_turn = None if winner else (game["opponent_id"] if user["id"] == game["creator_id"] else game["creator_id"])
    supabase.table("games").update({
        "board": board,
        "current_turn": next_turn,
        "winner": winner
    }).eq("id", game_id).execute()

    # Обновляем статистику
    if winner:
        def update_stats(uid, name, field):
            res = supabase.table("stats").select("*").eq("user_id", uid).execute()
            if res.
                supabase.table("stats").update({field: res.data[0][field] + 1}).eq("user_id", uid).execute()
            else:
                supabase.table("stats").insert({"user_id": uid, "username": name, field: 1}).execute()
        c_id, o_id = game["creator_id"], game["opponent_id"]
        c_name, o_name = game["creator_name"], game["opponent_name"] or "Unknown"
        if winner == "X":
            update_stats(c_id, c_name, "wins")
            if o_id: update_stats(o_id, o_name, "losses")
        elif winner == "O" and o_id:
            update_stats(o_id, o_name, "wins")
            update_stats(c_id, c_name, "losses")
        elif winner == "draw":
            update_stats(c_id, c_name, "draws")
            if o_id: update_stats(o_id, o_name, "draws")

    await broadcast_game_update(game_id)
    return {"status": "ok"}

# === API: статистика ===
@app.get("/api/stats")
async def get_stats(request: Request):
    user = validate_init_data(request.headers.get("X-Init-Data"), BOT_TOKEN)
    res = supabase.table("stats").select("*").eq("user_id", user["id"]).execute()
    if res.
        return res.data[0]
    return {"wins": 0, "losses": 0, "draws": 0, "username": user["first_name"]}

# === Telegram webhook ===
@app.post("/webhook")
async def telegram_webhook(request: Request):
    bot = Bot(token=BOT_TOKEN)
    update = Update(**await request.json())
    if update.message and update.message.text == "/start":
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Играть в Крестики-нолики",
                web_app=WebAppInfo(url=f"{WEBHOOK_URL}/mini/index.html")
            )
        ]])
        await bot.send_message(update.message.from_user.id, "Нажмите, чтобы начать!", reply_markup=kb)
    return {"ok": True}