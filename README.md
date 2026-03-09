# go2-robot

CLI tool and AI agent skill for controlling the [Unitree Go2](https://www.unitree.com/go2/) quadruped robot via WebRTC.

- **`go2` CLI** — command-line tool for scripted and interactive robot control
- **AI Skill** — `CLAUDE.md` teaches Claude Code / Cursor how to drive the robot
- **Python library** — `from go2 import Go2Connection` for custom integrations
- **HTTP server** — persistent WebRTC connection with REST API for fast curl-based control

## Installation

```bash
git clone https://github.com/krestnikov/go2-python.git
cd go2-python
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the `go2` command globally in your virtualenv.

### Requirements

- Python >= 3.11
- Robot and computer on the same network:
  - **AP mode** — connect to robot's Wi-Fi (`GoxxxxxxWiFi5G`), IP: `192.168.12.1`
  - **STA mode** — robot and computer on the same router, use robot's IP

Default IP: `192.168.1.66`. Override with `GO2_IP` env var or `--ip` flag.

## Quick Start

```bash
# One-off commands (connects, executes, disconnects — ~7s each):
go2 exec stand_up
go2 move -x 0.5 -d 2
go2 exec hello

# Persistent server (connect once, commands take ~2-3s):
go2 serve &
curl -s localhost:8090/state
curl -s -X POST localhost:8090/exec -H 'Content-Type: application/json' \
  -d '{"command":"stand_up"}'
curl -s -X POST localhost:8090/move -H 'Content-Type: application/json' \
  -d '{"x":0.5,"duration":2}'
```

There are three separate pieces in this repo:
- `go2` CLI — standalone commands; simplest, but slower because each command reconnects
- `go2 serve` — persistent REST server; fastest control path for scripts and AI agents
- `python web.py` — browser UI for debugging and manual control; separate from `go2 serve`

## Which Mode To Use?

- Use `go2 serve` if you want the fastest control path for AI agents, scripts, or repeated commands
- Use `go2` CLI for one-off commands or quick manual checks when you do not want to keep a server running
- Use `python web.py` if you want a browser interface with live video, telemetry, buttons, and sliders
- Use `CLAUDE.md` or Cursor rules as agent instructions only; they do not start `go2 serve` or `web.py` automatically
- For best AI-agent performance: start `go2 serve` first, then let the agent use its REST API

## CLI Reference

All subcommands support `--json` for machine-readable output (for AI agents and scripts).

### Movement

```bash
go2 move -x 0.5 -d 2                    # forward 2 sec
go2 move -x 0.5 --yaw -0.15 -d 2        # forward with drift compensation
go2 move --forward 0.5 -d 2             # shortcut
go2 move --turn-left 0.5 -d 2           # turn left
go2 move -y 0.3 -d 2                    # strafe left

# With camera snapshot after movement:
go2 --json --snap /tmp/go2.jpg move -x 0.5 -d 2
```

### Commands

```bash
go2 exec stand_up                        # single command
go2 exec stand_up hello dance1 --delay 2 # sequence with delay
go2 exec forward -d 3                    # movement shortcut
```

### Telemetry

```bash
go2 telemetry                            # one-shot, human-readable
go2 --json state                         # one-shot, JSON
go2 --json telemetry -s -i 0.2           # streaming at 5 Hz
```

### Camera

```bash
go2 image -o snapshot.jpg                # save frame
go2 --json image -o -                    # base64 JSON to stdout
go2 image -o frames.jpg -s -n 5 -i 0.5  # 5 frames, 0.5s interval
```

### Parameters

```bash
go2 set body_height 0.2                  # body height (0.1-0.32)
go2 set speed_level 2                    # speed 1-3
go2 set gait 1                           # trot
go2 set euler 0.1,0,0                    # body tilt
```

### Other

```bash
go2 list                                 # all commands, params, API IDs
go2 raw 1008 -p '{"x":0.3,"y":0,"z":0}' # raw sport API command
go2 serve                                # start HTTP server
```

## HTTP Server

The server maintains a persistent WebRTC connection, making commands ~3x faster than CLI mode.
Start it as a separate process, then control the robot through its REST endpoints. This is the preferred path for AI agents and automation.

```bash
go2 serve                   # start on localhost:8090
go2 serve --host 0.0.0.0    # expose to network
go2 serve --port 9000       # custom port
```

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/state` | GET | Robot telemetry JSON |
| `/move` | POST | `{"x","y","yaw","duration","snap_path","snap_width"}` |
| `/exec` | POST | `{"command"}` or `{"commands":[],"delay"}` |
| `/set` | POST | `{"param","value"}` |
| `/raw` | POST | `{"api_id","param"}` |
| `/image` | GET | JPEG frame (`?width=320`, `?path=/tmp/f.jpg`) |
| `/connect` | POST | Reconnect: `{"robot_ip":"..."}` |
| `/disconnect` | POST | Disconnect |
| `/list` | GET | List all commands |

### Response Format

```json
{
  "status": "ok",
  "command": "move",
  "params": {"x": 0.5, "duration": 2},
  "state": {"battery_soc": 85, "body_height": 0.32, "position": [1.2, 0.3, 0.0], ...},
  "snap": {"path": "/tmp/go2.jpg", "size_bytes": 12345}
}
```

## AI Agent Skill

### Claude Code

When you run `claude` in this project directory, Claude Code automatically loads `CLAUDE.md` which teaches it how to control the robot — safety rules, curl commands, movement mechanics, and navigation algorithms.
`CLAUDE.md` is only the agent instruction file; it does not start any server by itself.

```bash
cd go2-python
claude
# "Stand the robot up and walk 2 meters forward"
# "Take a photo and describe what the robot sees"
# "Navigate to the red chair in the room"
```

To use the skill in another project, copy `CLAUDE.md` to that project's root.

### Cursor

Copy `CLAUDE.md` content to `.cursorrules` in your project root.

### How It Works

For best performance, start `go2 serve` separately first, then let the agent use the HTTP server via `curl`.
If the server is unavailable, the agent can still fall back to standalone CLI commands, but that mode is much slower.

The AI agent then:
1. Read camera frames and telemetry
2. Make movement decisions based on visual feedback
3. Execute commands with safety checks
4. Iterate until the goal is reached

## Python Library

```python
import asyncio
from go2 import Go2Connection

async def main():
    conn = Go2Connection(robot_ip="192.168.1.66")
    await conn.connect()

    conn.stand_up()
    await asyncio.sleep(2)
    conn.move(x=0.3, y=0, yaw=0)
    await asyncio.sleep(3)
    conn.stop()

    print(f"Battery: {conn.state.battery.soc}%")
    print(f"Position: {conn.state.position}")

    await conn.disconnect()

asyncio.run(main())
```

## Web UI (Legacy)

A browser-based debugging interface with live video, telemetry, command buttons, and optional LLM-driven navigation. This is a working tool that will be developed further.
It is a separate server from `go2 serve` and manages its own robot connection.

```bash
# Optional: configure LLM in .env (see .env.example)
python web.py
# Open http://127.0.0.1:8080
```

Features:
- Live MJPEG video stream
- Real-time telemetry display
- Command buttons and parameter sliders
- LLM-based action recommendations (requires OpenAI API key)

## Available Commands

| Command | Description |
|---------|-------------|
| `stand_up` | Stand up |
| `stand_down` | Lie down |
| `sit` | Sit |
| `balance` | Balance stand |
| `recovery` | Recovery stand |
| `stop` | Stop moving |
| `damp` | Disable motors |
| `hello` | Wave hello |
| `stretch` | Stretch |
| `content` | Happy gesture |
| `wallow` | Wallow |
| `dance1` / `dance2` | Dance |
| `front_flip` | Front flip |
| `front_jump` | Front jump |
| `front_pounce` | Front pounce |
| `wiggle_hips` | Wiggle hips |
| `finger_heart` | Finger heart |
| `handstand` | Handstand |
| `cross_step` | Cross step |
| `bound` | Bound |
| `moon_walk` | Moon walk |
| `economic_gait` | Economic gait |
| `lead_follow` | Lead follow |

## Project Structure

```
go2-python/
├── go2/
│   ├── __init__.py         # Exports: Go2Connection, SportCommand, RobotState
│   ├── cli.py              # CLI entry point (go2 command)
│   ├── server.py           # Persistent HTTP server (go2 serve)
│   ├── connection.py       # Go2Connection — WebRTC + commands
│   ├── commands.py         # SportCommand enum (47 API IDs)
│   ├── telemetry.py        # RobotState dataclass
│   ├── data_channel.py     # Data channel message routing
│   ├── signaling.py        # HTTP signaling (encrypted)
│   ├── crypto.py           # AES/RSA cryptography
│   └── constants.py        # IP, ports, topics
├── web.py                  # Web UI (legacy debugging tool)
├── web/                    # Web UI static assets
├── CLAUDE.md               # AI agent skill
├── pyproject.toml          # Package configuration
└── requirements.txt        # Dependencies
```

## Protocol

Connection via WebRTC with SCTP data channel. HTTP signaling:
- **Port 9991** — new protocol (RSA + AES-256-ECB encrypted SDP)
- **Port 8081** — old or new protocol (depends on firmware)

All methods are tried automatically in sequence.

## Copyright

Copyright (c) 2025 Konstantin Krestnikov

License will be added later.
