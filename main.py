import os
import hashlib
import hmac
import json
import time
import logging
import urllib.parse
import asyncio
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
session: Optional[aiohttp.ClientSession] = None  # Глобальная сессия для lifespan

# Валидация initData - ИСПРАВЛЕНО
def validate_init_data(init_data_str: str, bot_token: str) -> dict:
    try:
        pairs = [pair.split("=", 1) for pair in init_data_str.split("&")]
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
        if time.time() - auth_date > 86400:  # 24 часа
            raise HTTPException(status_code=403, detail="Истекло время действия initData")

        data_check_pairs = [(k, v) for k, v in data_dict.items() if k != "hash"]
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data_check_pairs)) # ИСПРАВЛЕНО: \n
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if computed_hash != received_hash:
            raise HTTPException(status_code=403, detail="Некорректный хэш")

        user_data = json.loads(data_dict["user"])
        logger.info(f"Пользователь успешно валидирован: ID {user_data.get('id')}")
        return user_data
    except json.JSONDecodeError:
        logger.error("Ошибка: Неверный формат JSON в данных пользователя initData")
        raise HTTPException(status_code=403, detail="Некорректные данные initData")
    except Exception as e:
        logger.error(f"Ошибка валидации: {e}")
        raise HTTPException(status_code=403, detail="Некорректные данные initData")


# Работа с базой данных
def is_game_id_unique(game_id: str) -> bool:
    try:
        result = supabase.table("games").select("id").eq("id", game_id).execute()
        return not bool(result.data) # Явно проверяем на пустой список
    except Exception as e:
        logger.error(f"Ошибка проверки уникальности game_id: {e}")
        return False

def get_game_by_id(game_id: str):
    try:
        result = supabase.table("games").select("*").eq("id", game_id).execute()
        if result.data:
            game_data = result.data[0]
            board = game_data.get("board")
            # Проверяем и конвертируем строку в список списков, если нужно
            if isinstance(board, str):
                try:
                    parsed_board = json.loads(board)
                    if isinstance(parsed_board, list) and len(parsed_board) == 3 and all(isinstance(row, list) and len(row) == 3 for row in parsed_board):
                         game_data["board"] = parsed_board
                         logger.debug(f"Доска для игры {game_id} была строкой, преобразована в список списков.")
                    else:
                         logger.error(f"Доска для игры {game_id} - строка, но не корректный JSON массив 3x3: {board}")
                         return None # Возвращаем None, если доска испорчена
                except json.JSONDecodeError:
                    logger.error(f"Доска для игры {game_id} - строка, но не корректный JSON: {board}")
                    return None # Возвращаем None, если доска испорчена
            return result.data
        return None
    except Exception as e:
        logger.error(f"Ошибка получения игры: {e}")
        return None

def update_game(game_id: str, data: dict):
    try:
        # Убедимся, что board отправляется как список списков
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
                     return  # Не обновляем, если доска испорчена
             except json.JSONDecodeError:
                 logger.error(f"Доска в update_game была строкой, но не корректный JSON: {board}")
                 return  # Не обновляем, если доска испорчена
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
        # Проверка строк
        for i in range(3):
            if all(board[i][j] == symbol for j in range(3)):
                return True
        # Проверка столбцов
        for j in range(3):
            if all(board[i][j] == symbol for i in range(3)):
                return True
        # Проверка диагоналей
        if all(board[i][i] == symbol for i in range(3)):
            return True
        if all(board[i][2 - i] == symbol for i in range(3)):
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
    logger.info("Lifespan startup completed.")
    try:
        yield
    finally:
        await session.close()
        await bot.session.close() # Закрытие сессии бота
        logger.info("Lifespan shutdown completed.")

app = FastAPI(lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://web.telegram.org", "https://t.me", "http://localhost:3000", WEBHOOK_URL],
    allow_methods=["*"],
    allow_headers=["*"],
    # allow_credentials=True # Включите, если нужно передавать куки/авторизацию
)

# Монтируем статические файлы из папки 'static'
app.mount("/mini", StaticFiles(directory="static"), name="mini")

