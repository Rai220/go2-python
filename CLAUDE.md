# Go2 Robot — AI Agent Skill

You control a **real physical Unitree Go2 quadruped robot** via the `go2` CLI tool and its HTTP server.
The robot has a camera, IMU, 12 motors, and a battery. It can walk, run, dance, and do tricks.

## CRITICAL SAFETY RULES

- **NEVER collide with walls, furniture, people, or animals.** Check telemetry and camera before moving.
- **Before any movement** verify: robot is standing (`body_height > 0.3`) and battery > 20%.
- **Do NOT execute dangerous tricks** (front_flip, handstand, bound) without explicit user request.
- **Use moderate speeds** (0.1-0.5), never 1.0. Sudden movements can damage the robot.
- **Always send `stop` after movement** — never leave the robot moving uncontrolled.
- **If battery < 15%** — warn the user and refuse energy-intensive commands.

## How to Control the Robot

### Preferred: Server Mode (fast, ~2-3s per command)

Always prefer this mode for multi-step work. First start the persistent HTTP server as a separate process, then control the robot through its REST API with `curl`. This is much faster than running one-off CLI commands for every action.

Important: `CLAUDE.md` is only an instruction file for the agent. It does not start `go2 serve` automatically, so the agent should explicitly ensure the server is running before beginning robot actions.

Start the persistent HTTP server, then use curl:

```bash
# Check if server is running:
curl -s localhost:8090/state

# If "Connection refused" — start the server:
go2 serve &
# Wait ~10s for connection, then check again

# If "Robot not connected" — reconnect:
curl -s -X POST localhost:8090/connect -H 'Content-Type: application/json' -d '{}'
```

Operational rule:
- For any non-trivial task, first check `localhost:8090/state`
- If the server is not running, start `go2 serve` separately
- Once the server is available, prefer `curl` requests to `/state`, `/move`, `/exec`, `/image`, and `/set`
- Use standalone `go2 ...` CLI commands only as a fallback when the HTTP server cannot be used

### Server REST Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/state` | GET | Robot telemetry JSON |
| `/move` | POST | Move: `{"x","y","yaw","duration","snap_path","snap_width"}` |
| `/exec` | POST | Command: `{"command","snap_path","snap_width"}` or `{"commands":[],"delay"}` |
| `/set` | POST | Parameter: `{"param","value"}` |
| `/raw` | POST | Raw: `{"api_id","param"}` |
| `/image` | GET | JPEG frame, `?width=320` to resize, `?path=/tmp/f.jpg` to save |
| `/connect` | POST | Reconnect: `{"robot_ip":"..."}` |
| `/disconnect` | POST | Disconnect |
| `/list` | GET | List all commands |

### curl Examples

```bash
# Telemetry
curl -s localhost:8090/state

# Camera image
curl -s 'localhost:8090/image?width=320' -o /tmp/go2.jpg

# Move forward with yaw correction + snap after
curl -s -X POST localhost:8090/move -H 'Content-Type: application/json' \
  -d '{"x":0.5,"yaw":-0.15,"duration":2,"snap_path":"/tmp/go2.jpg","snap_width":320}'

# Execute command + snap after
curl -s -X POST localhost:8090/exec -H 'Content-Type: application/json' \
  -d '{"command":"stand_up","snap_path":"/tmp/go2.jpg","snap_width":320}'

# Multiple commands
curl -s -X POST localhost:8090/exec -H 'Content-Type: application/json' \
  -d '{"commands":["stand_up","hello"],"delay":2}'

# Set parameter
curl -s -X POST localhost:8090/set -H 'Content-Type: application/json' \
  -d '{"param":"speed_level","value":2}'
```

### Response Format

```json
{
  "status": "ok",
  "command": "move",
  "params": {"x": 0.5, "yaw": -0.15, "duration": 2},
  "state": {"battery_soc": 85, "position": [...], ...},
  "snap": {"path": "/tmp/go2.jpg", "size_bytes": 12345}
}
```

Errors: `{"status":"error","error":"description"}` + HTTP 400/409/503.

### Alternative: CLI Mode (standalone, ~7s per command)

