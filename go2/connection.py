"""WebRTC connection to Unitree Go2 robot."""

import asyncio
import io
import json
import logging
import re
from typing import Callable

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCDataChannel
from PIL import Image

from go2.commands import SportCommand, build_sport_command
from go2.constants import (
    DATA_CHANNEL_NAME,
    DEFAULT_IP,
    HEARTBEAT_INTERVAL,
    SIGNALING_PORT_NEW,
    SIGNALING_PORT_OLD,
    TOPIC_LOW_STATE,
    TOPIC_MULTIPLE_STATE,
    TOPIC_SPORT_REQUEST,
    TOPIC_SPORT_STATE,
)
from go2.data_channel import DataChannelHandler
from go2.signaling import patch_sdp, signaling_new, signaling_old
from go2.telemetry import RobotState

logger = logging.getLogger(__name__)


def _unify_ice_credentials(sdp: str, pc: RTCPeerConnection) -> str:
    """Unify ICE credentials across SDP and aiortc internals.

    aiortc generates different credentials per m= line, but the Go2 robot
    uses BUNDLE and expects all sections to have the same credentials.

    This function:
    1. Picks the first ufrag/pwd from the SDP
    2. Finds the internal ICE connection that owns those credentials
    3. Patches ALL internal connections to use the same credentials
    4. Rewrites the SDP so every m= section has the same ufrag/pwd

    The previous approach picked conns[0] from __iceTransports (a set with
    non-deterministic order) which could differ from ufrags[0] in the SDP,
    causing STUN BINDING errors and data-channel timeouts.
    """
    ufrags = re.findall(r"a=ice-ufrag:(\S+)", sdp)
    pwds = re.findall(r"a=ice-pwd:(\S+)", sdp)
    if not ufrags or not pwds:
        return sdp

    master_ufrag = ufrags[0]
    master_pwd = pwds[0]

    # Patch internal ICE connections to match the SDP
    ice_transports = list(pc._RTCPeerConnection__iceTransports)
    # Find the connection that owns the master credentials
    master_conn = None
    for t in ice_transports:
        if t._connection._local_username == master_ufrag:
            master_conn = t._connection
            break

    if master_conn is None and ice_transports:
        # Fallback: if no connection matches (shouldn't happen), use first
        master_conn = ice_transports[0]._connection
        master_ufrag = master_conn._local_username
        master_pwd = master_conn._local_password

    if master_conn is not None:
        for t in ice_transports:
            conn = t._connection
            if conn is not master_conn:
                conn._local_username = master_ufrag
                conn._local_password = master_pwd

    # Rewrite SDP
    sdp = re.sub(r"a=ice-ufrag:\S+", f"a=ice-ufrag:{master_ufrag}", sdp)
    sdp = re.sub(r"a=ice-pwd:\S+", f"a=ice-pwd:{master_pwd}", sdp)
    return sdp


