"""Alice skill backend with a task queue for the home computer.

Flow:
  1. Alice sends a phrase  -> POST "/"   : simple answers (time, date) are computed here at
     once; anything else is queued, and the handler waits a few seconds for the PC so a fast
     command (browser, disk space) is answered in the same turn.
  2. The PC polls          -> GET  /pending : the oldest task is handed out once.
  3. The PC answers        -> POST /answer : the answer is stored for the next turn.
  4. Alice asks again      -> POST "/"   : the stored answer is spoken and cleared.

Alice keeps listening for only a few seconds after a reply, so the two-turn flow
("приняла" -> wait -> "что там") is a fallback, not the normal path.
"""

import asyncio
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request

app = FastAPI()

TASK_TTL_SECONDS = 180
ANSWER_TTL_SECONDS = 900
# Alice gives a skill 4.5 seconds; keep a margin for the network round trip.
WAIT_FOR_PC_SECONDS = 3.8

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
ANSWER: dict = {"text": None, "at": 0.0, "task": None}

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


def _clear_answer() -> None:
    ANSWER["text"] = None
    ANSWER["at"] = 0.0
    ANSWER["task"] = None


def _alice_response(body: dict, text: str) -> dict:
    return {
        "version": body.get("version", "1.0"),
        "session": body.get("session", {}),
        "response": {"end_session": False, "text": text},
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

    # Wait for the PC: fast local commands come back in well under a second, and answering
    # them in this same turn is what keeps the Alice dialog natural.
    deadline = time.time() + WAIT_FOR_PC_SECONDS
    while time.time() < deadline:
        if ANSWER["text"] is not None and ANSWER["task"] == task_id:
            text = ANSWER["text"]
            _clear_answer()
            return _alice_response(body, text)
        await asyncio.sleep(0.15)

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
    ANSWER["task"] = body.get("id")
    return {"ok": True, "queued": True}


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "pending": len(PENDING), "has_answer": ANSWER["text"] is not None}
