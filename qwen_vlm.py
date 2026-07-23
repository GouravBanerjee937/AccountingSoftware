"""Qwen VLM client: read a document image/PDF into Markdown and classify it.

This replaces the previous OpenAI-vision OCR step. The document's page image(s)
are sent to the same Qwen server used for text extraction (an OpenAI-compatible
vLLM endpoint); the model returns a faithful Markdown transcription plus a
document-type classification, and this module returns (markdown, doc_type).

PDFs are rasterised to page images with PyMuPDF first (the endpoint accepts image
input only — it rejects PDF data). Images are sent as-is.

The system prompt lives in prompts_vlm/vlm_markdown_classify.txt; the
<<COMPANY_DIRECTION>> placeholder is filled from the profile's company names,
mirroring the previous classifier so Sales-vs-Purchase direction is unchanged.
"""

import base64
import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import qwen_client  # reuse the endpoint URL, model, max_tokens and <think> stripping

# Business document categories (must match server.DOC_TYPES).
DOC_TYPES = ('Sales Invoice', 'Purchase Invoice', 'Sales Return',
             'Purchase Return', 'Receipt', 'Payment')

_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'prompts_vlm', 'vlm_markdown_classify.txt')

_IMAGE_MEDIA = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.webp': 'image/webp', '.gif': 'image/gif', '.bmp': 'image/bmp',
    '.tif': 'image/tiff', '.tiff': 'image/tiff',
}

# PDFs are rasterised to images (the VLM endpoint rejects PDF input).
PDF_DPI = int(os.environ.get('QWEN_VLM_PDF_DPI', '200'))
# Read up to this many pages. Pages beyond it are skipped — but the read now warns
# visibly when that happens (see transcribe_and_classify), so nothing is dropped
# silently. Tune via QWEN_VLM_MAX_PAGES for longer documents.
MAX_PAGES = int(os.environ.get('QWEN_VLM_MAX_PAGES', '20'))
# Multi-page PDFs are read one request PER PAGE, run concurrently (the vLLM
# endpoint batches them, so wall-clock ≈ the slowest page rather than the sum).
# Concurrency is a FIXED cap (independent of MAX_PAGES) so a long document can't
# flood the single-GPU server — a 20-page doc runs in waves of this size.
_CONCURRENCY = max(1, int(os.environ.get('QWEN_VLM_CONCURRENCY', '5')))
# Repetition penalty for the VLM. Greedy decoding (temperature 0) can lock into a
# degenerate loop on hard regions — e.g. a complex merged-header tax table makes
# the model emit blank rows "| | | |" hundreds of times instead of transcribing
# it, dropping real data and running to the token cap. A mild penalty (>1) makes
# repeating recent tokens less likely, breaking the loop so the model reads on.
# Verified: 1.1 stops the loop AND recovers the otherwise-lost table, while
# leaving legitimately-repeated cell values (HSN, unit, qty) unchanged.
_REP_PENALTY = float(os.environ.get('QWEN_VLM_REP_PENALTY', '1.1'))

_DELIM = '===MARKDOWN==='

# Marker placed between per-page transcripts so the extraction step can split the
# document back into pages (a large invoice is extracted one page at a time to
# stay under the model's output-token limit). An HTML comment: invisible when the
# Markdown is rendered, and it will not appear inside real invoice content.
PAGE_DELIM = '\n\n<!-- PAGE BREAK -->\n\n'


def _company_direction(companies):
    """The Sales-vs-Purchase direction block (identical wording to the old
    ocr_client classifier), injected into the VLM prompt at runtime."""
    if companies:
        names = "; ".join(companies)
        return (
            f'OUR COMPANY is the business using this app. Its name may appear as any of: {names}.\n'
            "   Match our names case-insensitively and loosely (ignore Pvt/Ltd/LLP and punctuation).\n"
            "     - If OUR company is the seller / supplier / issuer / payee -> Sales side (Sales Invoice, Sales Return, Receipt).\n"
            "     - If OUR company is the buyer / recipient / payer -> Purchase side (Purchase Invoice, Purchase Return, Payment).\n"
            '     - If OUR company matches NEITHER party, or BOTH, or the direction is unclear, set doc_type to "" (empty string). Do NOT guess the direction.'
        )
    return ('No company names are configured, so the Sales-vs-Purchase direction cannot be '
            'determined. Set doc_type to "" (empty string).')


def _build_prompt(companies):
    with open(_PROMPT_PATH, encoding='utf-8') as f:
        template = f.read()
    return template.replace('<<COMPANY_DIRECTION>>', _company_direction(companies or []))


