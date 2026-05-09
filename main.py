import asyncio
import io
import os
from collections import defaultdict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
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

DPI = 300
CANVAS_WIDTH_MM = 99
CANVAS_HEIGHT_MM = 148
CIRCLE_DIAMETER_MM = 54
DRAW_TEST_BORDER = True


def mm_to_px(value_mm: float) -> int:
    return round(value_mm / 25.4 * DPI)


CANVAS_WIDTH_PX = mm_to_px(CANVAS_WIDTH_MM)
CANVAS_HEIGHT_PX = mm_to_px(CANVAS_HEIGHT_MM)
CIRCLE_DIAMETER_PX = mm_to_px(CIRCLE_DIAMETER_MM)
CIRCLE_POSITIONS_PX = [
    (mm_to_px(0), mm_to_px(0)),
    (mm_to_px((CANVAS_WIDTH_MM - CIRCLE_DIAMETER_MM) / 2), mm_to_px(CANVAS_HEIGHT_MM - CIRCLE_DIAMETER_MM)),
    (mm_to_px(CANVAS_WIDTH_MM - CIRCLE_DIAMETER_MM), mm_to_px(30)),
]


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
    canvas = Image.new("RGBA", (CANVAS_WIDTH_PX, CANVAS_HEIGHT_PX), (255, 255, 255, 255))

    for message, position in zip(sorted(messages, key=lambda m: m.message_id), CIRCLE_POSITIONS_PX):
        buffer = io.BytesIO()
        await bot.download(message.photo[-1], destination=buffer)
        buffer.seek(0)

        source_image = Image.open(buffer)
        circle_image = make_circle_image(source_image, CIRCLE_DIAMETER_PX)
        canvas.alpha_composite(circle_image, position)

    if DRAW_TEST_BORDER:
        draw = ImageDraw.Draw(canvas)
        draw.rectangle(
            (0, 0, CANVAS_WIDTH_PX - 1, CANVAS_HEIGHT_PX - 1),
            outline=(220, 30, 30, 255),
            width=3,
        )

    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="PNG", dpi=(DPI, DPI))
    output.seek(0)

    return BufferedInputFile(output.read(), filename="result.png")


async def process_album_later(key):
    chat_id, media_group_id = key

    try:
        await asyncio.sleep(2)

        messages = albums.pop(key, [])
        album_tasks.pop(key, None)

        if len(messages) < 3:
            missing_count = 3 - len(messages)
            await bot.send_message(
                chat_id,
                f"Получил фото: {len(messages)}. Жду еще: {missing_count}.",
            )
            return

        if len(messages) > 3:
            await bot.send_message(
                chat_id,
                f"Получил фото: {len(messages)}. Нужно отправить ровно 3 фото одним альбомом.",
            )
            return

        result_image = await build_result_image(messages)
        await bot.send_document(chat_id, result_image, caption="Готово")

    except asyncio.CancelledError:
        return
    except Exception as error:
        await bot.send_message(chat_id, f"Не получилось собрать картинку: {error}")


@router.message(CommandStart())
async def handle_start(message: Message):
    await message.answer("Привет, загружай 3 фотографии для коллажа.")


@router.message(F.photo)
async def handle_photo(message: Message):
    if not message.media_group_id:
        await message.answer("Получил фото: 1. Жду еще: 2.")
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
