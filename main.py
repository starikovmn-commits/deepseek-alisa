"""Alice skill backend with a task queue for the home computer.

Flow:
  1. Alice sends a phrase  -> POST "/"   : simple answers (time, date) are computed
     here at once; everything else is queued and Alice hears "приняла".
  2. The PC polls          -> GET  /pending : the oldest task is handed out once.
  3. The PC answers        -> POST /answer : the answer is stored for the next turn.
  4. Alice asks again      -> POST "/"   : the stored answer is spoken and cleared.

Simple questions must be answered in the same turn: Alice keeps listening for only
a few seconds after a reply, so a two-turn flow ("приняла" -> wait -> "что там")
only works for tasks the PC can finish quickly.

A single owner uses the skill, so one queue and one answer slot are enough.
"""

import asyncio
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request

app = FastAPI()

TASK_TTL_SECONDS = 180
ANSWER_TTL_SECONDS = 900

# Alice keeps the session open for a few seconds after the reply, so a fast PC
# answer is delivered in the SAME turn; only slow ones need "что там".
WAIT_SECONDS = 3.6
WAIT_STEP_SECONDS = 0.12

# Moscow has been UTC+3 all year round since 2014, so a fixed offset needs no tzdata.
MSK = timezone(timedelta(hours=3))

MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
)

PENDING: "OrderedDict[str, dict]" = OrderedDict()
# One answer slot, but each answer remembers which task it belongs to: a request
# waiting in the same turn takes only its own answer, never someone else's.
ANSWER: dict = {"text": None, "at": 0.0, "id": None}


def _clear_answer() -> None:
    ANSWER["text"] = None
    ANSWER["at"] = 0.0
    ANSWER["id"] = None

TIME_WORDS = (
    "который час", "сколько время", "сколько времени", "сколько сейчас времени",
    "время сейчас", "чей час",
)
DATE_WORDS = ("какое сегодня число", "какое число", "какая сегодня дата", "какое сегодня")
WEEKDAY_WORDS = ("какой сегодня день", "какой день недели", "какой сегодня день недели")


def _gc() -> None:
    now = time.time()
    while PENDING:
        task_id, task = next(iter(PENDING.items()))
        if now - task["at"] <= TASK_TTL_SECONDS:
            break
        PENDING.pop(task_id, None)
    if ANSWER["text"] is not None and now - ANSWER["at"] > ANSWER_TTL_SECONDS:
        _clear_answer()


def _alice_response(body: dict, text: str, end: bool = False) -> dict:
    return {
        "version": body.get("version", "1.0"),
        "session": body.get("session", {}),
        "response": {"end_session": end, "text": text},
    }


def fast_answer(utterance: str) -> "str | None":
    """Answer questions that need no computer at all, in the same turn."""
    low = utterance.lower()
    if any(word in low for word in TIME_WORDS):
        return datetime.now(MSK).strftime("Сейчас %H:%M")
    if any(word in low for word in DATE_WORDS):
        now = datetime.now(MSK)
        return f"Сегодня {now.day} {MONTHS[now.month - 1]} {now.year} года."
    if any(word in low for word in WEEKDAY_WORDS):
        now = datetime.now(MSK)
        return f"Сегодня {WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]}."
    return None


RESULT_WORDS = ("что там", "готово", "результат", "что слышно", "отчёт", "отчет")

# Voice command that hands control back to Alice: the skill keeps the session open,
# so without this the user stays "inside" it until the platform times out.
EXIT_WORDS = (
    "верни алису", "вернись к алисе", "верни меня алисе", "переключи на алису",
    "хватит", "отключись", "закончим", "выход", "до свидания",
)


@app.post("/")
async def alice(request: Request) -> dict:
    body = await request.json()
    utterance = (
        body.get("request", {}).get("original_utterance")
        or body.get("request", {}).get("command")
        or ""
    ).strip()
    _gc()
    lowered = utterance.lower()

    if utterance:
        quick = fast_answer(utterance)
        if quick:
            return _alice_response(body, quick)
        if any(word in lowered for word in EXIT_WORDS):
            return _alice_response(body, "Хорошо, возвращаю Алису.", end=True)

    asked_for_result = any(word in lowered for word in RESULT_WORDS)
    if ANSWER["text"] is not None and (asked_for_result or not utterance):
        text = ANSWER["text"]
        _clear_answer()
        return _alice_response(body, text)

    if asked_for_result and ANSWER["text"] is None:
        return _alice_response(body, "Ещё думаю. Спроси «что там» чуть позже.")

    if not utterance:
        return _alice_response(body, "Слушаю.")

    task_id = f"t{int(time.time() * 1000)}"
    PENDING[task_id] = {"text": utterance, "at": time.time()}

    # Wait briefly for the PC: a fast answer lands in this very turn, so the user
    # asks one phrase instead of two. Slow work still falls back to "что там".
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        if ANSWER["text"] is not None and ANSWER["id"] in (None, task_id):
            text = ANSWER["text"]
            _clear_answer()
            PENDING.pop(task_id, None)
            return _alice_response(body, text)
        await asyncio.sleep(WAIT_STEP_SECONDS)

    return _alice_response(body, "Приняла. Скажи «что там» через несколько секунд.")


@app.get("/pending")
async def pending() -> dict:
    _gc()
    if not PENDING:
        return {"id": None, "text": None}
    task_id, task = next(iter(PENDING.items()))
    PENDING.pop(task_id, None)
    return {"id": task_id, "text": task["text"]}


@app.post("/answer")
async def answer(request: Request) -> dict:
    body = await request.json()
    text = str(body.get("text") or "").strip()
    if not text:
        return {"ok": False, "reason": "empty answer"}
    ANSWER["text"] = text[:900]
    ANSWER["at"] = time.time()
    ANSWER["id"] = str(body.get("id") or "").strip() or None
    return {"ok": True, "queued": True}


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "pending": len(PENDING), "has_answer": ANSWER["text"] is not None}
