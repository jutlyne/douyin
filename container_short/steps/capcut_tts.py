"""CapCut common_task TTS adapter.

This is adapted from sample/test.py, but shaped as a pipeline helper:
text -> create CapCut TTS task -> poll query -> download the produced MP3.

CapCut's private response shape can vary, so the parser below walks the JSON
recursively and accepts common audio URL fields instead of hard-coding one path.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
import uuid
from copy import deepcopy
from urllib.parse import urlencode

from .ffmpeg_ops import duration_seconds


BASE = "https://editor-api-sg.capcutapi.com"

DEFAULT_VOICE = "BV074_streaming"
DEFAULT_RESOURCE_ID = "7102355709945188865"

NETWORK_RETRY_ATTEMPTS = 5
NETWORK_RETRY_BACKOFF_SECONDS = 1.0
TRANSIENT_HTTP_STATUS_CODES = frozenset({408, 425, 429})
TASK_RETRY_ATTEMPTS = 3
TASK_RETRY_BACKOFF_SECONDS = 2.0
CONCURRENT_LIMIT_RETRY_BACKOFF_SECONDS = 5.0

DEFAULT_DEVICE = {
    "aid": "359289",
    "app_name": "CapCut",
    "appvr": "8.7.0",
    "version_name": "8.7.0",
    "version_code": "8.7.0",
    "channel": "capcutpc_google",
    "device_platform": "mac",
    "device_type": "MacBookPro17,1",
    "device_brand": "MacBookPro17,1",
    "os_version": "15.7.4",
    "device_id": "7647183892936328721",
    "iid": "7647185302080423697",
    "region": "VN",
    "loc": "VN",
    "lan": "vi-VN",
    "pf": "3",
    "tdid": "7647183892936328721",
}

TTS_SIGN_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAmTd34Lw4b7IuldSXh/zY
CMla+ITdGG5TeWz6ad+OySd4r+IrY45AoqrYUxhQ2dl+7z+i7r/5vEa8rr39BYfB
8AGMQLmZA8HmgpWBsqrn/V6daUALkKnkLb70Fn32CJigIuGXAYqxUdGuI340aC+0
v5Es3puJsHyzf01/AelE4Cdc6bZhQrASJLBh8R3BQToYClmDVSDUQk28o8sl/guA
Z4n303Vj+6Siv1HayPCdV6kpVVnMBAG4+umUbwGmn132N3fgpzLarFF3XyWmS1zh
D/J07iM/rP8GDO9IskHNHd2phrO0G6KzrcFAnTBHjVv+hCBEfzN/no3FNA9AuC36
mwIDAQAB
-----END PUBLIC KEY-----"""


