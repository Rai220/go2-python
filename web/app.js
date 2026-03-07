const state = {
  connected: false,
  connecting: false,
  llmAvailable: false,
  llmPending: false,
  videoEnabled: true,
  keys: new Set(),
  driveTimer: null,
  statusTimer: null,
  videoTimer: null,
  currentFrameUrl: null,
  lastVector: { x: 0, y: 0, yaw: 0 },
};

const connectionBadge = document.getElementById("connectionBadge");
const statusMessage = document.getElementById("statusMessage");
const connectForm = document.getElementById("connectForm");
const connectButton = connectForm.querySelector("button[type='submit']");
const disconnectButton = document.getElementById("disconnectButton");
const videoToggleButton = document.getElementById("videoToggleButton");
const llmActionButton = document.getElementById("llmActionButton");
const llmGoal = document.getElementById("llmGoal");
const llmResult = document.getElementById("llmResult");
const llmState = document.getElementById("llmState");
const oldSignaling = document.getElementById("oldSignaling");
const robotIp = document.getElementById("robotIp");
const commandsGrid = document.getElementById("commandsGrid");
const cameraFeed = document.getElementById("cameraFeed");
const videoState = document.getElementById("videoState");

const fieldMap = {
  batteryValue: (payload) => `${payload?.battery?.soc ?? "-"}%`,
  powerValue: (payload) => formatNumber(payload?.power_v, 1, "V"),
  modeValue: (payload) => `${payload?.mode ?? "-"}`,
  gaitValue: (payload) => `${payload?.gait_type ?? "-"}`,
  heightValue: (payload) => formatNumber(payload?.body_height, 3, "m"),
  speedValue: (payload) => `${payload?.speed_level ?? "-"}`,
  positionValue: (payload) => formatVector(payload?.position, 2),
  imuValue: (payload) => formatVector(payload?.imu?.rpy, 1),
};

function formatNumber(value, digits, suffix = "") {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "-";
  }
  return `${value.toFixed(digits)}${suffix}`;
}

