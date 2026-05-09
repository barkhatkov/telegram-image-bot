import asyncio
import io
import os
from collections import defaultdict

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
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
last_collages = {}
pending_adjustments = {}

DPI = 300
CANVAS_WIDTH_MM = 99
CANVAS_HEIGHT_MM = 148
CIRCLE_DIAMETER_MM = 54
DRAW_TEST_BORDER = True
DEFAULT_ZOOM = 1.12
MIN_ZOOM = 0.5
MAX_ZOOM = 2.0


def mm_to_px(value_mm: float) -> int:
    return round(value_mm / 25.4 * DPI)


CANVAS_WIDTH_PX = mm_to_px(CANVAS_WIDTH_MM)
CANVAS_HEIGHT_PX = mm_to_px(CANVAS_HEIGHT_MM)
CIRCLE_DIAMETER_PX = mm_to_px(CIRCLE_DIAMETER_MM)
CIRCLE_POSITIONS_PX = [
    (mm_to_px(5), mm_to_px(4)),
    (mm_to_px(43), mm_to_px(43)),
    (mm_to_px((CANVAS_WIDTH_MM - CIRCLE_DIAMETER_MM) / 2), mm_to_px(94)),
]
PHOTO_LABELS = ["верхнее", "среднее", "нижнее"]


def make_default_adjustments() -> list[dict]:
    return [
        {"offset_x": 0, "offset_y": 0, "zoom": DEFAULT_ZOOM},
        {"offset_x": 0, "offset_y": 0, "zoom": DEFAULT_ZOOM},
        {"offset_x": 0, "offset_y": 0, "zoom": DEFAULT_ZOOM},
    ]


def apply_adjustment(adjustment: dict, action: str, percent: int) -> None:
    ratio = percent / 100
    step = round(CIRCLE_DIAMETER_PX * ratio)

    if action == "up":
        adjustment["offset_y"] -= step
    elif action == "down":
        adjustment["offset_y"] += step
    elif action == "left":
        adjustment["offset_x"] -= step
    elif action == "right":
        adjustment["offset_x"] += step
    elif action == "bigger":
        adjustment["zoom"] *= 1 + ratio
    elif action == "smaller":
        adjustment["zoom"] /= 1 + ratio

    adjustment["zoom"] = max(MIN_ZOOM, min(MAX_ZOOM, adjustment["zoom"]))


def photo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Фото верхнее", callback_data="photo:0")],
            [InlineKeyboardButton(text="Фото среднее", callback_data="photo:1")],
            [InlineKeyboardButton(text="Фото нижнее", callback_data="photo:2")],
        ]
    )


def adjustment_keyboard(photo_index: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Выше", callback_data=f"adjust:{photo_index}:up")],
            [InlineKeyboardButton(text="Ниже", callback_data=f"adjust:{photo_index}:down")],
            [InlineKeyboardButton(text="Левее", callback_data=f"adjust:{photo_index}:left")],
            [InlineKeyboardButton(text="Правее", callback_data=f"adjust:{photo_index}:right")],
            [InlineKeyboardButton(text="Крупнее", callback_data=f"adjust:{photo_index}:bigger")],
            [InlineKeyboardButton(text="Меньше", callback_data=f"adjust:{photo_index}:smaller")],
            [InlineKeyboardButton(text="Назад к фото", callback_data="photos")],
        ]
    )


ACTION_NAMES = {
    "up": "выше",
    "down": "ниже",
    "left": "левее",
    "right": "правее",
    "bigger": "крупнее",
    "smaller": "меньше",
}


