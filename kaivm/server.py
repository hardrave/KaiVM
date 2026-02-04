from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any, Set

from fastapi import FastAPI, WebSocket, Request, BackgroundTasks, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from kaivm.agent.runner import AgentConfig, KaiVMAgent
from kaivm.gemini.client import GeminiPlanner, DEFAULT_MODEL
from kaivm.hid.keyboard import KeyboardHID, ASCII_MAP, KEYCODES, MOD_NAMES, MOD_LCTRL, MOD_LSHIFT, MOD_LALT, MOD_LGUI
from kaivm.hid.mouse import MouseHID
from kaivm.util.log import get_logger, setup_logging
from kaivm.util.paths import LATEST_JPG

log = get_logger("kaivm.server")

# Global state
class AppState:
    agent_running: bool = False
    agent_task: Optional[asyncio.Task] = None
    logs: List[str] = []
    current_instruction: str = ""
    last_status: str = "Idle"
    planned_actions: List[Dict[str, Any]] = []

state = AppState()

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(verbose=True)
    log.info("Server starting...")
    yield
    log.info("Server shutting down...")

app = FastAPI(lifespan=lifespan)

# Static files
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

def _read_latest_loop():
    """Yields MJPEG stream from latest.jpg."""
    while True:
        if LATEST_JPG.exists():
            try:
                data = LATEST_JPG.read_bytes()
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + data + b"\r\n"
                )
            except Exception:
                pass
        time.sleep(0.1)

