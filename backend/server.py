from fastapi import FastAPI, APIRouter, HTTPException, Request, BackgroundTasks
from fastapi.responses import Response
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import uuid
from datetime import datetime, timedelta
import json
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather, Say
from emergentintegrations.llm.chat import LlmChat, UserMessage
import asyncio
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleAPIRequest
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
import re

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Twilio setup
twilio_client = Client(os.environ['TWILIO_ACCOUNT_SID'], os.environ['TWILIO_AUTH_TOKEN'])

# Create the main app without a prefix
app = FastAPI(title="AI Hospital Appointment Booking Agent")

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Conversation state management
conversation_states = {}

# Define Models
class Patient(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    phone_number: str
    name: Optional[str] = None
    preferred_doctor: Optional[str] = None
    appointment_date: Optional[str] = None
    appointment_time: Optional[str] = None
    booking_timestamp: datetime = Field(default_factory=datetime.utcnow)
    conversation_complete: bool = False

class CallRequest(BaseModel):
    phone_number: str
    patient_name: Optional[str] = None

class ConversationState(BaseModel):
    patient_id: str
    call_sid: str
    phone_number: str
    conversation_stage: str = "greeting"  # greeting, name, doctor, datetime, confirmation
    collected_data: Dict[str, Any] = {}
    attempts: int = 0
    
# System prompt for the AI agent
SYSTEM_PROMPT = """You are an AI assistant for a hospital appointment booking system. Your role is to:

1. Greet patients warmly and professionally
2. Collect their name if not provided
3. Ask for their preferred doctor
4. Ask for their preferred appointment date and time
5. Confirm all details back to them
6. Be patient and understanding if they need to repeat information
7. Handle unclear responses by asking for clarification
8. Keep responses brief and conversational

Current conversation stage: {stage}
Collected data so far: {data}

Based on the patient's response, provide a natural, helpful response and indicate what information you still need to collect.
"""

# Initialize LLM Chat
async def get_ai_response(user_message: str, conversation_state: ConversationState) -> str:
    """Get AI response using emergentintegrations library"""
    chat = LlmChat(
        api_key=os.environ['OPENAI_API_KEY'],
        session_id=conversation_state.call_sid,
        system_message=SYSTEM_PROMPT.format(
            stage=conversation_state.conversation_stage,
            data=conversation_state.collected_data
        )
    ).with_model("openai", "gpt-4o")
    
    user_msg = UserMessage(text=user_message)
    response = await chat.send_message(user_msg)
    return response

# Helper function to extract information from text
def extract_appointment_info(text: str) -> Dict[str, Any]:
    """Extract appointment information from user input"""
    info = {}
    
    # Extract doctor names (simple pattern matching)
    doctor_patterns = [
        r'dr\.?\s+([a-zA-Z]+)',
        r'doctor\s+([a-zA-Z]+)',
        r'([a-zA-Z]+)\s+doctor'
    ]
    
    for pattern in doctor_patterns:
        match = re.search(pattern, text.lower())
        if match:
            info['doctor'] = match.group(1).title()
            break
    
    # Extract dates and times (basic patterns)
    date_patterns = [
        r'(monday|tuesday|wednesday|thursday|friday|saturday|sunday)',
        r'(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}',
        r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}',
        r'tomorrow|today|next week'
    ]
    
    time_patterns = [
        r'(\d{1,2}:\d{2})\s*(am|pm)?',
        r'(\d{1,2})\s*(am|pm)',
        r'(morning|afternoon|evening|noon)'
    ]
    
    for pattern in date_patterns:
        match = re.search(pattern, text.lower())
        if match:
            info['date'] = match.group(0)
            break
    
    for pattern in time_patterns:
        match = re.search(pattern, text.lower())
        if match:
            info['time'] = match.group(0)
            break
    
    return info

# API Routes
@api_router.post("/make-call")
async def make_call(call_request: CallRequest, background_tasks: BackgroundTasks):
    """Initiate an outbound call to a patient"""
    try:
        # Create patient record
        patient = Patient(
            phone_number=call_request.phone_number,
            name=call_request.patient_name
        )
        
        # Store patient in database
        await db.patients.insert_one(patient.dict())
        
        # Make the call using Twilio
        webhook_url = f"{os.environ.get('REACT_APP_BACKEND_URL', 'http://localhost:8001')}/api/voice/webhook"
        call = twilio_client.calls.create(
            url=webhook_url,
            to=call_request.phone_number,
            from_=os.environ.get('TWILIO_PHONE_NUMBER', '+15551234567'),
            method='POST'
        )
        
        # Initialize conversation state
        conversation_states[call.sid] = ConversationState(
            patient_id=patient.id,
            call_sid=call.sid,
            phone_number=call_request.phone_number,
            collected_data={"name": call_request.patient_name} if call_request.patient_name else {}
        )
        
        return {
            "status": "success",
            "call_sid": call.sid,
            "patient_id": patient.id,
            "message": f"Call initiated to {call_request.phone_number}"
        }
        
    except Exception as e:
        logging.error(f"Error making call: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to make call: {str(e)}")

