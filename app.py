from flask import Flask, request, jsonify, send_file
import edge_tts
import asyncio
import tempfile
import os
import uuid

app = Flask(__name__)


@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "edge-tts-api"
    })


async def create_speech(
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
        volume=volume
    )

    await communicate.save(output_path)


@app.route("/tts", methods=["POST"])
def tts():
    data = request.get_json(silent=True) or {}

    text = str(
        data.get("text", "")
    ).strip()

    voice = str(
        data.get(
            "voice",
            "zh-TW-YunJheNeural"
        )
    ).strip()

    rate = str(
        data.get(
            "rate",
            "-12%"
        )
    ).strip()

    pitch = str(
        data.get(
            "pitch",
            "-7Hz"
        )
    ).strip()

    volume = str(
        data.get(
            "volume",
            "+0%"
        )
    ).strip()

    if not text:
        return jsonify({
            "error": "text is required"
        }), 400

    # --------------------------------------------------
    # 基本格式防呆
    # --------------------------------------------------

    if not rate:
        rate = "-12%"

    if not pitch:
        pitch = "-7Hz"

    if not volume:
        volume = "+0%"

    filename = (
        f"tts_{uuid.uuid4().hex}.mp3"
    )

    output_path = os.path.join(
        tempfile.gettempdir(),
        filename
    )

    try:
        asyncio.run(
            create_speech(
                text=text,
                voice=voice,
                rate=rate,
                pitch=pitch,
                volume=volume,
                output_path=output_path
            )
        )

        if not os.path.exists(output_path):
            return jsonify({
                "error": "audio file was not generated"
            }), 500

        if os.path.getsize(output_path) == 0:
            return jsonify({
                "error": "generated audio file is empty"
            }), 500

        return send_file(
            output_path,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name="voice.mp3"
        )

    except Exception as e:
        return jsonify({
            "error": str(e),
            "voice": voice,
            "rate": rate,
            "pitch": pitch,
            "volume": volume
        }), 500