# WebSockets
@app.websocket("/ws/{game_id}")
async def game_websocket(websocket: WebSocket, game_id: str):
    await websocket.accept()
    if game_id not in active_connections:
        active_connections[game_id] = []
    active_connections[game_id].append(weakref.ref(websocket))
    logger.info(f"WebSocket подключен к игре {game_id}. Всего соединений: {len(active_connections[game_id])}")

    try:
        game = get_game_by_id(game_id)
        if game:
            await websocket.send_json({"type": "game", **game[0]})
        else:
            logger.warning(f"WebSocket подключился к несуществующей игре {game_id}")
            await websocket.send_json({"type": "error", "message": "Игра не найдена"})
            await websocket.close(code=1008, reason="Игра не найдена")
            return

        while True:
            # Ожидаем сообщения от клиента (не используется напрямую в этой логике)
            await websocket.receive_text()
    except WebSocketDisconnect as e:
        logger.info(f"WebSocket отключен для игры {game_id}. Код: {e.code}, Причина: {e.reason}")
        # Удаляем недоступные ссылки
        active_connections[game_id] = [ref for ref in active_connections[game_id] if ref() is not None]
        if not active_connections[game_id]:
            del active_connections[game_id]
            logger.debug(f"Удален словарь соединений для игры {game_id}, так как он пуст.")
    except Exception as e:
        logger.error(f"Неожиданная ошибка WebSocket для игры {game_id}: {e}")
        active_connections[game_id] = [ref for ref in active_connections[game_id] if ref() is not None]
        if not active_connections[game_id]:
            del active_connections[game_id]

@app.websocket("/ws/chat/{game_id}")
async def chat_websocket(websocket: WebSocket, game_id: str):
    await websocket.accept()
    logger.info(f"WebSocket чата подключен к игре {game_id}")
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            user = validate_init_data(msg["initData"], BOT_TOKEN)
            user_id = user["id"]
            username = user.get("first_name", "Unknown")

            # Проверяем, что пользователь участвует в игре
            game_list = get_game_by_id(game_id)
            if not game_list:
                logger.warning(f"Пользователь {user_id} пытается отправить сообщение в несуществующую игру {game_id}")
                await websocket.send_json({"type": "error", "message": "Игра не найдена"})
                continue

            game = game_list[0]
            if str(user_id) != str(game.get("creator_id")) and str(user_id) != str(game.get("opponent_id")):
                 logger.warning(f"Пользователь {user_id} не участвует в игре {game_id}, доступ к чату запрещен.")
                 await websocket.send_json({"type": "error", "message": "Доступ запрещен"})
                 continue

            text = msg.get("text", "").strip()[:100] # Ограничение длины
            if not text:
                 continue # Игнорируем пустые сообщения

            # Сохраняем сообщение
            supabase.table("messages").insert({
                "game_id": game_id,
                "user_id": user_id,
                "username": username,
                "text": text,
                "timestamp": time.time()
            }).execute()

            full_msg = {
                "type": "chat",
                "username": username,
                "text": text,
                "timestamp": time.time()
            }

            # Рассылаем сообщение всем подключенным к этой игре (включая отправителя)
            if game_id in active_connections:
                for ref in active_connections[game_id][:]:
                    ws = ref()
                    if ws:
                        try:
                            await ws.send_json(full_msg)
                        except Exception as e:
                            logger.error(f"Ошибка отправки сообщения в WebSocket: {e}")
                            # Удаляем недоступное соединение
                            try:
                                active_connections[game_id].remove(ref)
                            except ValueError:
                                pass # Уже удалено

    except WebSocketDisconnect as e:
        logger.info(f"WebSocket чата отключен для игры {game_id}. Код: {e.code}, Причина: {e.reason}")
    except Exception as e:
        logger.error(f"Ошибка WebSocket чата для игры {game_id}: {e}")

async def broadcast_game_update(game_id: str):
    try:
        game_list = get_game_by_id(game_id)
        if not game_list:
            logger.warning(f"Попытка трансляции обновления для несуществующей игры {game_id}")
            return
        game = game_list[0]
        msg = {"type": "game", **game}
        logger.debug(f"Трансляция обновления игры {game_id} соединениям: {len(active_connections.get(game_id, []))}")

        if game_id in active_connections:
            for ref in active_connections[game_id][:]:
                ws = ref()
                if ws:
                    try:
                        await ws.send_json(msg)
                    except Exception as e:
                        logger.error(f"Ошибка отправки обновления в WebSocket: {e}")
                        # Удаляем недоступное соединение
                        try:
                            active_connections[game_id].remove(ref)
                        except ValueError:
                            pass # Уже удалено
    except Exception as e:
        logger.error(f"Ошибка трансляции обновления игры {game_id}: {e}")

