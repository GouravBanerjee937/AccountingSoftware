"""OCR a local image via the hosted PP-OCRv5 demo on Hugging Face Spaces.

This calls the public Gradio Space `PaddlePaddle/PP-OCRv5_Online_Demo`. It is a
free, shared demo — expect cold-start delays and occasional unavailability. For
production you'd self-host PaddleOCR instead.
"""

from gradio_client import Client, handle_file

SPACE = "PaddlePaddle/PP-OCRv5_Online_Demo"


def ocr_file(path):
    """Run OCR on the file at `path` and return the recognized text (lines
    joined by newlines). Raises on failure so the caller can report it.

    A fresh Client per call keeps each request's Gradio session isolated (the
    server is multi-threaded), and download_files=False avoids fetching the
    annotated result images (their CDN 403s) — we only want the text JSON.
    """
    client = Client(SPACE, verbose=False, download_files=False,
                    httpx_kwargs={"timeout": 180})

    # Step 1: run OCR. The demo wants the file as both the document and the
    # image input; the remaining args mirror the demo's default settings.
    client.predict(
        handle_file(path),   # file_path  (Upload document)
        handle_file(path),   # image_input
        False, False, False,  # orientation/unwarp/textline toggles
        "min", 736, 0.3, 0.6, 1.5, 0.0,  # detection/recognition thresholds
        api_name="/process_file",
    )

    # Step 2: read the results the demo just produced. Index 10 is the JSON
    # component holding the structured OCR output.
    out = client.predict(api_name="/update_display")
    payload = out[10] if isinstance(out, (list, tuple)) and len(out) > 10 else {}
    value = payload.get("value", {}) if isinstance(payload, dict) else {}

    lines = []
    for result in value.get("ocrResults", []):
        lines.extend(result.get("prunedResult", {}).get("rec_texts", []))
    return "\n".join(lines)