function formatVector(value, digits) {
  if (!Array.isArray(value)) {
    return "-";
  }
  return value.map((item) => Number(item).toFixed(digits)).join(", ");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed: ${response.status}`);
  }

  return response.json();
}

function setStatus(text, isError = false) {
  statusMessage.textContent = text;
  statusMessage.style.color = isError ? "var(--danger)" : "";
}

function setConnected(connected) {
  state.connected = connected;
  connectionBadge.textContent = state.connecting ? "connecting" : connected ? "connected" : "disconnected";
  connectionBadge.classList.toggle("online", connected && !state.connecting);
  connectionBadge.classList.toggle("offline", !connected && !state.connecting);
  connectionBadge.classList.toggle("pending", state.connecting);
}

function setConnecting(connecting) {
  state.connecting = connecting;
  connectButton.disabled = connecting || state.connected;
  disconnectButton.disabled = connecting || !state.connected;
  videoToggleButton.disabled = connecting || !state.connected;
  llmActionButton.disabled = connecting || !state.connected || !state.llmAvailable || state.llmPending;
  robotIp.disabled = connecting || state.connected;
  oldSignaling.disabled = connecting || state.connected;
  setConnected(state.connected);
}

function escapeHtml(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function renderLlmDecision(decision = null, execution = null) {
  if (!decision) {
    llmResult.classList.add("empty");
    llmResult.innerHTML = "Connect to the robot and ask the model for a safe next step.";
    return;
  }

  const goalText = currentGoal();
  const details = decision.duration_seconds > 0
    ? `${decision.action}, ${decision.duration_seconds.toFixed(1)}s`
    : decision.action;

  const safetyNotes = Array.isArray(decision.safety_notes) ? decision.safety_notes.filter(Boolean) : [];
  const notesMarkup = safetyNotes.length
    ? `<ul>${safetyNotes.map((note) => `<li>${escapeHtml(note)}</li>`).join("")}</ul>`
    : "<p>No extra safety notes.</p>";
  const executionNotes = execution?.executed
    ? `<p>Executed on robot${Array.isArray(execution.pre_actions) && execution.pre_actions.length ? `, with pre-actions: ${escapeHtml(execution.pre_actions.join(", "))}` : ""}.</p>`
    : "<p>Not executed.</p>";

  llmResult.classList.remove("empty");
  llmResult.innerHTML = `
    <div>
      <span class="subtle">Goal</span>
      <p>${escapeHtml(goalText || "No explicit goal provided.")}</p>
    </div>
    <div>
      <span class="subtle">Suggested action</span>
      <strong>${escapeHtml(decision.summary || decision.action_type)}</strong>
    </div>
    <div>
      <span class="subtle">Details</span>
      <p>${escapeHtml(details)}</p>
    </div>
    <div>
      <span class="subtle">Reason</span>
      <p>${escapeHtml(decision.reason || "No reason provided.")}</p>
    </div>
    <div>
      <span class="subtle">Safety notes</span>
      ${notesMarkup}
    </div>
    <div>
      <span class="subtle">Execution</span>
      ${executionNotes}
    </div>
  `;
}

function currentGoal() {
  return llmGoal.value.trim();
}

function saveGoal() {
  window.localStorage.setItem("go2.llmGoal", llmGoal.value);
}

function restoreGoal() {
  llmGoal.value = window.localStorage.getItem("go2.llmGoal") || "";
}

function setLlmState() {
  if (!state.llmAvailable) {
    llmState.textContent = "set OPENAI_API_KEY";
  } else if (state.llmPending) {
    llmState.textContent = "analyzing frame";
  } else if (!state.connected) {
    llmState.textContent = "connect robot first";
  } else {
    llmState.textContent = "ready";
  }
  llmActionButton.disabled = state.connecting || !state.connected || !state.llmAvailable || state.llmPending;
}

function setVideoSource() {
  if (state.videoTimer) {
    window.clearInterval(state.videoTimer);
    state.videoTimer = null;
  }
  if (state.currentFrameUrl) {
    URL.revokeObjectURL(state.currentFrameUrl);
    state.currentFrameUrl = null;
  }

  if (!state.connected || !state.videoEnabled) {
    cameraFeed.removeAttribute("src");
    videoState.textContent = state.connected ? "video disabled" : "waiting for connection";
    return;
  }

  const refreshFrame = async () => {
    try {
      const response = await fetch(`/api/video-frame?t=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`video frame not ready: ${response.status}`);
      }
      const blob = await response.blob();
      const nextUrl = URL.createObjectURL(blob);
      const previousUrl = state.currentFrameUrl;
      state.currentFrameUrl = nextUrl;
      cameraFeed.src = nextUrl;
      if (previousUrl) {
        URL.revokeObjectURL(previousUrl);
      }
      videoState.textContent = "live stream";
    } catch {
      videoState.textContent = "waiting for frames";
    }
  };

  refreshFrame();
  state.videoTimer = window.setInterval(() => {
    refreshFrame().catch(() => {});
  }, 400);
}

function updateTelemetry(robotState) {
  for (const [id, formatter] of Object.entries(fieldMap)) {
    document.getElementById(id).textContent = formatter(robotState);
  }
}

function isTypingTarget(target) {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  return Boolean(target.closest("input, textarea, select, [contenteditable='true']"));
}

function currentDriveVector() {
  const x = (state.keys.has("KeyW") ? 0.3 : 0) + (state.keys.has("KeyS") ? -0.3 : 0);
  const y = (state.keys.has("KeyA") ? 0.3 : 0) + (state.keys.has("KeyD") ? -0.3 : 0);
  const yaw = (state.keys.has("KeyQ") ? 0.6 : 0) + (state.keys.has("KeyE") ? -0.6 : 0);
  return { x, y, yaw };
}

function sameVector(a, b) {
  return a.x === b.x && a.y === b.y && a.yaw === b.yaw;
}

async function sendDriveVector(force = false) {
  if (!state.connected || state.connecting) {
    return;
  }

  const nextVector = currentDriveVector();
  if (!force && sameVector(nextVector, state.lastVector) && nextVector.x === 0 && nextVector.y === 0) {
    return;
  }

  state.lastVector = nextVector;
  await api("/api/move", {
    method: "POST",
    body: JSON.stringify(nextVector),
  });
}