# API endpoints
@app.post("/api/create-game")
async def create_game(request: Request):
    try:
        data = await request.json()
        logger.info(f"Запрос на создание игры. initData: {data.get('initData')[:50]}...") # Логируем начало
        user = validate_init_data(data["initData"], BOT_TOKEN)
        user_id = user["id"]
        username = user.get("first_name", "Unknown")

        game_id = str(uuid.uuid4())[:8]
        while not is_game_id_unique(game_id):
            game_id = str(uuid.uuid4())[:8]

        initial_board = [[None]*3 for _ in range(3)]
        game_data = {
            "id": game_id,
            "creator_id": user_id,
            "creator_name": username,
            "current_turn": user_id,
            "board": initial_board,
            "game_started": False,
            "winner": None,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        supabase.table("games").insert(game_data).execute()

        invite_link = f"http://t.me/Alex_tictactoeBot?start={game_id}"
        logger.info(f"Игра создана: {game_id} пользователем {user_id}")
        return {"game_id": game_id, "invite_link": invite_link}
    except HTTPException:
        # Пробрасываем HTTP ошибки
        raise
    except Exception as e:
        logger.error(f"Неожиданная ошибка создания игры: {e}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")

@app.post("/api/join-game")
async def join_game(request: Request):
    try:
        data = await request.json()
        logger.info(f"Запрос на присоединение к игре {data.get('game_id')}")
        user = validate_init_data(data["initData"], BOT_TOKEN)
        user_id = user["id"]
        username = user.get("first_name", "Unknown")

        game_id = data["game_id"]
        if not game_id or not isinstance(game_id, str) or len(game_id) != 8:
            raise HTTPException(status_code=400, detail="Некорректный ID игры")

        game_list = get_game_by_id(game_id)
        if not game_list:
            raise HTTPException(status_code=404, detail="Игра не найдена")

        game = game_list[0]
        creator_id = game.get("creator_id")
        opponent_id = game.get("opponent_id")

        if opponent_id: # Игра уже заполнена
            raise HTTPException(status_code=400, detail="Игра уже заполнена")
        if str(user_id) == str(creator_id): # Попытка присоединиться к своей же игре
            raise HTTPException(status_code=400, detail="Невозможно присоединиться к своей игре")

        # Обновляем игру, добавляя второго игрока
        update_game(game_id, {
            "opponent_id": user_id,
            "opponent_name": username,
            "game_started": False # Игра не начинается автоматически
        })
        logger.info(f"Пользователь {user_id} присоединился к игре {game_id}")

        # Рассылаем обновление
        await broadcast_game_update(game_id)
        return {"status": "ok"}

    except HTTPException:
        # Пробрасываем HTTP ошибки
        raise
    except Exception as e:
        logger.error(f"Неожиданная ошибка присоединения к игре: {e}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")

@app.post("/api/start-game")
async def start_game(request: Request):
    try:
        data = await request.json()
        logger.info(f"Запрос на начало игры {data.get('game_id')}")
        user = validate_init_data(data["initData"], BOT_TOKEN)
        user_id = user["id"]

        game_id = data["game_id"]
        if not game_id or not isinstance(game_id, str) or len(game_id) != 8:
            raise HTTPException(status_code=400, detail="Некорректный ID игры")

        game_list = get_game_by_id(game_id)
        if not game_list:
            raise HTTPException(status_code=404, detail="Игра не найдена")

        game = game_list[0]
        opponent_id = game.get("opponent_id")
        creator_id = game.get("creator_id")

        if not opponent_id: # Нет второго игрока
            raise HTTPException(status_code=400, detail="Невозможно начать игру: нет второго игрока")
        if game.get("game_started"): # Игра уже началась
            raise HTTPException(status_code=400, detail="Игра уже началась")
        if str(user_id) != str(opponent_id): # Только второй игрок может начать
            raise HTTPException(status_code=403, detail="Только второй игрок может начать игру")

        # Начинаем игру
        update_game(game_id, {
            "game_started": True,
            "current_turn": creator_id # Первым ходит создатель
        })
        logger.info(f"Игра {game_id} начата пользователем {user_id}")

        # Рассылаем обновление
        await broadcast_game_update(game_id)
        return {"status": "ok"}

    except HTTPException:
        # Пробрасываем HTTP ошибки
        raise
    except Exception as e:
        logger.error(f"Неожиданная ошибка начала игры: {e}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")

@app.post("/api/make-move")
async def make_move(request: Request):
    try:
        data = await request.json()
        logger.info(f"Запрос на ход в игре {data.get('game_id')}")
        user = validate_init_data(data["initData"], BOT_TOKEN)
        user_id = user["id"]

        game_id = data["game_id"]
        if not game_id or not isinstance(game_id, str) or len(game_id) != 8:
            raise HTTPException(status_code=400, detail="Некорректный ID игры")

        row = data.get("row")
        col = data.get("col")
        if not isinstance(row, int) or not isinstance(col, int) or not (0 <= row <= 2) or not (0 <= col <= 2):
            raise HTTPException(status_code=400, detail="Некорректные координаты хода")

        game_list = get_game_by_id(game_id)
        if not game_list:
            raise HTTPException(status_code=404, detail="Игра не найдена")

        game = game_list[0]

        if not game.get("game_started"):
            raise HTTPException(status_code=400, detail="Игра ещё не началась")
        if game.get("winner") is not None:
             raise HTTPException(status_code=400, detail="Игра уже завершена")

        if game["current_turn"] != user_id:
            raise HTTPException(status_code=400, detail="Сейчас не ваша очередь ходить")

        symbol = "X" if user_id == game["creator_id"] else "O"
        board = game["board"]

        if board[row][col] is not None:
            raise HTTPException(status_code=400, detail="Эта ячейка уже занята")

        board[row][col] = symbol

        winner = None
        if check_win(board, symbol):
            winner = symbol
        elif all(cell is not None for r in board for cell in r):
            winner = "draw"

        next_turn = None if winner else (
            game["opponent_id"] if user_id == game["creator_id"] else game["creator_id"]
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
            logger.info(f"Игра {game_id} завершена. Победитель: {winner}")

        await broadcast_game_update(game_id)
        logger.info(f"Ход игрока {user_id} в ячейку ({row}, {col}) в игре {game_id} успешно выполнен.")
        return {"status": "ok"}

    except HTTPException:
        # Пробрасываем HTTP ошибки
        raise
    except Exception as e:
        logger.error(f"Неожиданная ошибка хода: {e}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")

@app.post("/api/restart-game")
async def restart_game(request: Request):
    try:
        data = await request.json()
        logger.info(f"Запрос на перезапуск игры {data.get('game_id')}")
        user = validate_init_data(data["initData"], BOT_TOKEN)
        user_id = user["id"]

        old_game_id = data["game_id"]
        if not old_game_id or not isinstance(old_game_id, str) or len(old_game_id) != 8:
            raise HTTPException(status_code=400, detail="Некорректный ID старой игры")

        old_game_list = get_game_by_id(old_game_id)
        if not old_game_list:
            raise HTTPException(status_code=404, detail="Старая игра не найдена")

        old_game = old_game_list[0]

        # Проверяем, что запрос от создателя старой игры
        if str(user_id) != str(old_game["creator_id"]):
            raise HTTPException(status_code=403, detail="Только создатель игры может начать новую")

        # Проверяем, что игра завершена
        if old_game.get("winner") is None:
             raise HTTPException(status_code=400, detail="Невозможно перезапустить незавершённую игру")

        # Создаём новую игру с теми же ID игроков
        new_game_id = str(uuid.uuid4())[:8]
        while not is_game_id_unique(new_game_id):
            new_game_id = str(uuid.uuid4())[:8]

        initial_board = [[None]*3 for _ in range(3)]
        new_game_data = {
            "id": new_game_id,
            "creator_id": old_game["creator_id"],
            "creator_name": old_game["creator_name"],
            "opponent_id": old_game.get("opponent_id"),
            "opponent_name": old_game.get("opponent_name"),
            "current_turn": old_game["creator_id"],
            "board": initial_board,
            "game_started": True, # Новая игра начинается сразу
            "winner": None,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        supabase.table("games").insert(new_game_data).execute()
        logger.info(f"Создана новая игра {new_game_id} для перезапуска из {old_game_id}")

        # Уведомляем клиентов старой игры о переходе на новую
        restart_msg = {"type": "restart", "new_game_id": new_game_id}
        if old_game_id in active_connections:
            for ref in active_connections[old_game_id][:]:
                ws = ref()
                if ws:
                    try:
                        await ws.send_json(restart_msg)
                    except Exception as e:
                        logger.error(f"Ошибка отправки уведомления перезапуска: {e}")
            # Закрываем старые соединения
            for ref in active_connections[old_game_id][:]:
                ws = ref()
                if ws:
                    try:
                        await ws.close(code=1000, reason="Игра перезапущена")
                    except Exception as e:
                        logger.error(f"Ошибка закрытия WebSocket при перезапуске: {e}")
            del active_connections[old_game_id]
            logger.info(f"Старая игра {old_game_id} закрыта, клиенты уведомлены.")

        # Рассылаем обновление новой игры
        await broadcast_game_update(new_game_id)

        logger.info(f"Игра перезапущена: {old_game_id} -> {new_game_id}")
        return {"new_game_id": new_game_id, "status": "ok"}

    except HTTPException:
        # Пробрасываем HTTP ошибки
        raise
    except Exception as e:
        logger.error(f"Неожиданная ошибка перезапуска игры: {e}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")

@app.post("/api/end-game")
async def end_game(request: Request):
    try:
        data = await request.json()
        logger.info(f"Запрос на завершение игры {data.get('game_id')}")
        user = validate_init_data(data["initData"], BOT_TOKEN)
        user_id = user["id"]

        game_id = data["game_id"]
        if not game_id or not isinstance(game_id, str) or len(game_id) != 8:
            raise HTTPException(status_code=400, detail="Некорректный ID игры")

        game_list = get_game_by_id(game_id)
        if not game_list:
            raise HTTPException(status_code=404, detail="Игра не найдена")

        game = game_list[0]

        # Только создатель может завершать игру
        if str(user_id) != str(game["creator_id"]):
            raise HTTPException(status_code=403, detail="Только создатель игры может завершить её")

        # Пометить как завершённую/закрытую (опционально, можно просто оставить как есть)
        # update_game(game_id, {"game_closed": True}) # Пример поля для закрытия

        # Рассылаем обновление (например, статус "Игра завершена создателем")
        # Для простоты, просто рассылаем текущее состояние
        await broadcast_game_update(game_id)
        logger.info(f"Игра {game_id} завершена пользователем {user_id}")
        return {"status": "ok"}

    except HTTPException:
        # Пробрасываем HTTP ошибки
        raise
    except Exception as e:
        logger.error(f"Неожиданная ошибка завершения игры: {e}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")

@app.get("/api/stats")
async def get_stats(request: Request):
    try:
        init_data = request.headers.get("X-Init-Data")
        if not init_data:
            raise HTTPException(status_code=400, detail="Отсутствует X-Init-Data в заголовках")
        user = validate_init_data(init_data, BOT_TOKEN)
        user_id = user["id"]

        res = supabase.table("stats").select("*").eq("user_id", user_id).execute()
        if res.data:
            return res.data[0]
        return {
            "user_id": user_id,
            "username": user.get("first_name", "Unknown"),
            "wins": 0,
            "losses": 0,
            "draws": 0
        }
    except HTTPException:
        # Пробрасываем HTTP ошибки
        raise
    except Exception as e:
        logger.error(f"Неожиданная ошибка получения статистики: {e}")
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
            username = update.message.from_user.first_name

            if text == "/start":
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Создать новую игру", web_app=WebAppInfo(url=f"{WEBHOOK_URL}/mini/index.html"))]
                ])
                await bot.send_message(user_id, "Нажмите, чтобы создать новую игру!", reply_markup=kb)
            elif text.startswith("/start "):
                game_id = text.split(" ", 1)[1].strip()
                if len(game_id) != 8:
                    await bot.send_message(user_id, "❌ Некорректный ID игры.")
                    return {"ok": True}

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

# Эндпоинт для отдачи index.html (если он не нуждается в подстановке URL)
# FastAPI автоматически отдаст файл из папки static/mini/index.html по адресу /mini/index.html
# при монтировании StaticFiles. Если в index.html не нужен динамический URL, этот эндпоинт можно убрать.
# @app.get("/mini/index.html")
# async def serve_index():
#     with open("static/index.html", "r", encoding="utf-8") as f:
#         content = f.read()
#     # Replace the placeholder with the actual URL if needed
#     content = content.replace("{{WEBHOOK_URL}}", WEBHOOK_URL)
#     return content
