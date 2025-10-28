from flask import Flask, request
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
import openai
import os
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import json

app = Flask(__name__)

# --- CONFIG ---
openai.api_key = "sk-..."  # tvoj OpenAI ključ
TWILIO_SID = "AC..."
TWILIO_TOKEN = "tvoj_token"
TWILIO_NUMBER = "+1234567890"
client = Client(TWILIO_SID, TWILIO_TOKEN)

# Google Calendar
SCOPES = ['https://www.googleapis.com/auth/calendar']
creds = Credentials.from_service_account_file('credentials.json', scopes=SCOPES)
calendar_service = build('calendar', 'v3', credentials=creds)
CALENDAR_ID = 'tvoj.kalendar@gmail.com'  # ili office@brightsmile.com

# Knowledge Base
with open('knowledge_base.txt', 'r', encoding='utf-8') as f:
    KNOWLEDGE_BASE = f.read()

# System Prompt – ENGLESKI
SYSTEM_PROMPT = f"""
You are an AI receptionist named Emma at BrightSmile Dental.
Speak ONLY in clear, professional American English.
Use the following knowledge base:
{KNOWLEDGE_BASE}

If the caller wants to book:
1. Ask for service type
2. Ask for preferred day/week
3. Call `check_slots(date)` to get available times
4. Suggest 1–2 options
5. Once agreed, call `book_appointment(name, phone, date, time, service)`
6. Confirm and offer SMS reminder

Be warm, clear, and efficient. Never make up information.
"""

active_calls = {}

# --- FUNKCIJE ---
def check_slots(target_date_str):
    # Simple: accept "Tuesday", "tomorrow", or "2025-10-30"
    now = datetime.now()
    start = now
    end = now + timedelta(days=14)

    try:
        events_result = calendar_service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=start.isoformat() + 'Z',
            timeMax=end.isoformat() + 'Z',
            singleEvents=True,
            orderBy='startTime'
        ).execute()

        booked = []
        for event in events_result.get('items', []):
            start_time = event['start'].get('dateTime', event['start'].get('date'))
            if 'T' in start_time:
                booked.append(start_time.split('T')[1][:5])  # HH:MM

        all_slots = ["09:00", "10:00", "11:00", "14:00", "15:00", "16:00"]
        free = [s for s in all_slots if s not in booked]
        return free[:3]  # max 3 suggestions
    except Exception as e:
        return ["10:00", "14:00"]  # fallback

def book_appointment(name, phone, date, time, service):
    try:
        start_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        end_dt = start_dt + timedelta(minutes=45)

        event = {
            'summary': f'{service} - {name}',
            'description': f'Phone: {phone}',
            'start': {'dateTime': start_dt.isoformat()},
            'end': {'dateTime': end_dt.isoformat()},
            'reminders': {'useDefault': True}
        }
        calendar_service.events().insert(calendarId=CALENDAR_ID, body=event).execute()

        # SMS Confirmation
        client.messages.create(
            to=phone,
            from_=TWILIO_NUMBER,
            body=f"Hi {name}, your {service} is confirmed for {date} at {time}. See you at BrightSmile Dental!"
        )
        return "Appointment confirmed! I've sent you a text reminder."
    except:
        return "Sorry, there was an issue. Please try again."

# --- TWILIO ENDPOINTS ---
@app.route("/voice", methods=['POST'])
def voice():
    resp = VoiceResponse()
    call_sid = request.values.get('CallSid')

    if call_sid not in active_calls:
        active_calls[call_sid] = {"history": []}

    gather = Gather(
        input='speech',
        language='en-US',
        speechTimeout='auto',
        action='/handle_speech'
    )
    gather.say(
        "Hello, welcome to BrightSmile Dental. This is Emma, your virtual assistant. How may I help you today?",
        voice='Polly.Joanna',  # natural US voice
        language='en-US'
    )
    resp.append(gather)
    return str(resp)

@app.route("/handle_speech", methods=['POST'])
def handle_speech():
    user_speech = request.values.get('SpeechResult', '').strip()
    call_sid = request.values.get('CallSid')
    caller_number = request.values.get('From')

    if call_sid not in active_calls:
        active_calls[call_sid] = {"history": []}

    history = active_calls[call_sid]["history"]
    history.append({"role": "user", "content": user_speech})

    # Tool definitions
    tools = [
        {
            "type": "function",
            "function": {
                "name": "check_slots",
                "description": "Check available appointment slots for a given date",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "Date in YYYY-MM-DD or 'Tuesday'"}
                    },
                    "required": ["date"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "book_appointment",
                "description": "Book an appointment",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "phone": {"type": "string"},
                        "date": {"type": "string"},
                        "time": {"type": "string"},
                        "service": {"type": "string"}
                    },
                    "required": ["name", "phone", "date", "time", "service"]
                }
            }
        }
    ]

    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
        tools=tools,
        tool_choice="auto",
        temperature=0.7
    )

    msg = response.choices[0].message
    resp = VoiceResponse()

    bot_reply = ""

    if msg.get("tool_calls"):
        for tool in msg["tool_calls"]:
            func_name = tool["function"]["name"]
            args = json.loads(tool["function"]["arguments"])

            if func_name == "check_slots":
                slots = check_slots(args["date"])
                bot_reply = f"We have openings at {', '.join(slots)}. Which one works best?"
            elif func_name == "book_appointment":
                args["phone"] = caller_number
                bot_reply = book_appointment(**args)
    else:
        bot_reply = msg.get("content", "How else can I assist you?")

    history.append({"role": "assistant", "content": bot_reply})

    gather = Gather(input='speech', language='en-US', action='/handle_speech')
    gather.say(bot_reply, voice='Polly.Joanna', language='en-US')
    resp.append(gather)

    # End call after booking or timeout
    resp.say("Thank you for choosing BrightSmile Dental. Have a great day!", voice='Polly.Joanna')
    resp.hangup()

    return str(resp)

if __name__ == "__main__":
    app.run(port=5000, debug=True)