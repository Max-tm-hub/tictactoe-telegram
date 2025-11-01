from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Dict, Any
import uuid
import json
import logging
from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

app = FastAPI()
logger = logging.getLogger(__name__)

# Хранилище игр
games: Dict[str, Dict] = {}

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, game_id: str):
        await websocket.accept()
        if game_id not in self.active_connections:
            self.active_connections[game_id] = []
        self.active_connections[game_id].append(websocket)

    def disconnect(self, websocket: WebSocket, game_id: str):
        if game_id in self.active_connections:
            self.active_connections[game_id].remove(websocket)

    async def broadcast(self, data: Dict, game_id: str):
        if game_id in self.active_connections:
            for connection in self.active_connections[game_id]:
                await connection.send_text(json.dumps(data))

manager = ConnectionManager()

@app.websocket("/ws/{game_id}")
async def websocket_endpoint(websocket: WebSocket, game_id: str):
    await manager.connect(websocket, game_id)
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)
            if message["type"] == "move":
                game = games.get(game_id, {})
                game["board"] = message["board"]
                await manager.broadcast(message, game_id)
    except WebSocketDisconnect:
        manager.disconnect(websocket, game_id)

@app.post("/create_game")
async def create_game(creator_id: int):
    game_id = str(uuid.uuid4())
    games[game_id] = {
        "creator_id": creator_id,
        "opponent_id": None,
        "players": [],
        "board": ["", "", "", "", "", "", "", "", ""],
        "current_player": "X",
        "winner": None,
        "status": "waiting"
    }
    return {"game_id": game_id}

@app.post("/join_game/{game_id}")
async def join_game(game_id: str, player_id: int):
    if game_id not in games:
        raise HTTPException(status_code=404, detail="Игра не найдена")

    game = games[game_id]
    if len(game["players"]) >= 2:
        raise HTTPException(status_code=400, detail="Игра уже заполнена")

    game["players"].append(player_id)
    if len(game["players"]) == 2:
        game["status"] = "started"
        await manager.broadcast({"type": "game_start", "game": game}, game_id)

    return {"status": "joined", "game": game}

@app.get("/game/{game_id}")
async def get_game(game_id: str):
    if game_id not in games:
        raise HTTPException(status_code=404, detail="Игра не найдена")
    return games[game_id]

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        bot = Bot(token="YOUR_BOT_TOKEN")
        update_data = await request.json()
        update = Update(**update_data)
        if update.message and update.message.text:
            text = update.message.text.strip()
            user_id = update.message.from_user.id
            if text == "/start":
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Создать новую игру", web_app=WebAppInfo(url="YOUR_WEBHOOK_URL/mini/index.html"))]
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
                    [InlineKeyboardButton(text="Открыть игру", web_app=WebAppInfo(url=f"YOUR_WEBHOOK_URL/mini/index.html?startapp={game_id}"))]
                ])
                await bot.send_message(user_id, "Нажмите кнопку ниже, чтобы присоединиться:", reply_markup=kb)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Ошибка вебхука: {e}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка сервера")

def get_game_by_id(game_id: str) -> List[Dict]:
    return [game for gid, game in games.items() if gid == game_id]
