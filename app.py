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


async def create_speech(text, voice, rate, volume, output_path):
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=rate,
        volume=volume
    )
    await communicate.save(output_path)


@app.route("/tts", methods=["POST"])
def tts():
    data = request.get_json(silent=True) or {}

    text = str(data.get("text", "")).strip()
    voice = data.get("voice", "zh-TW-YunJheNeural")
    rate = data.get("rate", "-5%")
    volume = data.get("volume", "+0%")

    if not text:
        return jsonify({"error": "text is required"}), 400

    filename = f"tts_{uuid.uuid4().hex}.mp3"
    output_path = os.path.join(tempfile.gettempdir(), filename)

    try:
        asyncio.run(
            create_speech(
                text,
                voice,
                rate,
                volume,
                output_path
            )
        )

        return send_file(
            output_path,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name="voice.mp3"
        )

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 500
