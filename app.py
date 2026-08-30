from flask import (
    Flask,
    request,
    jsonify,
    send_file,
    after_this_request,
)

import edge_tts
import asyncio
import tempfile
import os
import uuid
import time
import re


# ============================================================
# Edge TTS API v2
#
# 專門供 n8n 使用
#
# Endpoints:
# GET  /
# GET  /health
# POST /tts
#
# Features:
# - Render cold-start health check
# - Edge TTS retry
# - Per-attempt timeout
# - Input validation
# - Temporary file cleanup
# - Clear JSON errors
# ============================================================


app = Flask(__name__)


# ============================================================
# CONFIG
# ============================================================

SERVICE_NAME = "edge-tts-api"
SERVICE_VERSION = "2.0.0"

DEFAULT_VOICE = "zh-TW-YunJheNeural"
DEFAULT_RATE = "-6%"
DEFAULT_PITCH = "-6Hz"
DEFAULT_VOLUME = "+0%"

# 單一 TTS Part 最大字數
MAX_TEXT_LENGTH = 3000

# Edge TTS 本身重試次數
MAX_TTS_ATTEMPTS = 3

# 每次產生語音允許時間
TTS_TIMEOUT_SECONDS = 120

# 重試間隔
RETRY_DELAYS = [
    2,
    5,
    10,
]


# ============================================================
# HELPERS
# ============================================================

def clean_text(value):
    if value is None:
        return ""

    return str(value).strip()


def safe_remove(path):
    if not path:
        return

    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def normalize_rate(value):
    value = clean_text(value)

    if not value:
        return DEFAULT_RATE

    if not re.fullmatch(
        r"[+-]\d+%",
        value
    ):
        return DEFAULT_RATE

    return value


def normalize_pitch(value):
    value = clean_text(value)

    if not value:
        return DEFAULT_PITCH

    if not re.fullmatch(
        r"[+-]\d+Hz",
        value,
        flags=re.IGNORECASE,
    ):
        return DEFAULT_PITCH

    return value


def normalize_volume(value):
    value = clean_text(value)

    if not value:
        return DEFAULT_VOLUME

    if not re.fullmatch(
        r"[+-]\d+%",
        value
    ):
        return DEFAULT_VOLUME

    return value


# ============================================================
# HEALTH
# ============================================================

def health_payload():
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "tts_provider": "edge-tts",
        "default_voice": DEFAULT_VOICE,
        "max_text_length": MAX_TEXT_LENGTH,
        "max_tts_attempts": MAX_TTS_ATTEMPTS,
        "tts_timeout_seconds": TTS_TIMEOUT_SECONDS,
    }


@app.route(
    "/",
    methods=["GET"]
)
def root():
    return jsonify(
        health_payload()
    )


@app.route(
    "/health",
    methods=["GET"]
)
def health():
    return jsonify(
        health_payload()
    )


# ============================================================
# ASYNC TTS
# ============================================================

async def create_speech_once(
    text,
    voice,
    rate,
    pitch,
    volume,
    output_path
):
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=rate,
        pitch=pitch,
        volume=volume,
    )

    await communicate.save(
        output_path
    )


async def create_speech_with_timeout(
    text,
    voice,
    rate,
    pitch,
    volume,
    output_path
):
    await asyncio.wait_for(
        create_speech_once(
            text=text,
            voice=voice,
            rate=rate,
            pitch=pitch,
            volume=volume,
            output_path=output_path,
        ),
        timeout=TTS_TIMEOUT_SECONDS,
    )


# ============================================================
# TTS RETRY
# ============================================================

def generate_tts_with_retry(
    text,
    voice,
    rate,
    pitch,
    volume,
    output_path
):
    last_error = None

    for attempt in range(
        1,
        MAX_TTS_ATTEMPTS + 1
    ):
        # 每次重試前移除不完整檔案
        safe_remove(
            output_path
        )

        try:
            asyncio.run(
                create_speech_with_timeout(
                    text=text,
                    voice=voice,
                    rate=rate,
                    pitch=pitch,
                    volume=volume,
                    output_path=output_path,
                )
            )

            # --------------------------------------------
            # 驗證檔案
            # --------------------------------------------

            if not os.path.exists(
                output_path
            ):
                raise RuntimeError(
                    "audio file was not generated"
                )

            file_size = os.path.getsize(
                output_path
            )

            if file_size <= 100:
                raise RuntimeError(
                    f"generated audio file is too small: {file_size} bytes"
                )

            return {
                "success": True,
                "attempt": attempt,
                "file_size": file_size,
            }

        except asyncio.TimeoutError:
            last_error = (
                f"TTS generation timeout after "
                f"{TTS_TIMEOUT_SECONDS} seconds"
            )

        except Exception as error:
            last_error = clean_text(
                error
            )

        # --------------------------------------------
        # Retry delay
        # --------------------------------------------

        if attempt < MAX_TTS_ATTEMPTS:
            delay_index = min(
                attempt - 1,
                len(RETRY_DELAYS) - 1,
            )

            time.sleep(
                RETRY_DELAYS[
                    delay_index
                ]
            )

    return {
        "success": False,
        "attempt": MAX_TTS_ATTEMPTS,
        "error": last_error or "unknown TTS error",
    }


