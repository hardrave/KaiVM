from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
import uuid
from typing import Optional, List, Dict, Any, Set

from fastapi import FastAPI, WebSocket, Request, BackgroundTasks, WebSocketDisconnect
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from kaivm.agent.runner import AgentConfig, KaiVMAgent
from kaivm.gemini.client import GeminiPlanner, DEFAULT_MODEL
from kaivm.hid.keyboard import KeyboardHID, ASCII_MAP, KEYCODES, MOD_NAMES, MOD_LCTRL, MOD_LSHIFT, MOD_LALT, MOD_LGUI
from kaivm.hid.mouse import MouseHID, AbsoluteMouseHID
from kaivm.util.log import get_logger, setup_logging
from kaivm.util.paths import LATEST_JPG, STOP_FILE, CALIBRATION_FILE

log = get_logger("kaivm.server")

class Event(BaseModel):
    id: str
    name: str
    condition: str
    action: str
    model: str = DEFAULT_MODEL
    interval: int = 60
    enabled: bool = True
    last_check: float = 0.0

class EventCreate(BaseModel):
    name: str
    condition: str
    action: str
    model: str = DEFAULT_MODEL
    interval: int = 60

class StartEventsRequest(BaseModel):
    api_key: Optional[str] = None
    max_steps: int = 10
    timeout: int = 120

class SyncEventsRequest(BaseModel):
    events: List[Event]

class EventsManager:
    def __init__(self):
        self.events: Dict[str, Event] = {}
        self.running: bool = False
        self.task: Optional[asyncio.Task] = None
        self.logs: List[str] = []
        self.api_key: Optional[str] = None
        self.max_steps: int = 10
        self.timeout: int = 120

    def add_event(self, evt: EventCreate) -> Event:
        new_event = Event(
            id=str(uuid.uuid4()),
            name=evt.name,
            condition=evt.condition,
            action=evt.action,
            model=evt.model,
            interval=evt.interval,
            enabled=True,
            last_check=0.0
        )
        self.events[new_event.id] = new_event
        self.log(f"Event added: {new_event.name}")
        return new_event

    def remove_event(self, event_id: str):
        if event_id in self.events:
            del self.events[event_id]
            self.log(f"Event removed: {event_id}")

    def sync_events(self, events: List[Event]):
        self.events = {e.id: e for e in events}
        self.log(f"Events synced: {len(events)} events loaded.")

    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.logs.append(f"[{ts}] {msg}")
        if len(self.logs) > 100:
            self.logs.pop(0)

    async def start_loop(self, api_key: Optional[str] = None, max_steps: int = 10, timeout: int = 120):
        if self.running: return
        self.running = True
        self.api_key = api_key
        self.max_steps = max_steps
        self.timeout = timeout
        self.log(f"Events mode started (Steps={self.max_steps}, Timeout={self.timeout}s).")
        self.task = asyncio.create_task(self._loop())

    async def stop_loop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.log("Events mode stopped.")

    async def _loop(self):
        # We'll instantiate planners as needed or reuse them?
        # For simplicity, let's instantiate per check for now, or per loop iteration if we want to support dynamic models.
        # Actually, if we want to support switching models per event, we should instantiate inside.
        
        while self.running:
            now = time.time()
            active_events = [e for e in self.events.values() if e.enabled]
            
            for event in active_events:
                if not self.running: break
                
                if now - event.last_check >= event.interval:
                    event.last_check = now
                    try:
                        self.log(f"Checking event: {event.name}")
                        
                        if not LATEST_JPG.exists():
                            self.log("Skipping check: No video stream.")
                            continue
                            
                        jpeg = LATEST_JPG.read_bytes()
                        
                        # Use event-specific model
                        planner = GeminiPlanner(model=event.model, api_key=self.api_key)
                        
                        # Check condition
                        res = await asyncio.to_thread(planner.check_condition, event.condition, jpeg)
                        
                        if res.get("met"):
                            self.log(f"EVENT TRIGGERED: {event.name}")
                            self.log(f"Reasoning: {res.get('reasoning')}")
                            
                            # Execute Action
                            await self._execute_action(event.action, event.model)
                            
                        else:
                            self.log(f"Event {event.name} not met. ({res.get('reasoning')})")
                            
                    except Exception as e:
                        log.error(f"Event loop error: {e}")
                        self.log(f"Error checking {event.name}: {e}")
            
            await asyncio.sleep(1)

    async def _execute_action(self, instruction: str, model_name: str = DEFAULT_MODEL):
        self.log(f"Executing action: {instruction}")
        
        # We reuse the agent runner logic but we need to run it here.
        # We can't reuse _agent_runner_thread easily because it modifies global state.
        # We should probably adapt it.
        # For now, let's just run a short agent task.
        
        try:
             # Load calibration if available
            cal_x_s, cal_y_s, cal_x_o, cal_y_o = 1.0, 1.0, 0.0, 0.0
            if state.mouse_calibration:
                try:
                    parts = [float(x.strip()) for x in state.mouse_calibration.split(",")]
                    if len(parts) == 4:
                        cal_x_s, cal_y_s, cal_x_o, cal_y_o = parts
                except: pass

            planner = GeminiPlanner(model=model_name, timeout_steps=2, api_key=self.api_key)
            agent = KaiVMAgent(
                planner=planner,
                kbd=KeyboardHID(),
                mouse=MouseHID(),
                abs_mouse=AbsoluteMouseHID(),
                cfg=AgentConfig(
                    max_steps=self.max_steps,
                    overall_timeout_s=self.timeout,
                    allow_danger=True, # Actions might need danger
                    cal_x_scale=cal_x_s, cal_y_scale=cal_y_s, 
                    cal_x_offset=cal_x_o, cal_y_offset=cal_y_o,
                ),
            )
            
            # Run in thread to avoid blocking main loop completely (though strictly we are already in threadpool usually?)
            # No, we are in asyncio loop. agent.run is synchronous/blocking.
            # So we MUST run in thread.
            res = await asyncio.to_thread(agent.run, instruction)
            self.log(f"Action finished: {res}")
            
        except Exception as e:
            self.log(f"Action failed: {e}")