class Go2Connection:
    """Manages WebRTC connection to a Unitree Go2 robot."""

    def __init__(
        self,
        robot_ip: str = DEFAULT_IP,
        use_new_signaling: bool = True,
        capture_video_frames: bool = False,
    ) -> None:
        self.robot_ip = robot_ip
        self.use_new_signaling = use_new_signaling
        self.capture_video_frames = capture_video_frames

        self.pc: RTCPeerConnection | None = None
        self.dc: RTCDataChannel | None = None
        self.handler = DataChannelHandler()
        self.state = RobotState()

        self._heartbeat_task: asyncio.Task | None = None
        self._video_task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._validated = asyncio.Event()
        self._on_state_update: Callable | None = None
        self._latest_video_frame: bytes | None = None
        self._video_track = None
        self._video_frame_count = 0

        # Wire telemetry handlers
        self.handler.subscribe(TOPIC_SPORT_STATE, self._on_sport_state)
        self.handler.subscribe(TOPIC_LOW_STATE, self._on_low_state)
        self.handler.subscribe(TOPIC_MULTIPLE_STATE, self._on_multiple_state)
        self.handler.on_validated(self._on_validation_done)

    def on_state_update(self, callback: Callable[[RobotState], None]) -> None:
        """Set callback invoked on each telemetry update."""
        self._on_state_update = callback

    async def connect(self) -> None:
        """Establish WebRTC connection to the robot."""
        logger.info("Connecting to Go2 at %s ...", self.robot_ip)

        self.pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[]))

        # Create data channel
        self.dc = self.pc.createDataChannel(DATA_CHANNEL_NAME, ordered=True)
        self._setup_data_channel(self.dc)

        # Also handle data channel created by remote peer
        @self.pc.on("datachannel")
        def on_datachannel(channel):
            logger.info("Remote data channel received: %s", channel.label)
            self.dc = channel
            self._setup_data_channel(channel)

        @self.pc.on("track")
        def on_track(track):
            logger.info("Track received: %s", track.kind)
            if track.kind == "video":
                self._video_track = track
                if self.capture_video_frames and (self._connected.is_set() or self._validated.is_set()):
                    self._start_video_consumer()

        # Add video transceiver (receive only)
        self.pc.addTransceiver("video", direction="recvonly")
        # Add audio transceiver
        self.pc.addTransceiver("audio", direction="sendrecv")

        # Create and patch SDP offer — unify ICE credentials across all m= lines
        # (robot uses BUNDLE and expects the same ufrag/pwd everywhere)
        offer = await self.pc.createOffer()
        offer.sdp = patch_sdp(offer.sdp)
        offer.sdp = _unify_ice_credentials(offer.sdp, self.pc)

        await self.pc.setLocalDescription(offer)

        # Send offer via signaling (try multiple methods)
        answer_sdp = await self._try_signaling(offer.sdp)

        answer = RTCSessionDescription(sdp=answer_sdp, type="answer")
        await self.pc.setRemoteDescription(answer)

        # Re-patch ICE credentials after setRemoteDescription, in case
        # aiortc's BUNDLE handling reset internal state.
        _unify_ice_credentials(offer.sdp, self.pc)

        # Suppress "RTCIceTransport is closed" errors from aiortc's __connect
        # task — BUNDLE negotiation closes redundant transports, and aiortc
        # raises InvalidStateError for them. These errors are harmless.
        self._suppress_stale_connect_tasks()

        logger.info("Waiting for data channel to open...")
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=15)
        except asyncio.TimeoutError:
            raise ConnectionError("Data channel did not open within 15 seconds")

        logger.info("Waiting for validation...")
        try:
            await asyncio.wait_for(self._validated.wait(), timeout=10)
        except asyncio.TimeoutError:
            raise ConnectionError("Validation did not complete within 10 seconds")

        # Start heartbeat
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        # Subscribe to telemetry
        self._send(DataChannelHandler.build_subscribe(TOPIC_SPORT_STATE))
        self._send(DataChannelHandler.build_subscribe(TOPIC_LOW_STATE))
        self._send(DataChannelHandler.build_subscribe(TOPIC_MULTIPLE_STATE))

        logger.info("Connected and ready!")

    async def _consume_video(self, track) -> None:
        """Consume the incoming video track and cache the latest JPEG frame."""
        while True:
            try:
                frame = await track.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Video track ended: %s", exc)
                break

            try:
                rgb_frame = frame.to_ndarray(format="rgb24")
                buffer = io.BytesIO()
                Image.fromarray(rgb_frame).save(buffer, format="JPEG", quality=75)
                self._latest_video_frame = buffer.getvalue()
                self._video_frame_count += 1
                if self._video_frame_count == 1:
                    logger.info("First video frame received")
            except Exception:
                logger.exception("Failed to convert video frame")

    def latest_video_frame(self) -> bytes | None:
        """Return the most recent JPEG-encoded frame, if available."""
        return self._latest_video_frame

    async def wait_for_video_frame(self, timeout: float = 10.0) -> bytes:
        """Wait until a JPEG frame is available and return it."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            frame = self._latest_video_frame
            if frame is not None:
                return frame
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"No video frame received within {timeout:.1f} seconds")
            await asyncio.sleep(0.05)

    async def save_video_frame(self, path: str, timeout: float = 10.0) -> str:
        """Wait for a frame and save it to disk."""
        frame = await self.wait_for_video_frame(timeout=timeout)
        with open(path, "wb") as file_obj:
            file_obj.write(frame)
        return path

    def _suppress_stale_connect_tasks(self) -> None:
        """Eat exceptions from aiortc's internal __connect tasks.

        When BUNDLE is negotiated, aiortc closes redundant ICE transports.
        The __connect coroutine then raises InvalidStateError for those closed
        transports.  The error is logged as 'Task exception was never retrieved'
        and can poison the connection.  We retrieve them eagerly so they don't
        propagate.
        """
        if self.pc is None:
            return
        for task in asyncio.all_tasks():
            coro = task.get_coro()
            coro_name = getattr(coro, "__qualname__", "")
            if "RTCPeerConnection.__connect" in coro_name and task.done():
                task.exception()  # mark as retrieved

    async def _try_signaling(self, sdp: str) -> str:
        """Try signaling methods in order: new(9991), new(8081), old(8081)."""
        errors = []
        if self.use_new_signaling:
            for port in [SIGNALING_PORT_NEW, SIGNALING_PORT_OLD]:
                try:
                    return await signaling_new(self.robot_ip, sdp, port=port)
                except Exception as e:
                    errors.append(f"new({port}): {e}")
                    logger.warning("Signaling new on port %d failed: %s", port, e)
        try:
            return await signaling_old(self.robot_ip, sdp)
        except Exception as e:
            errors.append(f"old(8081): {e}")
        raise ConnectionError(f"All signaling methods failed: {'; '.join(errors)}")

    def _setup_data_channel(self, dc: RTCDataChannel) -> None:
        @dc.on("open")
        def on_open():
            logger.info("Data channel opened")
            self._connected.set()
            if self.capture_video_frames and self._video_track is not None:
                self._start_video_consumer()

        @dc.on("message")
        def on_message(message):
            # If we get a message, channel is open (even if "open" event didn't fire)
            if not self._connected.is_set():
                logger.info("Data channel opened (detected via message)")
                self._connected.set()
                if self.capture_video_frames and self._video_track is not None:
                    self._start_video_consumer()
            if dc.readyState != "open":
                dc._setReadyState("open")
            if isinstance(message, bytes):
                logger.debug("Binary message received (%d bytes)", len(message))
                return
            self.handler.handle_message(message)
            # Check for pending validation response
            pending = self.handler.get_pending_validation()
            if pending:
                self._send_via_channel(dc, json.dumps(pending))

        @dc.on("close")
        def on_close():
            logger.warning("Data channel closed")
            self._connected.clear()
            self._validated.clear()

    def _on_validation_done(self) -> None:
        self._validated.set()
        if self.capture_video_frames and self._video_track is not None:
            self._start_video_consumer()

    def _start_video_consumer(self) -> None:
        if self._video_track is None or self._video_task:
            return
        self._video_task = asyncio.create_task(self._consume_video(self._video_track))

    def _send_via_channel(self, dc: RTCDataChannel | None, payload: str) -> bool:
        """Send text over SCTP, even if aiortc still reports connecting."""
        if dc is None:
            return False

        try:
            if dc.readyState == "open":
                dc.send(payload)
            else:
                dc.transport._data_channel_send(dc, payload)
            return True
        except Exception:
            logger.exception("Failed to send data channel payload")
            return False

    def _send(self, msg: dict) -> None:
        self._send_via_channel(self.dc, json.dumps(msg))

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            self._send(DataChannelHandler.build_heartbeat())

    # --- Telemetry handlers ---

    def _on_sport_state(self, data) -> None:
        self.state.update_from_sport_state(data)
        if self._on_state_update:
            self._on_state_update(self.state)

    def _on_low_state(self, data) -> None:
        self.state.update_from_low_state(data)
        if self._on_state_update:
            self._on_state_update(self.state)

    def _on_multiple_state(self, data) -> None:
        self.state.update_from_multiple_state(data)
        if self._on_state_update:
            self._on_state_update(self.state)

    # --- Commands ---

    def send_command(self, command: SportCommand, parameter: dict | str | None = None) -> None:
        """Send a sport command to the robot."""
        data = build_sport_command(command, parameter)
        msg = DataChannelHandler.build_request(TOPIC_SPORT_REQUEST, data)
        self._send(msg)
        logger.info("Sent command: %s", command.name)

    def move(self, x: float = 0.0, y: float = 0.0, yaw: float = 0.0) -> None:
        """Move the robot. x=forward/back, y=left/right, yaw=rotation."""
        self.send_command(SportCommand.MOVE, {"x": x, "y": y, "z": yaw})

    def stop(self) -> None:
        self.send_command(SportCommand.STOP_MOVE)

    def stand_up(self) -> None:
        self.send_command(SportCommand.STAND_UP)

    def stand_down(self) -> None:
        self.send_command(SportCommand.STAND_DOWN)

    def sit(self) -> None:
        self.send_command(SportCommand.SIT)

    def balance_stand(self) -> None:
        self.send_command(SportCommand.BALANCE_STAND)

    def recovery_stand(self) -> None:
        self.send_command(SportCommand.RECOVERY_STAND)

    def hello(self) -> None:
        self.send_command(SportCommand.HELLO)

    def stretch(self) -> None:
        self.send_command(SportCommand.STRETCH)

    def dance1(self) -> None:
        self.send_command(SportCommand.DANCE1)

    def dance2(self) -> None:
        self.send_command(SportCommand.DANCE2)

    def set_body_height(self, height: float) -> None:
        self.send_command(SportCommand.BODY_HEIGHT, {"data": height})

    def set_speed_level(self, level: int) -> None:
        self.send_command(SportCommand.SPEED_LEVEL, {"data": level})

    def set_euler(self, roll: float = 0.0, pitch: float = 0.0, yaw: float = 0.0) -> None:
        self.send_command(SportCommand.EULER, {"roll": roll, "pitch": pitch, "yaw": yaw})

    def set_foot_raise_height(self, height: float) -> None:
        self.send_command(SportCommand.FOOT_RAISE_HEIGHT, {"data": height})

    def switch_gait(self, gait: int) -> None:
        """Switch gait: 0=idle, 1=trot, 2=run, 3=stairs."""
        self.send_command(SportCommand.SWITCH_GAIT, {"d": gait})

    def front_flip(self) -> None:
        self.send_command(SportCommand.FRONT_FLIP)

    def front_jump(self) -> None:
        self.send_command(SportCommand.FRONT_JUMP)

    def front_pounce(self) -> None:
        self.send_command(SportCommand.FRONT_POUNCE)

    def wiggle_hips(self) -> None:
        self.send_command(SportCommand.WIGGLE_HIPS)

    def finger_heart(self) -> None:
        self.send_command(SportCommand.FINGER_HEART)

    def damp(self) -> None:
        self.send_command(SportCommand.DAMP)

    def content(self) -> None:
        self.send_command(SportCommand.CONTENT)

    def wallow(self) -> None:
        self.send_command(SportCommand.WALLOW)

    def handstand(self) -> None:
        self.send_command(SportCommand.HANDSTAND)

    def cross_step(self) -> None:
        self.send_command(SportCommand.CROSS_STEP)

    def bound(self) -> None:
        self.send_command(SportCommand.BOUND)

    def moon_walk(self) -> None:
        self.send_command(SportCommand.MOON_WALK)

    def economic_gait(self) -> None:
        self.send_command(SportCommand.ECONOMIC_GAIT)

    def lead_follow(self) -> None:
        self.send_command(SportCommand.LEAD_FOLLOW)

    def video(self, on: bool = True) -> None:
        logger.debug("Video stream %s", "on" if on else "off")
        self._send(DataChannelHandler.build_video(on))

    def audio(self, on: bool = True) -> None:
        self._send(DataChannelHandler.build_audio(on))

    def subscribe(self, topic: str, callback: Callable) -> None:
        """Subscribe to a custom topic."""
        self.handler.subscribe(topic, callback)
        self._send(DataChannelHandler.build_subscribe(topic))

    async def disconnect(self) -> None:
        """Close the connection."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if self._video_task:
            self._video_task.cancel()
            self._video_task = None
        self._latest_video_frame = None
        self._video_frame_count = 0
        if self.pc:
            await self.pc.close()
            self.pc = None
        self._connected.clear()
        self._validated.clear()
        logger.info("Disconnected")
