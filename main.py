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
import aiohttp
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Переменные окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if not all([BOT_TOKEN, SUPABASE_URL, SUPABASE_KEY, WEBHOOK_URL]):
    raise EnvironmentError("Отсутствуют обязательные переменные окружения")

supabase: Optional[Client] = None
active_connections: Dict[str, List[weakref.ref]] = {}
session = None

# Валидация initData
def validate_init_data(init_data: str, bot_token: str) -> dict:
    try:
        pairs = [pair.split("=", 1) for pair in init_data.split("&")]
        data_dict = {}
        received_hash = None
        for k, v in pairs:
            if k == "hash":
                received_hash = urllib.parse.unquote(v)
            else:
                data_dict[k] = urllib.parse.unquote(v)
        if received_hash is None:
            raise ValueError("Хэш не найден")
        auth_date = int(data_dict.get("auth_date", 0))
        if time.time() - auth_date > 86400:
            raise HTTPException(status_code=403, detail="Истекло время действия initData")
        data_check_pairs = [(k, v) for k, v in data_dict.items() if k != "hash"]
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data_check_pairs))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if computed_hash != received_hash:
            raise HTTPException(status_code=403, detail="Некорректный хэш")
        user_data = json.loads(data_dict["user"])
        logger.info(f"Пользователь успешно валидирован: ID {user_data.get('id')}")
        return user_data
    except Exception as e:
        logger.error(f"Ошибка валидации: {e}")
        raise HTTPException(status_code=403, detail="Некорректные данные initData")

# Работа с базой данных
def is_game_id_unique(game_id: str) -> bool:
    try:
        result = supabase.table("games").select("id").eq("id", game_id).execute()
        return not result.data
    except Exception as e:
        logger.error(f"Ошибка проверки уникальности game_id: {e}")
        return False

def get_game_by_id(game_id: str):
    try:
        result = supabase.table("games").select("*").eq("id", game_id).execute()
        if result.data:
            # Убедимся, что board - это список списков, а не строка
            game_data = result.data[0]
            board = game_data.get("board")
            if isinstance(board, str):
                try:
                    parsed_board = json.loads(board)
                    if isinstance(parsed_board, list) and len(parsed_board) == 3 and all(isinstance(row, list) and len(row) == 3 for row in parsed_board):
                         game_data["board"] = parsed_board
                         logger.debug(f"Доска для игры {game_id} была строкой, преобразована в список списков.")
                    else:
                         logger.error(f"Доска для игры {game_id} - строка, но не корректный JSON массив 3x3: {board}")
                         # Возвращаем None или пустую игру, если доска испорчена
                         return None
                except json.JSONDecodeError:
                    logger.error(f"Доска для игры {game_id} - строка, но не корректный JSON: {board}")
                    # Возвращаем None или пустую игру, если доска испорчена
                    return None
            return result.data
        return None
    except Exception as e:
        logger.error(f"Ошибка получения игры: {e}")
        return None

def update_game(game_id: str, data: dict):
    try:
        # Убедимся, что board отправляется как список списков (Supabase сам его сериализует)
        # Если board - строка, не пытаемся её парсить перед отправкой, а оставляем как есть или преобразуем обратно в список
        board = data.get("board")
        if isinstance(board, str):
             # Если вдруг board пришёл строкой в update, попробуем его распарсить перед отправкой
             try:
                 parsed_board = json.loads(board)
                 if isinstance(parsed_board, list) and len(parsed_board) == 3 and all(isinstance(row, list) and len(row) == 3 for row in parsed_board):
                     data["board"] = parsed_board
                     logger.debug(f"Доска в update_game была строкой, преобразована в список списков перед отправкой.")
                 else:
                     logger.error(f"Доска в update_game была строкой, но не корректный JSON массив 3x3: {board}")
                     return # Не обновляем, если доска испорчена
             except json.JSONDecodeError:
                 logger.error(f"Доска в update_game была строкой, но не корректный JSON: {board}")
                 return # Не обновляем, если доска испорчена
        supabase.table("games").update(data).eq("id", game_id).execute()
    except Exception as e:
        logger.error(f"Ошибка обновления игры: {e}")