def _image_data_urls(path):
    """Return (list of data: URLs, total_page_count) for the document. PDFs are
    rasterised (first MAX_PAGES pages at PDF_DPI); pages beyond MAX_PAGES are not
    included, but total_page_count reports the real page count so the caller can
    warn when pages were skipped. Images pass through (total_page_count = 1)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == '.pdf':
        try:
            import fitz  # PyMuPDF
        except ImportError as e:
            raise RuntimeError('PyMuPDF (pymupdf) is required to read PDFs') from e
        urls = []
        with fitz.open(path) as doc:
            total = len(doc)
            for i in range(min(total, MAX_PAGES)):
                pix = doc.load_page(i).get_pixmap(dpi=PDF_DPI)
                b64 = base64.b64encode(pix.tobytes('png')).decode()
                urls.append(f'data:image/png;base64,{b64}')
        if not urls:
            raise RuntimeError('the PDF has no pages to read')
        return urls, total
    media = _IMAGE_MEDIA.get(ext)
    if not media:
        raise RuntimeError(f'unsupported file type for the VLM: {ext}')
    with open(path, 'rb') as f:
        b64 = base64.b64encode(f.read()).decode()
    return [f'data:{media};base64,{b64}'], 1


def _parse(reply):
    """Split the model reply (DOC_TYPE: ... / ===MARKDOWN=== / <md>) into
    (markdown, doc_type). doc_type is validated against DOC_TYPES, else ''."""
    text = qwen_client._THINK_RE.sub('', reply or '').strip()
    doc_type = ''
    markdown = text
    idx = text.find(_DELIM)
    if idx != -1:
        head = text[:idx]
        markdown = text[idx + len(_DELIM):].lstrip('\n')
        for line in head.splitlines():
            s = line.strip()
            if s.upper().startswith('DOC_TYPE:'):
                doc_type = s.split(':', 1)[1].strip()
                break
    else:
        # No delimiter emitted: pull a leading DOC_TYPE line if present, keep the rest.
        lines = text.splitlines()
        if lines and lines[0].strip().upper().startswith('DOC_TYPE:'):
            doc_type = lines[0].split(':', 1)[1].strip()
            markdown = '\n'.join(lines[1:]).lstrip('\n')
    if doc_type not in DOC_TYPES:
        doc_type = ''
    return markdown, doc_type


def _post_page(prompt, url, max_tokens):
    """Send the prompt + a single page image to the VLM; return the parsed
    chat-completions response. Reads only module constants, so it is safe to
    call from multiple threads concurrently."""
    body = json.dumps({
        'model': qwen_client.QWEN_MODEL,
        'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': prompt},
            {'type': 'image_url', 'image_url': {'url': url}},
        ]}],
        'temperature': 0,
        'repetition_penalty': _REP_PENALTY,
        'max_tokens': max_tokens,
        'stream': False,
    }).encode('utf-8')
    req = urllib.request.Request(
        qwen_client._chat_completions_url(),
        data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=900) as resp:
        return json.loads(resp.read())


def _reply_of(data):
    return data['choices'][0]['message']['content']


def _merge_meta(metas, seconds):
    """Sum token usage across the per-page responses; report the wall-clock
    seconds. Keeps the exact meta shape usage_meta() produces (None-safe)."""
    def _sum(key):
        vals = [m.get(key) for m in metas if m.get(key) is not None]
        return sum(vals) if vals else None
    return {
        'prompt_tokens': _sum('prompt_tokens'),
        'completion_tokens': _sum('completion_tokens'),
        'total_tokens': _sum('total_tokens'),
        'seconds': seconds,
    }


def transcribe_and_classify(path, companies=None):
    """Read the document via the Qwen VLM. Returns (markdown_text, doc_type, meta),
    where doc_type is one of DOC_TYPES or '' when the direction is undetermined,
    and meta holds the real token usage + measured wall-clock time in seconds.
    Raises on transport/endpoint errors so the caller can report the failure.

    Multi-page PDFs are read one request PER PAGE, run concurrently: the page
    images are rasterised once up front (PyMuPDF is not thread-safe), then only
    the HTTP calls are parallelised. Pages are reassembled in order and the first
    non-empty page classification wins. A single-page document (or any image) is
    read in one request, identical to the original single-shot path."""
    prompt = _build_prompt(companies)
    urls, total_pages = _image_data_urls(path)   # rasterised once, single-threaded
    t0 = time.monotonic()

    # Fast path: one page/image → one request (byte-for-byte the original call).
    if len(urls) == 1:
        data = _post_page(prompt, urls[0], qwen_client.QWEN_MAX_TOKENS)
        seconds = round(time.monotonic() - t0, 2)
        markdown, doc_type = _parse(_reply_of(data))
        meta = qwen_client.usage_meta(data, seconds)
    else:
        # Multi-page: fire one request per page concurrently. ex.map preserves input
        # order; any page raising propagates out (all-or-nothing, no partial read).
        def _run(i):
            return i, _post_page(prompt, urls[i], qwen_client.QWEN_MAX_TOKENS)
        workers = min(len(urls), _CONCURRENCY)
        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(_run, range(len(urls))))
        except Exception as e:
            raise RuntimeError(f'VLM page read failed: {e}') from e
        seconds = round(time.monotonic() - t0, 2)

        results.sort(key=lambda r: r[0])       # reassemble strictly in page order
        pages_md, metas, doc_type = [], [], ''
        for _, data in results:
            md_i, dt_i = _parse(_reply_of(data))
            if md_i and md_i.strip():
                pages_md.append(md_i.strip())
            metas.append(qwen_client.usage_meta(data, 0))
            if not doc_type and dt_i:          # first non-empty classification wins
                doc_type = dt_i
        markdown = PAGE_DELIM.join(pages_md)
        meta = _merge_meta(metas, seconds)

    # Never drop pages silently: if the document had more pages than we read,
    # prepend a visible warning to the transcript and record it in the meta.
    pages_read = len(urls)
    if total_pages > pages_read:
        skipped = total_pages - pages_read
        note = (f'> **⚠️ Note:** this document has **{total_pages} pages**, but only the '
                f'first **{pages_read}** were read — **{skipped} page(s) skipped**. '
                f'Increase QWEN_VLM_MAX_PAGES to read the rest.')
        markdown = note + '\n\n' + markdown
        meta['pages_total'] = total_pages
        meta['pages_read'] = pages_read
        meta['pages_skipped'] = skipped

    return markdown, doc_type, meta