Each CLI command connects, executes, disconnects. Slower but simpler for one-off commands.

```bash
go2 exec stand_up
go2 move -x 0.5 --yaw -0.15 -d 2
go2 --json state
go2 --json --snap /tmp/go2.jpg move -x 0.5 -d 2
go2 set speed_level 2
go2 image -o /tmp/go2.jpg
go2 list
```

## Available Commands

**Basic:** `stand_up`, `stand_down`, `sit`, `balance`, `recovery`, `stop`, `damp`
**Gestures:** `hello`, `stretch`, `content`, `wallow`
**Dance:** `dance1`, `dance2`
**Tricks (CAUTION):** `front_flip`, `front_jump`, `front_pounce`, `wiggle_hips`, `finger_heart`, `handstand`, `cross_step`, `bound`, `moon_walk`
**Gaits:** `economic_gait`, `lead_follow`

## Parameters

- `body_height` — body height (float, 0.1-0.32)
- `foot_raise_height` — foot raise height (float)
- `speed_level` — speed 1-3
- `gait` — gait type 0-3
- `euler` — tilt r,p,y: `{"param":"euler","value":[0.1,0,0]}`
- `video` / `audio` — on/off

## Movement Mechanics

### Duration and Speed
- **Always use `duration: 2`** — the only reliable duration. 4-5s is unreliable, 1s is too short for turns.
- `x=0.5` for 2s gives ~0.7-0.8m. `x=0.3` for 2s gives ~0.45m.
- For ~3m distance: 4 steps at x=0.5 with yaw corrections between them.

### Yaw Drift (Critical)
- **Robot drifts left systematically.** Each 2s step drifts +0.15-0.2 rad (~10°).
- **Compensate every forward step:** add `"yaw": -0.15` to every move.
- **Monitor `state.imu.rpy[2]`** — correct if deviation > 0.1 rad.
- **Turns need duration=2:** `{"yaw": -0.5, "duration": 2}` gives ~0.5 rad (~28°). Duration 1 gives only ~0.1 rad.

### Standing Up
- `stand_up` may fail on first attempt. If robot is lying (`body_height < 0.2`), use sequence: `damp` → 3s pause → `recovery` → 5s pause → `stand_up` → 3s pause.
- `body_height > 0.3` = robot is standing. `mode` field is unreliable.

## Navigation Algorithm

### Speed Rules
- **Always `x=0.5`** until target fills >40% of frame height (~0.5m away)
- **Only last step at `x=0.3`** when target is already large in frame
- **Combine turns with forward movement** — don't waste a separate command on pure turns:
  - Target slightly left → `{"x":0.5,"yaw":0.2,"duration":2}`
  - Target slightly right → `{"x":0.5,"yaw":-0.2,"duration":2}`
  - Target far off-center → `{"x":0.3,"yaw":0.5,"duration":2}`
  - Pure turn only if target is outside frame or >45° turn needed

### Navigation Loop
1. **Stand + snap:** `exec stand_up` with `snap_path`
2. **Read snap** — assess distance and target position
3. **Verify:** `battery_soc > 20`, `body_height > 0.3`, path clear
4. **Step + snap:** `move` with x, yaw correction, snap_path
5. **Read snap** — assess result, adjust yaw, repeat
6. **Final approach:** `{"x":0.3,"duration":2}` → `stop`

### Recommended Parameters

| Task | curl JSON body | Effect |
|------|---------------|--------|
| Confident walk | `{"x":0.5,"yaw":-0.15,"duration":2}` | ~0.7m, drift compensated |
| Careful approach | `{"x":0.3,"duration":2}` | ~0.45m |
| Fast approach | `{"x":0.8,"duration":2}` (speed_level 2) | ~1.2m |
| Turn right | `{"yaw":-0.5,"duration":2}` | ~0.5 rad (~28°) |
| Turn left | `{"yaw":0.5,"duration":2}` | ~0.5 rad (~28°) |
| Sidestep | `{"y":0.3,"duration":2}` | strafe ~0.4m |

Default robot IP: `192.168.1.66`. Configurable via `GO2_IP` env var or `--ip` flag.