def make_circle_image(image: Image.Image, size: int, adjustment: dict) -> Image.Image:
    image = image.convert("RGB")

    width, height = image.size
    zoom = max(MIN_ZOOM, min(MAX_ZOOM, adjustment["zoom"]))
    base_scale = max(size / width, size / height)
    scaled_width = round(width * base_scale * zoom)
    scaled_height = round(height * base_scale * zoom)
    image = image.resize((scaled_width, scaled_height), Image.Resampling.LANCZOS)

    max_offset_x = max(0, (scaled_width - size) // 2)
    max_offset_y = max(0, (scaled_height - size) // 2)
    offset_x = max(-max_offset_x, min(max_offset_x, adjustment["offset_x"]))
    offset_y = max(-max_offset_y, min(max_offset_y, adjustment["offset_y"]))

    left = (size - scaled_width) // 2 + offset_x
    top = (size - scaled_height) // 2 + offset_y
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


async def process_album_later(key):
    chat_id, media_group_id = key

    try:
        await asyncio.sleep(2)

        messages = albums.pop(key, [])
        album_tasks.pop(key, None)

        if len(messages) < 3:
            await bot.send_message(
                chat_id,
                "Необходимо загрузить три фотографии одновременно, а не последовательно.",
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
        pending_adjustments.pop(chat_id, None)

        result_image = await build_result_image(file_ids, adjustments)
        await bot.send_document(chat_id, result_image, caption="Готово")
        await bot.send_message(
            chat_id,
            "Можно настроить положение фото. Выберите фото:",
            reply_markup=photo_keyboard(),
        )

    except asyncio.CancelledError:
        return
    except Exception as error:
        await bot.send_message(chat_id, f"Не получилось собрать картинку: {error}")


@router.message(CommandStart())
async def handle_start(message: Message):
    pending_adjustments.pop(message.chat.id, None)
    await message.answer(
        "Привет, загружай 3 фотографии для коллажа.\n"
        "После результата можно будет настроить каждое фото кнопками.",
    )


@router.callback_query(F.data == "photos")
async def handle_photos_button(callback: CallbackQuery):
    pending_adjustments.pop(callback.message.chat.id, None)
    await callback.message.edit_text(
        "Выберите фото для настройки:",
        reply_markup=photo_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("photo:"))
async def handle_photo_button(callback: CallbackQuery):
    pending_adjustments.pop(callback.message.chat.id, None)
    photo_index = int(callback.data.split(":")[1])
    await callback.message.edit_text(
        f"Фото {PHOTO_LABELS[photo_index]}. Выберите, что изменить:",
        reply_markup=adjustment_keyboard(photo_index),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adjust:"))
async def handle_adjustment_button(callback: CallbackQuery):
    _, photo_index_text, action = callback.data.split(":")
    photo_index = int(photo_index_text)

    collage = last_collages.get(callback.message.chat.id)
    if not collage:
        await callback.answer("Сначала загрузите 3 фотографии.", show_alert=True)
        return

    pending_adjustments[callback.message.chat.id] = {
        "photo_index": photo_index,
        "action": action,
    }
    await callback.message.edit_text(
        f"Фото {PHOTO_LABELS[photo_index]}: {ACTION_NAMES[action]}.\n"
        "Введите процент от 1 до 99 следующим сообщением.",
        reply_markup=adjustment_keyboard(photo_index),
    )
    await callback.answer()


@router.message(F.text)
async def handle_text(message: Message):
    collage = last_collages.get(message.chat.id)
    if not collage:
        await message.answer("Сначала загружай 3 фотографии для коллажа.")
        return

    pending_adjustment = pending_adjustments.get(message.chat.id)
    if pending_adjustment:
        percent_text = (message.text or "").strip().replace("%", "")
        if not percent_text.isdigit():
            await message.answer("Введите число от 1 до 99, например: 10")
            return

        percent = int(percent_text)
        if percent < 1 or percent > 99:
            await message.answer("Процент должен быть от 1 до 99.")
            return

        pending_adjustments.pop(message.chat.id, None)
        photo_index = pending_adjustment["photo_index"]
        action = pending_adjustment["action"]
        apply_adjustment(collage["adjustments"][photo_index], action, percent)

        result_image = await build_result_image(collage["file_ids"], collage["adjustments"])
        await bot.send_document(message.chat.id, result_image, caption="Готово, обновил")
        await message.answer(
            f"Фото {PHOTO_LABELS[photo_index]}. Выберите следующее действие:",
            reply_markup=adjustment_keyboard(photo_index),
        )
        return

    await message.answer(
        "Для настройки используйте кнопки: Фото верхнее, Фото среднее или Фото нижнее.",
        reply_markup=photo_keyboard(),
    )


@router.message(F.photo)
async def handle_photo(message: Message):
    if not message.media_group_id:
        await message.answer("Необходимо загрузить три фотографии одновременно, а не последовательно.")
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