def update_stats(user_id: str, username: str, field: str):
    try:
        if not user_id:
            return
        res = supabase.table("stats").select("*").eq("user_id", user_id).execute()
        if res.data:
            current = res.data[0][field]
            supabase.table("stats").update({field: current + 1}).eq("user_id", user_id).execute()
        else:
            supabase.table("stats").insert({"user_id": user_id, "username": username, field: 1}).execute()
    except Exception as e:
        logger.error(f"Ошибка обновления статистики: {e}")

def check_win(board: list, symbol: str) -> bool:
    # board уже должен быть списком списков к моменту вызова этой функции
    try:
        for i in range(3):
            if all(board[i][j] == symbol for j in range(3)) or all(board[j][i] == symbol for j in range(3)):
                return True
        if all(board[i][i] == symbol for i in range(3)) or all(board[i][2 - i] == symbol for i in range(3)):
            return True
        return False
    except (TypeError, IndexError) as e:
        logger.error(f"Ошибка в check_win: {e}, board: {board}")
        return False # Не считаем победу, если доска испорчена

# Lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    global session, supabase
    session = aiohttp.ClientSession()
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    bot = Bot(token=BOT_TOKEN)
    await bot.set_webhook(f"{WEBHOOK_URL}/webhook")
    yield
    await session.close()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://web.telegram.org", "https://t.me", "http://localhost:3000", WEBHOOK_URL],
    allow_methods=["*"],
    allow_headers=["*"]
)

app.mount("/mini", StaticFiles(directory="static"), name="mini")

# WebSockets
@app.websocket("/ws/{game_id}")
async def game_websocket(websocket: WebSocket, game_id: str):
    await websocket.accept()
    if game_id not in active_connections:
        active_connections[game_id] = []
    active_connections[game_id].append(weakref.ref(websocket))
    try:
        game = get_game_by_id(game_id)
        if game:
            await websocket.send_json({"type": "game", **game[0]})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info(f"WebSocket отключен для игры {game_id}")
        active_connections[game_id] = [ref for ref in active_connections[game_id] if ref() is not None]
        if not active_connections[game_id]:
            del active_connections[game_id]
    except Exception as e:
        logger.error(f"Ошибка WebSocket для игры {game_id}: {e}")
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
                            await ws.send_json(full_msg)
                        except Exception as e:
                            logger.error(f"Ошибка отправки сообщения в WebSocket: {e}")
    except WebSocketDisconnect:
        logger.info(f"WebSocket чата отключен для игры {game_id}")
    except Exception as e:
        logger.error(f"Ошибка WebSocket чата для игры {game_id}: {e}")

async def broadcast_game_update(game_id: str):
    try:
        game_list = get_game_by_id(game_id)
        if not game_list:
            return
        game = game_list[0]
        msg = {"type": "game", **game}
        if game_id in active_connections:
            for ref in active_connections[game_id][:]:
                ws = ref()
                if ws:
                    try:
                        await ws.send_json(msg)
                    except Exception as e:
                        logger.error(f"Ошибка отправки сообщения в WebSocket: {e}")
    except Exception as e:
        logger.error(f"Ошибка трансляции обновления игры: {e}")

# API endpoints
@app.post("/api/create-game")
async def create_game(request: Request):
    try:
        data = await request.json()
        logger.info(f"Получены данные initData: {data.get('initData')}")
        user = validate_init_data(data["initData"], BOT_TOKEN)
        game_id = str(uuid.uuid4())[:8]
        while not is_game_id_unique(game_id):
            game_id = str(uuid.uuid4())[:8]
        # board должен быть списком списков
        initial_board = [[None]*3 for _ in range(3)]
        supabase.table("games").insert({
            "id": game_id,
            "creator_id": user["id"],
            "creator_name": user["first_name"],
            "current_turn": user["id"],
            "board": initial_board, # Отправляем как список списков
            "game_started": False,  # Игра не начинается автоматически
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }).execute()
        invite_link = f"http://t.me/Alex_tictactoeBot?start={game_id}"
        logger.info(f"Игра создана: {game_id}")
        return {"game_id": game_id, "invite_link": invite_link}
    except Exception as e:
        logger.error(f"Ошибка создания игры: {e}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")

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
            raise HTTPException(status_code=400, detail="Невозможно присоединиться к игре")
        update_game(game_id, {
            "opponent_id": user["id"],
            "opponent_name": user["first_name"],
            "game_started": False  # Игра не начинается автоматически
        })
        await broadcast_game_update(game_id)
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка присоединения к игре: {e}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")

