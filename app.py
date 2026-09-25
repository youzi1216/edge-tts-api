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
# Edge TTS API v3
#
# 專門供 n8n / Midnight Files 使用
#
# Endpoints:
# GET  /
# GET  /health
# POST /tts
#
# Modes:
# - documentary_dynamic
# - standard
#
# documentary_dynamic:
# - 自動分句
# - 一般敘述
# - 問句
# - 轉折
# - 重要揭露
# - 短句強調
# - 結尾懸念
# - 動態 rate / pitch
#
# Features:
# - Render cold-start health check
# - Edge TTS retry
# - Overall timeout
# - Input validation
# - Temporary file cleanup
# - Clear JSON errors
# - Backward compatible
# ============================================================


app = Flask(__name__)


# ============================================================
# CONFIG
# ============================================================

SERVICE_NAME = "edge-tts-api"
SERVICE_VERSION = "3.0.0-documentary-dynamic"

DEFAULT_VOICE = "zh-TW-YunJheNeural"

# ------------------------------------------------------------
# 注意：
# Dynamic mode 會再依內容改變這些基準值
# 所以基準不需要設得太誇張
# ------------------------------------------------------------

DEFAULT_RATE = "+1%"
DEFAULT_PITCH = "+0Hz"
DEFAULT_VOLUME = "+0%"

DEFAULT_MODE = "documentary_dynamic"

AVAILABLE_MODES = {
    "standard",
    "documentary_dynamic",
}


# 單一 n8n TTS Part 最大字數
MAX_TEXT_LENGTH = 3000

# Dynamic 模式最大語音段數
MAX_DYNAMIC_SEGMENTS = 28

# 每一段盡量控制在這個範圍附近
TARGET_SEGMENT_CHARS = 120

# 過短文字會盡量與前後合併
MIN_SEGMENT_CHARS = 18

# 過長一定再拆
MAX_SEGMENT_CHARS = 220


# Edge TTS 整體重試
MAX_TTS_ATTEMPTS = 3

# 一次完整 Part 的最大允許處理時間
TTS_TIMEOUT_SECONDS = 240

RETRY_DELAYS = [
    2,
    5,
    10,
]


# ============================================================
# DYNAMIC PROSODY CONFIG
# ============================================================

# 這些不是絕對 rate / pitch，
# 而是「加在使用者傳入基準值上」的偏移量。

PROSODY_PROFILES = {

    # 一般敘述
    "normal": {
        "rate_delta": 0,
        "pitch_delta": 0,
    },

    # 說明資料 / 背景
    "explanation": {
        "rate_delta": -1,
        "pitch_delta": 0,
    },

    # 問句
    "question": {
        "rate_delta": -1,
        "pitch_delta": 3,
    },

    # 轉折
    "contrast": {
        "rate_delta": -3,
        "pitch_delta": -1,
    },

    # 重大發現
    "reveal": {
        "rate_delta": -5,
        "pitch_delta": -2,
    },

    # 短句強調
    "impact": {
        "rate_delta": -2,
        "pitch_delta": 2,
    },

    # 緊張升高
    "tension": {
        "rate_delta": 3,
        "pitch_delta": 1,
    },

    # 收尾 / 懸念
    "ending": {
        "rate_delta": -6,
        "pitch_delta": -3,
    },
}


# ============================================================
# KEYWORDS
# ============================================================

CONTRAST_KEYWORDS = (
    "但",
    "但是",
    "然而",
    "可是",
    "不過",
    "問題是",
    "真正的問題",
    "奇怪的是",
    "關鍵是",
    "真正關鍵",
)


REVEAL_KEYWORDS = (
    "直到",
    "最後",
    "結果",
    "真正",
    "發現",
    "找到",
    "證實",
    "確認",
    "消失",
    "不見了",
    "沒有人",
    "沒有回來",
    "死亡",
    "遺體",
    "破口",
    "割開",
    "只剩",
    "竟然",
)


TENSION_KEYWORDS = (
    "突然",
    "立刻",
    "開始",
    "再次",
    "越來越",
    "緊接著",
    "就在這時",
    "下一秒",
    "同一時間",
)


EXPLANATION_KEYWORDS = (
    "根據",
    "資料顯示",
    "紀錄顯示",
    "研究指出",
    "調查顯示",
    "換句話說",
    "也就是說",
    "原因是",
    "這表示",
    "當時",
)


ENDING_KEYWORDS = (
    "至今",
    "直到今天",
    "仍然沒有答案",
    "仍然未知",
    "沒有人知道",
    "也許",
    "究竟",
    "真相",
    "最後留下",
    "唯一留下",
)


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


def clamp(value, minimum, maximum):
    return max(
        minimum,
        min(
            maximum,
            value,
        ),
    )


