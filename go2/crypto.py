"""Cryptography utilities for Go2 WebRTC signaling."""

import base64
import hashlib
import uuid

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad

from go2.constants import CON_NOTIFY_KEY


def generate_aes_key() -> str:
    """Generate a 32-char hex AES-256 key from UUID."""
    return uuid.uuid4().hex


def aes256_ecb_encrypt(plaintext: str, key: str) -> str:
    """Encrypt with AES-256-ECB + PKCS7 padding, return base64."""
    cipher = AES.new(key.encode(), AES.MODE_ECB)
    padded = pad(plaintext.encode(), AES.block_size)
    encrypted = cipher.encrypt(padded)
    return base64.b64encode(encrypted).decode()


def aes256_ecb_decrypt(ciphertext_b64: str, key: str) -> str:
    """Decrypt AES-256-ECB base64 ciphertext."""
    cipher = AES.new(key.encode(), AES.MODE_ECB)
    encrypted = base64.b64decode(ciphertext_b64)
    decrypted = unpad(cipher.decrypt(encrypted), AES.block_size)
    return decrypted.decode()


def aes128_gcm_decrypt(data1_b64: str) -> str:
    """Decrypt con_notify response with hardcoded AES-128-GCM key."""
    raw = base64.b64decode(data1_b64)
    ciphertext = raw[:-28]
    nonce = raw[-28:-16]
    tag = raw[-16:]
    cipher = AES.new(CON_NOTIFY_KEY, AES.MODE_GCM, nonce=nonce)
    decrypted = cipher.decrypt_and_verify(ciphertext, tag)
    return decrypted.decode()


def rsa_encrypt(plaintext: bytes, public_key_b64: str) -> str:
    """RSA PKCS1_v1_5 encrypt, chunked, return base64."""
    der_bytes = base64.b64decode(public_key_b64)
    rsa_key = RSA.import_key(der_bytes)
    cipher = PKCS1_v1_5.new(rsa_key)
    chunk_size = rsa_key.size_in_bytes() - 11
    chunks = []
    for i in range(0, len(plaintext), chunk_size):
        chunk = plaintext[i : i + chunk_size]
        chunks.append(cipher.encrypt(chunk))
    return base64.b64encode(b"".join(chunks)).decode()


def compute_path_ending(data1: str) -> str:
    """Compute the URL path ending from con_notify data1."""
    last_10 = data1[-10:]
    str_arr = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
    result = ""
    for i in range(0, len(last_10), 2):
        if i + 1 < len(last_10):
            ch = last_10[i + 1]
            if ch in str_arr:
                result += str(str_arr.index(ch))
    return result


def validation_response(challenge_key: str) -> str:
    """Compute MD5-based validation response for data channel handshake."""
    prefixed = f"UnitreeGo2_{challenge_key}"
    md5_hex = hashlib.md5(prefixed.encode()).hexdigest()
    md5_bytes = bytes.fromhex(md5_hex)
    return base64.b64encode(md5_bytes).decode()