class RetryableCapCutTaskError(RuntimeError):
    """A provider task failed for a narrowly recognized transient reason."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def compact_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _random_decimal_id(prefix: str = "7", length: int = 19) -> str:
    if len(prefix) >= length:
        return prefix[:length]
    digits = "".join(str(secrets.randbelow(10)) for _ in range(length - len(prefix)))
    return prefix + digits


def _random_device_identity() -> dict:
    device_id = _random_decimal_id()
    return {
        "device_id": device_id,
        "tdid": device_id,
        "iid": _random_decimal_id(),
    }


def _load_device(device_json_path: str | None) -> dict:
    device = deepcopy(DEFAULT_DEVICE)
    if device_json_path:
        if device_json_path.lstrip().startswith("{"):
            overrides = json.loads(device_json_path)
        elif device_json_path.startswith("gs://"):
            from . import gcsio

            local_path = os.path.join(tempfile.mkdtemp(prefix="capcut_device_"), "device.json")
            gcsio.download(device_json_path, local_path)
            with open(local_path, "r", encoding="utf-8") as fp:
                overrides = json.load(fp)
        else:
            with open(device_json_path, "r", encoding="utf-8") as fp:
                overrides = json.load(fp)
        device.update(overrides)
    else:
        device.update(_random_device_identity())
    if not device.get("tdid"):
        device["tdid"] = device["device_id"]
    if not device.get("iid"):
        device["iid"] = _random_decimal_id()
    return device


def make_device(device_json_path: str | None = None) -> dict:
    """Create one CapCut device profile for a whole job."""
    return _load_device(device_json_path)


def _der_len(data: bytes, pos: int) -> tuple[int, int]:
    first = data[pos]
    pos += 1
    if first < 0x80:
        return first, pos
    nbytes = first & 0x7F
    return int.from_bytes(data[pos : pos + nbytes], "big"), pos + nbytes


def _der_value(data: bytes, pos: int, tag: int) -> tuple[bytes, int]:
    if data[pos] != tag:
        raise ValueError(f"bad DER tag: expected 0x{tag:02x}, got 0x{data[pos]:02x}")
    length, pos = _der_len(data, pos + 1)
    return data[pos : pos + length], pos + length


def _der_int(data: bytes, pos: int) -> tuple[int, int]:
    raw, pos = _der_value(data, pos, 0x02)
    return int.from_bytes(raw.lstrip(b"\x00"), "big"), pos


def _rsa_public_numbers_from_pem(pem: str) -> tuple[int, int]:
    b64 = "".join(line for line in pem.splitlines() if not line.startswith("-----"))
    der = base64.b64decode(b64)
    outer, pos = _der_value(der, 0, 0x30)
    if pos != len(der):
        raise ValueError("trailing data in public key")
    _, pos = _der_value(outer, 0, 0x30)
    bit_string, pos = _der_value(outer, pos, 0x03)
    if pos != len(outer) or not bit_string or bit_string[0] != 0:
        raise ValueError("bad subjectPublicKeyInfo")
    rsa_seq, pos = _der_value(bit_string[1:], 0, 0x30)
    if pos != len(bit_string[1:]):
        raise ValueError("trailing data in RSA public key")
    modulus, pos = _der_int(rsa_seq, 0)
    exponent, pos = _der_int(rsa_seq, pos)
    if pos != len(rsa_seq):
        raise ValueError("trailing integer data in RSA public key")
    return modulus, exponent


def _rsa_encrypt_pkcs1v15(message: str) -> str:
    modulus, exponent = _rsa_public_numbers_from_pem(TTS_SIGN_PUBLIC_KEY_PEM)
    key_len = (modulus.bit_length() + 7) // 8
    msg = message.encode("utf-8")
    if len(msg) > key_len - 11:
        raise ValueError("message too long for RSA PKCS#1 v1.5")
    ps_len = key_len - len(msg) - 3
    ps = bytearray()
    while len(ps) < ps_len:
        chunk = secrets.token_bytes(ps_len - len(ps))
        ps.extend(b for b in chunk if b != 0)
    encoded = b"\x00\x02" + bytes(ps[:ps_len]) + b"\x00" + msg
    encrypted = pow(int.from_bytes(encoded, "big"), exponent, modulus).to_bytes(key_len, "big")
    return base64.b64encode(encrypted).decode("ascii")


def _make_tts_payload_sign(ssml: str, extra_info: str, device_id: str, app_id: str) -> str:
    ssml_md5 = hashlib.md5(ssml.encode("utf-8")).hexdigest()
    sign_input = f"appid:{app_id}&did:{device_id}&creditDisable:false&ssml:{ssml_md5}"
    sign_input += f"&extraInfo:{extra_info}"
    return _rsa_encrypt_pkcs1v15(sign_input)


def _make_x_ss_stub(body_text: str) -> str:
    return hashlib.md5(body_text.encode("utf-8")).hexdigest()


def _make_sign_header(url: str, appvr: str, device_time: str, tdid: str) -> str:
    path = url.split("?", 1)[0]
    sign_str = f"9e2c|{path[-7:]}|3|{appvr}|{device_time}|{tdid}|11ac"
    return hashlib.md5(sign_str.encode("utf-8")).hexdigest()


def _make_trace_id() -> str:
    seed = uuid.uuid4().hex[:32]
    return f"00-{seed}-{seed[:16]}-01"


def _escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _common_query(device: dict, babi_param: dict | None = None, *, include_region: bool) -> dict:
    q = {
        "app_name": device["app_name"],
        "device_type": device["device_type"],
        "os_version": device["os_version"],
        "channel": device["channel"],
        "version_name": device["version_name"],
        "device_brand": device["device_brand"],
        "device_id": device["device_id"],
        "iid": device["iid"],
        "version_code": device["version_code"],
        "device_platform": device["device_platform"],
        "aid": device["aid"],
    }
    if include_region:
        q["region"] = device["region"]
    if babi_param is not None:
        q["babi_param"] = compact_json(babi_param)
    return q


def _base_headers(device: dict, body_text: str, *, appid: bool) -> dict:
    now = str(int(time.time()))
    headers = {
        "content-type": "application/json",
        "appvr": device["appvr"],
        "ch": device["channel"],
        "device-time": now,
        "lan": device["lan"],
        "loc": device["loc"],
        "pf": device["pf"],
        "sign-ver": "1",
        "tdid": device["tdid"],
        "x-ss-stub": _make_x_ss_stub(body_text),
        "x-ss-dp": device["aid"],
        "x-khronos": now,
        "x-tt-trace-id": _make_trace_id(),
        "user-agent": "Cronet/TTNetVersion:1d7cc3b1 2025-07-16 QuicVersion:52c2b40d 2025-04-03",
        "accept-encoding": "gzip, deflate",
        "store-country-code": device["loc"].lower(),
        "store-country-code-src": "did",
        "is-dispatch-us-ttp": "0",
        "is-app-region-us-ttp": "0",
    }
    if appid:
        headers["app-sdk-version"] = device["appvr"]
        headers["appid"] = device["aid"]
    return headers


def _request_parts(path: str, query: dict, body: dict, device: dict, *, appid: bool) -> tuple[str, dict, str]:
    body_text = compact_json(body)
    url = BASE + path + "?" + urlencode(query)
    headers = _base_headers(device, body_text, appid=appid)
    lower_headers = {k.lower(): v for k, v in headers.items()}
    headers["sign"] = _make_sign_header(
        url, device["appvr"], lower_headers["device-time"], device["tdid"]
    )
    return url, headers, body_text


def _tts_new_request(text: str, voice: str, resource_id: str, rate: float, device: dict):
    babi = {
        "feature_entrance": "editor",
        "feature_entrance_detail": "editor-feature-text_to_speech",
        "feature_key": "text_to_speech",
        "scenario": "video_editor",
    }
    ssml = (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="vi-VN">\n'
        f'    <voice name="{voice}" mock_tone_info="" platform="sami" '
        f'resource_id="{resource_id}" emotion="" emotion_scale="0" style="" role="" '
        'moyin_emotion="" is_clone_tone="false" need_subtitle_timestamp="false">\n'
        f'        <prosody rate="{rate:.4f}">{_escape_xml(text)}</prosody>\n'
        "    </voice>\n"
        "</speak>"
    )
    extra_info = compact_json({"benefit_info": {}})
    payload = {
        "audio_format": "mp3",
        "babi_param": compact_json(babi),
        "credit_disable": False,
        "extra_info": extra_info,
        "need_merge_voice": False,
        "need_subtitle_timestamp": False,
        "scene": "text_to_speech",
        "ssml": ssml,
    }
    payload["sign"] = _make_tts_payload_sign(ssml, extra_info, device["device_id"], device["aid"])
    bind_id = str(uuid.uuid4())
    body = {
        "bind_id": bind_id,
        "can_queue": True,
        "enter_from": "text_to_speech",
        "tasks": [
            {
                "context": str(uuid.uuid4()),
                "payload": compact_json(payload),
                "req_key": "sami_text_to_speech",
                "task_version": "v3",
            }
        ],
    }
    return _request_parts(
        "/lv/v1/common_task/new",
        _common_query(device, babi, include_region=True),
        body,
        device,
        appid=True,
    ), bind_id


def _tts_query_request(task_id: str, token: str, bind_id: str, device: dict):
    body = {
        "tasks": [
            {
                "bind_id": bind_id,
                "id": task_id,
                "req_key": "sami_text_to_speech",
                "task_version": "v3",
                "token": token,
            }
        ]
    }
    return _request_parts(
        "/lv/v1/common_task/query",
        _common_query(device, None, include_region=False),
        body,
        device,
        appid=True,
    )


def _checked_json_response(resp, label: str) -> dict:
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"{label} returned non-JSON HTTP {resp.status_code}: {resp.text[:500]}") from exc
    if resp.status_code >= 400:
        raise RuntimeError(f"{label} HTTP {resp.status_code}: {data}")
    ret = data.get("ret")
    if ret not in (None, 0, "0"):
        msg = data.get("errmsg") or data.get("msg") or data.get("message")
        raise RuntimeError(f"{label} ret={ret}: {msg or data}")
    return data


def _walk_jsonish(value):
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped[0] in "[{":
            try:
                yield from _walk_jsonish(json.loads(stripped))
            except json.JSONDecodeError:
                pass
        yield value
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_jsonish(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_jsonish(child)


def _extract_task_ref(data: dict, bind_id: str) -> tuple[str, str, str]:
    for node in _walk_jsonish(data):
        if not isinstance(node, dict):
            continue
        task_id = node.get("id") or node.get("task_id") or node.get("taskId")
        token = node.get("token")
        if task_id and token:
            return str(task_id), str(token), str(node.get("bind_id") or bind_id)
    raise RuntimeError(f"CapCut TTS response missing task id/token: {str(data)[:800]}")


def _extract_audio_url(data: dict) -> str | None:
    preferred = {
        "audio_url",
        "audio_url_v2",
        "url",
        "download_url",
        "play_url",
        "voice_url",
        "media_url",
    }
    fallback: list[str] = []
    for node in _walk_jsonish(data):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    if key in preferred:
                        return value
                    if any(ext in value.lower() for ext in (".mp3", ".m4a", ".wav", "audio")):
                        fallback.append(value)
        elif isinstance(node, str) and node.startswith(("http://", "https://")):
            if any(ext in node.lower() for ext in (".mp3", ".m4a", ".wav", "audio")):
                fallback.append(node)
    return fallback[0] if fallback else None


def _looks_failed(data: dict) -> bool:
    fail_words = ("fail", "failed", "error", "timeout", "cancel")
    for node in _walk_jsonish(data):
        if isinstance(node, dict):
            for key in ("status", "task_status", "state", "message", "msg"):
                value = str(node.get(key, "")).lower()
                if any(word in value for word in fail_words):
                    return True
    return False


def _retryable_task_failure_reason(data: dict) -> str | None:
    """Return a stable label only for narrowly observed transient failures."""

    for node in _walk_jsonish(data):
        if not isinstance(node, dict):
            continue
        raw_code = node.get("err_code")
        try:
            error_code = int(raw_code)
        except (TypeError, ValueError):
            error_code = -1
        message = " ".join(
            str(node.get(key, ""))
            for key in ("err_msg", "message", "msg")
        ).lower()
        if error_code == 23084 or "audio duration is zero" in message:
            return "zero-audio"
        if error_code == 50000011 or "exceededconcurrentlimit" in message:
            return "concurrent-limit"
    return None


def _looks_retryable_task_failure(data: dict) -> bool:
    """Return true only for CapCut's narrowly recognized transient failures."""

    return _retryable_task_failure_reason(data) is not None


