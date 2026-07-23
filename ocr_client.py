"""OCR + document-type classification via an OpenAI vision model.

NOTE: Currently NOT wired into the app — the document-read + classification step
was moved to the Qwen VLM (see qwen_vlm.py). This module is kept intact (not
removed) as a working fallback / for possible future use; import it to re-enable
the OpenAI-vision path.


Uses the OpenAI Chat Completions API with the app's existing OPENAI_API_KEY.
Accepts images and PDFs directly (base64 inline — no local PDF rendering needed).
Returns (ocr_text, doc_type) where doc_type is one of the app's business
categories, or '' if the model couldn't confidently classify it.

Model is overridable via OCR_VISION_MODEL (default: gpt-4.1).
"""

import base64
import json
import os

# Best available vision model by default; override with OCR_VISION_MODEL.
VISION_MODEL = os.environ.get('OCR_VISION_MODEL', 'gpt-4.1')

# Must match the app's business document categories (server.DOC_TYPES).
DOC_TYPES = ('Sales Invoice', 'Purchase Invoice', 'Sales Return',
             'Purchase Return', 'Receipt', 'Payment')

_IMAGE_MEDIA = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.webp': 'image/webp', '.gif': 'image/gif', '.bmp': 'image/bmp',
    '.tif': 'image/tiff', '.tiff': 'image/tiff',
}

def _build_prompt(companies):
    """OCR + classification prompt. The direction (Sales vs Purchase, Receipt vs
    Payment) is decided by matching OUR COMPANY names against the parties."""
    if companies:
        names = "; ".join(companies)
        direction = (
            f'OUR COMPANY is the business using this app. Its name may appear as any of: {names}.\n'
            "Match our names case-insensitively and loosely (ignore Pvt/Ltd/LLP and punctuation).\n"
            "  - If OUR company is the seller / supplier / issuer / payee -> Sales side "
            "(Sales Invoice, Sales Return, Receipt).\n"
            "  - If OUR company is the buyer / recipient / payer -> Purchase side "
            "(Purchase Invoice, Purchase Return, Payment).\n"
            '  - If OUR company matches NEITHER party, or BOTH, or the direction is unclear, '
            'set doc_type to "" (empty string). Do NOT guess the direction.\n'
        )
    else:
        # No company names configured -> we cannot tell Sales from Purchase, so
        # don't guess: return an empty doc_type and let the user pick.
        direction = (
            'No company names are configured, so the Sales-vs-Purchase direction cannot be '
            'determined. Set doc_type to "" (empty string).\n'
        )
    return (
        "You are an OCR + document-classification engine for an Indian GST accounting app.\n"
        "Do two things with the attached document:\n"
        "1. OCR: transcribe ALL text exactly as printed, preserving the reading/line order.\n"
        "2. CLASSIFY the document as EXACTLY ONE of these types: " + ", ".join(DOC_TYPES) + ".\n"
        "   - Sales Invoice / Purchase Invoice: a tax invoice / bill for goods or services.\n"
        "   - Sales Return / Purchase Return: a credit/debit note reversing an invoice.\n"
        "   - Receipt: money received (payment received, advance, receipt voucher).\n"
        "   - Payment: money paid out (payment advice/voucher).\n"
        + direction +
        'Return ONLY a JSON object of the form '
        '{"ocr_text": "<all the text>", "doc_type": "<one value from the list above, or empty>"}. '
        "No markdown, no code fences, no commentary."
    )


def _client():
    from openai import OpenAI
    key = os.environ.get('OPENAI_API_KEY')
    if not key:
        raise RuntimeError('OPENAI_API_KEY is not set')
    return OpenAI(api_key=key)


def _content_block(path):
    """Build the OpenAI content block for a file (image or PDF)."""
    ext = os.path.splitext(path)[1].lower()
    with open(path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()
    if ext == '.pdf':
        return {"type": "file", "file": {
            "filename": os.path.basename(path),
            "file_data": f"data:application/pdf;base64,{b64}",
        }}
    media = _IMAGE_MEDIA.get(ext)
    if not media:
        raise RuntimeError(f'unsupported file type for OCR: {ext}')
    return {"type": "image_url", "image_url": {"url": f"data:{media};base64,{b64}"}}


def ocr_and_classify(path, companies=None):
    """OCR the document and classify its type. Returns (text, doc_type).
    `companies` is the list of OUR company names, used to decide the Sales vs
    Purchase direction; without it the direction stays unset ("")."""
    client = _client()
    resp = client.chat.completions.create(
        model=VISION_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": [
            {"type": "text", "text": _build_prompt(companies or [])},
            _content_block(path),
        ]}],
    )
    raw = (resp.choices[0].message.content or "").strip()
    try:
        obj = json.loads(raw)
    except Exception:
        obj = {}
    text = str(obj.get("ocr_text", "") or "")
    doc_type = str(obj.get("doc_type", "") or "").strip()
    if doc_type not in DOC_TYPES:
        doc_type = ""
    return text, doc_type
