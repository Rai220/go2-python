"""Telemetry data parsing for Unitree Go2."""

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class IMUState:
    rpy: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    quaternion: list[float] = field(default_factory=lambda: [1.0, 0.0, 0.0, 0.0])
    gyroscope: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    accelerometer: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    temperature: int = 0


@dataclass
class MotorState:
    q: float = 0.0
    temperature: int = 0
    lost: int = 0


@dataclass
class BatteryState:
    soc: int = 0  # 0-100%
    current: int = 0
    cycle: int = 0


@dataclass
class RobotState:
    """Aggregated robot state from telemetry."""
    # From sportmodestate
    mode: int = 0
    gait_type: int = 0
    body_height: float = 0.32
    foot_raise_height: float = 0.08
    position: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    velocity: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    yaw_speed: float = 0.0
    foot_force: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])

    # From lowstate
    imu: IMUState = field(default_factory=IMUState)
    motors: list[MotorState] = field(default_factory=lambda: [MotorState() for _ in range(12)])
    battery: BatteryState = field(default_factory=BatteryState)
    power_v: float = 0.0

    # From multiplestate
    volume: int = 0
    brightness: int = 0
    obstacles_avoid: bool = False
    speed_level: int = 1

    def update_from_sport_state(self, data: dict) -> None:
        self.mode = data.get("mode", self.mode)
        self.gait_type = data.get("gait_type", self.gait_type)
        self.body_height = data.get("body_height", self.body_height)
        self.foot_raise_height = data.get("foot_raise_height", self.foot_raise_height)
        self.position = data.get("position", self.position)
        self.velocity = data.get("velocity", self.velocity)
        self.yaw_speed = data.get("yaw_speed", self.yaw_speed)
        self.foot_force = data.get("foot_force", self.foot_force)
        if "imu_state" in data:
            self._update_imu(data["imu_state"])

    def update_from_low_state(self, data: dict) -> None:
        if "imu_state" in data:
            self._update_imu(data["imu_state"])
        if "motor_state" in data:
            for i, m in enumerate(data["motor_state"][:12]):
                self.motors[i].q = m.get("q", 0.0)
                self.motors[i].temperature = m.get("temperature", 0)
                self.motors[i].lost = m.get("lost", 0)
        if "bms_state" in data:
            bms = data["bms_state"]
            self.battery.soc = bms.get("soc", 0)
            self.battery.current = bms.get("current", 0)
            self.battery.cycle = bms.get("cycle", 0)
        self.power_v = data.get("power_v", self.power_v)

    def update_from_multiple_state(self, data_str: str) -> None:
        try:
            data = json.loads(data_str) if isinstance(data_str, str) else data_str
        except (json.JSONDecodeError, TypeError):
            return
        self.volume = data.get("volume", self.volume)
        self.brightness = data.get("brightness", self.brightness)
        self.obstacles_avoid = data.get("obstaclesAvoidSwitch", self.obstacles_avoid)
        self.speed_level = data.get("speedLevel", self.speed_level)
        if "bodyHeight" in data:
            self.body_height = data["bodyHeight"]
        if "footRaiseHeight" in data:
            self.foot_raise_height = data["footRaiseHeight"]

    def _update_imu(self, imu: dict) -> None:
        self.imu.rpy = imu.get("rpy", self.imu.rpy)
        self.imu.quaternion = imu.get("quaternion", self.imu.quaternion)
        self.imu.gyroscope = imu.get("gyroscope", self.imu.gyroscope)
        self.imu.accelerometer = imu.get("accelerometer", self.imu.accelerometer)
        self.imu.temperature = imu.get("temperature", self.imu.temperature)
