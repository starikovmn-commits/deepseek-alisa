"""Alice skill backend with a task queue for the home computer.

Flow:
  1. Alice sends a phrase  -> POST "/"   : the task is queued, Alice hears "приняла".
  2. The PC polls          -> GET  /pending : the oldest task is handed out once.
  3. The PC answers        -> POST /answer : the answer is stored for the next turn.
  4. Alice asks again      -> POST "/"   : the stored answer is spoken and cleared.

A single owner uses the skill, so one queue and one answer slot are enough.
"""

import os
import time
from collections import OrderedDict

from fastapi import FastAPI, Request

app = FastAPI()

TASK_TTL_SECONDS = 180
ANSWER_TTL_SECONDS = 900

PENDING: "OrderedDict[str, dict]" = OrderedDict()
ANSWER: dict = {"text": None, "at": 0.0}


def _gc() -> None:
    now = time.time()
    while PENDING:
        task_id, task = next(iter(PENDING.items()))
        if now - task["at"] <= TASK_TTL_SECONDS:
            break
        PENDING.pop(task_id, None)
    if ANSWER["text"] is not None and now - ANSWER["at"] > ANSWER_TTL_SECONDS:
        ANSWER["text"] = None
        ANSWER["at"] = 0.0


def _alice_response(body: dict, text: str) -> dict:
    return {
        "version": body.get("version", "1.0"),
        "session": body.get("session", {}),
        "response": {"end_session": False, "text": text},
    }


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

    asked_for_result = any(word in lowered for word in RESULT_WORDS)
    if ANSWER["text"] is not None and (asked_for_result or not utterance):
        text = ANSWER["text"]
        ANSWER["text"] = None
        ANSWER["at"] = 0.0
        return _alice_response(body, text)

    if not utterance:
        return _alice_response(body, "Слушаю.")

    task_id = f"t{int(time.time() * 1000)}"
    PENDING[task_id] = {"text": utterance, "at": time.time()}
    prefix = "Есть готовый ответ, скажи «что там». " if ANSWER["text"] is not None else ""
    return _alice_response(body, f"{prefix}Приняла: {utterance}. Скажи «что там», когда будет готово.")


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
    return {"ok": True, "queued": True}


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "pending": len(PENDING), "has_answer": ANSWER["text"] is not None}