function startDriveLoop() {
  if (state.driveTimer) {
    return;
  }
  state.driveTimer = window.setInterval(() => {
    sendDriveVector().catch((error) => {
      setStatus(error.message, true);
    });
  }, 120);
}

function stopDriveLoop() {
  if (state.driveTimer) {
    window.clearInterval(state.driveTimer);
    state.driveTimer = null;
  }
}

function refreshMoveButtons() {
  document.querySelectorAll("[data-key]").forEach((button) => {
    button.classList.toggle("active", state.keys.has(button.dataset.key));
  });
}

function clearKeys() {
  if (state.keys.size === 0) {
    return;
  }

  state.keys.clear();
  refreshMoveButtons();
  sendDriveVector(true).catch((error) => {
    setStatus(error.message, true);
  });
}

async function loadCommands() {
  const payload = await api("/api/commands");
  commandsGrid.innerHTML = "";

  payload.commands.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = item.label;
    button.dataset.command = item.name;
    button.addEventListener("click", async () => {
      if (!state.connected || state.connecting) {
        return;
      }
      try {
        await api("/api/command", {
          method: "POST",
          body: JSON.stringify({ command: item.name }),
        });
        setStatus(`Command sent: ${item.label}`);
      } catch (error) {
        setStatus(error.message, true);
      }
    });
    commandsGrid.appendChild(button);
  });
}

async function refreshStatus() {
  const payload = await api("/api/status");
  setConnecting(Boolean(payload.connect_in_progress));
  setConnected(payload.connected);
  state.llmAvailable = Boolean(payload.llm_available);
  setLlmState();
  if (!state.connected && !state.connecting) {
    robotIp.value = payload.robot_ip || "";
  }
  oldSignaling.checked = !payload.use_new_signaling;
  updateTelemetry(payload.state);
  videoState.textContent = payload.video_available ? "live stream" : "waiting for frames";

  if (payload.connect_in_progress) {
    setStatus(`Connecting: ${payload.connect_phase}`);
  } else if (payload.connected && payload.last_error) {
    setStatus(payload.last_error, true);
  } else if (!payload.connected) {
    setStatus("Connect to start camera, telemetry and commands.");
  }
}

async function connectRobot(event) {
  event.preventDefault();
  if (state.connecting) {
    return;
  }
  setConnecting(true);
  setStatus("Connecting...");

  try {
    const payload = await api("/api/connect", {
      method: "POST",
      body: JSON.stringify({
        robot_ip: robotIp.value.trim(),
        old_signaling: oldSignaling.checked,
      }),
    });

    setConnected(payload.connected);
    state.videoEnabled = true;
    videoToggleButton.textContent = "Video Off";
    setVideoSource();
    await refreshStatus();
    setStatus(`Connected to ${payload.robot_ip}`);
  } catch (error) {
    setConnecting(false);
    setConnected(false);
    setVideoSource();
    setStatus(error.message, true);
  }
}

async function disconnectRobot() {
  if (state.connecting) {
    return;
  }
  try {
    await api("/api/disconnect", { method: "POST", body: "{}" });
    setConnected(false);
    clearKeys();
    stopDriveLoop();
    state.lastVector = { x: 0, y: 0, yaw: 0 };
    setVideoSource();
    renderLlmDecision(null);
    setLlmState();
    setStatus("Disconnected.");
    await refreshStatus();
  } catch (error) {
    setStatus(error.message, true);
  }
}

async function toggleVideo() {
  if (state.connecting || !state.connected) {
    return;
  }
  state.videoEnabled = !state.videoEnabled;
  videoToggleButton.textContent = state.videoEnabled ? "Video Off" : "Video On";

  try {
    await api("/api/video", {
      method: "POST",
      body: JSON.stringify({ enabled: state.videoEnabled }),
    });
    setVideoSource();
  } catch (error) {
    state.videoEnabled = !state.videoEnabled;
    videoToggleButton.textContent = state.videoEnabled ? "Video Off" : "Video On";
    setStatus(error.message, true);
  }
}