# Global state
class AppState:
    agent_running: bool = False
    agent_task: Optional[asyncio.Task] = None
    logs: List[str] = []
    current_instruction: str = ""
    last_status: str = "Idle"
    planned_actions: List[Dict[str, Any]] = []
    mouse_calibration: Optional[str] = None
    events: EventsManager = EventsManager()

state = AppState()

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(verbose=True)
    log.info("Server starting...")
    
    if CALIBRATION_FILE.exists():
        try:
            state.mouse_calibration = CALIBRATION_FILE.read_text().strip()
            log.info(f"Loaded calibration: {state.mouse_calibration}")
        except Exception as e:
            log.error(f"Failed to load calibration: {e}")
            
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
    thinking_level: Optional[str] = None
    max_steps: int = 30
    allow_danger: bool = False
    dry_run: bool = False
    api_key: Optional[str] = None
    timeout: int = 120

    @field_validator('max_steps')
    @classmethod
    def check_max_steps(cls, v: int) -> int:
        return v if v > 0 else 10000

    @field_validator('timeout')
    @classmethod
    def check_timeout(cls, v: int) -> int:
        return v if v > 0 else 1440 # 24h

class AskRequest(BaseModel):
    instruction: str
    model: str = DEFAULT_MODEL
    api_key: Optional[str] = None
    attach_screen: bool = True

class MoveAbsRequest(BaseModel):
    x: int
    y: int

class CalibrationPoint(BaseModel):
    hid_x: int
    hid_y: int
    screen_x: int
    screen_y: int
    screen_w: int
    screen_h: int

class CalculateCalibrationRequest(BaseModel):
    points: List[CalibrationPoint]

@app.post("/api/hid/move_absolute")
async def move_mouse_absolute(req: MoveAbsRequest):
    mouse = AbsoluteMouseHID()
    mouse.move(req.x, req.y)
    return {"status": "moved", "x": req.x, "y": req.y}

