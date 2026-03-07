"""Data channel message handling for Go2 WebRTC."""

import json
import logging
from datetime import datetime
from typing import Callable

from go2.crypto import validation_response

logger = logging.getLogger(__name__)


class DataChannelHandler:
    """Handles data channel message routing, validation, and heartbeat."""

    def __init__(self) -> None:
        self.validated = False
        self._subscribers: dict[str, list[Callable]] = {}
        self._response_handlers: dict[str, Callable] = {}
        self._on_validated: Callable | None = None

    def on_validated(self, callback: Callable) -> None:
        """Set callback for when validation completes."""
        self._on_validated = callback

    def subscribe(self, topic: str, callback: Callable) -> None:
        """Register a callback for messages on a topic."""
        self._subscribers.setdefault(topic, []).append(callback)

    def on_response(self, topic: str, callback: Callable) -> None:
        """Register a one-shot handler for response messages."""
        self._response_handlers[topic] = callback

    def handle_message(self, raw: str) -> None:
        """Process an incoming data channel message."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Non-JSON message: %s", raw[:100])
            return

        msg_type = msg.get("type", "")
        topic = msg.get("topic", "")
        data = msg.get("data")

        if msg_type == "validation":
            self._handle_validation(data)
        elif msg_type == "msg":
            self._dispatch_topic(topic, data)
        elif msg_type == "res":
            handler = self._response_handlers.pop(topic, None)
            if handler:
                handler(data)
        elif msg_type == "err":
            logger.error("Robot error on topic '%s': %s", topic, data)
        elif msg_type == "add_error":
            logger.warning("Robot error added: %s", data)
        elif msg_type == "rm_error":
            logger.info("Robot error cleared: %s", data)
        elif msg_type == "rtc_report":
            logger.debug("RTC report: %s", data)

    def _handle_validation(self, data) -> None:
        if data == "Validation Ok.":
            self.validated = True
            logger.info("Validation successful")
            if self._on_validated:
                self._on_validated()
            return
        # data is the challenge key
        response = validation_response(str(data))
        logger.info("Validation challenge received, sending response")
        self._validation_response = {
            "type": "validation",
            "topic": "",
            "data": response,
        }

    def get_pending_validation(self) -> dict | None:
        """Get pending validation response to send, if any."""
        resp = getattr(self, "_validation_response", None)
        self._validation_response = None
        return resp

    def _dispatch_topic(self, topic: str, data) -> None:
        handlers = self._subscribers.get(topic, [])
        for handler in handlers:
            try:
                handler(data)
            except Exception:
                logger.exception("Error in handler for topic '%s'", topic)

    @staticmethod
    def build_subscribe(topic: str) -> dict:
        return {"type": "subscribe", "topic": topic}

    @staticmethod
    def build_unsubscribe(topic: str) -> dict:
        return {"type": "unsubscribe", "topic": topic}

    @staticmethod
    def build_heartbeat() -> dict:
        now = datetime.now()
        return {
            "type": "heartbeat",
            "topic": "",
            "data": {
                "timeInStr": now.strftime("%Y-%m-%d %H:%M:%S"),
                "timeInNum": int(now.timestamp()),
            },
        }

    @staticmethod
    def build_request(topic: str, data) -> dict:
        return {"type": "req", "topic": topic, "data": data}

    @staticmethod
    def build_video(on: bool) -> dict:
        return {"type": "vid", "topic": "", "data": "on" if on else "off"}

    @staticmethod
    def build_audio(on: bool) -> dict:
        return {"type": "aud", "topic": "", "data": "on" if on else "off"}