async function requestLlmAction() {
  if (state.connecting || !state.connected || !state.llmAvailable || state.llmPending) {
    return;
  }

  const goal = currentGoal();
  state.llmPending = true;
  setLlmState();
  setStatus(goal ? "Sending current frame and goal to LLM..." : "Sending current frame to LLM...");

  try {
    const payload = await api("/api/llm-action", {
      method: "POST",
      body: JSON.stringify({ goal }),
    });
    renderLlmDecision(payload.decision, payload.execution);
    setStatus(
      payload.execution?.executed
        ? `LLM executed: ${payload.decision.summary}`
        : goal
          ? `LLM suggestion for goal: ${payload.decision.summary}`
          : `LLM suggestion: ${payload.decision.summary}`,
    );
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    state.llmPending = false;
    setLlmState();
  }
}

function bindMovementButtons() {
  document.querySelectorAll("[data-key]").forEach((button) => {
    const key = button.dataset.key;
    const press = async () => {
      if (state.connecting || !state.connected) {
        return;
      }
      state.keys.add(key);
      refreshMoveButtons();
      startDriveLoop();
      try {
        await sendDriveVector(true);
      } catch (error) {
        setStatus(error.message, true);
      }
    };

    const release = () => {
      state.keys.delete(key);
      refreshMoveButtons();
      sendDriveVector(true).catch((error) => {
        setStatus(error.message, true);
      });
      if (state.keys.size === 0) {
        stopDriveLoop();
      }
    };

    button.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      press();
    });
    button.addEventListener("pointerup", release);
    button.addEventListener("pointerleave", release);
    button.addEventListener("pointercancel", release);
  });

  document.querySelectorAll("[data-command='stop']").forEach((button) => {
    button.addEventListener("click", async () => {
      if (state.connecting || !state.connected) {
        return;
      }
      clearKeys();
      stopDriveLoop();
      state.lastVector = { x: 0, y: 0, yaw: 0 };
      try {
        await api("/api/command", {
          method: "POST",
          body: JSON.stringify({ command: "stop" }),
        });
      } catch (error) {
        setStatus(error.message, true);
      }
    });
  });
}

function bindKeyboard() {
  window.addEventListener("keydown", (event) => {
    if (isTypingTarget(event.target)) {
      return;
    }
    if (!["KeyW", "KeyA", "KeyS", "KeyD", "KeyQ", "KeyE"].includes(event.code) || event.repeat || state.connecting || !state.connected) {
      return;
    }
    event.preventDefault();
    state.keys.add(event.code);
    refreshMoveButtons();
    startDriveLoop();
    sendDriveVector(true).catch((error) => {
      setStatus(error.message, true);
    });
  });

  window.addEventListener("keyup", (event) => {
    if (isTypingTarget(event.target)) {
      return;
    }
    if (!["KeyW", "KeyA", "KeyS", "KeyD", "KeyQ", "KeyE"].includes(event.code) || state.connecting || !state.connected) {
      return;
    }
    event.preventDefault();
    state.keys.delete(event.code);
    refreshMoveButtons();
    sendDriveVector(true).catch((error) => {
      setStatus(error.message, true);
    });
    if (state.keys.size === 0) {
      stopDriveLoop();
    }
  });

  window.addEventListener("blur", clearKeys);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearKeys();
      stopDriveLoop();
    }
  });
}

async function init() {
  connectForm.addEventListener("submit", connectRobot);
  disconnectButton.addEventListener("click", disconnectRobot);
  videoToggleButton.addEventListener("click", toggleVideo);
  llmActionButton.addEventListener("click", requestLlmAction);
  llmGoal.addEventListener("input", saveGoal);

  bindMovementButtons();
  bindKeyboard();
  restoreGoal();
  await loadCommands();
  renderLlmDecision(null);
  await refreshStatus();
  setVideoSource();

  state.statusTimer = window.setInterval(() => {
    refreshStatus().catch((error) => {
      setStatus(error.message, true);
    });
  }, 1000);
}

init().catch((error) => {
  setStatus(error.message, true);
});