# ============================================================
# POST /tts
# ============================================================

@app.route(
    "/tts",
    methods=["POST"]
)
def tts():

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

    data = request.get_json(
        silent=True
    ) or {}


    # --------------------------------------------------------
    # Input
    # --------------------------------------------------------

    text = clean_text(
        data.get(
            "text",
            ""
        )
    )

    voice = clean_text(
        data.get(
            "voice",
            DEFAULT_VOICE
        )
    )

    rate = normalize_rate(
        data.get(
            "rate",
            DEFAULT_RATE
        )
    )

    pitch = normalize_pitch(
        data.get(
            "pitch",
            DEFAULT_PITCH
        )
    )

    volume = normalize_volume(
        data.get(
            "volume",
            DEFAULT_VOLUME
        )
    )


    # --------------------------------------------------------
    # Validation
    # --------------------------------------------------------

    if not text:
        return jsonify({
            "status": "error",
            "error": "text is required",
        }), 400


    if len(text) > MAX_TEXT_LENGTH:
        return jsonify({
            "status": "error",
            "error": "text is too long",
            "char_count": len(text),
            "max_text_length": MAX_TEXT_LENGTH,
        }), 400


    if not voice:
        voice = DEFAULT_VOICE


    # --------------------------------------------------------
    # Temporary file
    # --------------------------------------------------------

    filename = (
        f"tts_{uuid.uuid4().hex}.mp3"
    )

    output_path = os.path.join(
        tempfile.gettempdir(),
        filename
    )


    # --------------------------------------------------------
    # Generate
    # --------------------------------------------------------

    result = generate_tts_with_retry(
        text=text,
        voice=voice,
        rate=rate,
        pitch=pitch,
        volume=volume,
        output_path=output_path,
    )


    # --------------------------------------------------------
    # Failed
    # --------------------------------------------------------

    if not result.get(
        "success"
    ):
        safe_remove(
            output_path
        )

        return jsonify({
            "status": "error",
            "service": SERVICE_NAME,
            "error": result.get(
                "error"
            ),
            "attempts": result.get(
                "attempt"
            ),
            "char_count": len(text),
            "voice": voice,
            "rate": rate,
            "pitch": pitch,
            "volume": volume,
        }), 503


    # --------------------------------------------------------
    # Cleanup after response
    # --------------------------------------------------------

    @after_this_request
    def cleanup(
        response
    ):
        safe_remove(
            output_path
        )

        return response


    # --------------------------------------------------------
    # Return MP3
    # --------------------------------------------------------

    response = send_file(
        output_path,
        mimetype="audio/mpeg",
        as_attachment=True,
        download_name="voice.mp3",
    )

    # Debug headers
    response.headers[
        "X-TTS-Service"
    ] = SERVICE_NAME

    response.headers[
        "X-TTS-Version"
    ] = SERVICE_VERSION

    response.headers[
        "X-TTS-Attempt"
    ] = str(
        result.get(
            "attempt",
            1
        )
    )

    response.headers[
        "X-TTS-Characters"
    ] = str(
        len(text)
    )

    return response


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(404)
def not_found(
    error
):
    return jsonify({
        "status": "error",
        "error": "endpoint not found",
        "available_endpoints": [
            "GET /",
            "GET /health",
            "POST /tts",
        ],
    }), 404


@app.errorhandler(405)
def method_not_allowed(
    error
):
    return jsonify({
        "status": "error",
        "error": "method not allowed",
    }), 405


@app.errorhandler(500)
def internal_error(
    error
):
    return jsonify({
        "status": "error",
        "error": "internal server error",
    }), 500


# ============================================================
# LOCAL / DIRECT START
# ============================================================

if __name__ == "__main__":
    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