@app.get("/stream")
async def video_stream():
    return StreamingResponse(
        _read_latest_loop(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

def _map_key(k: str) -> Optional[int]:
    # Returns keycode or None
    if len(k) == 1:
        if k in ASCII_MAP:
            return ASCII_MAP[k][1]
        # Fallback for letters not covered? (though ASCII_MAP covers a-zA-Z)
        if k.lower() in ASCII_MAP:
            return ASCII_MAP[k.lower()][1]
    
    k_up = k.upper()
    if k_up in KEYCODES:
        return KEYCODES[k_up]
    
    # Mapping for browser keys to our KEYCODES
    if k == "ArrowUp": return KEYCODES["UP"]
    if k == "ArrowDown": return KEYCODES["DOWN"]
    if k == "ArrowLeft": return KEYCODES["LEFT"]
    if k == "ArrowRight": return KEYCODES["RIGHT"]
    if k == "Enter": return KEYCODES["ENTER"]
    if k == "Escape": return KEYCODES["ESC"]
    if k == "Backspace": return KEYCODES["BACKSPACE"]
    if k == "Tab": return KEYCODES["TAB"]
    if k == " ": return KEYCODES["SPACE"]
    
    return None

@app.websocket("/ws/input")
async def websocket_input(websocket: WebSocket):
    await websocket.accept()
    
    kbd = KeyboardHID()
    mouse = MouseHID()
    
    pressed_keys: Set[int] = set()
    
    try:
        while True:
            data = await websocket.receive_json()
            t = data.get("type")
            
            if t == "mousemove":
                dx = int(data.get("dx", 0))
                dy = int(data.get("dy", 0))
                # Mouse move usually doesn't change button state, but we need to send it if we are holding drag
                # However, MouseHID.move(dx, dy) sends 0 buttons.
                # Use send_report with current button state if we were tracking it, 
                # but browser pointer lock usually handles moves.
                # Simple implementation: just move.
                # Better: track buttons.
                btns = int(data.get("buttons", 0)) # We expect frontend to send button mask if possible
                mouse.send_report(btns, dx, dy)
            
            elif t in ("mousedown", "mouseup"):
                btn = data.get("button", 0) # 0: left, 1: middle, 2: right (JS standard)
                # Map JS button to HID mask
                # JS: 0=Left, 1=Middle, 2=Right
                # HID: 1=Left, 2=Right, 4=Middle
                mask = 0
                if btn == 0: mask = 1
                elif btn == 1: mask = 4
                elif btn == 2: mask = 2
                
                # We need to accumulate buttons if multiple are pressed?
                # Browser "buttons" property (bitmask) is: 1=Left, 2=Right, 4=Middle (Wait, standard is 1=Left, 2=Right, 4=Middle, 8=Back, 16=Forward)
                # But 'button' property on event is index.
                # Let's trust the "buttons" bitmask from the event if provided.
                
                # If frontend sends "buttons" bitmask:
                # JS bitmask: 1=Left, 2=Right, 4=Middle
                # HID bitmask: 1=Left, 2=Right, 4=Middle
                # They MATCH!
                
                buttons_mask = int(data.get("buttons", 0))
                mouse.send_report(buttons_mask, 0, 0)

            elif t in ("keydown", "keyup"):
                k = data.get("key", "")
                
                # Calculate modifiers
                mod = 0
                if data.get("ctrlKey"): mod |= MOD_LCTRL
                if data.get("shiftKey"): mod |= MOD_LSHIFT
                if data.get("altKey"): mod |= MOD_LALT
                if data.get("metaKey"): mod |= MOD_LGUI
                
                keycode = _map_key(k)
                if keycode:
                    if t == "keydown":
                        pressed_keys.add(keycode)
                    else:
                        pressed_keys.discard(keycode)
                
                # If key is a modifier (Shift, Control), _map_key returns None, 
                # but we updated 'mod' state.
                # We still need to send the report if only modifier changed.
                
                kbd.send_report(mod, list(pressed_keys))

    except WebSocketDisconnect:
        # Release all keys on disconnect
        try:
            kbd.send_report(0, [])
        except:
            pass
    except Exception as e:
        log.error(f"WebSocket input error: {e}")
        try:
            kbd.send_report(0, [])
        except:
            pass

class RunRequest(BaseModel):
    instruction: str
    model: str = DEFAULT_MODEL
    thinking_level: str = "low"
    max_steps: int = 30
    allow_danger: bool = False
    dry_run: bool = False
    api_key: Optional[str] = None

def _agent_runner_thread(req: RunRequest):
    state.agent_running = True
    state.current_instruction = req.instruction
    state.logs.append(f"Starting agent: {req.instruction}")
    state.last_status = "Running"
    
    try:
        planner = GeminiPlanner(
            model=req.model,
            thinking_level=req.thinking_level,
            timeout_steps=2,
            api_key=req.api_key,
        )
        
        # We need a custom logger/callback to update state.logs
        # For now, we'll just let the agent run and we might need to capture stdout/logs differently 
        # or modify the agent to accept a callback.
        # Since we can't easily modify the agent deeply in this step without touching runner.py, 
        # let's assume standard logging goes to stderr/stdout and we might not see it in the UI 
        # unless we redirect it.
        # However, for the UI status, we really want to see what's happening.
        
        # Let's subclass or wrap the agent to update our state.
        
        agent = KaiVMAgent(
            planner=planner,
            kbd=KeyboardHID(),
            mouse=MouseHID(),
            cfg=AgentConfig(
                max_steps=req.max_steps,
                overall_timeout_s=3600.0,
                allow_danger=req.allow_danger,
                dry_run=req.dry_run,
                do_replug=True,
            ),
        )
        
        # Monkey patch the agent's planner to intercept plans for the UI
        original_plan = planner.plan
        def _intercept_plan(*args, **kwargs):
            res = original_plan(*args, **kwargs)
            # Update UI state with planned actions
            # We'd need to parse them, but for now just raw JSON if possible or we parse it
            # parse_plan is in kaivm.agent.validate
            from kaivm.agent.validate import parse_plan
            try:
                reasoning = res.get("reasoning", "")
                if reasoning:
                    state.logs.append(f"Thinking: {reasoning}")
                
                actions = parse_plan(res)
                state.planned_actions = [
                    {"type": a.type, "key": a.key, "text": a.text, "dx": a.dx, "dy": a.dy, "button": a.button, "ms": a.ms, "summary": a.summary}
                    for a in actions
                ]
                state.logs.append(f"Planned {len(actions)} actions.")
            except Exception as e:
                state.logs.append(f"Error parsing plan: {e}")
            return res
        
        planner.plan = _intercept_plan

        res = agent.run(req.instruction)
        state.logs.append(f"Agent finished: {res}")
        state.last_status = f"Done: {res}"
        
    except Exception as e:
        log.exception("Agent error")
        state.logs.append(f"Error: {e}")
        state.last_status = "Error"
    finally:
        state.agent_running = False

@app.post("/api/run")
async def run_agent(req: RunRequest, background_tasks: BackgroundTasks):
    if state.agent_running:
        return JSONResponse({"error": "Agent already running"}, status_code=400)
    
    state.logs = []
    state.planned_actions = []
    background_tasks.add_task(_agent_runner_thread, req)
    return {"status": "started"}

@app.post("/api/stop")
async def stop_agent():
    # Write stop file
    from kaivm.util.paths import STOP_FILE
    STOP_FILE.touch()
    state.logs.append("Stop requested...")
    return {"status": "stopping"}

@app.get("/api/state")
async def get_state():
    return {
        "running": state.agent_running,
        "instruction": state.current_instruction,
        "status": state.last_status,
        "logs": state.logs[-50:], # Last 50 logs
        "planned_actions": state.planned_actions
    }

@app.get("/")
async def index():
    return HTMLResponse((STATIC_DIR / "index.html").read_text())

def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    main()