@app.post("/api/calibrate/calculate")
async def calculate_calibration(req: CalculateCalibrationRequest):
    if len(req.points) < 2:
        return JSONResponse({"error": "Need at least 2 points"}, status_code=400)
    
    # We use a simple linear regression or just average scaling from pairs
    # Logic: HID_norm = Screen_norm * Scale + Offset
    # We want to find Scale and Offset that minimizes error, or just use min/max points.
    
    # Let's use the min/max approach similar to calibrate.py but robust to multiple points.
    # Actually, if we have corners, we can just use linear fit.
    
    # Let's collect X and Y data separately
    # X_hid_norm = X_screen_norm * Sx + Ox
    
    # We have list of (X_screen_norm, X_hid_norm)
    data_x = []
    data_y = []
    
    for p in req.points:
        nx_screen = (p.screen_x + 0.5) / p.screen_w
        ny_screen = (p.screen_y + 0.5) / p.screen_h
        
        nx_hid = p.hid_x / 32767.0
        ny_hid = p.hid_y / 32767.0
        
        data_x.append((nx_screen, nx_hid))
        data_y.append((ny_screen, ny_hid))
        
    def solve_linear(data):
        # simple least squares for y = mx + c
        # m = (N*sum(xy) - sum(x)sum(y)) / (N*sum(x^2) - sum(x)^2)
        # c = (sum(y) - m*sum(x)) / N
        N = len(data)
        sum_x = sum(d[0] for d in data)
        sum_y = sum(d[1] for d in data)
        sum_xy = sum(d[0]*d[1] for d in data)
        sum_xx = sum(d[0]**2 for d in data)
        
        denom = (N * sum_xx - sum_x**2)
        if abs(denom) < 1e-9:
            return 1.0, 0.0 # fallback
            
        m = (N * sum_xy - sum_x * sum_y) / denom
        c = (sum_y - m * sum_x) / N
        return m, c

    sx, ox = solve_linear(data_x)
    sy, oy = solve_linear(data_y)
    
    res = f"{sx:.4f},{sy:.4f},{ox:.4f},{oy:.4f}"
    
    log.info(f"Calibration Input Points (Screen -> HID):")
    for i, p in enumerate(req.points):
        log.info(f"  P{i}: Screen({p.screen_x},{p.screen_y}) -> HID({p.hid_x},{p.hid_y})")
    
    log.info(f"Calibration Result: Scale=({sx:.4f}, {sy:.4f}), Offset=({ox:.4f}, {oy:.4f})")
    
    if abs(ox) > 0.2 or abs(oy) > 0.2:
        log.warning(f"Large calibration offset detected! (ox={ox:.2f}, oy={oy:.2f})")
    if sx < 0.5 or sy < 0.5:
        log.warning(f"Small calibration scale detected! (sx={sx:.2f}, sy={sy:.2f})")
    
    # Save to state and file
    state.mouse_calibration = res
    try:
        CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
        CALIBRATION_FILE.write_text(res)
        log.info(f"Saved calibration to {CALIBRATION_FILE}")
    except Exception as e:
        log.error(f"Failed to save calibration file: {e}")

    return {"result": res}

