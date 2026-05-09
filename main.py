import asyncio
import io
import os
from collections import defaultdict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import BufferedInputFile, Message, Update
from fastapi import FastAPI, HTTPException, Request
from PIL import Image, ImageDraw


BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"].rstrip("/")
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
app = FastAPI()

albums = defaultdict(list)
album_tasks = {}


def make_circle_image(image: Image.Image, size: int) -> Image.Image:
    image = image.convert("RGB")

    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2

    image = image.crop((left, top, left + side, top + side))
    image = image.resize((size, size), Image.Resampling.LANCZOS)

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size, size), fill=255)

    result = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    result.paste(image, (0, 0), mask)
    return result


async def build_result_image(messages: list[Message]) -> BufferedInputFile:
    canvas_width = 1200
    canvas_height = 800
    circle_size = 280

    canvas = Image.new("RGBA", (canvas_width, canvas_height), (245, 245, 245, 255))

    positions = [
        (120, 260),
        (460, 260),
        (800, 260),
    ]

    for message, position in zip(sorted(messages, key=lambda m: m.message_id), positions):
        buffer = io.BytesIO()
        await bot.download(message.photo[-1], destination=buffer)
        buffer.seek(0)

        source_image = Image.open(buffer)
        circle_image = make_circle_image(source_image, circle_size)
        canvas.alpha_composite(circle_image, position)

    output = io.BytesIO()
    canvas.save(output, format="PNG")
    output.seek(0)

    return BufferedInputFile(output.read(), filename="result.png")


async def process_album_later(key):
    chat_id, media_group_id = key

    try:
        await asyncio.sleep(2)

        messages = albums.pop(key, [])
        album_tasks.pop(key, None)

        if len(messages) != 3:
            await bot.send_message(
                chat_id,
                f"Получил фото: {len(messages)}. Нужно отправить ровно 3 фото одним альбомом.",
            )
            return

        result_image = await build_result_image(messages)
        await bot.send_photo(chat_id, result_image, caption="Готово")

    except asyncio.CancelledError:
        return
    except Exception as error:
        await bot.send_message(chat_id, f"Не получилось собрать картинку: {error}")


@router.message(F.photo)
async def handle_photo(message: Message):
    if not message.media_group_id:
        await message.answer("Отправьте ровно 3 фото одним альбомом.")
        return

    key = (message.chat.id, message.media_group_id)
    albums[key].append(message)

    old_task = album_tasks.get(key)
    if old_task:
        old_task.cancel()

    album_tasks[key] = asyncio.create_task(process_album_later(key))


@app.on_event("startup")
async def on_startup():
    dp.include_router(router)
    await bot.set_webhook(f"{WEBHOOK_URL}/webhook/{WEBHOOK_SECRET}")


@app.on_event("shutdown")
async def on_shutdown():
    await bot.session.close()


@app.get("/")
async def healthcheck():
    return {"status": "ok"}


@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)

    return {"ok": True}
