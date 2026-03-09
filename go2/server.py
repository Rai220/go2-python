#!/usr/bin/env python3
"""Persistent HTTP server for Unitree Go2 robot.

Maintains a single WebRTC connection, exposes REST endpoints for fast
curl-based control without per-command reconnection overhead.
"""

import argparse
import asyncio
import io
import json
import logging
from pathlib import Path

from aiohttp import web
from PIL import Image

from go2.connection import Go2Connection
from go2.commands import SportCommand
from go2.constants import DEFAULT_IP

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers (from main.py)
# ---------------------------------------------------------------------------

SIMPLE_COMMANDS = {
    "stand_up":       ("Stand up",                    "stand_up"),
    "stand_down":     ("Lie down",                    "stand_down"),
    "sit":            ("Sit",                         "sit"),
    "balance":        ("Balance stand",               "balance_stand"),
    "recovery":       ("Recovery stand",              "recovery_stand"),
    "stop":           ("Stop moving",                 "stop"),
    "damp":           ("Damp (disable motors)",       "damp"),
    "hello":          ("Wave hello",                  "hello"),
    "stretch":        ("Stretch",                     "stretch"),
    "content":        ("Happy content gesture",       "content"),
    "wallow":         ("Wallow",                      "wallow"),
    "dance1":         ("Dance 1",                     "dance1"),
    "dance2":         ("Dance 2",                     "dance2"),
    "front_flip":     ("Front flip",                  "front_flip"),
    "front_jump":     ("Front jump",                  "front_jump"),
    "front_pounce":   ("Front pounce",                "front_pounce"),
    "wiggle_hips":    ("Wiggle hips",                 "wiggle_hips"),
    "finger_heart":   ("Finger heart",                "finger_heart"),
    "handstand":      ("Handstand",                   "handstand"),
    "cross_step":     ("Cross step",                  "cross_step"),
    "bound":          ("Bound",                       "bound"),
    "moon_walk":      ("Moon walk",                   "moon_walk"),
    "economic_gait":  ("Economic gait",               "economic_gait"),
    "lead_follow":    ("Lead follow",                 "lead_follow"),
}


def _state_to_dict(state) -> dict:
    """Convert RobotState to a flat JSON-friendly dict."""
    return {
        "battery_soc": state.battery.soc,
        "battery_current": state.battery.current,
        "battery_cycle": state.battery.cycle,
        "power_v": state.power_v,
        "mode": state.mode,
        "gait_type": state.gait_type,
        "body_height": state.body_height,
        "foot_raise_height": state.foot_raise_height,
        "speed_level": state.speed_level,
        "position": state.position,
        "velocity": state.velocity,
        "yaw_speed": state.yaw_speed,
        "foot_force": state.foot_force,
        "imu": {
            "rpy": state.imu.rpy,
            "quaternion": state.imu.quaternion,
            "gyroscope": state.imu.gyroscope,
            "accelerometer": state.imu.accelerometer,
            "temperature": state.imu.temperature,
        },
        "motors": [
            {"q": m.q, "temperature": m.temperature, "lost": m.lost}
            for m in state.motors
        ],
        "volume": state.volume,
        "brightness": state.brightness,
        "obstacles_avoid": state.obstacles_avoid,
    }


async def _sustained_move(conn: Go2Connection, x: float, y: float, yaw: float, duration: float) -> None:
    """Send MOVE commands repeatedly for the given duration, then stop."""
    INTERVAL = 0.1
    deadline = asyncio.get_running_loop().time() + duration
    while asyncio.get_running_loop().time() < deadline:
        conn.move(x=x, y=y, yaw=yaw)
        remaining = deadline - asyncio.get_running_loop().time()
        await asyncio.sleep(min(INTERVAL, max(0, remaining)))
    conn.stop()