def _agent_runner_thread(req: RunRequest):
    if STOP_FILE.exists():
        try:
            STOP_FILE.unlink()
        except Exception:
            pass

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
        
        # Parse calibration
        cal_x_s, cal_y_s, cal_x_o, cal_y_o = 1.0, 1.0, 0.0, 0.0
        if state.mouse_calibration:
            try:
                parts = [float(x.strip()) for x in state.mouse_calibration.split(",")]
                if len(parts) == 4:
                    cal_x_s, cal_y_s, cal_x_o, cal_y_o = parts
                    state.logs.append(f"Using calibration: {state.mouse_calibration}")
            except Exception as e:
                state.logs.append(f"Invalid calibration ignored: {e}")

        agent = KaiVMAgent(
            planner=planner,
            kbd=KeyboardHID(),
            mouse=MouseHID(),
            abs_mouse=AbsoluteMouseHID(),
            cfg=AgentConfig(
                max_steps=req.max_steps,
                overall_timeout_s=req.timeout * 60.0,
                allow_danger=req.allow_danger,
                dry_run=req.dry_run,
                do_replug=True,
                cal_x_scale=cal_x_s,
                cal_y_scale=cal_y_s,
                cal_x_offset=cal_x_o,
                cal_y_offset=cal_y_o,
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
    STOP_FILE.touch()
    state.logs.append("Stop requested...")
    return {"status": "stopping"}

@app.post("/api/ask")
async def ask_agent(req: AskRequest):
    # Clear state for a fresh answer
    state.logs = []
    state.planned_actions = []
    state.last_status = "Thinking..."
    
    # Check screen if requested
    data = None
    if req.attach_screen:
        if not LATEST_JPG.exists():
            return JSONResponse({"error": "No video stream available"}, status_code=400)
        try:
            data = LATEST_JPG.read_bytes()
        except Exception:
            return JSONResponse({"error": "Failed to read stream"}, status_code=500)
            
    # state.logs.append(f"Q: {req.instruction}") # User wants ONLY answer? 
    # "I should only get an answer to the question."
    # Let's verify if they mean no log of the question. 
    # Usually "Agent Answer" box implies the answer. 
    # But seeing the question is helpful. Let's keep it minimal.
    
    try:
        if req.attach_screen and data:
            planner = GeminiPlanner(
                model=req.model,
                api_key=req.api_key,
            )
            # Run in thread pool
            loop = asyncio.get_event_loop()
            answer = await loop.run_in_executor(None, planner.ask, req.instruction, data)
        else:
            # Just text chat? GeminiPlanner.ask requires bytes currently.
            # We can mock it or just fail. 
            # The prompt "Ask model about current screen" implies screen is needed.
            # But "option to attach screenshot" implies it might be optional.
            # If not attached, we can't really "Ask Screen".
            # Let's assume for now we just say "Screenshot required" or 
            # if we really want text only, we'd need to adjust GeminiPlanner.
            # For this prototype, let's assume attach_screen is usually true.
            if not req.attach_screen:
                 answer = "Screenshot attachment is disabled. (Text-only chat not implemented yet)"
            else:
                 answer = "No data."

        state.logs.append(answer)
        state.last_status = "Done"
        return {"answer": answer}
        
    except Exception as e:
        log.exception("Ask error")
        state.logs.append(f"Error: {e}")
        state.last_status = "Error"
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/events")
async def list_events():
    return list(state.events.events.values())

@app.post("/api/events")
async def create_event(evt: EventCreate):
    return state.events.add_event(evt)

@app.delete("/api/events/{event_id}")
async def delete_event(event_id: str):
    state.events.remove_event(event_id)
    return {"status": "deleted"}

@app.post("/api/events/sync")
async def sync_events(req: SyncEventsRequest):
    state.events.sync_events(req.events)
    return {"status": "synced", "count": len(req.events)}

@app.post("/api/events/start")
async def start_events(req: StartEventsRequest):
    if state.agent_running:
         return JSONResponse({"error": "Agent is running. Stop it first."}, status_code=400)
    await state.events.start_loop(api_key=req.api_key, max_steps=req.max_steps, timeout=req.timeout)
    return {"status": "started"}

@app.post("/api/events/stop")
async def stop_events():
    await state.events.stop_loop()
    return {"status": "stopped"}

@app.get("/api/state")
async def get_state():
    logs = state.logs
    # If events mode is active, prefer showing event logs in the main window
    # or we can have the frontend decide.
    # The user asked for "Agent Reasoning" in events mode.
    if state.events.running:
        logs = state.events.logs

    return {
        "running": state.agent_running,
        "events_running": state.events.running,
        "instruction": state.current_instruction,
        "status": state.last_status,
        "logs": logs[-50:], # Last 50 logs
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
