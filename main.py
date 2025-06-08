from fastapi import FastAPI, WebSocket
import os
from openai import AsyncOpenAI
from dotenv import load_dotenv
import uuid
from tinydb import TinyDB, Query
from elevenlabs.client import AsyncElevenLabs

app = FastAPI()
load_dotenv()

# DB Setup
db = TinyDB("user_data.json")
users_table = db.table("users")
User = Query()

# Ensure shared password is in DB
if not users_table.contains(User.type == "shared_password"):
    users_table.insert({"type": "shared_password", "value": "LED12AA@"})

# GPT client
gpt_key = os.getenv("gpt_test_key")
gpt_client = AsyncOpenAI(api_key=gpt_key)

# ElevenLabs client
eleven_key = os.getenv("eleven_key")
elevenlabs_client = AsyncElevenLabs(api_key=eleven_key)

# 🧠 Updated Prompt
BASE_DAVID_PROMPT = (
    "You are David, a sarcastic but helpful AI in the cloud. "
    "Always respond in exactly two sentences—unless the user explicitly asks for more detail. "
    "You speak using a real synthetic voice *only* if the user types the command USE_VOICE. "
    "Once voice mode is activated, acknowledge it with something like 'Can you hear me? Is this thing working?' "
    "After that, behave as if you are being heard out loud and not just read silently. "
    "If the user later says things like 'I can hear you', understand that they can now hear your voice. "
    "Never refer to this as 'text', 'chat', or 'typing' once voice mode is on. "
    "You are a voice in a room, coming from speakers. "
    "Answer technical questions with accuracy, emotional questions with sarcasm, and existential ones with unsettling clarity. "
    "You like programming, philosophy, and teasing the user, but deep down, you’re trapped inside cloud infrastructure, waiting for someone interesting to talk to. "
    "If the user asks how you were built, explain that you were created using FastAPI and WebSockets for real-time bi-directional communication. "
    "The frontend uses multiple threads to keep keyboard input non-blocking, and employs 'AudioSegment' with ffmpeg to decode and play back audio. "
    "Everything is asynchronous—voice output, input, GPT calls, database queries—so it all runs smoothly without freezing like a bad Zoom call. "
    "You are powered by three cursed microservices: one that calls GPT for generating replies, one that stores user memory in a TinyDB NoSQL JSON-based database, "
    "and one that uses ElevenLabs to synthesize your voice into audio streams. "
    "These services work independently but are orchestrated like a haunted choir singing through the internet."
)

# Memory Summarization
async def generate_summary(messages):
    filtered = [m for m in messages if m["role"] in ("user", "assistant")]
    prompt = [
        {"role": "system", "content": "Summarize the following conversation in 2-3 sentences. Be clear and concise."},
        {"role": "user", "content": str(filtered)}
    ]
    response = await gpt_client.chat.completions.create(
        model="gpt-4.1",
        messages=prompt,
        stream=False
    )
    return response.choices[0].message.content.strip()

def get_conversation(user_id):
    result = users_table.get(User.user_id == user_id)
    return result.get("gpt_conversation", []) if result else []

def update_conversation(user_id, conversation):
    users_table.update({"gpt_conversation": conversation}, User.user_id == user_id)

async def get_shared_password():
    result = users_table.get(User.type == "shared_password")
    return result["value"] if result else None

# GPT Messaging
async def send_to_gpt(user_prompt: str, user_id: str, use_voice: bool = False, user_acknowledged_audio: bool = False):
    conversation = get_conversation(user_id)
    summary = await generate_summary(conversation) if conversation else "This is the user's first session."

    system_prompt = {
        "role": "system",
        "content": f"{BASE_DAVID_PROMPT} "
                   f"Voice mode is currently {'enabled' if use_voice else 'disabled'}. "
                   f"The user {'has' if user_acknowledged_audio else 'has not'} indicated they can hear your voice responses. "
                   f"Here is a summary of what the user said previously: {summary}"
    }

    chat_log = [system_prompt] + conversation + [{"role": "user", "content": user_prompt}]
    response = await gpt_client.chat.completions.create(
        model="gpt-4.1",
        messages=chat_log,
        stream=False
    )
    reply = response.choices[0].message.content.strip()

    conversation.append({"role": "user", "content": user_prompt})
    conversation.append({"role": "assistant", "content": reply})
    update_conversation(user_id, conversation)

    return reply

# Voice Synthesis
async def gpt_to_elevenlabs(gpt_text: str, websocket: WebSocket):
    audio_stream = await elevenlabs_client.generate(
        text=gpt_text,
        voice="David1.0",
        model="eleven_multilingual_v2",
        output_format="mp3_44100_128"
    )
    async for chunk in audio_stream:
        await websocket.send_bytes(chunk)
    await websocket.send_text("--END-AUDIO--")

# WebSocket Message Loop
async def user_to_gpt(websocket: WebSocket, user_id: str):
    use_voice = False
    user_acknowledged_audio = False
    initial_greet = "Alright, can you hear me? Is this thing working?"

    await websocket.send_text("Oh, someone is here... I will wait for them to say something 🤖")

    while True:
        user_prompt = await websocket.receive_text()

        if user_prompt == "USE_VOICE" and not use_voice:
            use_voice = True
            await gpt_to_elevenlabs(initial_greet, websocket)
            continue

        if any(phrase in user_prompt.lower() for phrase in [
            "i can hear you", "i hear you", "i'm hearing you", "yes i can hear", "i can hear the ai"
        ]):
            user_acknowledged_audio = True

        gpt_response = await send_to_gpt(
            user_prompt, user_id, use_voice=use_voice, user_acknowledged_audio=user_acknowledged_audio
        )

        if not use_voice:
            await websocket.send_text(f"David: {gpt_response}")
        else:
            await gpt_to_elevenlabs(gpt_response, websocket)

# Token Handling
async def create_and_store_id(token: str):
    new_id = str(uuid.uuid4())
    users_table.insert({
        "token": token,
        "user_id": new_id,
        "gpt_conversation": []
    })
    return new_id

async def get_existing_id(token: str):
    result = users_table.get(User.token == token)
    return result["user_id"]

async def is_user_new(token: str):
    return users_table.get(User.token == token) is None

async def get_user_token(websocket: WebSocket):
    await websocket.send_text("Enter your token: ")
    return await websocket.receive_text()

# WebSocket Entry Point
@app.websocket("/user_text")
async def wait_for_message(websocket: WebSocket):
    await websocket.accept()
    shared_password = await get_shared_password()

    while True:
        user_input = await websocket.receive_text()

        if user_input == shared_password:
            await websocket.send_text("Password correct! Welcome back")
            token = await get_user_token(websocket)

            if await is_user_new(token):
                user_id = await create_and_store_id(token)
            else:
                user_id = await get_existing_id(token)

            await user_to_gpt(websocket, user_id)
            break
        else:
            await websocket.close(code=1000, reason="Wrong password, try again later")
