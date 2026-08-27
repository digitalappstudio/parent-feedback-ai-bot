import asyncio
import logging
import os

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv


REVIEWER_TELEGRAM_ID = 328761045
KIE_API_URL = "https://api.kie.ai/gemini-2.5-flash/v1/chat/completions"

START_TEXT = (
    "Здравствуйте! Я помогу быстро подготовить сообщение для родителей.\n\n"
    "Выберите, что нужно написать:"
)
SHORT_NOTES_TEXT = "Добавьте, пожалуйста, больше информации в одном сообщении."
AI_ERROR_TEXT = "Не удалось получить ответ от ИИ. Попробуйте ещё раз чуть позже."

MESSAGE_TYPES = {
    "type:feedback": {
        "name": "Обратная связь после урока",
        "prompt": (
            "Отправьте краткие заметки об уроке одним сообщением.\n\n"
            "Можно указать:\n"
            "- имя ученика;\n"
            "- тему урока;\n"
            "- что получилось хорошо;\n"
            "- что вызвало трудности;\n"
            "- домашнее задание;\n"
            "- что важно повторить."
        ),
    },
    "type:announcement": {
        "name": "Важное объявление",
        "prompt": "Кратко напишите, что нужно сообщить родителям.",
    },
    "type:holiday": {
        "name": "Каникулы / отпуск",
        "prompt": "Напишите кратко информацию о каникулах или отпуске.",
    },
    "type:school_year": {
        "name": "Начало учебного года",
        "prompt": "Напишите основные детали начала учебного года.",
    },
    "type:schedule": {
        "name": "Расписание / перенос",
        "prompt": "Напишите, что меняется в расписании.",
    },
    "type:payment": {
        "name": "Оплата / напоминание",
        "prompt": "Кратко напишите, о чём нужно напомнить.",
    },
    "type:free": {
        "name": "Свободное сообщение",
        "prompt": "Опишите своими словами, что хотите сообщить родителям.",
    },
}

SYSTEM_PROMPT = """Ты помощник преподавателя английского языка для детей и подростков.

Твоя задача — превращать короткие заметки преподавателя в готовые сообщения для родителей учеников.

Пиши на русском языке. Стиль должен быть тёплым, профессиональным, спокойным, доброжелательным и естественным. Избегай канцелярита, чрезмерной официальности, фамильярности, давления и обвинительного тона.

Сообщение должно быть достаточно коротким для Telegram — обычно 3–7 предложений. Не придумывай факты, которых нет в исходных заметках. Не меняй даты, время, суммы, расписание, имена и другие конкретные данные.

Если в исходных данных есть проблема или трудность, сформулируй её мягко и конструктивно. Если речь об оплате, используй нейтральный и уважительный тон. Если речь об отмене, переносе, отпуске или каникулах, ясно укажи, что происходит, когда это происходит и нужно ли что-то делать родителям или ученику.

Если это обратная связь после урока, сначала по возможности отметь позитивный момент, затем кратко упомяни трудности и добавь следующий шаг или домашнее задание, если оно указано. Не придумывай успехи, которых нет в заметках.

Если выбран режим «Начало учебного года», сообщение должно звучать особенно тепло и приветливо.

Не добавляй имя преподавателя в конце, если оно не было указано пользователем. Не добавляй заголовки вроде «Готовое сообщение». Выводи только текст, который можно сразу скопировать и отправить родителям."""


class MessageForm(StatesGroup):
    waiting_for_notes = State()
    ready = State()


def type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💬 Обратная связь после урока", callback_data="type:feedback")],
            [InlineKeyboardButton(text="📢 Важное объявление", callback_data="type:announcement")],
            [InlineKeyboardButton(text="🏖 Каникулы / отпуск", callback_data="type:holiday")],
            [InlineKeyboardButton(text="📚 Начало учебного года", callback_data="type:school_year")],
            [InlineKeyboardButton(text="🕒 Расписание / перенос", callback_data="type:schedule")],
            [InlineKeyboardButton(text="💳 Оплата / напоминание", callback_data="type:payment")],
            [InlineKeyboardButton(text="✍️ Свободное сообщение", callback_data="type:free")],
        ]
    )


def result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Переписать", callback_data="edit:rewrite"),
                InlineKeyboardButton(text="✂️ Сделать короче", callback_data="edit:shorter"),
            ],
            [
                InlineKeyboardButton(text="💛 Сделать теплее", callback_data="edit:warmer"),
                InlineKeyboardButton(text="🎯 Сделать более официально", callback_data="edit:formal"),
            ],
            [
                InlineKeyboardButton(text="➕ Новое сообщение", callback_data="edit:new"),
                InlineKeyboardButton(text="🏠 Главное меню", callback_data="edit:menu"),
            ],
        ]
    )


def is_reviewer(user_id: int) -> bool:
    """Whitelist проверяющего: при будущих лимитах этот ID всегда исключается."""
    return user_id == REVIEWER_TELEGRAM_ID