@api_router.post("/voice/webhook")
async def voice_webhook(request: Request):
    """Handle Twilio voice webhook"""
    try:
        form_data = await request.form()
        call_sid = form_data.get('CallSid')
        
        # Get or create conversation state
        if call_sid not in conversation_states:
            # This shouldn't happen but handle gracefully
            response = VoiceResponse()
            response.say("I'm sorry, there was an error with the call. Please try again later.")
            response.hangup()
            return Response(content=str(response), media_type="application/xml")
        
        conversation_state = conversation_states[call_sid]
        
        # Initial greeting
        response = VoiceResponse()
        if conversation_state.conversation_stage == "greeting":
            if conversation_state.collected_data.get("name"):
                greeting = f"Hello {conversation_state.collected_data['name']}, this is the AI assistant from your hospital. I'm calling to help you book an appointment. May I proceed?"
            else:
                greeting = "Hello, this is the AI assistant from your hospital. I'm calling to help you book an appointment. May I start by getting your name?"
            
            gather = Gather(
                num_digits=0,
                action=f"/api/voice/process-speech",
                method="POST",
                speech_timeout="auto",
                timeout=10
            )
            gather.say(greeting)
            response.append(gather)
            
            # Fallback if no input
            response.say("I didn't receive any input. Please try again.")
            response.redirect(f"/api/voice/webhook")
        
        return Response(content=str(response), media_type="application/xml")
        
    except Exception as e:
        logging.error(f"Voice webhook error: {str(e)}")
        response = VoiceResponse()
        response.say("I'm sorry, there was an error. Please try again later.")
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

@api_router.post("/voice/process-speech")
async def process_speech(request: Request, background_tasks: BackgroundTasks):
    """Process speech input from the patient"""
    try:
        form_data = await request.form()
        call_sid = form_data.get('CallSid')
        speech_result = form_data.get('SpeechResult', '')
        
        if call_sid not in conversation_states:
            response = VoiceResponse()
            response.say("Sorry, there was an error with the call.")
            response.hangup()
            return Response(content=str(response), media_type="application/xml")
        
        conversation_state = conversation_states[call_sid]
        
        # Get AI response
        ai_response = await get_ai_response(speech_result, conversation_state)
        
        # Extract information from speech
        extracted_info = extract_appointment_info(speech_result)
        
        # Update conversation state based on current stage
        if conversation_state.conversation_stage == "greeting":
            # Look for name in the response
            if "name" not in conversation_state.collected_data:
                # Try to extract name from response
                words = speech_result.split()
                if len(words) >= 2 and ("i'm" in speech_result.lower() or "my name is" in speech_result.lower()):
                    # Simple name extraction
                    if "i'm" in speech_result.lower():
                        name_part = speech_result.lower().split("i'm")[1].strip()
                    else:
                        name_part = speech_result.lower().split("my name is")[1].strip()
                    
                    conversation_state.collected_data["name"] = name_part.split()[0].title()
                    conversation_state.conversation_stage = "doctor"
                else:
                    # Ask for name more directly
                    conversation_state.conversation_stage = "name"
            else:
                conversation_state.conversation_stage = "doctor"
        
        elif conversation_state.conversation_stage == "name":
            # Extract name from response
            conversation_state.collected_data["name"] = speech_result.strip().title()
            conversation_state.conversation_stage = "doctor"
        
        elif conversation_state.conversation_stage == "doctor":
            if extracted_info.get("doctor"):
                conversation_state.collected_data["doctor"] = extracted_info["doctor"]
                conversation_state.conversation_stage = "datetime"
            # Stay in doctor stage if no doctor found
        
        elif conversation_state.conversation_stage == "datetime":
            if extracted_info.get("date"):
                conversation_state.collected_data["date"] = extracted_info["date"]
            if extracted_info.get("time"):
                conversation_state.collected_data["time"] = extracted_info["time"]
            
            # If we have both date and time, move to confirmation
            if "date" in conversation_state.collected_data and "time" in conversation_state.collected_data:
                conversation_state.conversation_stage = "confirmation"
        
        elif conversation_state.conversation_stage == "confirmation":
            # Handle confirmation response
            if "yes" in speech_result.lower() or "confirm" in speech_result.lower() or "correct" in speech_result.lower():
                # Schedule appointment background task
                background_tasks.add_task(schedule_appointment, conversation_state)
                conversation_state.conversation_stage = "complete"
            elif "no" in speech_result.lower() or "incorrect" in speech_result.lower():
                # Reset to gather information again
                conversation_state.conversation_stage = "doctor"
                conversation_state.collected_data = {"name": conversation_state.collected_data.get("name", "")}
        
        # Generate TwiML response
        response = VoiceResponse()
        
        if conversation_state.conversation_stage == "complete":
            response.say("Perfect! Your appointment has been booked and logged. You will receive a confirmation shortly. Thank you for calling. Goodbye!")
            response.hangup()
        else:
            # Determine next question based on stage
            if conversation_state.conversation_stage == "name":
                next_question = "Could you please tell me your name?"
            elif conversation_state.conversation_stage == "doctor":
                next_question = "Which doctor would you like to see?"
            elif conversation_state.conversation_stage == "datetime":
                if "date" not in conversation_state.collected_data:
                    next_question = "What date would you prefer for your appointment?"
                elif "time" not in conversation_state.collected_data:
                    next_question = "What time would you prefer?"
                else:
                    next_question = "Thank you for providing the date and time."
            elif conversation_state.conversation_stage == "confirmation":
                name = conversation_state.collected_data.get("name", "")
                doctor = conversation_state.collected_data.get("doctor", "")
                date = conversation_state.collected_data.get("date", "")
                time = conversation_state.collected_data.get("time", "")
                next_question = f"Let me confirm your appointment: {name}, you want to see Dr. {doctor} on {date} at {time}. Is this correct?"
            
            gather = Gather(
                num_digits=0,
                action=f"/api/voice/process-speech",
                method="POST",
                speech_timeout="auto",
                timeout=10
            )
            gather.say(f"{ai_response} {next_question}")
            response.append(gather)
            
            # Fallback
            response.say("I didn't catch that. Let me try again.")
            response.redirect(f"/api/voice/process-speech")
        
        return Response(content=str(response), media_type="application/xml")
        
    except Exception as e:
        logging.error(f"Speech processing error: {str(e)}")
        response = VoiceResponse()
        response.say("I'm sorry, there was an error processing your response. Please try again.")
        response.hangup()
        return Response(content=str(response), media_type="application/xml")

