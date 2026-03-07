#!/usr/bin/env python3
"""Web UI for viewing camera, telemetry, and controlling the Unitree Go2."""

import argparse
import asyncio
import base64
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

from go2.connection import Go2Connection
from go2.constants import DEFAULT_IP

logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE pairs from .env without overriding real env vars."""
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        os.environ[key] = value


load_dotenv(Path(__file__).parent / ".env")

LLM_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
LLM_MODEL = os.getenv("GO2_LLM_MODEL", "gpt-4.1-mini")
MAX_LLM_MOVE_SPEED = 0.3
MAX_LLM_YAW_SPEED = 0.5
MAX_LLM_MOVE_DURATION = 2.0
MIN_BATTERY_FOR_MOVE = 20
MAX_LLM_GOAL_LENGTH = 500
AUTO_STAND_BEFORE_LLM_MOVE = True
LLM_ACTIONS = {
    "FORWARD",
    "BACKWARD",
    "LEFT",
    "RIGHT",
    "TURN_LEFT",
    "TURN_RIGHT",
    "STAND_UP",
    "STAND_DOWN",
    "STOP",
    "NONE",
}
LLM_MOVE_VECTORS = {
    "FORWARD": {"x": 0.2, "y": 0.0, "yaw": 0.0},
    "BACKWARD": {"x": -0.15, "y": 0.0, "yaw": 0.0},
    "LEFT": {"x": 0.0, "y": 0.2, "yaw": 0.0},
    "RIGHT": {"x": 0.0, "y": -0.2, "yaw": 0.0},
    "TURN_LEFT": {"x": 0.0, "y": 0.0, "yaw": 0.4},
    "TURN_RIGHT": {"x": 0.0, "y": 0.0, "yaw": -0.4},
}

COMMANDS = {
    # Basic state
    "stand_up": ("Stand up", lambda c: c.stand_up()),
    "stand_down": ("Lie down", lambda c: c.stand_down()),
    "sit": ("Sit", lambda c: c.sit()),
    "balance": ("Balance stand", lambda c: c.balance_stand()),
    "recovery": ("Recovery stand", lambda c: c.recovery_stand()),
    "stop": ("Stop moving", lambda c: c.stop()),
    "damp": ("Damp (disable motors)", lambda c: c.damp()),
    # Gestures
    "hello": ("Wave hello", lambda c: c.hello()),
    "stretch": ("Stretch", lambda c: c.stretch()),
    "content": ("Happy content", lambda c: c.content()),
    "wallow": ("Wallow", lambda c: c.wallow()),
    # Dance
    "dance1": ("Dance 1", lambda c: c.dance1()),
    "dance2": ("Dance 2", lambda c: c.dance2()),
    # Tricks
    "front_flip": ("Front flip", lambda c: c.front_flip()),
    "front_jump": ("Front jump", lambda c: c.front_jump()),
    "front_pounce": ("Front pounce", lambda c: c.front_pounce()),
    "wiggle_hips": ("Wiggle hips", lambda c: c.wiggle_hips()),
    "finger_heart": ("Finger heart", lambda c: c.finger_heart()),
    "handstand": ("Handstand", lambda c: c.handstand()),
    "cross_step": ("Cross step", lambda c: c.cross_step()),
    "bound": ("Bound", lambda c: c.bound()),
    "moon_walk": ("Moon walk", lambda c: c.moon_walk()),
    # Gait modes
    "economic_gait": ("Economic gait", lambda c: c.economic_gait()),
    "lead_follow": ("Lead follow", lambda c: c.lead_follow()),
}


class RobotController:
    """Owns a single robot connection shared by the web API."""

    MAX_CONNECT_ATTEMPTS = 4

    def __init__(self) -> None:
        self.conn: Go2Connection | None = None
        self.robot_ip = DEFAULT_IP
        self.use_new_signaling = True
        self.last_error: str | None = None
        self.connect_in_progress = False
        self.connect_phase = "idle"
        self._lock = asyncio.Lock()
        self._reconnect_task: asyncio.Task | None = None
        self._video_keepalive_task: asyncio.Task | None = None

    async def connect(self, robot_ip: str, use_new_signaling: bool) -> None:
        self.connect_in_progress = True
        try:
            async with self._lock:
                self.connect_phase = "disconnecting_previous"
                await self._disconnect_locked()
                last_exc: Exception | None = None

                for attempt in range(1, self.MAX_CONNECT_ATTEMPTS + 1):
                    conn = Go2Connection(
                        robot_ip=robot_ip,
                        use_new_signaling=use_new_signaling,
                        capture_video_frames=True,
                    )
                    try:
                        self.connect_phase = f"attempt_{attempt}"
                        logger.info("Connect attempt %d/%d to %s", attempt, self.MAX_CONNECT_ATTEMPTS, robot_ip)
                        await conn.connect()
                        self.connect_phase = "enabling_video"
                        # Request video multiple times — Go2 sometimes needs several requests
                        for _ in range(6):
                            conn.video(True)
                            if conn.latest_video_frame() is not None:
                                break
                            await asyncio.sleep(0.4)
                        self.conn = conn
                        self.robot_ip = robot_ip
                        self.use_new_signaling = use_new_signaling
                        self.last_error = None
                        self.connect_phase = "connected"
                        # Start background tasks for connection health
                        self._start_background_tasks()
                        return
                    except Exception as exc:
                        last_exc = exc
                        logger.warning("Connect attempt %d failed: %s", attempt, exc)
                        await conn.disconnect()
                        if attempt < self.MAX_CONNECT_ATTEMPTS:
                            self.connect_phase = f"retry_wait_{attempt}"
                            await asyncio.sleep(1.0)

                assert last_exc is not None
                self.connect_phase = "failed"
                raise last_exc
        finally:
            self.connect_in_progress = False

    def _start_background_tasks(self) -> None:
        self._stop_background_tasks()
        self._reconnect_task = asyncio.create_task(self._watch_connection())
        self._video_keepalive_task = asyncio.create_task(self._video_keepalive())

    def _stop_background_tasks(self) -> None:
        for task_name in ("_reconnect_task", "_video_keepalive_task"):
            task = getattr(self, task_name, None)
            if task:
                task.cancel()
            setattr(self, task_name, None)

    async def _watch_connection(self) -> None:
        """Monitor data channel health; auto-reconnect on drop."""
        while True:
            await asyncio.sleep(5.0)
            conn = self.conn
            if conn is None:
                return
            # Check if data channel is still alive
            dc_alive = conn.dc is not None and conn.dc.readyState == "open"
            if not dc_alive:
                logger.warning("Data channel lost (readyState=%s), attempting reconnect...",
                               conn.dc.readyState if conn.dc else "None")
                self.last_error = "Data channel lost, reconnecting..."
                self.connect_phase = "reconnecting"
                try:
                    await self.connect(self.robot_ip, self.use_new_signaling)
                    logger.info("Auto-reconnect succeeded")
                except Exception as e:
                    logger.error("Auto-reconnect failed: %s", e)
                    self.last_error = f"Auto-reconnect failed: {e}"
                return

    async def _video_keepalive(self) -> None:
        """Periodically re-request video to keep the stream alive."""
        while True:
            await asyncio.sleep(5.0)
            conn = self.conn
            if conn is None:
                return
            if conn._validated.is_set():
                conn.video(True)

    async def disconnect(self) -> None:
        async with self._lock:
            self._stop_background_tasks()
            await self._disconnect_locked()

    async def _disconnect_locked(self) -> None:
        if self.conn is not None:
            await self.conn.disconnect()
            self.conn = None

    def is_connected(self) -> bool:
        return self.conn is not None and self.conn._validated.is_set()

    def state_payload(self) -> dict:
        connected = self.is_connected()
        state = asdict(self.conn.state) if self.conn else None
        return {
            "connected": connected,
            "robot_ip": self.robot_ip,
            "use_new_signaling": self.use_new_signaling,
            "video_available": bool(self.conn and self.conn.latest_video_frame()),
            "connect_in_progress": self.connect_in_progress,
            "connect_phase": self.connect_phase,
            "llm_available": bool(os.getenv("OPENAI_API_KEY")),
            "llm_model": LLM_MODEL,
            "state": state,
            "last_error": self.last_error,
        }

    def require_connection(self) -> Go2Connection:
        if self.conn is None or not self.conn._validated.is_set():
            raise web.HTTPConflict(text="Robot is not connected")
        return self.conn

    def execute_command(self, name: str) -> None:
        conn = self.require_connection()
        if name not in COMMANDS:
            raise web.HTTPBadRequest(text=f"Unknown command: {name}")
        COMMANDS[name][1](conn)

    def move(self, x: float, y: float, yaw: float) -> None:
        conn = self.require_connection()
        if x == 0 and y == 0 and yaw == 0:
            conn.stop()
            return
        conn.move(x=x, y=y, yaw=yaw)

    def set_video(self, enabled: bool) -> None:
        self.require_connection().video(enabled)

    def latest_video_frame(self) -> bytes | None:
        conn = self.conn
        if conn is None:
            return None
        return conn.latest_video_frame()


def json_response(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _safe_state_for_llm(state: dict | None) -> dict:
    if not isinstance(state, dict):
        return {}

    return {
        "mode": state.get("mode"),
        "gait_type": state.get("gait_type"),
        "body_height": state.get("body_height"),
        "position": state.get("position"),
        "velocity": state.get("velocity"),
        "yaw_speed": state.get("yaw_speed"),
        "foot_force": state.get("foot_force"),
        "obstacles_avoid": state.get("obstacles_avoid"),
        "speed_level": state.get("speed_level"),
        "power_v": state.get("power_v"),
        "battery_soc": (state.get("battery") or {}).get("soc"),
        "imu_rpy": ((state.get("imu") or {}).get("rpy")),
    }


def _format_decision_summary(decision: dict) -> str:
    action = decision["action"]
    if action in LLM_MOVE_VECTORS:
        return f"{action} for {decision['duration_seconds']:.1f}s"
    if action == "NONE":
        return "No safe action"
    return action


async def _sustained_move(conn: Go2Connection, x: float, y: float, yaw: float, duration: float) -> None:
    """Send periodic move commands, then stop the robot."""
    interval = 0.1
    deadline = asyncio.get_running_loop().time() + duration
    while asyncio.get_running_loop().time() < deadline:
        conn.move(x=x, y=y, yaw=yaw)
        remaining = deadline - asyncio.get_running_loop().time()
        await asyncio.sleep(min(interval, max(0.0, remaining)))
    conn.stop()


def _normalize_llm_decision(raw: object, robot_state: dict | None) -> dict:
    if not isinstance(raw, dict):
        raise web.HTTPBadGateway(text="LLM returned invalid JSON payload")

    action = str(raw.get("action", "NONE")).strip().upper()
    reason = str(raw.get("reason", "")).strip() or "No explanation provided."
    safety_notes_raw = raw.get("safety_notes", [])
    safety_notes = [str(note).strip() for note in safety_notes_raw if str(note).strip()] if isinstance(safety_notes_raw, list) else []

    state = _safe_state_for_llm(robot_state)
    battery_soc = state.get("battery_soc")
    obstacles_avoid = state.get("obstacles_avoid")
    if action not in LLM_ACTIONS:
        raise web.HTTPBadGateway(text=f"LLM returned unsupported action: {action}")

    try:
        duration_seconds = round(_clamp(float(raw.get("duration_seconds", 1.0)), 0.0, MAX_LLM_MOVE_DURATION), 2)
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadGateway(text=f"LLM duration is invalid: {exc}") from exc

    if action in LLM_MOVE_VECTORS:
        duration_seconds = max(0.5, duration_seconds)
        if battery_soc is not None and battery_soc < MIN_BATTERY_FOR_MOVE:
            action = "NONE"
            reason = f"{reason} Movement blocked because battery is below {MIN_BATTERY_FOR_MOVE}%."
            safety_notes.append("Battery too low for movement.")
        elif obstacles_avoid is False:
            action = "NONE"
            reason = f"{reason} Movement blocked because obstacle avoidance is disabled."
            safety_notes.append("Obstacle avoidance is off.")
    else:
        duration_seconds = 0.0

    decision = {
        "action": action,
        "duration_seconds": duration_seconds,
        "reason": reason,
        "safety_notes": safety_notes,
        "summary": "",
    }
    decision["summary"] = _format_decision_summary(decision)
    return decision


async def _execute_llm_decision(controller: RobotController, decision: dict, robot_state: dict | None) -> dict:
    conn = controller.require_connection()
    action = decision["action"]
    execution = {
        "executed": False,
        "pre_actions": [],
        "final_state": robot_state,
    }

    if action == "NONE":
        return execution

    if action in {"STAND_UP", "STAND_DOWN", "STOP"}:
        if action == "STAND_UP":
            conn.stand_up()
        elif action == "STAND_DOWN":
            conn.stand_down()
        else:
            conn.stop()
        await asyncio.sleep(0.5)
        execution["executed"] = True
    elif action in LLM_MOVE_VECTORS:
        state = _safe_state_for_llm(robot_state)
        if AUTO_STAND_BEFORE_LLM_MOVE and state.get("mode") == 0:
            conn.stand_up()
            execution["pre_actions"].append("STAND_UP")
            await asyncio.sleep(1.5)

        vector = LLM_MOVE_VECTORS[action]
        await _sustained_move(
            conn,
            x=vector["x"],
            y=vector["y"],
            yaw=vector["yaw"],
            duration=decision["duration_seconds"],
        )
        await asyncio.sleep(0.3)
        execution["executed"] = True
    else:
        raise web.HTTPBadGateway(text=f"Unsupported action for execution: {action}")

    execution["final_state"] = controller.state_payload().get("state")
    return execution


async def _request_llm_decision(frame: bytes, robot_state: dict | None, goal: str = "") -> dict:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise web.HTTPServiceUnavailable(text="OPENAI_API_KEY is not set")

    image_base64 = base64.b64encode(frame).decode("ascii")
    telemetry = _safe_state_for_llm(robot_state)
    goal = goal.strip()[:MAX_LLM_GOAL_LENGTH]

    system_prompt = (
        "You are planning the next action for a real Unitree Go2 robot. "
        "Safety is more important than progress. "
        "You must only return data matching the provided JSON schema. "
        "Choose exactly one immediate action that best advances the goal. "
        "Allowed actions: FORWARD, BACKWARD, LEFT, RIGHT, TURN_LEFT, TURN_RIGHT, STAND_UP, STAND_DOWN, STOP, NONE. "
        "For motion actions, duration_seconds should usually be between 0.8 and 1.5 seconds. "
        "If the target object is visible ahead and the path looks clear, prefer FORWARD instead of NONE. "
        "Do not return NONE just because mode is 0; a motion action can be chosen and the server will stand the robot first if needed. "
        "Only return NONE when the scene is too uncertain or unsafe, or when movement would clearly not help. "
        "Never choose motion if battery_soc < 20 or if obstacles_avoid is false. "
        "Keep reason concise."
    )
    user_prompt = (
        "Analyze the current robot camera frame and telemetry. "
        f"User goal: {_json_text(goal or 'No explicit goal provided.')}. "
        "Choose exactly one next action. "
        f"Telemetry: {_json_text(telemetry)}"
    )

    payload = {
        "model": LLM_MODEL,
        "temperature": 0.1,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "go2_llm_action",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": sorted(LLM_ACTIONS),
                        },
                        "duration_seconds": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": MAX_LLM_MOVE_DURATION,
                        },
                        "reason": {"type": "string"},
                        "safety_notes": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["action", "duration_seconds", "reason", "safety_notes"],
                },
            },
        },
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}},
                ],
            },
        ],
    }

    async with controller_session() as session:
        async with session.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
        ) as response:
            raw_text = await response.text()
            if response.status >= 400:
                raise web.HTTPBadGateway(text=f"LLM request failed: {raw_text}")

    try:
        response_payload = json.loads(raw_text)
        content = response_payload["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise web.HTTPBadGateway(text=f"Failed to decode LLM response: {exc}") from exc

    return _normalize_llm_decision(parsed, robot_state)


def controller_session():
    return ClientSession(timeout=ClientTimeout(total=45))


async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html")


async def handle_commands(request: web.Request) -> web.Response:
    items = [{"name": name, "label": label} for name, (label, _) in COMMANDS.items()]
    return json_response({"commands": items})


async def handle_status(request: web.Request) -> web.Response:
    controller: RobotController = request.app["controller"]
    return json_response(controller.state_payload())


async def handle_connect(request: web.Request) -> web.Response:
    controller: RobotController = request.app["controller"]
    body = await request.json()
    robot_ip = str(body.get("robot_ip") or DEFAULT_IP)
    use_new_signaling = not bool(body.get("old_signaling", False))

    try:
        await controller.connect(robot_ip=robot_ip, use_new_signaling=use_new_signaling)
    except Exception as exc:
        controller.last_error = str(exc)
        logger.exception("Failed to connect")
        return json_response({"ok": False, "error": str(exc)}, status=500)

    return json_response({"ok": True, **controller.state_payload()})


async def handle_disconnect(request: web.Request) -> web.Response:
    controller: RobotController = request.app["controller"]
    await controller.disconnect()
    return json_response({"ok": True, **controller.state_payload()})


async def handle_command(request: web.Request) -> web.Response:
    controller: RobotController = request.app["controller"]
    body = await request.json()
    command = str(body.get("command", ""))
    controller.execute_command(command)
    return json_response({"ok": True})


async def handle_move(request: web.Request) -> web.Response:
    controller: RobotController = request.app["controller"]
    body = await request.json()
    x = float(body.get("x", 0.0))
    y = float(body.get("y", 0.0))
    yaw = float(body.get("yaw", 0.0))
    controller.move(x=x, y=y, yaw=yaw)
    return json_response({"ok": True})


async def handle_video(request: web.Request) -> web.Response:
    controller: RobotController = request.app["controller"]
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    controller.set_video(enabled)
    return json_response({"ok": True, "enabled": enabled})


async def handle_set(request: web.Request) -> web.Response:
    controller: RobotController = request.app["controller"]
    conn = controller.require_connection()
    body = await request.json()
    param = str(body.get("param", ""))
    value = body.get("value")

    if param == "body_height":
        conn.set_body_height(float(value))
    elif param == "foot_raise_height":
        conn.set_foot_raise_height(float(value))
    elif param == "speed_level":
        conn.set_speed_level(int(value))
    elif param == "gait":
        conn.switch_gait(int(value))
    elif param == "euler":
        if isinstance(value, list) and len(value) == 3:
            conn.set_euler(float(value[0]), float(value[1]), float(value[2]))
        else:
            raise web.HTTPBadRequest(text="euler requires [roll, pitch, yaw]")
    else:
        raise web.HTTPBadRequest(text=f"Unknown parameter: {param}")

    return json_response({"ok": True, "param": param, "value": value})


async def handle_video_frame(request: web.Request) -> web.Response:
    controller: RobotController = request.app["controller"]
    controller.require_connection()

    frame = controller.latest_video_frame()
    if frame is None:
        raise web.HTTPServiceUnavailable(text="No video frame available yet")

    return web.Response(
        body=frame,
        headers={
            "Content-Type": "image/jpeg",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        },
    )


async def handle_video_stream(request: web.Request) -> web.StreamResponse:
    controller: RobotController = request.app["controller"]
    controller.require_connection()

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "multipart/x-mixed-replace; boundary=frame",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Connection": "close",
        },
    )
    await response.prepare(request)

    try:
        while True:
            frame = controller.latest_video_frame()
            if frame:
                await response.write(
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii")
                    + frame
                    + b"\r\n"
                )
            await asyncio.sleep(0.1)
    except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
        pass

    return response


async def handle_llm_action(request: web.Request) -> web.Response:
    controller: RobotController = request.app["controller"]
    controller.require_connection()
    body = await request.json()

    frame = controller.latest_video_frame()
    if frame is None:
        raise web.HTTPServiceUnavailable(text="No video frame available yet")

    state = controller.state_payload().get("state")
    goal = str(body.get("goal", "")).strip()
    decision = await _request_llm_decision(frame, state, goal=goal)
    execution = await _execute_llm_decision(controller, decision, state)
    return json_response({"ok": True, "goal": goal, "decision": decision, "execution": execution})


async def on_cleanup(app: web.Application) -> None:
    controller: RobotController = app["controller"]
    controller._stop_background_tasks()
    await controller.disconnect()


def build_app() -> web.Application:
    app = web.Application()
    app["controller"] = RobotController()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/commands", handle_commands)
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/connect", handle_connect)
    app.router.add_post("/api/disconnect", handle_disconnect)
    app.router.add_post("/api/command", handle_command)
    app.router.add_post("/api/move", handle_move)
    app.router.add_post("/api/video", handle_video)
    app.router.add_post("/api/set", handle_set)
    app.router.add_post("/api/llm-action", handle_llm_action)
    app.router.add_get("/api/video-frame", handle_video_frame)
    app.router.add_get("/api/video-stream", handle_video_stream)
    app.router.add_static("/", WEB_DIR)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Web UI for Unitree Go2")
    parser.add_argument("--host", default="127.0.0.1", help="Web server host")
    parser.add_argument("--port", type=int, default=8080, help="Web server port")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.debug:
        logging.getLogger("aiortc").setLevel(logging.WARNING)
        logging.getLogger("aioice").setLevel(logging.WARNING)
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
        logging.getLogger("aiohttp.server").setLevel(logging.WARNING)
        logging.getLogger("go2.connection").setLevel(logging.WARNING)

    web.run_app(
        build_app(),
        host=args.host,
        port=args.port,
        access_log=None if not args.debug else logging.getLogger("aiohttp.access"),
    )


if __name__ == "__main__":
    main()