# ============================================================
# NORMALIZERS
# ============================================================

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


def normalize_mode(value):
    value = clean_text(
        value
    ).lower()

    if value in AVAILABLE_MODES:
        return value

    return DEFAULT_MODE


# ============================================================
# PROSODY PARSERS
# ============================================================

def parse_rate(value):
    value = normalize_rate(
        value
    )

    match = re.fullmatch(
        r"([+-])(\d+)%",
        value
    )

    if not match:
        return 0

    number = int(
        match.group(2)
    )

    if match.group(1) == "-":
        number = -number

    return number


def parse_pitch(value):
    value = normalize_pitch(
        value
    )

    match = re.fullmatch(
        r"([+-])(\d+)Hz",
        value,
        flags=re.IGNORECASE,
    )

    if not match:
        return 0

    number = int(
        match.group(2)
    )

    if match.group(1) == "-":
        number = -number

    return number


def format_rate(value):
    value = int(
        round(value)
    )

    value = clamp(
        value,
        -25,
        25,
    )

    if value >= 0:
        return f"+{value}%"

    return f"{value}%"


def format_pitch(value):
    value = int(
        round(value)
    )

    value = clamp(
        value,
        -12,
        12,
    )

    if value >= 0:
        return f"+{value}Hz"

    return f"{value}Hz"


# ============================================================
# SENTENCE SPLITTING
# ============================================================

def split_sentences(text):

    text = clean_text(
        text
    )

    if not text:
        return []

    # 清理過量換行
    text = re.sub(
        r"\r\n?",
        "\n",
        text
    )

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    # 以中文句號 / 問號 / 驚嘆號 / 分號 /
    # 段落換行作為主要邊界
    parts = re.split(
        r"(?<=[。！？!?；;])\s*|\n+",
        text
    )

    result = []

    for part in parts:

        part = clean_text(
            part
        )

        if not part:
            continue

        result.append(
            part
        )

    return result


# ============================================================
# LONG SENTENCE SPLITTING
# ============================================================

def split_long_sentence(sentence):

    sentence = clean_text(
        sentence
    )

    if len(sentence) <= MAX_SEGMENT_CHARS:
        return [sentence]

    pieces = re.split(
        r"(?<=[，、：:])",
        sentence
    )

    output = []

    current = ""

    for piece in pieces:

        piece = clean_text(
            piece
        )

        if not piece:
            continue

        candidate = (
            current + piece
        )

        if (
            current
            and
            len(candidate) >
            TARGET_SEGMENT_CHARS
        ):
            output.append(
                current
            )

            current = piece

        else:
            current = candidate

    if current:
        output.append(
            current
        )

    # 如果還是過長，再硬切
    final_output = []

    for item in output:

        if len(item) <= MAX_SEGMENT_CHARS:
            final_output.append(
                item
            )

            continue

        start = 0

        while start < len(item):

            end = min(
                len(item),
                start + MAX_SEGMENT_CHARS,
            )

            final_output.append(
                item[start:end]
            )

            start = end

    return final_output


# ============================================================
# BUILD SEGMENTS
# ============================================================

def build_dynamic_segments(text):

    sentences = split_sentences(
        text
    )

    expanded = []

    for sentence in sentences:
        expanded.extend(
            split_long_sentence(
                sentence
            )
        )

    if not expanded:
        return []


    # --------------------------------------------------------
    # 合併過短句子
    # --------------------------------------------------------

    merged = []

    current = ""

    for sentence in expanded:

        # 問句、短促揭露句保留獨立
        preserve_short = (
            sentence.endswith("?")
            or
            sentence.endswith("？")
            or
            detect_raw_impact(
                sentence
            )
        )

        if preserve_short:

            if current:
                merged.append(
                    current
                )

                current = ""

            merged.append(
                sentence
            )

            continue


        if not current:
            current = sentence

            continue


        candidate = (
            current
            + sentence
        )


        if (
            len(current) <
            MIN_SEGMENT_CHARS
            or
            len(candidate) <=
            TARGET_SEGMENT_CHARS
        ):

            current = candidate

        else:

            merged.append(
                current
            )

            current = sentence


    if current:
        merged.append(
            current
        )


    # --------------------------------------------------------
    # 避免 segment 過多
    # --------------------------------------------------------

    while (
        len(merged) >
        MAX_DYNAMIC_SEGMENTS
    ):

        new_list = []

        index = 0

        while index < len(merged):

            if (
                index + 1 <
                len(merged)
            ):

                joined = (
                    merged[index]
                    +
                    merged[index + 1]
                )

                if (
                    len(joined) <=
                    MAX_SEGMENT_CHARS
                ):
                    new_list.append(
                        joined
                    )

                    index += 2

                    continue

            new_list.append(
                merged[index]
            )

            index += 1

        if len(new_list) == len(merged):
            break

        merged = new_list


    return merged


