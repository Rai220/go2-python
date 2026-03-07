#!/usr/bin/env python3
"""CLI for controlling Unitree Go2 robot via WebRTC.

Designed for both interactive use and programmatic control by neural networks.
All subcommands support --json for machine-readable output.
"""

import argparse
import asyncio
import base64
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path

from go2.connection import Go2Connection
from go2.commands import SportCommand
from go2.constants import DEFAULT_IP


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _print_state_human(state) -> None:
    """Print telemetry in human-readable format."""
    print(f"  Battery:          {state.battery.soc}%  ({state.power_v:.1f}V)")
    print(f"  Mode:             {state.mode}")
    print(f"  Gait type:        {state.gait_type}")
    print(f"  Body height:      {state.body_height:.3f}")
    print(f"  Foot raise:       {state.foot_raise_height:.3f}")
    print(f"  Speed level:      {state.speed_level}")
    print(f"  Position:         ({state.position[0]:.3f}, {state.position[1]:.3f}, {state.position[2]:.3f})")
    print(f"  Velocity:         ({state.velocity[0]:.3f}, {state.velocity[1]:.3f}, {state.velocity[2]:.3f})")
    print(f"  Yaw speed:        {state.yaw_speed:.3f}")
    print(f"  IMU RPY:          ({state.imu.rpy[0]:.2f}, {state.imu.rpy[1]:.2f}, {state.imu.rpy[2]:.2f})")
    print(f"  IMU Quaternion:   ({state.imu.quaternion[0]:.3f}, {state.imu.quaternion[1]:.3f}, {state.imu.quaternion[2]:.3f}, {state.imu.quaternion[3]:.3f})")
    print(f"  IMU Gyro:         ({state.imu.gyroscope[0]:.3f}, {state.imu.gyroscope[1]:.3f}, {state.imu.gyroscope[2]:.3f})")
    print(f"  IMU Accel:        ({state.imu.accelerometer[0]:.3f}, {state.imu.accelerometer[1]:.3f}, {state.imu.accelerometer[2]:.3f})")
    print(f"  Foot force:       {state.foot_force}")
    print(f"  Obstacles avoid:  {state.obstacles_avoid}")
    print(f"  Volume:           {state.volume}")
    print(f"  Brightness:       {state.brightness}")
    motors_temp = [m.temperature for m in state.motors]
    print(f"  Motors temp:      {motors_temp}")


def _live_telemetry_line(state) -> None:
    """Print one-line live telemetry (for --telemetry flag)."""
    print(
        f"\r  Bat:{state.battery.soc}% "
        f"Pos:({state.position[0]:.2f},{state.position[1]:.2f},{state.position[2]:.2f}) "
        f"Vel:({state.velocity[0]:.2f},{state.velocity[1]:.2f},{state.velocity[2]:.2f}) "
        f"RPY:({state.imu.rpy[0]:.1f},{state.imu.rpy[1]:.1f},{state.imu.rpy[2]:.1f}) "
        f"Mode:{state.mode}",
        end="",
        flush=True,
    )


async def _connect(args, retries: int = 3) -> Go2Connection:
    """Create and connect Go2Connection from parsed args, with retries."""
    last_exc = None
    for attempt in range(1, retries + 1):
        conn = Go2Connection(
            robot_ip=args.ip,
            use_new_signaling=not args.old_signaling,
            capture_video_frames=getattr(args, '_need_video', False),
        )
        try:
            await conn.connect()
            return conn
        except Exception as e:
            last_exc = e
            logger.warning("Connect attempt %d/%d failed: %s", attempt, retries, e)
            await conn.disconnect()
            if attempt < retries:
                await asyncio.sleep(1.0)
    raise last_exc