async def schedule_appointment(conversation_state: ConversationState):
    """Schedule appointment and log to Google Sheets"""
    try:
        # Update patient in database
        await db.patients.update_one(
            {"id": conversation_state.patient_id},
            {"$set": {
                "name": conversation_state.collected_data.get("name"),
                "preferred_doctor": conversation_state.collected_data.get("doctor"),
                "appointment_date": conversation_state.collected_data.get("date"),
                "appointment_time": conversation_state.collected_data.get("time"),
                "conversation_complete": True
            }}
        )
        
        # Log appointment data for Google Sheets
        appointment_data = {
            "patient_name": conversation_state.collected_data.get("name", ""),
            "phone_number": conversation_state.phone_number,
            "doctor": conversation_state.collected_data.get("doctor", ""),
            "date": conversation_state.collected_data.get("date", ""),
            "time": conversation_state.collected_data.get("time", ""),
            "booking_timestamp": datetime.utcnow().isoformat()
        }
        
        # Log to Google Sheets
        await log_to_google_sheets(appointment_data)
        
        logging.info(f"Appointment scheduled: {appointment_data}")
        
        # Clean up conversation state
        if conversation_state.call_sid in conversation_states:
            del conversation_states[conversation_state.call_sid]
            
    except Exception as e:
        logging.error(f"Error scheduling appointment: {str(e)}")

async def log_to_google_sheets(appointment_data: dict):
    """Log appointment data to Google Sheets"""
    try:
        # For now, we'll use a simplified approach
        # In production, you would use service account credentials
        logging.info(f"Logging to Google Sheets: {appointment_data}")
        
        # This is a placeholder - in production you would:
        # 1. Use service account credentials
        # 2. Call Google Sheets API to append data
        # 3. Handle authentication and authorization
        
        # Example of what the Google Sheets API call would look like:
        # service = build('sheets', 'v4', credentials=credentials)
        # sheet = service.spreadsheets()
        # values = [[
        #     appointment_data['patient_name'],
        #     appointment_data['phone_number'],
        #     appointment_data['doctor'],
        #     appointment_data['date'],
        #     appointment_data['time'],
        #     appointment_data['booking_timestamp']
        # ]]
        # body = {'values': values}
        # result = sheet.values().append(
        #     spreadsheetId=os.environ['GOOGLE_SHEETS_SPREADSHEET_ID'],
        #     range='Sheet1!A1',
        #     valueInputOption='RAW',
        #     body=body
        # ).execute()
        
        return True
        
    except Exception as e:
        logging.error(f"Error logging to Google Sheets: {str(e)}")
        return False

@api_router.get("/appointments")
async def get_appointments():
    """Get all appointments"""
    try:
        appointments = await db.patients.find({"conversation_complete": True}).to_list(100)
        return appointments
    except Exception as e:
        logging.error(f"Error fetching appointments: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch appointments")

@api_router.get("/call-status/{call_sid}")
async def get_call_status(call_sid: str):
    """Get call status"""
    try:
        call = twilio_client.calls(call_sid).fetch()
        return {
            "call_sid": call_sid,
            "status": call.status,
            "duration": call.duration,
            "start_time": call.start_time,
            "end_time": call.end_time
        }
    except Exception as e:
        logging.error(f"Error fetching call status: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch call status")

# Basic health check
@api_router.get("/")
async def root():
    return {"message": "AI Hospital Appointment Booking Agent API"}

@api_router.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow()}

# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()