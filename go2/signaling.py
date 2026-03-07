"""HTTP signaling for Go2 WebRTC connection."""

import asyncio
import base64
import json
import logging
import random
import re
import string
from functools import partial
from urllib.request import Request, urlopen

from go2.constants import SIGNALING_PORT_NEW, SIGNALING_PORT_OLD
from go2.crypto import (
    aes128_gcm_decrypt,
    aes256_ecb_decrypt,
    aes256_ecb_encrypt,
    compute_path_ending,
    generate_aes_key,
    rsa_encrypt,
)

logger = logging.getLogger(__name__)

_ICE_CHARS = string.ascii_letters + string.digits + "+/"


def generate_ice_credentials() -> tuple[str, str]:
    """Generate shared ICE ufrag (4 chars) and pwd (22 chars)."""
    ufrag = "".join(random.choice(_ICE_CHARS) for _ in range(4))
    pwd = "".join(random.choice(_ICE_CHARS) for _ in range(22))
    return ufrag, pwd


def patch_sdp(sdp: str) -> str:
    """Patch SDP: keep only SHA-256 fingerprints.

    Note: aiortc already generates unified ICE credentials for all m= lines,
    so we must NOT replace them — the internal ICE stack uses the original values.
    """
    lines = sdp.split("\r\n")
    patched = []
    for line in lines:
        if line.startswith("a=fingerprint:") and "sha-256" not in line.lower():
            continue
        patched.append(line)
    return "\r\n".join(patched)


def _http_post(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 10) -> str:
    """Synchronous HTTP POST using urllib (compatible with Boost.Beast server)."""
    req = Request(url, data=data, method="POST")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode()


async def _async_post(url: str, data: bytes | None = None, headers: dict | None = None) -> str:
    """Run HTTP POST in executor to not block the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_http_post, url, data, headers))


async def signaling_old(robot_ip: str, sdp: str) -> str:
    """Old signaling via port 8081 (unencrypted)."""
    url = f"http://{robot_ip}:{SIGNALING_PORT_OLD}/offer"
    body = json.dumps({
        "id": "STA_localNetwork",
        "sdp": sdp,
        "type": "offer",
        "token": "",
    }).encode()
    headers = {"Content-Type": "application/json"}
    raw = await _async_post(url, body, headers)
    answer = json.loads(raw)
    if answer.get("sdp") == "reject":
        raise ConnectionError("Robot rejected connection (another client may be connected)")
    logger.info("Old signaling: got SDP answer")
    return answer["sdp"]


async def signaling_new(robot_ip: str, sdp: str, port: int = SIGNALING_PORT_NEW) -> str:
    """New signaling (encrypted). Works on port 9991 or 8081."""
    # Step 1: Get RSA public key
    url = f"http://{robot_ip}:{port}/con_notify"
    raw = await _async_post(url)

    b64_str = raw.strip()
    notify_json = json.loads(base64.b64decode(b64_str))
    data1 = notify_json["data1"]
    data2 = notify_json.get("data2", 1)

    if data2 == 2:
        data1 = aes128_gcm_decrypt(data1)

    # Extract RSA public key (strip first and last 10 chars)
    public_key_b64 = data1[10:-10]
    path_ending = compute_path_ending(data1)
    logger.info("Got RSA public key from port %d, path_ending=%s", port, path_ending)

    # Step 2: Encrypt and send SDP
    aes_key = generate_aes_key()
    sdp_payload = json.dumps({
        "id": "STA_localNetwork",
        "sdp": sdp,
        "type": "offer",
        "token": "",
    })
    encrypted_sdp = aes256_ecb_encrypt(sdp_payload, aes_key)
    encrypted_key = rsa_encrypt(aes_key.encode(), public_key_b64)

    url = f"http://{robot_ip}:{port}/con_ing_{path_ending}"
    body = json.dumps({"data1": encrypted_sdp, "data2": encrypted_key}).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    encrypted_response = await _async_post(url, body, headers)
    answer_str = aes256_ecb_decrypt(encrypted_response.strip(), aes_key)
    answer = json.loads(answer_str)

    if answer.get("sdp") == "reject":
        raise ConnectionError("Robot rejected connection (another client may be connected)")
    logger.info("New signaling: got SDP answer from port %d", port)
    return answer["sdp"]
