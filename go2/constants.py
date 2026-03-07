import os

DEFAULT_IP = os.environ.get("GO2_IP", "192.168.1.66")
SIGNALING_PORT_OLD = 8081
SIGNALING_PORT_NEW = 9991

HEARTBEAT_INTERVAL = 2.0  # seconds

# AES-128-GCM key for con_notify decryption (firmware hardcoded)
CON_NOTIFY_KEY = bytes([232, 86, 130, 189, 22, 84, 155, 0, 142, 4, 166, 104, 43, 179, 235, 227])

# Telemetry topics
TOPIC_LOW_STATE = "rt/lf/lowstate"
TOPIC_SPORT_STATE = "rt/lf/sportmodestate"
TOPIC_MULTIPLE_STATE = "rt/multiplestate"

# Command topics
TOPIC_SPORT_REQUEST = "rt/api/sport/request"
TOPIC_VUI_REQUEST = "rt/api/vui/request"

# LiDAR topics
TOPIC_LIDAR_VOXEL = "rt/utlidar/voxel_map_compressed"

DATA_CHANNEL_NAME = "data"
