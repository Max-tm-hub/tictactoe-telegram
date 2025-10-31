import os
import hashlib
import hmac
import json
import time
import logging
import urllib.parse
from typing import Dict, List, Optional
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from contextlib import asynccontextmanager
from aiogram import Bot
from aiogram.types import Update, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
import weakref
import uuid

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Переменные окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if not all([BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY, WEBHOOK_URL]):
    raise EnvironmentError("Missing required environment variables")

supabase: Optional[Client] = None
active_connections: Dict[str, List[weakref.ref]] = {}

# --- Валидация initData ---
def validate_init_data(init_data: str, bot_token: str) -> dict:
    try:
        pairs = [pair.split('=', 1) for pair in init_data.split('&')]
        data_dict = {}
        received_hash = None
        for k, v in pairs:
            if k == 'hash':
                received_hash = urllib.parse.unquote(v)
            else:
                data_dict[k] = urllib.parse.unquote(v)
        if received_hash is None:
            raise ValueError("Hash not found")
        auth_date = int(data_dict.get("auth_date", 0))
        if time.time() - auth_date > 86400:
            raise HTTPException(status_code=403, detail="Init data expired")
        data_check_pairs = [(k, v) for k, v in data_dict.items() if k != 'hash']
        data_check_string = '\n'.join(f"{k}={v}" for k, v in sorted(data_check_pairs))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if computed_hash != received_hash:
            raise HTTPException(status_code=403, detail="Invalid hash")
        user_data = json.loads(data_dict["user"])
        logger.info(f"Validated user ID: {user_data.get('id')}")
        return user_data
    except Exception as e:
        logger.error(f"Validation error: {e}")
        raise HTTPException(status_code=403, detail="Invalid init data")

# --- Работа с БД ---
def get_game_by_id(game_id: str):
    try:
        result = supabase.table("games").select("*").eq("id", game_id).execute()
        return result.data
    except Exception as e:
        logger.error(f"DB error (get game): {e}")
        return None

def update_game(game_id: str, data: dict):
    try:
        supabase.table("games").update(data).eq("id", game_id).execute()
    except Exception as e:
        logger.error(f"DB error (update game): {e}")

def update_stats(user_id: str, username: str, field: str):
    try:
        if not user_id:
            return
        res = supabase.table("stats").select("*").eq("user_id", user_id).execute()
        if res.data:
            current = res.data[0][field]
            supabase.table("stats").update({field: current + 1}).eq("user_id", user_id).execute()
        else:
            supabase.table("stats").insert({
                "user_id": user_id,
                "username": username,
                field: 1
            }).execute()
    except Exception as e:
        logger.error(f"Stats update error: {e}")

def check_win(board: list, symbol: str) -> bool:
    for i in range(3):
        if all(board[i][j] == symbol for j in range(3)) or all(board[j][i] == symbol for j in range(3)):
            return True
    if all(board[i][i] == symbol for i in range(3)) or all(board[i][2 - i] == symbol for i in range(3)):
        return True
    return False

# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global supabase
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    bot = Bot(token=BOT_TOKEN)
    await bot.set_webhook(f"{WEBHOOK_URL}/webhook")
    yield

app = FastAPI(lifespan=lifespan)

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://web.telegram.org",
        "https://t.me",
        "http://localhost:3000",
        WEBHOOK_URL,
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/mini", StaticFiles(directory="static"), name="mini")

# --- WebSockets ---
@app.websocket("/ws/{game_id}")
async def game_websocket(websocket: WebSocket, game_id: str):
    await websocket.accept()
    if game_id not in active_connections:
        active_connections[game_id] = []
    active_connections[game_id].append(weakref.ref(websocket))
    try:
        game = get_game_by_id(game_id)
        if game:
            await websocket.send_text(json.dumps({"type": "game", **game[0]}))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections[game_id] = [ref for ref in active_connections[game_id] if ref() is not None]
        if not active_connections[game_id]:
            del active_connections[game_id]

@app.websocket("/ws/chat/{game_id}")
async def chat_websocket(websocket: WebSocket, game_id: str):
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            user = validate_init_data(msg["initData"], BOT_TOKEN)
            supabase.table("messages").insert({
                "game_id": game_id,
                "user_id": user["id"],
                "username": user["first_name"],
                "text": msg["text"][:100]
            }).execute()
            full_msg = {
                "type": "chat",
                "username": user["first_name"],
                "text": msg["text"][:100],
                "timestamp": time.time()
            }
            if game_id in active_connections:
                for ref in active_connections[game_id][:]:
                    ws = ref()
                    if ws:
                        try:
                            await ws.send_text(json.dumps(full_msg))
                        except:
                            pass
    except WebSocketDisconnect:
        pass

async def broadcast_game_update(game_id: str):
    try:
        game_list = get_game_by_id(game_id)
        if not game_list:
            return
        game = game_list[0]
        msg = json.dumps({"type": "game", **game})
        if game_id in active_connections:
            for ref in active_connections[game_id][:]:
                ws = ref()
                if ws:
                    try:
                        await ws.send_text(msg)
                    except:
                        pass
    except Exception as e:
        logger.error(f"Broadcast error: {e}")