# ============================================================
# PROSODY CLASSIFICATION
# ============================================================

def contains_any(
    text,
    keywords
):
    return any(
        keyword in text
        for keyword in keywords
    )


def detect_raw_impact(text):

    cleaned = re.sub(
        r"[，。！？!?、：:；;「」『』\"'\s]",
        "",
        text
    )

    length = len(
        cleaned
    )

    if length <= 12:
        return True

    return False


def classify_segment(
    text,
    index,
    total
):

    text = clean_text(
        text
    )

    if not text:
        return "normal"


    # --------------------------------------------------------
    # 最後一段優先視為 Ending
    # --------------------------------------------------------

    if (
        index ==
        total - 1
    ):

        return "ending"


    # --------------------------------------------------------
    # 問句
    # --------------------------------------------------------

    if (
        text.endswith("？")
        or
        text.endswith("?")
    ):
        return "question"


    # --------------------------------------------------------
    # 重大揭露
    # --------------------------------------------------------

    if contains_any(
        text,
        REVEAL_KEYWORDS
    ):

        if (
            len(text) <= 90
        ):
            return "reveal"


    # --------------------------------------------------------
    # 轉折
    # --------------------------------------------------------

    if contains_any(
        text,
        CONTRAST_KEYWORDS
    ):
        return "contrast"


    # --------------------------------------------------------
    # 短句
    # --------------------------------------------------------

    if detect_raw_impact(
        text
    ):
        return "impact"


    # --------------------------------------------------------
    # 緊張升高
    # --------------------------------------------------------

    if contains_any(
        text,
        TENSION_KEYWORDS
    ):
        return "tension"


    # --------------------------------------------------------
    # 解說
    # --------------------------------------------------------

    if contains_any(
        text,
        EXPLANATION_KEYWORDS
    ):
        return "explanation"


    # --------------------------------------------------------
    # 結尾語意
    # --------------------------------------------------------

    if (
        index >=
        total - 3
        and
        contains_any(
            text,
            ENDING_KEYWORDS
        )
    ):
        return "ending"


    return "normal"


# ============================================================
# BUILD PROSODY
# ============================================================

def build_segment_prosody(
    base_rate,
    base_pitch,
    profile_name
):

    base_rate_value = parse_rate(
        base_rate
    )

    base_pitch_value = parse_pitch(
        base_pitch
    )


    profile = PROSODY_PROFILES.get(
        profile_name,
        PROSODY_PROFILES[
            "normal"
        ],
    )


    rate_value = (
        base_rate_value
        +
        profile.get(
            "rate_delta",
            0
        )
    )


    pitch_value = (
        base_pitch_value
        +
        profile.get(
            "pitch_delta",
            0
        )
    )


    return (
        format_rate(
            rate_value
        ),
        format_pitch(
            pitch_value
        ),
    )


# ============================================================
# HEALTH
# ============================================================

