import os
from fastapi import FastAPI, Request
import httpx

app = FastAPI()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

@app.post("/")
async def main(request: Request):
    body = await request.json()
    user_text = body["request"]["original_utterance"]
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "openrouter/free",
                "messages": [{"role": "user", "content": user_text}],
            },
            timeout=60.0,
        )
    
    resp_json = response.json()
    if "choices" in resp_json:
        answer = resp_json["choices"][0]["message"]["content"]
    else:
        answer = f"Ошибка API: {resp_json}"
    
    return {
        "version": body["version"],
        "session": body["session"],
        "response": {"end_session": False, "text": answer},
    }
