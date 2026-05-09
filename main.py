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
pending_single_photos = defaultdict(list)
last_collages = {}

DPI = 300
CANVAS_WIDTH_MM = 99
CANVAS_HEIGHT_MM = 148
CIRCLE_DIAMETER_MM = 54
DRAW_TEST_BORDER = True
DEFAULT_ZOOM = 1.12
MOVE_STEP_PX = 45
ZOOM_STEP = 0.12
MIN_ZOOM = 1.0
MAX_ZOOM = 2.0


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


def make_default_adjustments() -> list[dict]:
    return [
        {"offset_x": 0, "offset_y": 0, "zoom": DEFAULT_ZOOM},
        {"offset_x": 0, "offset_y": 0, "zoom": DEFAULT_ZOOM},
        {"offset_x": 0, "offset_y": 0, "zoom": DEFAULT_ZOOM},
    ]


def make_circle_image(image: Image.Image, size: int, adjustment: dict) -> Image.Image:
    image = image.convert("RGB")

    width, height = image.size
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2

    image = image.crop((left, top, left + side, top + side))

    zoom = max(MIN_ZOOM, min(MAX_ZOOM, adjustment["zoom"]))
    scaled_size = round(size * zoom)
    image = image.resize((scaled_size, scaled_size), Image.Resampling.LANCZOS)

    max_offset = max(0, (scaled_size - size) // 2)
    offset_x = max(-max_offset, min(max_offset, adjustment["offset_x"]))
    offset_y = max(-max_offset, min(max_offset, adjustment["offset_y"]))

    left = (size - scaled_size) // 2 + offset_x
    top = (size - scaled_size) // 2 + offset_y
    square = Image.new("RGB", (size, size), (255, 255, 255))
    square.paste(image, (left, top))

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size, size), fill=255)

    result = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    result.paste(square, (0, 0), mask)
    return result


async def build_result_image(file_ids: list[str], adjustments: list[dict]) -> BufferedInputFile:
    canvas = Image.new("RGBA", (CANVAS_WIDTH_PX, CANVAS_HEIGHT_PX), (255, 255, 255, 255))

    for file_id, position, adjustment in zip(file_ids, CIRCLE_POSITIONS_PX, adjustments):
        buffer = io.BytesIO()
        await bot.download(file_id, destination=buffer)
        buffer.seek(0)

        source_image = Image.open(buffer)
        circle_image = make_circle_image(source_image, CIRCLE_DIAMETER_PX, adjustment)
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


def parse_adjustment_request(text: str, current_adjustments: list[dict]) -> list[dict] | None:
    text = text.lower().replace(",", " ")
    words = text.split()
    adjustments = [item.copy() for item in current_adjustments]
    changed = False
    selected_indexes = [0, 1, 2]

    for word in words:
        clean_word = word.strip(".:;!?")
        if clean_word in {"1", "1й", "1-й", "первый", "первую", "первое"}:
            selected_indexes = [0]
            continue
        elif clean_word in {"2", "2й", "2-й", "второй", "вторую", "второе"}:
            selected_indexes = [1]
            continue
        elif clean_word in {"3", "3й", "3-й", "третий", "третью", "третье"}:
            selected_indexes = [2]
            continue

        if clean_word in {"выше", "вверх", "подними", "поднять"}:
            for index in selected_indexes:
                adjustments[index]["offset_y"] -= MOVE_STEP_PX
            changed = True
        elif clean_word in {"ниже", "вниз", "опусти", "опустить"}:
            for index in selected_indexes:
                adjustments[index]["offset_y"] += MOVE_STEP_PX
            changed = True
        elif clean_word in {"левее", "влево", "налево"}:
            for index in selected_indexes:
                adjustments[index]["offset_x"] -= MOVE_STEP_PX
            changed = True
        elif clean_word in {"правее", "вправо", "направо"}:
            for index in selected_indexes:
                adjustments[index]["offset_x"] += MOVE_STEP_PX
            changed = True
        elif clean_word in {"крупнее", "увеличь", "увеличить", "приблизь", "приблизить"}:
            for index in selected_indexes:
                adjustments[index]["zoom"] += ZOOM_STEP
            changed = True
        elif clean_word in {"мельче", "уменьши", "уменьшить", "отдали", "отдалить"}:
            for index in selected_indexes:
                adjustments[index]["zoom"] -= ZOOM_STEP
            changed = True

    for index in range(3):
        adjustment = adjustments[index]
        adjustment["zoom"] = max(MIN_ZOOM, min(MAX_ZOOM, adjustment["zoom"]))

    return adjustments if changed else None


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

        file_ids = [
            message.photo[-1].file_id
            for message in sorted(messages, key=lambda message: message.message_id)
        ]
        adjustments = make_default_adjustments()
        last_collages[chat_id] = {"file_ids": file_ids, "adjustments": adjustments}

        result_image = await build_result_image(file_ids, adjustments)
        await bot.send_document(chat_id, result_image, caption="Готово")

    except asyncio.CancelledError:
        return
    except Exception as error:
        await bot.send_message(chat_id, f"Не получилось собрать картинку: {error}")


@router.message(CommandStart())
async def handle_start(message: Message):
    await message.answer(
        "Привет, загружай 3 фотографии для коллажа.\n"
        "После результата можно написать: 1 выше, 2 крупнее, 3 правее."
    )


@router.message(F.text)
async def handle_text(message: Message):
    collage = last_collages.get(message.chat.id)
    if not collage:
        await message.answer("Сначала загружай 3 фотографии для коллажа.")
        return

    adjustments = parse_adjustment_request(message.text or "", collage["adjustments"])
    if adjustments is None:
        await message.answer(
            "Не понял настройку. Можно написать, например: 1 выше, 2 крупнее, 3 правее."
        )
        return

    collage["adjustments"] = adjustments
    result_image = await build_result_image(collage["file_ids"], adjustments)
    await bot.send_document(message.chat.id, result_image, caption="Готово, обновил")


@router.message(F.photo)
async def handle_photo(message: Message):
    if not message.media_group_id:
        pending_single_photos[message.chat.id].append(message.photo[-1].file_id)
        photo_count = len(pending_single_photos[message.chat.id])

        if photo_count < 3:
            await message.answer(f"Получил фото: {photo_count}. Жду еще: {3 - photo_count}.")
            return

        file_ids = pending_single_photos.pop(message.chat.id)
        adjustments = make_default_adjustments()
        last_collages[message.chat.id] = {"file_ids": file_ids, "adjustments": adjustments}

        result_image = await build_result_image(file_ids, adjustments)
        await bot.send_document(message.chat.id, result_image, caption="Готово")
        return

    pending_single_photos.pop(message.chat.id, None)

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