def health_payload():

    return {

        "status":
            "ok",

        "service":
            SERVICE_NAME,

        "version":
            SERVICE_VERSION,

        "tts_provider":
            "edge-tts",

        "default_voice":
            DEFAULT_VOICE,

        "default_mode":
            DEFAULT_MODE,

        "available_modes":
            sorted(
                AVAILABLE_MODES
            ),

        "dynamic_prosody":
            True,

        "dynamic_profiles":
            sorted(
                PROSODY_PROFILES.keys()
            ),

        "max_dynamic_segments":
            MAX_DYNAMIC_SEGMENTS,

        "max_text_length":
            MAX_TEXT_LENGTH,

        "max_tts_attempts":
            MAX_TTS_ATTEMPTS,

        "tts_timeout_seconds":
            TTS_TIMEOUT_SECONDS,
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
# STANDARD TTS
# ============================================================

async def create_standard_speech(
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


# ============================================================
# DYNAMIC TTS
# ============================================================

async def create_dynamic_speech(
    text,
    voice,
    base_rate,
    base_pitch,
    volume,
    output_path
):

    segments = build_dynamic_segments(
        text
    )

    if not segments:
        raise RuntimeError(
            "no valid dynamic TTS segments generated"
        )


    with open(
        output_path,
        "wb"
    ) as output_file:

        for index, segment in enumerate(
            segments
        ):

            profile = classify_segment(
                text=segment,
                index=index,
                total=len(segments),
            )


            segment_rate, segment_pitch = (
                build_segment_prosody(
                    base_rate=base_rate,
                    base_pitch=base_pitch,
                    profile_name=profile,
                )
            )


            communicate = edge_tts.Communicate(
                text=segment,
                voice=voice,
                rate=segment_rate,
                pitch=segment_pitch,
                volume=volume,
            )


            audio_received = False


            async for chunk in communicate.stream():

                if (
                    chunk.get(
                        "type"
                    )
                    !=
                    "audio"
                ):
                    continue


                data = chunk.get(
                    "data"
                )


                if not data:
                    continue


                output_file.write(
                    data
                )


                audio_received = True


            if not audio_received:

                raise RuntimeError(
                    f"segment {index + 1} returned no audio"
                )


    return {
        "segments":
            len(segments),
    }


# ============================================================
# ASYNC WRAPPER
# ============================================================

async def create_speech_once(
    text,
    voice,
    rate,
    pitch,
    volume,
    mode,
    output_path
):

    if (
        mode ==
        "documentary_dynamic"
    ):

        return await create_dynamic_speech(
            text=text,
            voice=voice,
            base_rate=rate,
            base_pitch=pitch,
            volume=volume,
            output_path=output_path,
        )


    await create_standard_speech(
        text=text,
        voice=voice,
        rate=rate,
        pitch=pitch,
        volume=volume,
        output_path=output_path,
    )


    return {
        "segments":
            1,
    }


async def create_speech_with_timeout(
    text,
    voice,
    rate,
    pitch,
    volume,
    mode,
    output_path
):

    return await asyncio.wait_for(

        create_speech_once(
            text=text,
            voice=voice,
            rate=rate,
            pitch=pitch,
            volume=volume,
            mode=mode,
            output_path=output_path,
        ),

        timeout=
            TTS_TIMEOUT_SECONDS,
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
    mode,
    output_path
):

    last_error = None


    for attempt in range(
        1,
        MAX_TTS_ATTEMPTS + 1
    ):

        safe_remove(
            output_path
        )


        try:

            meta = asyncio.run(

                create_speech_with_timeout(
                    text=text,
                    voice=voice,
                    rate=rate,
                    pitch=pitch,
                    volume=volume,
                    mode=mode,
                    output_path=output_path,
                )
            )


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

                "success":
                    True,

                "attempt":
                    attempt,

                "file_size":
                    file_size,

                "segments":
                    int(
                        meta.get(
                            "segments",
                            1
                        )
                    ),
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


        if (
            attempt <
            MAX_TTS_ATTEMPTS
        ):

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

        "success":
            False,

        "attempt":
            MAX_TTS_ATTEMPTS,

        "error":
            last_error
            or
            "unknown TTS error",
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


    mode = normalize_mode(
        data.get(
            "mode",
            DEFAULT_MODE
        )
    )


    # --------------------------------------------------------
    # Validation
    # --------------------------------------------------------

    if not text:

        return jsonify({

            "status":
                "error",

            "error":
                "text is required",

        }), 400


    if (
        len(text) >
        MAX_TEXT_LENGTH
    ):

        return jsonify({

            "status":
                "error",

            "error":
                "text is too long",

            "char_count":
                len(text),

            "max_text_length":
                MAX_TEXT_LENGTH,

        }), 400


    if not voice:

        voice = DEFAULT_VOICE


    # --------------------------------------------------------
    # Temporary file
    # --------------------------------------------------------

    filename = (
        f"tts_"
        f"{uuid.uuid4().hex}"
        f".mp3"
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

        mode=mode,

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

            "status":
                "error",

            "service":
                SERVICE_NAME,

            "version":
                SERVICE_VERSION,

            "error":
                result.get(
                    "error"
                ),

            "attempts":
                result.get(
                    "attempt"
                ),

            "char_count":
                len(text),

            "voice":
                voice,

            "rate":
                rate,

            "pitch":
                pitch,

            "volume":
                volume,

            "mode":
                mode,

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

        mimetype=
            "audio/mpeg",

        as_attachment=
            True,

        download_name=
            "voice.mp3",
    )


    # --------------------------------------------------------
    # Debug headers
    # --------------------------------------------------------

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


    response.headers[
        "X-TTS-Mode"
    ] = mode


    response.headers[
        "X-TTS-Segments"
    ] = str(
        result.get(
            "segments",
            1
        )
    )


    response.headers[
        "X-TTS-Base-Rate"
    ] = rate


    response.headers[
        "X-TTS-Base-Pitch"
    ] = pitch


    return response


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(404)
def not_found(
    error
):

    return jsonify({

        "status":
            "error",

        "error":
            "endpoint not found",

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

        "status":
            "error",

        "error":
            "method not allowed",

    }), 405


@app.errorhandler(500)
def internal_error(
    error
):

    return jsonify({

        "status":
            "error",

        "error":
            "internal server error",

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
        host=
            "0.0.0.0",

        port=
            port,

        debug=
            False,
    )