async def _save_frame_with_retry(conn: Go2Connection, output_path: str, timeout: float) -> str:
    """Repeatedly request video until a frame arrives, then save it."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        conn.video(True)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"No video frame received within {timeout:.1f}s")
        try:
            return await conn.save_video_frame(output_path, timeout=min(1.5, remaining))
        except TimeoutError:
            await asyncio.sleep(0.35)


async def _maybe_snap(conn: Go2Connection, args) -> dict | None:
    """If --snap is set, capture a frame and return snap info dict."""
    if not getattr(args, 'snap', None):
        return None
    try:
        saved = await _save_frame_with_retry(conn, args.snap, timeout=10.0)
        snap_info = {"path": saved, "size_bytes": Path(saved).stat().st_size}
        if not args.json:
            print(f"  Snap: {saved}")
        return snap_info
    except Exception as e:
        snap_info = {"error": str(e)}
        if not args.json:
            print(f"  Snap failed: {e}")
        return snap_info


# ---------------------------------------------------------------------------
# All known simple commands (name -> method name on Go2Connection)
# ---------------------------------------------------------------------------

SIMPLE_COMMANDS = {
    # Basic state
    "stand_up":       ("Stand up",                    "stand_up"),
    "stand_down":     ("Lie down",                    "stand_down"),
    "sit":            ("Sit",                         "sit"),
    "balance":        ("Balance stand",               "balance_stand"),
    "recovery":       ("Recovery stand",              "recovery_stand"),
    "stop":           ("Stop moving",                 "stop"),
    "damp":           ("Damp (disable motors)",       "damp"),

    # Gestures
    "hello":          ("Wave hello",                  "hello"),
    "stretch":        ("Stretch",                     "stretch"),
    "content":        ("Happy content gesture",       "content"),
    "wallow":         ("Wallow",                      "wallow"),

    # Dance
    "dance1":         ("Dance 1",                     "dance1"),
    "dance2":         ("Dance 2",                     "dance2"),

    # Tricks
    "front_flip":     ("Front flip",                  "front_flip"),
    "front_jump":     ("Front jump",                  "front_jump"),
    "front_pounce":   ("Front pounce",                "front_pounce"),
    "wiggle_hips":    ("Wiggle hips",                 "wiggle_hips"),
    "finger_heart":   ("Finger heart",                "finger_heart"),
    "handstand":      ("Handstand",                   "handstand"),
    "cross_step":     ("Cross step",                  "cross_step"),
    "bound":          ("Bound",                       "bound"),
    "moon_walk":      ("Moon walk",                   "moon_walk"),

    # Gait modes
    "economic_gait":  ("Economic gait",               "economic_gait"),
    "lead_follow":    ("Lead follow",                 "lead_follow"),
}


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

async def _sustained_move(conn, x: float, y: float, yaw: float, duration: float) -> None:
    """Send MOVE commands repeatedly for the given duration, then stop.

    The Go2 robot requires periodic move commands to sustain movement.
    A single MOVE command only produces a brief twitch.
    """
    INTERVAL = 0.1  # send every 100ms
    deadline = asyncio.get_running_loop().time() + duration
    while asyncio.get_running_loop().time() < deadline:
        conn.move(x=x, y=y, yaw=yaw)
        remaining = deadline - asyncio.get_running_loop().time()
        await asyncio.sleep(min(INTERVAL, max(0, remaining)))
    conn.stop()


async def cmd_move(args) -> None:
    """Move the robot with given velocity for a duration."""
    use_json = args.json
    conn = await _connect(args)
    try:
        # Wait a bit for telemetry to arrive
        await asyncio.sleep(0.5)

        if args.duration > 0:
            await _sustained_move(conn, args.x, args.y, args.yaw, args.duration)
        else:
            conn.move(x=args.x, y=args.y, yaw=args.yaw)

        # Collect final state
        await asyncio.sleep(0.3)

        snap = await _maybe_snap(conn, args)

        if use_json:
            result = {
                "status": "ok",
                "command": "move",
                "params": {"x": args.x, "y": args.y, "yaw": args.yaw, "duration": args.duration},
                "state": _state_to_dict(conn.state),
            }
            if snap:
                result["snap"] = snap
            print(json.dumps(result))
        else:
            action = f"move x={args.x} y={args.y} yaw={args.yaw}"
            if args.duration > 0:
                action += f" for {args.duration}s (stopped)"
            else:
                action += " (continuous — send 'stop' to halt)"
            print(f"  {action}")
    finally:
        await conn.disconnect()


# Built-in movement shortcuts for exec: name -> (description, x, y, yaw)
MOVE_COMMANDS = {
    "forward":    ("Move forward",   0.3,  0.0, 0.0),
    "backward":   ("Move backward", -0.3,  0.0, 0.0),
    "left":       ("Strafe left",    0.0,  0.3, 0.0),
    "right":      ("Strafe right",   0.0, -0.3, 0.0),
    "turn_left":  ("Turn left",      0.0,  0.0, 0.5),
    "turn_right": ("Turn right",     0.0,  0.0,-0.5),
}


async def cmd_exec(args) -> None:
    """Execute one or more robot commands."""
    use_json = args.json
    conn = await _connect(args)
    try:
        await asyncio.sleep(0.5)

        results = []
        for cmd_name in args.commands:
            cmd_lower = cmd_name.lower()

            if cmd_lower in SIMPLE_COMMANDS:
                desc, method = SIMPLE_COMMANDS[cmd_lower]
                getattr(conn, method)()
                results.append({"command": cmd_lower, "status": "ok", "description": desc})
                if not use_json:
                    print(f"  {cmd_lower}: {desc}")
                # Wait for the command to take effect
                if args.duration > 0:
                    await asyncio.sleep(args.duration)

            elif cmd_lower in MOVE_COMMANDS:
                desc, mx, my, myaw = MOVE_COMMANDS[cmd_lower]
                dur = args.duration if args.duration > 0 else 2.0
                if not use_json:
                    print(f"  {cmd_lower}: {desc} for {dur}s")
                await _sustained_move(conn, mx, my, myaw, dur)
                results.append({"command": cmd_lower, "status": "ok", "description": desc, "duration": dur})

            else:
                results.append({"command": cmd_lower, "status": "error", "error": "unknown command"})
                if not use_json:
                    print(f"  {cmd_lower}: unknown command")

            if args.delay > 0 and cmd_name != args.commands[-1]:
                await asyncio.sleep(args.delay)

        # Wait for final state
        await asyncio.sleep(args.wait)

        snap = await _maybe_snap(conn, args)

        if use_json:
            result = {
                "status": "ok",
                "results": results,
                "state": _state_to_dict(conn.state),
            }
            if snap:
                result["snap"] = snap
            print(json.dumps(result))
    finally:
        await conn.disconnect()


async def cmd_set(args) -> None:
    """Set a robot parameter."""
    use_json = args.json
    conn = await _connect(args)
    try:
        await asyncio.sleep(0.5)

        param = args.param
        value = args.value

        if param == "body_height":
            conn.set_body_height(float(value))
        elif param == "foot_raise_height":
            conn.set_foot_raise_height(float(value))
        elif param == "speed_level":
            conn.set_speed_level(int(value))
        elif param == "gait":
            conn.switch_gait(int(value))
        elif param == "euler":
            parts = value.split(",")
            if len(parts) != 3:
                raise ValueError("euler requires 3 comma-separated values: roll,pitch,yaw")
            conn.set_euler(float(parts[0]), float(parts[1]), float(parts[2]))
        elif param == "video":
            conn.video(value.lower() in ("on", "true", "1"))
        elif param == "audio":
            conn.audio(value.lower() in ("on", "true", "1"))
        else:
            msg = f"Unknown parameter: {param}"
            if use_json:
                print(json.dumps({"status": "error", "error": msg}))
            else:
                print(f"  Error: {msg}")
            return

        await asyncio.sleep(0.5)

        snap = await _maybe_snap(conn, args)

        if use_json:
            result = {
                "status": "ok",
                "param": param,
                "value": value,
                "state": _state_to_dict(conn.state),
            }
            if snap:
                result["snap"] = snap
            print(json.dumps(result))
        else:
            print(f"  Set {param} = {value}")
    finally:
        await conn.disconnect()


async def cmd_telemetry(args) -> None:
    """Get robot telemetry."""
    use_json = args.json
    conn = await _connect(args)
    try:
        # Wait for telemetry to populate
        await asyncio.sleep(args.wait)

        if args.stream:
            # Stream mode: print telemetry continuously
            count = 0
            try:
                while args.count == 0 or count < args.count:
                    await asyncio.sleep(args.interval)
                    if use_json:
                        print(json.dumps({
                            "ts": time.time(),
                            "state": _state_to_dict(conn.state),
                        }), flush=True)
                    else:
                        _live_telemetry_line(conn.state)
                    count += 1
            except KeyboardInterrupt:
                if not use_json:
                    print()  # newline after \r output
        else:
            if use_json:
                print(json.dumps({
                    "ts": time.time(),
                    "state": _state_to_dict(conn.state),
                }))
            else:
                _print_state_human(conn.state)
    finally:
        await conn.disconnect()


async def cmd_image(args) -> None:
    """Capture camera image."""
    use_json = args.json
    args._need_video = True
    conn = await _connect(args)
    try:
        await asyncio.sleep(0.5)

        if args.stream:
            # Continuous frame capture
            count = 0
            try:
                while args.count == 0 or count < args.count:
                    frame = await conn.wait_for_video_frame(timeout=args.timeout)

                    if args.output == "-":
                        # stdout base64
                        if use_json:
                            print(json.dumps({
                                "ts": time.time(),
                                "frame": count,
                                "image_base64": base64.b64encode(frame).decode(),
                                "size_bytes": len(frame),
                                "state": _state_to_dict(conn.state),
                            }), flush=True)
                        else:
                            sys.stdout.buffer.write(frame)
                            sys.stdout.buffer.flush()
                    else:
                        # File output with frame numbering
                        p = Path(args.output)
                        if args.count != 1:
                            path = p.parent / f"{p.stem}_{count:04d}{p.suffix}"
                        else:
                            path = p
                        with open(path, "wb") as f:
                            f.write(frame)
                        if use_json:
                            print(json.dumps({
                                "ts": time.time(),
                                "frame": count,
                                "path": str(path),
                                "size_bytes": len(frame),
                            }), flush=True)
                        else:
                            print(f"  Frame {count}: {path}")

                    count += 1
                    if args.count == 0 or count < args.count:
                        await asyncio.sleep(args.interval)

            except KeyboardInterrupt:
                pass
        else:
            # Single frame
            saved_path = await _save_frame_with_retry(
                conn, args.output if args.output != "-" else "/tmp/go2_frame.jpg",
                timeout=args.timeout,
            )

            if args.output == "-":
                frame = conn.latest_video_frame()
                if use_json:
                    print(json.dumps({
                        "ts": time.time(),
                        "image_base64": base64.b64encode(frame).decode(),
                        "size_bytes": len(frame),
                        "state": _state_to_dict(conn.state),
                    }))
                else:
                    sys.stdout.buffer.write(frame)
            else:
                if use_json:
                    print(json.dumps({
                        "ts": time.time(),
                        "path": saved_path,
                        "size_bytes": Path(saved_path).stat().st_size,
                        "state": _state_to_dict(conn.state),
                    }))
                else:
                    print(f"  Saved: {saved_path}")
    finally:
        await conn.disconnect()


async def cmd_raw(args) -> None:
    """Send raw sport command by API ID."""
    use_json = args.json
    conn = await _connect(args)
    try:
        await asyncio.sleep(0.5)

        parameter = None
        if args.param:
            try:
                parameter = json.loads(args.param)
            except json.JSONDecodeError:
                parameter = args.param

        conn.send_command(args.api_id, parameter)
        await asyncio.sleep(args.wait)

        snap = await _maybe_snap(conn, args)

        if use_json:
            result = {
                "status": "ok",
                "api_id": args.api_id,
                "parameter": parameter,
                "state": _state_to_dict(conn.state),
            }
            if snap:
                result["snap"] = snap
            print(json.dumps(result))
        else:
            print(f"  Sent api_id={args.api_id}")
    finally:
        await conn.disconnect()


async def cmd_list(args) -> None:
    """List available commands and API IDs."""
    use_json = args.json

    commands = {}
    for name, (desc, _method) in SIMPLE_COMMANDS.items():
        commands[name] = desc

    params = {
        "body_height": "float — body height (e.g. 0.1-0.32)",
        "foot_raise_height": "float — foot raise height",
        "speed_level": "int — speed level 1-3",
        "gait": "int — gait type 0=idle, 1=trot, 2=run, 3=stairs",
        "euler": "r,p,y — body euler angles (comma-separated floats)",
        "video": "on/off — toggle video stream",
        "audio": "on/off — toggle audio stream",
    }

    api_ids = {cmd.name: cmd.value for cmd in SportCommand}

    if use_json:
        print(json.dumps({
            "commands": commands,
            "parameters": params,
            "api_ids": api_ids,
        }, indent=2))
    else:
        print("\n  Commands (use with: go2 exec <command> [...]):")
        print("  " + "-" * 50)
        for name, desc in sorted(commands.items()):
            print(f"    {name:20s} {desc}")
        print(f"\n  Parameters (use with: go2 set <param> <value>):")
        print("  " + "-" * 50)
        for name, desc in sorted(params.items()):
            print(f"    {name:20s} {desc}")
        print(f"\n  Sport API IDs (use with: go2 raw <api_id>):")
        print("  " + "-" * 50)
        for name, val in sorted(api_ids.items(), key=lambda x: x[1]):
            print(f"    {val:<6d} {name}")
        print()


async def cmd_interactive(args) -> None:
    """Interactive command loop."""
    args._need_video = False
    conn = await _connect(args)

    if args.telemetry:
        conn.on_state_update(_live_telemetry_line)

    try:
        print("\n--- Go2 Interactive Control ---")
        print("Commands: stand_up, stand_down, sit, balance, recovery, stop, hello,")
        print("  stretch, dance1, dance2, front_flip, wiggle_hips, moon_walk, ...")
        print("Movement (sustained, default 2s):")
        print("  forward [sec]          — move forward")
        print("  backward [sec]         — move backward")
        print("  left [sec] / right [sec] — strafe")
        print("  turn_left [sec] / turn_right [sec] — rotate")
        print("  move <x> <y> <yaw> [sec] — custom move")
        print("Other:")
        print("  set <param> <value>    — set parameter")
        print("  status                 — print telemetry")
        print("  list                   — list all commands")
        print("  quit                   — disconnect and exit")
        print()

        loop = asyncio.get_event_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, lambda: input("go2> ").strip())
            except (EOFError, KeyboardInterrupt):
                break

            if not line:
                continue

            parts = line.split()
            cmd = parts[0].lower()

            if cmd in ("quit", "exit"):
                break
            elif cmd == "status":
                _print_state_human(conn.state)
            elif cmd == "list":
                for name, (desc, _) in sorted(SIMPLE_COMMANDS.items()):
                    print(f"    {name:20s} {desc}")
            elif cmd == "move" and len(parts) >= 4:
                x, y, yaw = float(parts[1]), float(parts[2]), float(parts[3])
                duration = float(parts[4]) if len(parts) >= 5 else 2.0
                print(f"  Moving x={x} y={y} yaw={yaw} for {duration}s...")
                await _sustained_move(conn, x, y, yaw, duration)
                print(f"  Done, stopped")
            elif cmd in MOVE_COMMANDS:
                desc, mx, my, myaw = MOVE_COMMANDS[cmd]
                duration = float(parts[1]) if len(parts) >= 2 else 2.0
                print(f"  {desc} for {duration}s...")
                await _sustained_move(conn, mx, my, myaw, duration)
                print(f"  Done, stopped")
            elif cmd == "set" and len(parts) >= 3:
                param = parts[1]
                value = parts[2]
                if param == "body_height":
                    conn.set_body_height(float(value))
                elif param == "foot_raise_height":
                    conn.set_foot_raise_height(float(value))
                elif param == "speed_level":
                    conn.set_speed_level(int(value))
                elif param == "gait":
                    conn.switch_gait(int(value))
                elif param == "euler" and len(parts) >= 5:
                    conn.set_euler(float(parts[2]), float(parts[3]), float(parts[4]))
                elif param == "video":
                    conn.video(value.lower() in ("on", "true", "1"))
                elif param == "audio":
                    conn.audio(value.lower() in ("on", "true", "1"))
                else:
                    print(f"  Unknown param: {param}")
                    continue
                print(f"  {param} = {value}")
            elif cmd == "raw" and len(parts) >= 2:
                api_id = int(parts[1])
                param = None
                if len(parts) >= 3:
                    try:
                        param = json.loads(" ".join(parts[2:]))
                    except json.JSONDecodeError:
                        param = " ".join(parts[2:])
                conn.send_command(api_id, param)
                print(f"  Sent api_id={api_id}")
            elif cmd in SIMPLE_COMMANDS:
                desc, method = SIMPLE_COMMANDS[cmd]
                getattr(conn, method)()
                print(f"  {desc}")
            else:
                print(f"  Unknown: {cmd}. Type 'list' for available commands.")
    finally:
        await conn.disconnect()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="go2",
        description="Unitree Go2 robot CLI controller. Supports interactive, scripted, and neural-network-driven control.",
    )

    # Global flags
    parser.add_argument("--ip", default=DEFAULT_IP, help=f"Robot IP address (default: {DEFAULT_IP})")
    parser.add_argument("--old-signaling", action="store_true", help="Use old signaling protocol (port 8081)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--json", action="store_true", help="Output in JSON format (for programmatic use)")
    parser.add_argument("--snap", metavar="PATH",
                        help="Capture camera frame after command and save to PATH (adds 'snap' to JSON output)")

    sub = parser.add_subparsers(dest="subcmd", help="Subcommand")

    # --- move ---
    p_move = sub.add_parser("move", help="Move robot with velocity")
    p_move.add_argument("-x", "--x", type=float, default=0.0, help="Forward/backward velocity (-1..1)")
    p_move.add_argument("-y", "--y", type=float, default=0.0, help="Left/right strafe velocity (-1..1)")
    p_move.add_argument("--yaw", type=float, default=0.0, help="Rotation velocity (-1..1)")
    p_move.add_argument("-d", "--duration", type=float, default=2.0,
                        help="Duration in seconds (default: 2, 0 = send once, no auto-stop)")
    p_move.add_argument("-f", "--forward", type=float, dest="x_shortcut", metavar="SPEED",
                        help="Shortcut: move forward at SPEED")
    p_move.add_argument("-b", "--backward", type=float, dest="x_back", metavar="SPEED",
                        help="Shortcut: move backward at SPEED")
    p_move.add_argument("-l", "--left", type=float, dest="y_left", metavar="SPEED",
                        help="Shortcut: strafe left at SPEED")
    p_move.add_argument("-r", "--right", type=float, dest="y_right", metavar="SPEED",
                        help="Shortcut: strafe right at SPEED")
    p_move.add_argument("--turn-left", type=float, dest="yaw_left", metavar="SPEED",
                        help="Shortcut: turn left at SPEED")
    p_move.add_argument("--turn-right", type=float, dest="yaw_right", metavar="SPEED",
                        help="Shortcut: turn right at SPEED")

    # --- exec ---
    p_exec = sub.add_parser("exec", help="Execute one or more commands")
    p_exec.add_argument("commands", nargs="+",
                        help="Commands: stand_up, hello, dance1, forward, backward, left, right, turn_left, turn_right, ...")
    p_exec.add_argument("-d", "--duration", type=float, default=0.0,
                        help="Duration for each command in seconds (movement defaults to 2s)")
    p_exec.add_argument("--delay", type=float, default=1.0, help="Delay between commands in seconds (default: 1)")
    p_exec.add_argument("--wait", type=float, default=1.0, help="Wait after last command before reading state (default: 1)")

    # --- set ---
    p_set = sub.add_parser("set", help="Set a robot parameter")
    p_set.add_argument("param", help="Parameter name: body_height, foot_raise_height, speed_level, gait, euler, video, audio")
    p_set.add_argument("value", help="Value to set")

    # --- telemetry ---
    p_tel = sub.add_parser("telemetry", help="Get robot telemetry", aliases=["tel", "state"])
    p_tel.add_argument("-s", "--stream", action="store_true", help="Stream telemetry continuously")
    p_tel.add_argument("-n", "--count", type=int, default=0, help="Number of readings (0 = infinite, for --stream)")
    p_tel.add_argument("-i", "--interval", type=float, default=0.5, help="Interval between readings in seconds (default: 0.5)")
    p_tel.add_argument("--wait", type=float, default=1.0, help="Wait time for initial telemetry (default: 1)")

    # --- image ---
    p_img = sub.add_parser("image", help="Capture camera image", aliases=["img", "frame", "photo"])
    p_img.add_argument("-o", "--output", default="go2_frame.jpg",
                       help="Output file path, or '-' for stdout/base64 (default: go2_frame.jpg)")
    p_img.add_argument("-t", "--timeout", type=float, default=10.0, help="Timeout for frame capture (default: 10s)")
    p_img.add_argument("-s", "--stream", action="store_true", help="Capture frames continuously")
    p_img.add_argument("-n", "--count", type=int, default=0, help="Number of frames to capture (0 = infinite)")
    p_img.add_argument("-i", "--interval", type=float, default=1.0, help="Interval between frames in seconds (default: 1)")

    # --- raw ---
    p_raw = sub.add_parser("raw", help="Send raw sport command by API ID")
    p_raw.add_argument("api_id", type=int, help="Sport API ID (e.g. 1008 for MOVE)")
    p_raw.add_argument("-p", "--param", help="JSON parameter string")
    p_raw.add_argument("--wait", type=float, default=1.0, help="Wait after command (default: 1)")

    # --- list ---
    sub.add_parser("list", help="List available commands, parameters, and API IDs", aliases=["ls"])

    # --- interactive ---
    p_int = sub.add_parser("interactive", help="Interactive control mode", aliases=["i", "shell"])
    p_int.add_argument("--telemetry", action="store_true", help="Show live telemetry updates")

    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.debug:
        logging.getLogger("aiortc").setLevel(logging.WARNING)
        logging.getLogger("aioice").setLevel(logging.WARNING)

    # Default to interactive mode if no subcommand
    if not args.subcmd:
        args.subcmd = "interactive"
        args.telemetry = False

    # Handle move shortcuts
    if args.subcmd == "move":
        if args.x_shortcut is not None:
            args.x = args.x_shortcut
        if args.x_back is not None:
            args.x = -args.x_back
        if args.y_left is not None:
            args.y = args.y_left
        if args.y_right is not None:
            args.y = -args.y_right
        if args.yaw_left is not None:
            args.yaw = args.yaw_left
        if args.yaw_right is not None:
            args.yaw = -args.yaw_right

    args._need_video = bool(args.snap)

    handlers = {
        "move": cmd_move,
        "exec": cmd_exec,
        "set": cmd_set,
        "telemetry": cmd_telemetry,
        "tel": cmd_telemetry,
        "state": cmd_telemetry,
        "image": cmd_image,
        "img": cmd_image,
        "frame": cmd_image,
        "photo": cmd_image,
        "raw": cmd_raw,
        "list": cmd_list,
        "ls": cmd_list,
        "interactive": cmd_interactive,
        "i": cmd_interactive,
        "shell": cmd_interactive,
    }

    handler = handlers.get(args.subcmd)
    if not handler:
        parser.print_help()
        return

    try:
        await handler(args)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        if args.json:
            print(json.dumps({"status": "error", "error": str(e)}))
        else:
            logger.error("Error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