# --- API ---
@app.post("/api/create-game")
async def create_game(request: Request):
    try:
        data = await request.json()
        user = validate_init_data(data["initData"], BOT_TOKEN)
        game_id = str(uuid.uuid4())[:8]
        supabase.table("games").insert({
            "id": game_id,
            "creator_id": user["id"],
            "creator_name": user["first_name"],
            "current_turn": user["id"],
            "board": [["", "", ""], ["", "", ""], ["", "", ""]],
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }).execute()
        invite_link = f"https://t.me/Alex_tictactoeBot?start={game_id}"
        logger.info(f"Game created: {game_id}")
        return {"game_id": game_id, "invite_link": invite_link}
    except Exception as e:
        logger.error(f"Create game error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/api/join-game")
async def join_game(request: Request):
    try:
        data = await request.json()
        user = validate_init_data(data["initData"], BOT_TOKEN)
        game_id = data["game_id"]
        game_list = get_game_by_id(game_id)
        if not game_list:
            raise HTTPException(status_code=404, detail="Игра не найдена")
        game = game_list[0]
        if game.get("opponent_id") or str(game["creator_id"]) == str(user["id"]):
            raise HTTPException(status_code=400, detail="Невозможно присоединиться")
        update_game(game_id, {
            "opponent_id": user["id"],
            "opponent_name": user["first_name"]
        })
        await broadcast_game_update(game_id)
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Join error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/api/make-move")
async def make_move(request: Request):
    try:
        data = await request.json()
        user = validate_init_data(data["initData"], BOT_TOKEN)
        game_id = data["game_id"]
        row, col = data["row"], data["col"]
        if not (0 <= row <= 2 and 0 <= col <= 2):
            raise HTTPException(status_code=400, detail="Invalid row/col")
        game_list = get_game_by_id(game_id)
        if not game_list:
            raise HTTPException(status_code=404, detail="Игра не найдена")
        game = game_list[0]
        if game.get("winner") or game["current_turn"] != user["id"]:
            raise HTTPException(status_code=400, detail="Не ваш ход")
        symbol = "X" if user["id"] == game["creator_id"] else "O"
        board = game["board"]
        if board[row][col] != "":
            raise HTTPException(status_code=400, detail="Ячейка занята")
        board[row][col] = symbol
        winner = None
        if check_win(board, symbol):
            winner = symbol
        elif all(cell != "" for r in board for cell in r):
            winner = "draw"
        next_turn = None if winner else (
            game["opponent_id"] if user["id"] == game["creator_id"] else game["creator_id"]
        )
        update_game(game_id, {
            "board": board,
            "current_turn": next_turn,
            "winner": winner
        })
        if winner:
            c_id = game["creator_id"]
            o_id = game.get("opponent_id")
            c_name = game["creator_name"]
            o_name = game.get("opponent_name", "Unknown")
            if winner == "X":
                update_stats(c_id, c_name, "wins")
                if o_id:
                    update_stats(o_id, o_name, "losses")
            elif winner == "O" and o_id:
                update_stats(o_id, o_name, "wins")
                update_stats(c_id, c_name, "losses")
            elif winner == "draw":
                update_stats(c_id, c_name, "draws")
                if o_id:
                    update_stats(o_id, o_name, "draws")
        await broadcast_game_update(game_id)
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Move error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/api/stats")
async def get_stats(request: Request):
    try:
        init_data = request.headers.get("X-Init-Data")
        if not init_data:
            raise HTTPException(status_code=400, detail="Missing X-Init-Data")
        user = validate_init_data(init_data, BOT_TOKEN)
        res = supabase.table("stats").select("*").eq("user_id", user["id"]).execute()
        if res.data:
            return res.data[0]
        return {
            "user_id": user["id"],
            "username": user["first_name"],
            "wins": 0,
            "losses": 0,
            "draws": 0
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Stats error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

# --- Telegram Webhook ---
@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        bot = Bot(token=BOT_TOKEN)
        update_data = await request.json()
        update = Update(**update_data)
        if update.message and update.message.text:
            text = update.message.text.strip()
            user_id = update.message.from_user.id
            if text == "/start":
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="Играть в Крестики-нолики",
                        web_app=WebAppInfo(url=f"{WEBHOOK_URL}/mini/index.html")
                    )
                ]])
                await bot.send_message(user_id, "Нажмите, чтобы начать!", reply_markup=kb)
            elif text.startswith("/start "):
                game_id = text.split(" ", 1)[1].strip()
                game_list = get_game_by_id(game_id)
                if not game_list:
                    await bot.send_message(user_id, "❌ Игра не найдена.")
                    return {"ok": True}
                game = game_list[0]
                if game.get("opponent_id"):
                    await bot.send_message(user_id, "❌ Игра уже заполнена.")
                elif str(game["creator_id"]) == str(user_id):
                    await bot.send_message(user_id, "Вы — создатель игры. Открываю вашу игру...")
                else:
                    await bot.send_message(user_id, "🎮 Присоединяйтесь к игре!")
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(
                        text="Открыть игру",
                        web_app=WebAppInfo(url=f"{WEBHOOK_URL}/mini/index.html")
                    )
                ]])
                await bot.send_message(user_id, "Нажмите кнопку ниже:", reply_markup=kb)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