def _resize_frame(jpeg_bytes: bytes, width: int) -> bytes:
    """Resize JPEG frame to given width, preserving aspect ratio."""
    img = Image.open(io.BytesIO(jpeg_bytes))
    if img.width <= width:
        return jpeg_bytes
    ratio = width / img.width
    new_height = int(img.height * ratio)
    img = img.resize((width, new_height), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


async def _save_frame_with_retry(conn: Go2Connection, timeout: float = 10.0) -> bytes:
    """Repeatedly request video until a frame arrives."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        conn.video(True)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"No video frame received within {timeout:.1f}s")
        try:
            return await conn.wait_for_video_frame(timeout=min(1.5, remaining))
        except TimeoutError:
            await asyncio.sleep(0.35)


async def _do_snap(conn: Go2Connection, snap_path: str, snap_width: int | None) -> dict:
    """Capture a frame, optionally resize, save to file, return info dict."""
    frame = await _save_frame_with_retry(conn)
    if snap_width and snap_width > 0:
        frame = _resize_frame(frame, snap_width)
    p = Path(snap_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(frame)
    return {"path": str(p), "size_bytes": len(frame)}


# ---------------------------------------------------------------------------
# Robot connection manager
# ---------------------------------------------------------------------------

class RobotManager:
    """Manages a persistent WebRTC connection to the robot."""

    MAX_CONNECT_ATTEMPTS = 4

    def __init__(self) -> None:
        self.conn: Go2Connection | None = None
        self.robot_ip: str = DEFAULT_IP
        self._lock = asyncio.Lock()  # serializes move/exec commands
        self._reconnect_task: asyncio.Task | None = None
        self._video_keepalive_task: asyncio.Task | None = None

    async def connect(self, robot_ip: str) -> None:
        """Connect to robot with retries."""
        await self._cleanup_tasks()
        if self.conn is not None:
            await self.conn.disconnect()
            self.conn = None

        last_exc: Exception | None = None
        for attempt in range(1, self.MAX_CONNECT_ATTEMPTS + 1):
            conn = Go2Connection(
                robot_ip=robot_ip,
                use_new_signaling=True,
                capture_video_frames=True,
            )
            try:
                logger.info("Connect attempt %d/%d to %s", attempt, self.MAX_CONNECT_ATTEMPTS, robot_ip)
                await conn.connect()
                # Request video several times to get keyframe
                for _ in range(6):
                    conn.video(True)
                    if conn.latest_video_frame() is not None:
                        break
                    await asyncio.sleep(0.4)
                self.conn = conn
                self.robot_ip = robot_ip
                self._start_tasks()
                logger.info("Connected to %s", robot_ip)
                return
            except Exception as exc:
                last_exc = exc
                logger.warning("Connect attempt %d failed: %s", attempt, exc)
                await conn.disconnect()
                if attempt < self.MAX_CONNECT_ATTEMPTS:
                    await asyncio.sleep(1.0)
        raise last_exc  # type: ignore[misc]

    def _start_tasks(self) -> None:
        self._reconnect_task = asyncio.create_task(self._watch_connection())
        self._video_keepalive_task = asyncio.create_task(self._video_keepalive())

    async def _cleanup_tasks(self) -> None:
        for attr in ("_reconnect_task", "_video_keepalive_task"):
            task = getattr(self, attr)
            if task:
                task.cancel()
                setattr(self, attr, None)

    async def _watch_connection(self) -> None:
        """Auto-reconnect on data channel drop."""
        while True:
            await asyncio.sleep(5.0)
            conn = self.conn
            if conn is None:
                return
            dc_alive = conn.dc is not None and conn.dc.readyState == "open"
            if not dc_alive:
                logger.warning("Data channel lost, reconnecting...")
                try:
                    await self.connect(self.robot_ip)
                except Exception as e:
                    logger.error("Auto-reconnect failed: %s", e)
                return

    async def _video_keepalive(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            conn = self.conn
            if conn is None:
                return
            if conn._validated.is_set():
                conn.video(True)

    async def disconnect(self) -> None:
        await self._cleanup_tasks()
        if self.conn is not None:
            await self.conn.disconnect()
            self.conn = None

    def require(self) -> Go2Connection:
        """Return connection or raise HTTP error."""
        if self.conn is None or not self.conn._validated.is_set():
            raise web.HTTPConflict(text=json.dumps({"status": "error", "error": "Robot not connected"}),
                                   content_type="application/json")
        return self.conn

    def is_connected(self) -> bool:
        return self.conn is not None and self.conn._validated.is_set()


# ---------------------------------------------------------------------------
# HTTP Handlers
# ---------------------------------------------------------------------------

def _error(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"status": "error", "error": msg}, status=status)


async def handle_state(request: web.Request) -> web.Response:
    mgr: RobotManager = request.app["mgr"]
    conn = mgr.require()
    return web.json_response({"status": "ok", "state": _state_to_dict(conn.state)})


async def handle_move(request: web.Request) -> web.Response:
    mgr: RobotManager = request.app["mgr"]
    conn = mgr.require()

    body = await request.json()
    x = float(body.get("x", 0.0))
    y = float(body.get("y", 0.0))
    yaw = float(body.get("yaw", 0.0))
    duration = float(body.get("duration", 2.0))
    snap_path = body.get("snap_path")
    snap_width = body.get("snap_width")

    async with mgr._lock:
        if duration > 0:
            await _sustained_move(conn, x, y, yaw, duration)
        else:
            conn.move(x=x, y=y, yaw=yaw)
        await asyncio.sleep(0.3)

        result = {
            "status": "ok",
            "command": "move",
            "params": {"x": x, "y": y, "yaw": yaw, "duration": duration},
            "state": _state_to_dict(conn.state),
        }
        if snap_path:
            try:
                result["snap"] = await _do_snap(conn, snap_path, snap_width)
            except Exception as e:
                result["snap"] = {"error": str(e)}

    return web.json_response(result)


async def handle_exec(request: web.Request) -> web.Response:
    mgr: RobotManager = request.app["mgr"]
    conn = mgr.require()

    body = await request.json()

    # Single command or list
    commands = body.get("commands") or [body.get("command", "")]
    if isinstance(commands, str):
        commands = [commands]
    delay = float(body.get("delay", 1.0))
    wait = float(body.get("wait", 1.0))
    snap_path = body.get("snap_path")
    snap_width = body.get("snap_width")

    async with mgr._lock:
        results = []
        for i, cmd_name in enumerate(commands):
            cmd_lower = cmd_name.lower()
            if cmd_lower not in SIMPLE_COMMANDS:
                results.append({"command": cmd_lower, "status": "error", "error": "unknown command"})
                continue
            desc, method = SIMPLE_COMMANDS[cmd_lower]
            getattr(conn, method)()
            results.append({"command": cmd_lower, "status": "ok", "description": desc})
            if i < len(commands) - 1:
                await asyncio.sleep(delay)

        await asyncio.sleep(wait)

        result = {
            "status": "ok",
            "results": results,
            "state": _state_to_dict(conn.state),
        }
        if snap_path:
            try:
                result["snap"] = await _do_snap(conn, snap_path, snap_width)
            except Exception as e:
                result["snap"] = {"error": str(e)}

    return web.json_response(result)


async def handle_set(request: web.Request) -> web.Response:
    mgr: RobotManager = request.app["mgr"]
    conn = mgr.require()

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
        elif isinstance(value, str):
            parts = value.split(",")
            if len(parts) != 3:
                return _error("euler requires 3 values: roll,pitch,yaw")
            conn.set_euler(float(parts[0]), float(parts[1]), float(parts[2]))
        else:
            return _error("euler requires [r,p,y] or 'r,p,y'")
    elif param == "video":
        if isinstance(value, bool):
            conn.video(value)
        else:
            conn.video(str(value).lower() in ("on", "true", "1"))
    elif param == "audio":
        if isinstance(value, bool):
            conn.audio(value)
        else:
            conn.audio(str(value).lower() in ("on", "true", "1"))
    else:
        return _error(f"Unknown parameter: {param}")

    await asyncio.sleep(0.5)
    return web.json_response({
        "status": "ok",
        "param": param,
        "value": value,
        "state": _state_to_dict(conn.state),
    })


async def handle_raw(request: web.Request) -> web.Response:
    mgr: RobotManager = request.app["mgr"]
    conn = mgr.require()

    body = await request.json()
    api_id = int(body.get("api_id", 0))
    parameter = body.get("param")

    conn.send_command(api_id, parameter)
    await asyncio.sleep(1.0)

    return web.json_response({
        "status": "ok",
        "api_id": api_id,
        "parameter": parameter,
        "state": _state_to_dict(conn.state),
    })


async def handle_image(request: web.Request) -> web.Response:
    mgr: RobotManager = request.app["mgr"]
    conn = mgr.require()

    width = request.query.get("width")
    save_path = request.query.get("path")

    frame = await _save_frame_with_retry(conn)

    if width:
        frame = _resize_frame(frame, int(width))

    if save_path:
        p = Path(save_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(frame)

    return web.Response(
        body=frame,
        content_type="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


async def handle_connect(request: web.Request) -> web.Response:
    mgr: RobotManager = request.app["mgr"]
    body = await request.json()
    robot_ip = str(body.get("robot_ip", mgr.robot_ip))

    try:
        await mgr.connect(robot_ip)
    except Exception as e:
        return _error(f"Connection failed: {e}", status=503)

    return web.json_response({
        "status": "ok",
        "robot_ip": robot_ip,
        "state": _state_to_dict(mgr.conn.state),
    })


async def handle_disconnect(request: web.Request) -> web.Response:
    mgr: RobotManager = request.app["mgr"]
    await mgr.disconnect()
    return web.json_response({"status": "ok"})


async def handle_list(request: web.Request) -> web.Response:
    commands = {name: desc for name, (desc, _) in SIMPLE_COMMANDS.items()}
    params = {
        "body_height": "float -- body height (0.1-0.32)",
        "foot_raise_height": "float -- foot raise height",
        "speed_level": "int -- speed level 1-3",
        "gait": "int -- gait type 0=idle, 1=trot, 2=run, 3=stairs",
        "euler": "r,p,y -- body euler angles",
        "video": "on/off -- toggle video stream",
        "audio": "on/off -- toggle audio stream",
    }
    api_ids = {cmd.name: cmd.value for cmd in SportCommand}
    return web.json_response({"commands": commands, "parameters": params, "api_ids": api_ids})


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

async def on_startup(app: web.Application) -> None:
    mgr: RobotManager = app["mgr"]
    robot_ip = app["robot_ip"]

    async def bg_connect():
        try:
            await mgr.connect(robot_ip)
        except Exception as e:
            logger.error("Initial connection to %s failed: %s", robot_ip, e)
            logger.info("Server running without robot connection. Use POST /connect to retry.")

    asyncio.create_task(bg_connect())


async def on_cleanup(app: web.Application) -> None:
    mgr: RobotManager = app["mgr"]
    await mgr.disconnect()


def build_app(robot_ip: str) -> web.Application:
    app = web.Application()
    app["mgr"] = RobotManager()
    app["robot_ip"] = robot_ip

    app.router.add_get("/state", handle_state)
    app.router.add_post("/move", handle_move)
    app.router.add_post("/exec", handle_exec)
    app.router.add_post("/set", handle_set)
    app.router.add_post("/raw", handle_raw)
    app.router.add_get("/image", handle_image)
    app.router.add_post("/connect", handle_connect)
    app.router.add_post("/disconnect", handle_disconnect)
    app.router.add_get("/list", handle_list)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Persistent HTTP server for Unitree Go2 robot")
    parser.add_argument("--ip", default=DEFAULT_IP, help=f"Robot IP address (default: {DEFAULT_IP})")
    parser.add_argument("--host", default="127.0.0.1", help="Server bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8090, help="Server port (default: 8090)")
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

    web.run_app(
        build_app(args.ip),
        host=args.host,
        port=args.port,
        access_log=None if not args.debug else logging.getLogger("aiohttp.access"),
    )


if __name__ == "__main__":
    main()