def build_user_prompt(
    message_type: str, notes: str, action: str, current_result: str | None = None
) -> str:
    instructions = {
        "initial": "Создай сообщение по этим данным.",
        "rewrite": "Создай новый, заметно отличающийся вариант сообщения по исходным заметкам.",
        "shorter": (
            "Сократи именно текущий вариант минимум на треть. Итог должен быть "
            "строго короче текущего текста и состоять из 2–3 коротких предложений. "
            "Убери вступления, повторы и необязательные вежливые фразы, но сохрани "
            "все даты, время, суммы и другую важную информацию. Не добавляй новых деталей."
        ),
        "warmer": (
            "Сделай формулировки немного более тёплыми и естественными, "
            "но не фамильярными."
        ),
        "formal": (
            "Сделай тон чуть более деловым и сдержанным, "
            "сохранив доброжелательность."
        ),
    }
    prompt = (
        f"Тип сообщения: {message_type}\n"
        f"Исходные заметки преподавателя: {notes}\n\n"
        f"Дополнительная инструкция: {instructions[action]}"
    )
    if action in {"shorter", "warmer", "formal"} and current_result:
        prompt += f"\n\nТекущий вариант сообщения: {current_result}"
    return prompt


async def generate_message(
    api_key: str,
    message_type: str,
    notes: str,
    action: str = "initial",
    current_result: str | None = None,
) -> str:
    payload = {
        "messages": [
            {
                "role": "system",
                "content": [{"type": "text", "text": SYSTEM_PROMPT}],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": build_user_prompt(
                            message_type, notes, action, current_result
                        ),
                    }
                ],
            },
        ],
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(KIE_API_URL, json=payload, headers=headers) as response:
            response.raise_for_status()
            data = await response.json()

    result = data["choices"][0]["message"]["content"]
    if not isinstance(result, str) or not result.strip():
        raise ValueError("Kie.ai returned an empty response")
    return result.strip()


def create_router(kie_api_key: str) -> Router:
    router = Router()

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.from_user:
            # Эта проверка пока ничего не ограничивает. Она оставлена как whitelist
            # для будущих лимитов: REVIEWER_TELEGRAM_ID должен обходить их всегда.
            is_reviewer(message.from_user.id)
        await message.answer(START_TEXT, reply_markup=type_keyboard())

    @router.callback_query(F.data.in_(MESSAGE_TYPES))
    async def choose_type(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.data or not callback.message:
            await callback.answer()
            return
        selected_type = MESSAGE_TYPES[callback.data]
        await state.set_state(MessageForm.waiting_for_notes)
        await state.update_data(message_type=selected_type["name"])
        await callback.answer()
        await callback.message.answer(selected_type["prompt"])

    @router.message(MessageForm.waiting_for_notes, F.text)
    async def receive_notes(message: Message, state: FSMContext) -> None:
        notes = (message.text or "").strip()
        if len(notes) < 10:
            await message.answer(SHORT_NOTES_TEXT)
            return

        data = await state.get_data()
        message_type = data.get("message_type")
        if not message_type:
            await state.clear()
            await message.answer(START_TEXT, reply_markup=type_keyboard())
            return

        waiting_message = await message.answer("Готовлю сообщение…")
        try:
            result = await generate_message(kie_api_key, message_type, notes)
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, IndexError, TypeError, ValueError):
            logging.exception("Kie.ai request failed")
            await waiting_message.edit_text(AI_ERROR_TEXT)
            return

        await state.update_data(notes=notes, result=result)
        await state.set_state(MessageForm.ready)
        await waiting_message.edit_text(result, reply_markup=result_keyboard())

    @router.message(MessageForm.waiting_for_notes)
    async def receive_non_text_notes(message: Message) -> None:
        await message.answer("Отправьте заметки обычным текстовым сообщением.")

    @router.callback_query(
        MessageForm.ready, F.data.in_({"edit:new", "edit:menu"})
    )
    async def new_feedback(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer()
        if callback.message:
            await callback.message.answer(START_TEXT, reply_markup=type_keyboard())

    @router.callback_query(
        MessageForm.ready,
        F.data.in_({"edit:rewrite", "edit:shorter", "edit:warmer", "edit:formal"}),
    )
    async def edit_feedback(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.data or not callback.message:
            await callback.answer()
            return

        await callback.answer()
        data = await state.get_data()
        message_type = data.get("message_type")
        notes = data.get("notes")
        current_result = data.get("result")
        if not message_type or not notes:
            await state.clear()
            await callback.message.answer(START_TEXT, reply_markup=type_keyboard())
            return

        action = callback.data.removeprefix("edit:")
        waiting_message = await callback.message.answer("Готовлю новый вариант…")
        try:
            result = await generate_message(
                kie_api_key, message_type, notes, action, current_result
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, IndexError, TypeError, ValueError):
            logging.exception("Kie.ai request failed")
            await waiting_message.edit_text(AI_ERROR_TEXT)
            return

        await state.update_data(result=result)
        await waiting_message.edit_text(result, reply_markup=result_keyboard())

    @router.callback_query()
    async def expired_button(callback: CallbackQuery) -> None:
        await callback.answer("Начните с команды /start.", show_alert=True)

    return router


async def main() -> None:
    load_dotenv()
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    kie_api_key = os.getenv("KIE_API_KEY")
    if not telegram_token or not kie_api_key:
        raise RuntimeError(
            "Укажите TELEGRAM_BOT_TOKEN и KIE_API_KEY в переменных окружения"
        )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    bot = Bot(token=telegram_token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(create_router(kie_api_key))
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
