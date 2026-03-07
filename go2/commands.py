"""Sport commands for Unitree Go2 robot."""

import json
import random
import time
from enum import IntEnum


class SportCommand(IntEnum):
    # Basic state
    DAMP = 1001
    BALANCE_STAND = 1002
    STOP_MOVE = 1003
    STAND_UP = 1004
    STAND_DOWN = 1005
    RECOVERY_STAND = 1006

    # Posture & movement
    EULER = 1007
    MOVE = 1008
    SIT = 1009
    RISE_SIT = 1010
    SWITCH_GAIT = 1011
    TRIGGER = 1012
    BODY_HEIGHT = 1013
    FOOT_RAISE_HEIGHT = 1014
    SPEED_LEVEL = 1015

    # Gestures
    HELLO = 1016
    STRETCH = 1017
    TRAJECTORY_FOLLOW = 1018
    CONTINUOUS_GAIT = 1019
    CONTENT = 1020
    WALLOW = 1021

    # Dance
    DANCE1 = 1022
    DANCE2 = 1023

    # Query
    GET_BODY_HEIGHT = 1024
    GET_FOOT_RAISE_HEIGHT = 1025
    GET_SPEED_LEVEL = 1026
    GET_STATE = 1034

    # Tricks
    FRONT_FLIP = 1030
    FRONT_JUMP = 1031
    FRONT_POUNCE = 1032
    WIGGLE_HIPS = 1033
    ECONOMIC_GAIT = 1035
    FINGER_HEART = 1036
    STAND_OUT = 1039
    LEFT_FLIP = 1042
    RIGHT_FLIP = 1043
    BACK_FLIP = 1044
    LEAD_FOLLOW = 1045
    STANDUP = 1050
    CROSS_WALK = 1051
    HANDSTAND = 1301
    CROSS_STEP = 1302
    ONESIDED_STEP = 1303
    BOUND = 1304
    MOON_WALK = 1305


def _generate_request_id() -> int:
    ms = int(time.time() * 1000)
    return (ms % (2**31)) + random.randint(0, 1000)


def build_sport_command(api_id: int | SportCommand, parameter: dict | str | None = None) -> dict:
    """Build a sport command message dict ready for data channel."""
    data = {
        "header": {
            "identity": {
                "id": _generate_request_id(),
                "api_id": int(api_id),
            }
        },
        "parameter": "",
    }
    if parameter is not None:
        if isinstance(parameter, dict):
            data["parameter"] = json.dumps(parameter)
        else:
            data["parameter"] = str(parameter)
    return data