def _download_url(
    url: str,
    dst_path: str,
    timeout: int,
    *,
    attempts: int = NETWORK_RETRY_ATTEMPTS,
) -> None:
    import requests  # type: ignore

    if isinstance(attempts, bool) or not isinstance(attempts, int):
        raise ValueError("attempts must be an integer")
    if attempts < 1 or attempts > 8:
        raise ValueError("attempts must be between 1 and 8")
    transient_exceptions = (
        requests.exceptions.ConnectionError,
        requests.exceptions.SSLError,
        requests.exceptions.Timeout,
    )
    part_path = f"{dst_path}.{uuid.uuid4().hex}.part"
    for path in (part_path, dst_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    for attempt_index in range(attempts):
        retryable_response = False
        download_complete = False
        try:
            with requests.get(url, stream=True, timeout=timeout) as resp:
                retryable_response = (
                    resp.status_code in TRANSIENT_HTTP_STATUS_CODES
                    or 500 <= resp.status_code < 600
                )
                if not retryable_response or attempt_index + 1 >= attempts:
                    resp.raise_for_status()
                    if not 200 <= resp.status_code < 300:
                        raise requests.exceptions.HTTPError(
                            f"unexpected audio download HTTP status {resp.status_code}",
                            response=resp,
                        )
                    with open(part_path, "wb") as fp:
                        for chunk in resp.iter_content(chunk_size=1024 * 256):
                            if chunk:
                                fp.write(chunk)
                    download_complete = True
            if download_complete:
                os.replace(part_path, dst_path)
                return
        except transient_exceptions:
            try:
                os.remove(part_path)
            except FileNotFoundError:
                pass
            if attempt_index + 1 >= attempts:
                raise
        except Exception:
            try:
                os.remove(part_path)
            except FileNotFoundError:
                pass
            raise
        if not retryable_response and attempt_index + 1 >= attempts:
            raise AssertionError("unreachable CapCut download retry state")
        delay = NETWORK_RETRY_BACKOFF_SECONDS * (2**attempt_index)
        print(
            json.dumps(
                {
                    "stage": "capcut-network-retry",
                    "request": "tts-download",
                    "attempt": attempt_index + 2,
                    "max_attempts": attempts,
                    "delay_seconds": delay,
                }
            ),
            flush=True,
        )
        time.sleep(delay)
    raise AssertionError("unreachable CapCut download retry state")


def _post_with_transient_retries(
    url: str,
    *,
    headers: dict,
    body_text: str,
    label: str,
    timeout: int = 60,
    attempts: int = NETWORK_RETRY_ATTEMPTS,
):
    """POST with bounded retries for transport failures and retryable HTTP.

    CapCut occasionally closes a TLS handshake while a task is being polled.
    Retrying the same signed request preserves the task, voice, resource and
    provider rate.  Semantic/provider errors remain fail-closed and are never
    retried here.
    """

    import requests  # type: ignore

    if isinstance(attempts, bool) or not isinstance(attempts, int):
        raise ValueError("attempts must be an integer")
    if attempts < 1 or attempts > 8:
        raise ValueError("attempts must be between 1 and 8")
    transient_exceptions = (
        requests.exceptions.ConnectionError,
        requests.exceptions.SSLError,
        requests.exceptions.Timeout,
    )
    for attempt_index in range(attempts):
        try:
            response = requests.post(
                url,
                headers=headers,
                data=body_text.encode("utf-8"),
                timeout=timeout,
            )
        except transient_exceptions:
            if attempt_index + 1 >= attempts:
                raise
        else:
            retryable_status = (
                response.status_code in TRANSIENT_HTTP_STATUS_CODES
                or 500 <= response.status_code < 600
            )
            if not retryable_status or attempt_index + 1 >= attempts:
                return response
            response.close()
        delay = NETWORK_RETRY_BACKOFF_SECONDS * (2**attempt_index)
        print(
            json.dumps(
                {
                    "stage": "capcut-network-retry",
                    "request": label,
                    "attempt": attempt_index + 2,
                    "max_attempts": attempts,
                    "delay_seconds": delay,
                }
            ),
            flush=True,
        )
        time.sleep(delay)
    raise AssertionError("unreachable CapCut retry state")


def _synthesize(text: str, dst_mp3: str, *, voice: str, resource_id: str, rate: float,
                device: dict, poll_timeout: int) -> None:
    (url, headers, body_text), bind_id = _tts_new_request(text, voice, resource_id, rate, device)
    resp = _post_with_transient_retries(
        url,
        headers=headers,
        body_text=body_text,
        label="tts-new",
    )
    data = _checked_json_response(resp, "capcut tts-new")
    task_id, token, bind_id = _extract_task_ref(data, bind_id)

    deadline = time.time() + poll_timeout
    last_data = data
    while time.time() < deadline:
        q_url, q_headers, q_body = _tts_query_request(task_id, token, bind_id, device)
        q_resp = _post_with_transient_retries(
            q_url,
            headers=q_headers,
            body_text=q_body,
            label="tts-query",
        )
        last_data = _checked_json_response(q_resp, "capcut tts-query")
        audio_url = _extract_audio_url(last_data)
        if audio_url:
            _download_url(audio_url, dst_mp3, timeout=120)
            return
        if _looks_failed(last_data):
            message = f"CapCut TTS task failed: {str(last_data)[:1200]}"
            retry_reason = _retryable_task_failure_reason(last_data)
            if retry_reason is not None:
                raise RetryableCapCutTaskError(message, reason=retry_reason)
            raise RuntimeError(message)
        time.sleep(2.0)

    raise RuntimeError(f"CapCut TTS timed out waiting for audio URL: {str(last_data)[:1200]}")


def synthesize_once(
    text: str,
    dst_mp3: str,
    *,
    voice: str = DEFAULT_VOICE,
    resource_id: str = DEFAULT_RESOURCE_ID,
    device: dict | None = None,
    device_json_path: str | None = None,
    rate: float = 1.0,
    poll_timeout: int = 300,
) -> float:
    """Synthesize one TTS clip and return its duration."""
    if not text.strip():
        raise ValueError("text empty - cannot synthesize CapCut TTS.")
    device = device or make_device(device_json_path)
    for attempt_index in range(TASK_RETRY_ATTEMPTS):
        try:
            _synthesize(
                text,
                dst_mp3,
                voice=voice,
                resource_id=resource_id,
                rate=rate,
                device=device,
                poll_timeout=poll_timeout,
            )
            break
        except RetryableCapCutTaskError as exc:
            if attempt_index + 1 >= TASK_RETRY_ATTEMPTS:
                raise
            base_delay = (
                CONCURRENT_LIMIT_RETRY_BACKOFF_SECONDS
                if exc.reason == "concurrent-limit"
                else TASK_RETRY_BACKOFF_SECONDS
            )
            delay = base_delay * (2**attempt_index)
            print(
                json.dumps(
                    {
                        "stage": "capcut-task-retry",
                        "reason": exc.reason,
                        "attempt": attempt_index + 2,
                        "max_attempts": TASK_RETRY_ATTEMPTS,
                        "delay_seconds": delay,
                    }
                ),
                flush=True,
            )
            time.sleep(delay)
    return duration_seconds(dst_mp3)


def synthesize_to_fit(
    text: str,
    dst_mp3: str,
    *,
    target_seconds: float,
    voice: str = DEFAULT_VOICE,
    resource_id: str = DEFAULT_RESOURCE_ID,
    device_json_path: str | None = None,
    base_rate: float = 1.0,
    max_rate: float = 1.8,
    poll_timeout: int = 300,
) -> float:
    """Synthesize CapCut TTS and speed it up only if it exceeds target_seconds."""
    if not text.strip():
        raise ValueError("script_vi empty - cannot synthesize CapCut TTS.")

    device = _load_device(device_json_path)
    _synthesize(
        text,
        dst_mp3,
        voice=voice,
        resource_id=resource_id,
        rate=base_rate,
        device=device,
        poll_timeout=poll_timeout,
    )
    dur = duration_seconds(dst_mp3)
    if dur <= 0 or target_seconds <= 0:
        return dur

    if dur > target_seconds:
        rate = min(max_rate, base_rate * (dur / target_seconds))
        _synthesize(
            text,
            dst_mp3,
            voice=voice,
            resource_id=resource_id,
            rate=rate,
            device=device,
            poll_timeout=poll_timeout,
        )
        return duration_seconds(dst_mp3)
    return dur