@app.post("/api/start-game")
async def start_game(request: Request):
    try:
        data = await request.json()
        user = validate_init_data(data["initData"], BOT_TOKEN)
        game_id = data["game_id"]
        game_list = get_game_by_id(game_id)
        if not game_list:
            raise HTTPException(status_code=404, detail="Игра не найдена")
        game = game_list[0]
        if not game.get("opponent_id") or game.get("game_started"):
            raise HTTPException(status_code=400, detail="Невозможно начать игру")
        if str(user["id"]) != str(game["opponent_id"]): # Только второй игрок может начать
            raise HTTPException(status_code=403, detail="Только второй игрок может начать игру")
        update_game(game_id, {
            "game_started": True,
            "current_turn": game["creator_id"]  # Начинает первый игрок
        })
        await broadcast_game_update(game_id)
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка начала игры: {e}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")

@app.post("/api/make-move")
async def make_move(request: Request):
    try:
        data = await request.json()
        user = validate_init_data(data["initData"], BOT_TOKEN)
        game_id = data["game_id"]
        row, col = data["row"], data["col"]
        if not (0 <= row <= 2 and 0 <= col <= 2):
            raise HTTPException(status_code=400, detail="Некорректные координаты")
        game_list = get_game_by_id(game_id)
        if not game_list:
            raise HTTPException(status_code=404, detail="Игра не найдена")
        game = game_list[0]
        if not game.get("game_started"):
            raise HTTPException(status_code=400, detail="Игра ещё не началась")
        if game.get("winner") or game["current_turn"] != user["id"]:
            raise HTTPException(status_code=400, detail="Сейчас не ваша очередь ходить")
        symbol = "X" if user["id"] == game["creator_id"] else "O"
        board = game["board"] # board должен быть списком списков благодаря get_game_by_id
        if board[row][col] is not None:
            raise HTTPException(status_code=400, detail="Эта ячейка уже занята")
        board[row][col] = symbol
        winner = None
        if check_win(board, symbol):
            winner = symbol
        elif all(cell is not None for r in board for cell in r):
            winner = "draw"
        next_turn = None if winner else (
            game["opponent_id"] if user["id"] == game["creator_id"] else game["creator_id"]
        )
        update_game(game_id, {
            "board": board, # board как список списков
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
        logger.error(f"Ошибка хода: {e}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")

@app.get("/api/stats")
async def get_stats(request: Request):
    try:
        init_data = request.headers.get("X-Init-Data")
        if not init_data:
            raise HTTPException(status_code=400, detail="Отсутствует X-Init-Data")
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
        logger.error(f"Ошибка получения статистики: {e}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")

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
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Создать новую игру", web_app=WebAppInfo(url=f"{WEBHOOK_URL}/mini/index.html"))]
                ])
                await bot.send_message(user_id, "Нажмите, чтобы создать новую игру!", reply_markup=kb)
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
                    await bot.send_message(user_id, "Вы — создатель игры. Открываете свою игру...")
                else:
                    await bot.send_message(user_id, "🎮 Присоединяйтесь к игре!")
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Открыть игру", web_app=WebAppInfo(url=f"{WEBHOOK_URL}/mini/index.html?startapp={game_id}"))]
                ])
                await bot.send_message(user_id, "Нажмите кнопку ниже, чтобы присоединиться:", reply_markup=kb)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Ошибка вебхука: {e}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")

# Endpoint to serve the index.html file, replacing the placeholder
@app.get("/mini/index.html")
async def serve_index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        content = f.read()
    # Replace the placeholder with the actual URL
    content = content.replace("{{WEBHOOK_URL}}", WEBHOOK_URL)
    return content
