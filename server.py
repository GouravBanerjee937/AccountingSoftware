"""Small static + JSON API server backing index.html with CSV persistence."""

import base64
import csv
import json
import os
import re
import threading
import time
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR lets a hosted deploy point at a persistent disk; defaults to this folder.
DATA_DIR = os.environ.get('DATA_DIR', ROOT)
ITEMS_CSV = os.path.join(DATA_DIR, 'items.csv')
INVOICES_CSV = os.path.join(DATA_DIR, 'invoices.csv')
DOCUMENTS_CSV = os.path.join(DATA_DIR, 'documents.csv')
# Uploaded files (e.g. PDFs received over WhatsApp) live here and are served
# statically at /uploads/<filename> by the default file handler.
UPLOADS_DIR = os.path.join(DATA_DIR, 'uploads')
# Per-document processing results (OCR / Qwen / GPT) live here as <id>.json.
RESULTS_DIR = os.path.join(DATA_DIR, 'results')
# Profile: the email addresses and WhatsApp numbers configured by the user
# (used to whitelist which senders' documents get ingested).
PROFILE_JSON = os.path.join(DATA_DIR, 'profile.json')

# Twilio credentials — only needed to download media from the Twilio WhatsApp
# sandbox. Unset locally => media downloads that don't require auth still work.
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')

# Optional shared secret. When API_SECRET is set (e.g. in production), every
# write request must send a matching "X-API-Key" header. Unset locally => open,
# so your existing local setup keeps working unchanged.
API_SECRET = os.environ.get('API_SECRET')

ITEMS_HEADER = ['id', 'name', 'qty', 'price']
# Original 4 columns kept first (backward compatible); customer/status fields appended.
INVOICES_HEADER = ['number', 'item_name', 'qty', 'price',
                   'customer_name', 'customer_email', 'due_date', 'status', 'notes']
# `saved_type`/`saved_id` link a document to the per-type record created from it
# (appended columns => backward compatible with older documents.csv files).
DOCUMENTS_HEADER = ['id', 'upload_datetime', 'name', 'doc_type', 'file', 'source',
                    'saved_type', 'saved_id']

ALLOWED_STATUSES = ('unpaid', 'paid', 'overdue')
DEFAULT_STATUS = 'unpaid'

# File extensions accepted from a Scan upload or a Gmail attachment (PDF/images).
UPLOAD_EXTS = {'.pdf', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff'}

# Business document categories the user can classify a document as.
DOC_TYPES = ('Sales Invoice', 'Purchase Invoice', 'Sales Return',
             'Purchase Return', 'Receipt', 'Payment')

# Each saved document type persists to its own CSV: saved_<slug>.csv. One row per
# saved record; the full (nested) form JSON is kept in the data_json column.
TYPE_SLUGS = {
    'Sales Invoice': 'sales_invoice', 'Purchase Invoice': 'purchase_invoice',
    'Sales Return': 'sales_return', 'Purchase Return': 'purchase_return',
    'Receipt': 'receipt', 'Payment': 'payment',
}
SAVED_HEADER = ['id', 'created_at', 'document_name', 'amount', 'source_doc_id', 'data_json']


def _saved_csv(doc_type):
    slug = TYPE_SLUGS.get(doc_type)
    return os.path.join(DATA_DIR, f'saved_{slug}.csv') if slug else None


def valid_date(text):
    """True if text is a real YYYY-MM-DD date (empty is allowed = no due date)."""
    if not text:
        return True
    try:
        datetime.strptime(text, '%Y-%m-%d')
        return True
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------
# Storage backend: Postgres when DATABASE_URL is set (e.g. on Render with a
# Neon database), otherwise the original CSV files. The public load_*/save_*
# functions below pick the backend, so the request handlers never change.
# --------------------------------------------------------------------------
DATABASE_URL = os.environ.get('DATABASE_URL')
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg  # only needed/imported in the hosted (Postgres) mode


def _pg():
    """Open a fresh Postgres connection (simple + safe for low traffic)."""
    return psycopg.connect(DATABASE_URL)


def init_db():
    """Create the tables if they don't exist yet (Postgres mode only)."""
    if not USE_PG:
        return
    with _pg() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            qty INTEGER NOT NULL,
            price DOUBLE PRECISION NOT NULL)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS invoices (
            id SERIAL PRIMARY KEY,
            number TEXT, item_name TEXT, qty INTEGER, price DOUBLE PRECISION,
            customer_name TEXT, customer_email TEXT, due_date TEXT,
            status TEXT, notes TEXT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            upload_datetime TEXT NOT NULL,
            name TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            file TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'internal')""")


# ---- CSV helpers (used in local mode) ----
def ensure_csv(path, header):
    if not os.path.exists(path):
        with open(path, 'w', newline='') as f:
            csv.writer(f).writerow(header)


def read_csv(path, header):
    ensure_csv(path, header)
    with open(path, newline='') as f:
        return list(csv.DictReader(f))


def write_csv(path, header, rows):
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---- Public storage API (dispatches to Postgres or CSV) ----
def load_items():
    if USE_PG:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, name, qty, price FROM items ORDER BY id")
            return [{'id': r[0], 'name': r[1], 'qty': r[2], 'price': float(r[3])}
                    for r in cur.fetchall()]
    return [{'id': int(r['id']), 'name': r['name'],
             'qty': int(r['qty']), 'price': float(r['price'])}
            for r in read_csv(ITEMS_CSV, ITEMS_HEADER)]


def save_items(items):
    if USE_PG:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM items")
            cur.executemany(
                "INSERT INTO items (id, name, qty, price) VALUES (%s, %s, %s, %s)",
                [(i['id'], i['name'], i['qty'], i['price']) for i in items])
        return
    write_csv(ITEMS_CSV, ITEMS_HEADER, items)


def load_invoices():
    if USE_PG:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute("""SELECT number, item_name, qty, price, customer_name,
                customer_email, due_date, status, notes FROM invoices ORDER BY id""")
            return [{
                'number': r[0] or '', 'itemName': r[1] or '',
                'qty': int(r[2] or 0), 'price': float(r[3] or 0),
                'customerName': r[4] or '', 'customerEmail': r[5] or '',
                'dueDate': r[6] or '', 'status': r[7] or DEFAULT_STATUS,
                'notes': r[8] or '',
            } for r in cur.fetchall()]
    # CSV mode — .get() keeps pre-upgrade rows (missing new columns) working.
    invoices = []
    for r in read_csv(INVOICES_CSV, INVOICES_HEADER):
        invoices.append({
            'number': r.get('number', ''),
            'itemName': r.get('item_name', ''),
            'qty': int(r.get('qty') or 0),
            'price': float(r.get('price') or 0),
            'customerName': r.get('customer_name') or '',
            'customerEmail': r.get('customer_email') or '',
            'dueDate': r.get('due_date') or '',
            'status': r.get('status') or DEFAULT_STATUS,
            'notes': r.get('notes') or '',
        })
    return invoices


def save_invoices(invoices):
    if USE_PG:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM invoices")
            cur.executemany(
                """INSERT INTO invoices (number, item_name, qty, price,
                    customer_name, customer_email, due_date, status, notes)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [(i['number'], i['itemName'], i['qty'], i['price'],
                  i.get('customerName', ''), i.get('customerEmail', ''),
                  i.get('dueDate', ''), i.get('status', DEFAULT_STATUS),
                  i.get('notes', '')) for i in invoices])
        return
    rows = [{
        'number': i['number'], 'item_name': i['itemName'],
        'qty': i['qty'], 'price': i['price'],
        'customer_name': i.get('customerName', ''),
        'customer_email': i.get('customerEmail', ''),
        'due_date': i.get('dueDate', ''),
        'status': i.get('status', DEFAULT_STATUS),
        'notes': i.get('notes', ''),
    } for i in invoices]
    write_csv(INVOICES_CSV, INVOICES_HEADER, rows)


def load_documents():
    if USE_PG:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute("""SELECT id, upload_datetime, name, doc_type, file, source
                           FROM documents ORDER BY id""")
            return [{'id': r[0], 'uploadDateTime': r[1] or '',
                     'name': r[2] or '', 'docType': r[3] or '', 'file': r[4] or '',
                     'source': r[5] or 'internal', 'savedType': '', 'savedId': ''}
                    for r in cur.fetchall()]
    return [{'id': int(r['id']), 'uploadDateTime': r.get('upload_datetime', ''),
             'name': r.get('name', ''), 'docType': r.get('doc_type', ''),
             'file': r.get('file', ''), 'source': r.get('source') or 'internal',
             'savedType': r.get('saved_type') or '', 'savedId': r.get('saved_id') or ''}
            for r in read_csv(DOCUMENTS_CSV, DOCUMENTS_HEADER)]


def save_documents(documents):
    if USE_PG:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM documents")
            cur.executemany(
                """INSERT INTO documents (id, upload_datetime, name, doc_type, file, source)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                [(d['id'], d['uploadDateTime'], d['name'], d['docType'],
                  d.get('file', ''), d.get('source', 'internal')) for d in documents])
        return
    rows = [{'id': d['id'], 'upload_datetime': d['uploadDateTime'],
             'name': d['name'], 'doc_type': d['docType'],
             'file': d.get('file', ''), 'source': d.get('source', 'internal'),
             'saved_type': d.get('savedType', ''), 'saved_id': d.get('savedId', '')}
            for d in documents]
    write_csv(DOCUMENTS_CSV, DOCUMENTS_HEADER, rows)


def load_profile():
    """Return {'emails': [...], 'whatsapp': [...]} — two independent lists."""
    if os.path.isfile(PROFILE_JSON):
        with open(PROFILE_JSON, encoding='utf-8') as f:
            data = json.load(f)
    else:
        data = {}
    companies = list(data.get('companies', []))
    company = str(data.get('company', '') or '')
    if not companies and company:          # migrate the old single-name field
        companies = [company]
    return {'emails': list(data.get('emails', [])),
            'whatsapp': list(data.get('whatsapp', [])),
            'company': company, 'companies': companies}


def save_profile(profile):
    with open(PROFILE_JSON, 'w', encoding='utf-8') as f:
        json.dump(profile, f)


def poll_gmail():
    """Ingest invoice attachments sent by allowed senders (profile emails) to the
    connected system inbox. Each email is labelled so it is ingested only once.
    Safe to call anytime — returns a summary dict, never raises."""
    try:
        import gmail_client
        if not gmail_client.is_configured():
            return {'ok': False, 'error': 'Gmail not configured'}
        senders = load_profile().get('emails', [])
        if not senders:
            return {'ok': True, 'ingested': 0, 'note': 'no allowed sender emails in profile'}

        token = gmail_client.get_access_token()
        label_id = gmail_client.ensure_label(token)
        from_q = ' OR '.join(f'from:{s}' for s in senders)
        query = f'has:attachment ({from_q}) -label:{gmail_client.INGEST_LABEL}'
        ids = gmail_client.list_message_ids(token, query)

        os.makedirs(UPLOADS_DIR, exist_ok=True)
        ingested = 0
        for mid in ids:
            msg = gmail_client.get_message(token, mid)
            for filename, mime, att_id in gmail_client.iter_attachments(msg):
                ext = os.path.splitext(filename)[1].lower()
                if ext not in UPLOAD_EXTS and not (
                        mime == 'application/pdf' or mime.startswith('image/')):
                    continue
                data = gmail_client.get_attachment_bytes(token, mid, att_id)
                if not data:
                    continue
                stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
                stored = _safe_filename(f'email_{stamp}_{filename}')
                with open(os.path.join(UPLOADS_DIR, stored), 'wb') as f:
                    f.write(data)
                add_document(filename, '', file=f'uploads/{stored}', source='email')
                ingested += 1
            gmail_client.add_label(token, mid, label_id)   # don't reprocess
        return {'ok': True, 'ingested': ingested}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def _result_path(doc_id):
    return os.path.join(RESULTS_DIR, f'{int(doc_id)}.json')


def load_result(doc_id):
    """Return the stored {ocr, qwen, gpt} dict for a document (empty if none)."""
    path = _result_path(doc_id)
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return {}


def _parse_json_blob(s):
    """Best-effort parse of a model response (may have code fences / <think>)."""
    if not isinstance(s, str):
        return None
    s = re.sub(r'<think>.*?</think>', '', s, flags=re.S)
    s = s.replace('```json', '').replace('```', '')
    a, b = s.find('{'), s.rfind('}')
    if a == -1 or b == -1:
        return None
    try:
        return json.loads(s[a:b + 1])
    except Exception:
        return None


def _merge_invoice_jsons(pages):
    """Merge per-page extraction JSONs into one invoice document.

    A large invoice is extracted one page at a time (to stay under the model's
    output-token limit); this stitches the pages back together, matching how an
    invoice is laid out:
      - header fields (seller, buyer, invoice no/date, …) come from page 1,
      - line_items are concatenated across every page, in page order,
      - summary / tax_tables / other_charges come from the LAST page that has
        them (the totals and tax breakup sit on the final page).
    `pages` is a list in page order; entries that failed to parse (None) are
    skipped so a single bad page can't discard the rest.
    """
    import validate  # reuse the tolerant number coercion
    pages = [p for p in pages if isinstance(p, dict)]
    if not pages:
        return {}
    merged = dict(pages[0])                      # header from the first page
    items = []
    for p in pages:
        li = p.get('line_items')
        if isinstance(li, list):
            items.extend(li)
    merged['line_items'] = items                 # every page's items, in order

    def _amt(v):
        return validate._num(v)

    # summary: the last page that actually carries a grand_total
    for p in reversed(pages):
        s = p.get('summary')
        if isinstance(s, dict) and _amt(s.get('grand_total')):
            merged['summary'] = s
            break
    # tax_tables: the last page that carries any tax amount
    for p in reversed(pages):
        tt = p.get('tax_tables')
        if isinstance(tt, list) and any(
                _amt((t or {}).get('cgst_amount')) or _amt((t or {}).get('sgst_amount'))
                or _amt((t or {}).get('igst_amount')) for t in tt):
            merged['tax_tables'] = tt
            break
    # other_charges: the last page that carries any non-zero charge
    for p in reversed(pages):
        oc = p.get('other_charges')
        if isinstance(oc, list) and any(_amt((t or {}).get('amount')) for t in oc):
            merged['other_charges'] = oc
            break
    return merged


def _extract_invoice(system, text):
    """Run the Qwen extraction, splitting a page-delimited transcript into per-page
    chunks that are extracted concurrently and merged. Returns (response, meta),
    where response is a JSON string. Falls back to a single request when the
    transcript is one page (small invoices, single images) — byte-for-byte the
    original behaviour. Raises on transport errors (handled by the caller)."""
    import qwen_client, qwen_vlm
    from concurrent.futures import ThreadPoolExecutor
    chunks = [c for c in text.split(qwen_vlm.PAGE_DELIM) if c.strip()]
    if len(chunks) <= 1:
        return qwen_client.chat_with_usage(system, text)

    t0 = time.monotonic()
    workers = min(len(chunks), 5)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda c: qwen_client.chat_with_usage(system, c), chunks))
    seconds = round(time.monotonic() - t0, 2)

    parsed = [_parse_json_blob(r[0]) for r in results]
    merged = _merge_invoice_jsons(parsed)
    metas = [r[1] for r in results]

    def _sum(k):
        vals = [m.get(k) for m in metas if m.get(k) is not None]
        return sum(vals) if vals else None
    meta = {
        'prompt_tokens': _sum('prompt_tokens'),
        'completion_tokens': _sum('completion_tokens'),
        'total_tokens': _sum('total_tokens'),
        'seconds': seconds,
        'pages_extracted': len(chunks),
        'pages_parsed': sum(1 for p in parsed if p is not None),
    }
    return json.dumps(merged, ensure_ascii=False), meta


def amount_from_obj(obj):
    """Pull a best-effort total out of an extracted/edited document object,
    covering the invoice, credit/debit note and receipt/payment schemas."""
    if not isinstance(obj, dict):
        return None
    for path in (('summary', 'grand_total'), ('footer', 'total'),
                 ('footer', 'total_receipt'), ('receipt_details', 'amount_received')):
        d = obj
        for p in path:
            d = d.get(p) if isinstance(d, dict) else None
        if d not in (None, '', 0, 0.0):
            return d
    return None


def document_amount(doc_id):
    """Best-effort total amount for a document, from its stored Qwen JSON."""
    try:
        result = load_result(doc_id)
    except Exception:
        return None
    return amount_from_obj(_parse_json_blob(result.get('qwen')))


# ---- OCR status + background auto-OCR ----
# Every document with a file is OCR'd automatically as soon as it arrives (before
# it is opened), so the Documents page can show OCR status and offer a Retry.
# Status lives in the document's result file: 'pending' | 'running' | 'ok' | 'failed'.
_ocr_lock = threading.Lock()
_ocr_inflight = set()


def get_ocr_status(doc_id):
    r = load_result(doc_id)
    if r.get('ocr_status'):
        return r['ocr_status']
    if r.get('ocr'):
        return 'ok'          # legacy result files that predate status tracking
    return 'pending'


def set_ocr_status(doc_id, status, error=''):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    data = load_result(doc_id)
    data['ocr_status'] = status
    data['ocr_error'] = error
    with open(_result_path(doc_id), 'w', encoding='utf-8') as f:
        json.dump(data, f)


def run_ocr_for(doc):
    """Read one document's file to Markdown via the Qwen VLM, storing the text +
    status. Returns (ok, error)."""
    doc_id = doc['id']
    rel = doc.get('file', '')
    if not rel:
        set_ocr_status(doc_id, 'failed', 'this document has no file to read')
        return False, 'no file'
    fpath = os.path.join(DATA_DIR, rel)
    if not os.path.isfile(fpath):
        set_ocr_status(doc_id, 'failed', 'file not found on server')
        return False, 'file not found'
    set_ocr_status(doc_id, 'running')
    try:
        import qwen_vlm
        companies = load_profile().get('companies', [])
        text, suggested_type, meta = qwen_vlm.transcribe_and_classify(fpath, companies)
    except Exception as e:
        set_ocr_status(doc_id, 'failed', str(e))
        return False, str(e)
    # 'ocr' key kept for backward compatibility; it now holds the VLM Markdown.
    save_result_field(doc_id, 'ocr', text)
    # Real token usage + measured time for this VLM read (shown in the UI).
    save_result_field(doc_id, 'ocr_meta', meta)
    # The VLM also classifies the document type — store it as a suggestion the
    # UI pre-selects (the user can still override it).
    if suggested_type:
        save_result_field(doc_id, 'suggested_type', suggested_type)
    set_ocr_status(doc_id, 'ok')
    return True, None


def _auto_ocr_loop():
    """Continuously OCR any document that is still 'pending'. Sequential (one at
    a time) to avoid hammering the OCR service. 'failed' docs are left for the
    user to Retry (which re-queues them as 'pending')."""
    while True:
        try:
            for doc in load_documents():
                if not doc.get('file'):
                    continue
                if get_ocr_status(doc['id']) != 'pending':
                    continue
                with _ocr_lock:
                    if doc['id'] in _ocr_inflight:
                        continue
                    _ocr_inflight.add(doc['id'])
                try:
                    run_ocr_for(doc)
                finally:
                    with _ocr_lock:
                        _ocr_inflight.discard(doc['id'])
        except Exception as e:
            print('OCR worker error:', e)
        time.sleep(3)


# ---- Per-type saved-record store (one CSV per document type) ----
def load_saved(doc_type):
    """Return all saved records for a document type (newest-friendly order kept)."""
    path = _saved_csv(doc_type)
    if not path:
        return []
    out = []
    for r in read_csv(path, SAVED_HEADER):
        try:
            data = json.loads(r.get('data_json') or '{}')
        except Exception:
            data = {}
        try:
            rid = int(r['id'])
        except (KeyError, ValueError, TypeError):
            continue
        out.append({'id': rid, 'createdAt': r.get('created_at', ''),
                    'documentName': r.get('document_name', ''),
                    'amount': r.get('amount', ''),
                    'sourceDocId': r.get('source_doc_id', ''), 'data': data})
    return out


def write_saved(doc_type, records):
    path = _saved_csv(doc_type)
    if not path:
        return
    rows = [{'id': r['id'], 'created_at': r['createdAt'],
             'document_name': r['documentName'], 'amount': r['amount'],
             'source_doc_id': r['sourceDocId'],
             'data_json': json.dumps(r['data'], ensure_ascii=False)} for r in records]
    write_csv(path, SAVED_HEADER, rows)


def mark_document_saved(doc_id, doc_type, saved_id):
    """Record on the source document which per-type record was created from it."""
    documents = load_documents()
    doc = next((d for d in documents if d['id'] == doc_id), None)
    if not doc:
        return
    doc['savedType'] = doc_type
    doc['savedId'] = str(saved_id)
    save_documents(documents)


def save_result_field(doc_id, key, value):
    """Merge one result field (ocr/qwen/gpt) into the document's result file."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    data = load_result(doc_id)
    data[key] = value
    with open(_result_path(doc_id), 'w', encoding='utf-8') as f:
        json.dump(data, f)


def add_document(name, doc_type, file='', source='internal'):
    """Append one document row. Upload date/time is stamped here, server-side.
    `source` records where it came from: whatsapp / email / Home / internal."""
    documents = load_documents()
    next_id = max((d['id'] for d in documents), default=0) + 1
    new_doc = {'id': next_id,
               'uploadDateTime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
               'name': name, 'docType': doc_type, 'file': file, 'source': source,
               'savedType': '', 'savedId': ''}
    documents.append(new_doc)
    save_documents(documents)
    return new_doc


def _safe_filename(name):
    """Reduce an arbitrary string to a filesystem-safe basename."""
    base = os.path.basename(name or '').strip()
    base = re.sub(r'[^A-Za-z0-9._-]', '_', base)
    return base or 'file'


class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Twilio media URLs 302-redirect to a CDN that rejects a second auth
    mechanism — drop the Authorization header when following the redirect."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.headers = {k: v for k, v in new.headers.items()
                           if k.lower() != 'authorization'}
        return new


def download_media(url):
    """Fetch bytes from a media URL. Adds Twilio basic auth for twilio.com hosts."""
    req = urllib.request.Request(url)
    if 'twilio.com' in (urlparse(url).netloc or '') and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
        cred = base64.b64encode(
            f'{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}'.encode()).decode()
        req.add_header('Authorization', 'Basic ' + cred)
    opener = urllib.request.build_opener(_StripAuthOnRedirect)
    with opener.open(req, timeout=30) as resp:
        return resp.read()


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/items':
            return self._json(load_items())
        if path == '/api/invoices':
            return self._json(load_invoices())
        if path == '/api/documents':
            docs = load_documents()
            for d in docs:
                d['amount'] = document_amount(d['id'])
                r = load_result(d['id'])
                d['ocrStatus'] = get_ocr_status(d['id'])
                d['ocrError'] = r.get('ocr_error', '')
                d['suggestedType'] = r.get('suggested_type', '')
            return self._json(docs)
        if path == '/api/documents/result':
            qs = parse_qs(urlparse(self.path).query)
            try:
                doc_id = int(qs.get('id', [''])[0])
            except ValueError:
                return self._error(400, 'invalid id')
            return self._json(load_result(doc_id))
        if path == '/api/saved':
            qs = parse_qs(urlparse(self.path).query)
            doc_type = qs.get('type', [''])[0].strip()
            if doc_type not in DOC_TYPES:
                return self._error(400, 'invalid or missing type')
            records = load_saved(doc_type)
            id_q = qs.get('id', [None])[0]
            if id_q not in (None, ''):
                try:
                    rid = int(id_q)
                except ValueError:
                    return self._error(400, 'invalid id')
                rec = next((r for r in records if r['id'] == rid), None)
                if rec is None:
                    return self._error(404, 'record not found')
                return self._json(rec)   # full record incl. nested data
            # List view: omit the heavy data_json, newest first.
            summary = [{'id': r['id'], 'createdAt': r['createdAt'],
                        'documentName': r['documentName'], 'amount': r['amount'],
                        'sourceDocId': r['sourceDocId']} for r in records]
            summary.sort(key=lambda r: r['id'], reverse=True)
            return self._json(summary)
        if path == '/api/profile':
            return self._json(load_profile())
        if path == '/api/gmail/address':
            import gmail_client
            if not gmail_client.is_configured():
                return self._json({'configured': False, 'address': None})
            try:
                token = gmail_client.get_access_token()
                return self._json({'configured': True, 'address': gmail_client.get_address(token)})
            except Exception as e:
                return self._json({'configured': True, 'address': None, 'error': str(e)})
        return super().do_GET()

    def _authorized(self):
        """Allow if no secret is configured (local), else require a matching key."""
        if not API_SECRET:
            return True
        return self.headers.get('X-API-Key') == API_SECRET

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get('Content-Length', 0))
        raw = self.rfile.read(length) if length else b''

        # Binary file upload (Scan): raw file bytes in the body, name/type in query.
        if path == '/api/uploads':
            return self._upload_file(raw)

        body = raw.decode('utf-8', 'replace')

        # WhatsApp (Twilio) webhook: form-encoded, authenticated by Twilio's own
        # signature rather than our X-API-Key, so it runs before _authorized().
        if path == '/webhook/whatsapp':
            return self._whatsapp_webhook(body)

        if not self._authorized():
            return self._error(401, 'unauthorized: missing or wrong X-API-Key')
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._error(400, 'invalid json')

        if path == '/api/ask':
            return self._ask(data)
        if path == '/api/items':
            return self._create_item(data)
        if path == '/api/invoices':
            return self._create_invoice(data)
        if path == '/api/documents':
            return self._create_document(data)
        if path == '/api/documents/ocr':
            return self._ocr_document(data)
        if path == '/api/documents/ocr-retry':
            return self._ocr_retry(data)
        if path == '/api/documents/extract':
            return self._extract_document(data)
        if path == '/api/documents/validate':
            return self._validate_document(data)
        if path == '/api/documents/commit':
            return self._commit_documents(data)
        if path == '/api/documents/classify':
            return self._classify_file(data)
        if path == '/api/saved':
            return self._create_saved(data)
        if path == '/api/profile':
            return self._add_profile_entry(data)
        if path == '/api/profile/company':
            return self._set_company(data)
        if path == '/api/gmail/poll':
            return self._json(poll_gmail())
        return self._error(404, 'not found')

    def _add_profile_entry(self, data):
        """Add one email / whatsapp number / company name to the profile (deduped)."""
        kind = str(data.get('kind', '') or '').strip()
        value = str(data.get('value', '') or '').strip()
        if kind not in ('emails', 'whatsapp', 'companies'):
            return self._error(400, "kind must be 'emails', 'whatsapp' or 'companies'")
        if not value:
            return self._error(400, 'value is required')
        profile = load_profile()
        if value not in profile[kind]:
            profile[kind].append(value)
            save_profile(profile)
        return self._json(profile)

    def _set_company(self, data):
        """Set the single (editable) company name on the profile."""
        name = str(data.get('name', '') or '').strip()
        profile = load_profile()
        profile['company'] = name
        save_profile(profile)
        return self._json(profile)

    def _ask(self, data):
        """In-app assistant: forward the question to the OpenAI-powered agent."""
        question = str(data.get('question', '') or '').strip()
        if not question:
            return self._error(400, 'question is required')
        history = data.get('history') if isinstance(data.get('history'), list) else None
        try:
            import agent
            result = agent.ask(question, history)
        except Exception as e:  # never crash the server on an assistant problem
            return self._json({'answer': None, 'error': f'assistant error: {e}'})
        return self._json({'answer': result.get('answer'), 'error': result.get('error')})

    def do_DELETE(self):
        if not self._authorized():
            return self._error(401, 'unauthorized: missing or wrong X-API-Key')
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        if path == '/api/items':
            try:
                item_id = int(qs.get('id', [''])[0])
            except ValueError:
                return self._error(400, 'invalid id')
            items = load_items()
            remaining = [i for i in items if i['id'] != item_id]
            if len(remaining) == len(items):
                return self._error(404, 'item not found')
            save_items(remaining)
            return self._json({'deleted': item_id})
        if path == '/api/invoices':
            number = qs.get('number', [''])[0].strip()
            if not number:
                return self._error(400, 'invoice number required')
            invoices = load_invoices()
            remaining = [i for i in invoices if i['number'] != number]
            deleted = len(invoices) - len(remaining)
            save_invoices(remaining)
            return self._json({'deleted': deleted, 'number': number})
        if path == '/api/documents':
            try:
                doc_id = int(qs.get('id', [''])[0])
            except ValueError:
                return self._error(400, 'invalid id')
            documents = load_documents()
            doc = next((d for d in documents if d['id'] == doc_id), None)
            if doc is None:
                return self._error(404, 'document not found')
            # Remove the stored file too, if any (keep uploads/ tidy).
            rel = doc.get('file', '')
            if rel:
                fpath = os.path.join(DATA_DIR, rel)
                if os.path.commonpath([os.path.abspath(fpath), UPLOADS_DIR]) == UPLOADS_DIR \
                        and os.path.isfile(fpath):
                    try:
                        os.remove(fpath)
                    except OSError as e:
                        print(f'Could not remove {fpath}: {e}')
            # Drop the stored results file too, if any.
            rpath = _result_path(doc_id)
            if os.path.isfile(rpath):
                try:
                    os.remove(rpath)
                except OSError:
                    pass
            save_documents([d for d in documents if d['id'] != doc_id])
            return self._json({'deleted': doc_id})
        if path == '/api/profile':
            kind = qs.get('kind', [''])[0].strip()
            value = qs.get('value', [''])[0].strip()
            if kind not in ('emails', 'whatsapp', 'companies'):
                return self._error(400, "kind must be 'emails', 'whatsapp' or 'companies'")
            profile = load_profile()
            profile[kind] = [v for v in profile[kind] if v != value]
            save_profile(profile)
            return self._json(profile)
        if path == '/api/saved':
            doc_type = qs.get('type', [''])[0].strip()
            if doc_type not in DOC_TYPES:
                return self._error(400, 'invalid or missing type')
            try:
                rid = int(qs.get('id', [''])[0])
            except ValueError:
                return self._error(400, 'invalid id')
            records = load_saved(doc_type)
            remaining = [r for r in records if r['id'] != rid]
            if len(remaining) == len(records):
                return self._error(404, 'record not found')
            write_saved(doc_type, remaining)
            return self._json({'deleted': rid})
        return self._error(404, 'not found')

    def do_PUT(self):
        if not self._authorized():
            return self._error(401, 'unauthorized: missing or wrong X-API-Key')
        path = urlparse(self.path).path
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode('utf-8') if length else ''
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return self._error(400, 'invalid json')
        if path == '/api/items':
            return self._update_item(data)
        if path == '/api/invoices':
            return self._update_invoice(data)
        if path == '/api/documents':
            return self._update_document(data)
        return self._error(404, 'not found')

    def _update_document(self, data):
        """Re-classify a document's type to one of the allowed DOC_TYPES."""
        try:
            doc_id = int(data['id'])
        except (KeyError, ValueError, TypeError):
            return self._error(400, 'a valid document id is required')
        doc_type = str(data.get('docType', '') or '').strip()
        if doc_type not in DOC_TYPES:
            return self._error(400, f'document type must be one of: {", ".join(DOC_TYPES)}')
        documents = load_documents()
        doc = next((d for d in documents if d['id'] == doc_id), None)
        if doc is None:
            return self._error(404, 'document not found')
        doc['docType'] = doc_type
        save_documents(documents)
        return self._json(doc)

    def _update_item(self, data):
        """Update an existing item's name/qty/price by id. Only provided fields change."""
        try:
            item_id = int(data['id'])
        except (KeyError, ValueError, TypeError):
            return self._error(400, 'a valid item id is required')
        items = load_items()
        item = next((i for i in items if i['id'] == item_id), None)
        if item is None:
            return self._error(404, 'item not found')
        if 'name' in data:
            name = str(data['name'] or '').strip()
            if not name:
                return self._error(400, 'name cannot be empty')
            item['name'] = name
        if 'qty' in data:
            try:
                qty = int(data['qty'])
            except (ValueError, TypeError):
                return self._error(400, 'qty must be a whole number')
            if qty < 0:
                return self._error(400, 'qty cannot be negative')
            item['qty'] = qty
        if 'price' in data:
            try:
                price = float(data['price'])
            except (ValueError, TypeError):
                return self._error(400, 'price must be a number')
            if price < 0:
                return self._error(400, 'price cannot be negative')
            item['price'] = price
        save_items(items)
        return self._json(item)

    def _update_invoice(self, data):
        """Update an existing invoice's NON-stock fields by number (must be unique).
        Quantity and item are intentionally not editable here, so stock stays correct."""
        number = str(data.get('number', '') or '').strip()
        if not number:
            return self._error(400, 'invoice number is required')
        invoices = load_invoices()
        matches = [i for i in invoices if i['number'] == number]
        if not matches:
            return self._error(404, 'invoice not found')
        if len(matches) > 1:
            return self._error(400, f'{len(matches)} invoices share number "{number}"; '
                                    f'cannot edit unambiguously')
        inv = matches[0]
        if 'customerName' in data:
            inv['customerName'] = str(data['customerName'] or '').strip()
        if 'customerEmail' in data:
            inv['customerEmail'] = str(data['customerEmail'] or '').strip()
        if 'notes' in data:
            inv['notes'] = str(data['notes'] or '').strip()
        if 'dueDate' in data:
            dd = str(data['dueDate'] or '').strip()
            if not valid_date(dd):
                return self._error(400, 'due_date must be YYYY-MM-DD')
            inv['dueDate'] = dd
        if 'status' in data:
            st = str(data['status'] or '').strip().lower()
            if st not in ALLOWED_STATUSES:
                return self._error(400, f'status must be one of {", ".join(ALLOWED_STATUSES)}')
            inv['status'] = st
        if 'price' in data:
            try:
                price = float(data['price'])
            except (ValueError, TypeError):
                return self._error(400, 'price must be a number')
            if price < 0:
                return self._error(400, 'price cannot be negative')
            inv['price'] = price
        save_invoices(invoices)
        return self._json(inv)

    def _create_item(self, data):
        try:
            name = str(data['name']).strip()
            qty = int(data['qty'])
            price = float(data['price'])
        except (KeyError, ValueError, TypeError):
            return self._error(400, 'invalid item payload')
        if not name or qty < 0 or price < 0:
            return self._error(400, 'invalid item values')

        items = load_items()
        next_id = max((i['id'] for i in items), default=0) + 1
        new_item = {'id': next_id, 'name': name, 'qty': qty, 'price': price}
        items.append(new_item)
        save_items(items)
        return self._json(new_item, status=201)

    def _create_invoice(self, data):
        try:
            number = str(data['number']).strip()
            item_id = int(data['itemId'])
            qty = int(data['qty'])
            price = float(data['price'])
        except (KeyError, ValueError, TypeError):
            return self._error(400, 'invalid invoice payload')
        if not number or qty <= 0 or price < 0:
            return self._error(400, 'invalid invoice values')

        # New optional fields — validated only if provided, so existing callers
        # (and the existing UI) keep working unchanged.
        customer_name = str(data.get('customerName', '') or '').strip()
        customer_email = str(data.get('customerEmail', '') or '').strip()
        due_date = str(data.get('dueDate', '') or '').strip()
        status = str(data.get('status', '') or DEFAULT_STATUS).strip().lower()
        notes = str(data.get('notes', '') or '').strip()

        if status not in ALLOWED_STATUSES:
            return self._error(400, f'status must be one of {", ".join(ALLOWED_STATUSES)}')
        if not valid_date(due_date):
            return self._error(400, 'due_date must be YYYY-MM-DD')

        items = load_items()
        item = next((i for i in items if i['id'] == item_id), None)
        if item is None:
            return self._error(400, 'item not found')
        if qty > item['qty']:
            return self._error(400, f'only {item["qty"]} of "{item["name"]}" available')

        item['qty'] -= qty
        save_items(items)

        invoices = load_invoices()
        new_invoice = {
            'number': number, 'itemName': item['name'], 'qty': qty, 'price': price,
            'customerName': customer_name, 'customerEmail': customer_email,
            'dueDate': due_date, 'status': status, 'notes': notes,
        }
        invoices.append(new_invoice)
        save_invoices(invoices)

        return self._json({'invoice': new_invoice, 'item': item}, status=201)

    def _create_document(self, data):
        name = str(data.get('name', '') or '').strip()
        doc_type = str(data.get('docType', '') or '').strip()
        if not name:
            return self._error(400, 'document name is required')
        if not doc_type:
            return self._error(400, 'document type is required')

        return self._json(add_document(name, doc_type), status=201)

    def _upload_file(self, raw):
        """Stage one uploaded file (PDF/image) into uploads/. Does NOT create a
        document row yet — that happens on commit. Returns per-file status."""
        qs = parse_qs(urlparse(self.path).query)
        name = (qs.get('name', [''])[0] or 'file').strip()
        ctype = qs.get('type', [''])[0]
        ext = os.path.splitext(name)[1].lower()
        is_ok_type = (ext in UPLOAD_EXTS
                      or ctype == 'application/pdf' or ctype.startswith('image/'))
        if not is_ok_type:
            return self._json({'ok': False, 'name': name, 'size': len(raw),
                               'error': 'unsupported file type (PDF/images only)'})
        if not raw:
            return self._json({'ok': False, 'name': name, 'size': 0, 'error': 'empty file'})

        os.makedirs(UPLOADS_DIR, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        stored = _safe_filename(f'scan_{stamp}_{name}')
        with open(os.path.join(UPLOADS_DIR, stored), 'wb') as f:
            f.write(raw)
        return self._json({'ok': True, 'name': name, 'size': len(raw),
                           'file': f'uploads/{stored}'})

    def _classify_file(self, data):
        """Classify a staged (uploaded) file's document type WITHOUT committing it.
        Used by the Scan flow: when a user scans from a specific type page we ask the
        vision model what type it thinks the file is, so the UI can warn on a mismatch.
        Returns {'suggested_type': <one of DOC_TYPES or ''>}."""
        rel = str(data.get('file', '') or '').strip()
        if not rel.startswith('uploads/') or '..' in rel:
            return self._error(400, 'a valid uploaded file is required')
        fpath = os.path.join(DATA_DIR, rel)
        if not os.path.isfile(fpath):
            return self._error(404, 'file not found')
        try:
            import qwen_vlm
            companies = load_profile().get('companies', [])
            _text, suggested, _meta = qwen_vlm.transcribe_and_classify(fpath, companies)
        except Exception as e:
            # Best-effort: on any classifier error, return no suggestion (no popup).
            return self._json({'suggested_type': '', 'error': str(e)})
        return self._json({'suggested_type': suggested or ''})

    def _commit_documents(self, data):
        """Create document rows for the successfully-uploaded (staged) files."""
        doc_type = str(data.get('docType', '') or '').strip()
        source = str(data.get('source', '') or 'Home').strip()
        files = data.get('files') or []
        created = []
        for f in files:
            name = str(f.get('name', '') or '').strip()
            rel = str(f.get('file', '') or '').strip()
            # Only accept references that point inside uploads/ (no traversal).
            if not name or not rel.startswith('uploads/') or '..' in rel:
                continue
            if not os.path.isfile(os.path.join(DATA_DIR, rel)):
                continue
            created.append(add_document(name, doc_type, file=rel, source=source))
        return self._json({'created': created}, status=201)

    def _create_saved(self, data):
        """Persist a filled document form into its per-type CSV store. Computes a
        best-effort amount and, if it came from a Documents row, marks that row
        as processed (saved_type / saved_id)."""
        doc_type = str(data.get('type', '') or '').strip()
        if doc_type not in DOC_TYPES:
            return self._error(400, f'type must be one of: {", ".join(DOC_TYPES)}')
        payload = data.get('data')
        if not isinstance(payload, dict):
            return self._error(400, 'a data object is required')
        document_name = str(data.get('documentName', '') or '').strip() or 'Untitled'
        source_doc_id = str(data.get('sourceDocId', '') or '').strip()
        records = load_saved(doc_type)
        next_id = max((r['id'] for r in records), default=0) + 1
        amount = amount_from_obj(payload)
        rec = {'id': next_id,
               'createdAt': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
               'documentName': document_name,
               'amount': '' if amount is None else amount,
               'sourceDocId': source_doc_id, 'data': payload}
        records.append(rec)
        write_saved(doc_type, records)
        if source_doc_id:
            try:
                mark_document_saved(int(source_doc_id), doc_type, next_id)
            except (ValueError, TypeError):
                pass
        return self._json({'id': next_id, 'amount': rec['amount']}, status=201)

    def _ocr_document(self, data):
        """Read a document's stored file to Markdown via the Qwen VLM."""
        try:
            doc_id = int(data['id'])
        except (KeyError, ValueError, TypeError):
            return self._error(400, 'a valid document id is required')
        doc = next((d for d in load_documents() if d['id'] == doc_id), None)
        if doc is None:
            return self._error(404, 'document not found')
        ok, err = run_ocr_for(doc)
        if not ok:
            return self._json({'text': None, 'error': f'OCR service error: {err}'})
        result = load_result(doc_id)
        return self._json({'text': result.get('ocr', ''), 'meta': result.get('ocr_meta')})

    def _ocr_retry(self, data):
        """Re-queue a document for OCR (the background worker picks it up)."""
        try:
            doc_id = int(data['id'])
        except (KeyError, ValueError, TypeError):
            return self._error(400, 'a valid document id is required')
        if not any(d['id'] == doc_id for d in load_documents()):
            return self._error(404, 'document not found')
        set_ocr_status(doc_id, 'pending')
        return self._json({'ok': True, 'status': 'pending'})

    def _extract_document(self, data):
        """Send OCR text to Qwen with a system prompt chosen by document type."""
        return self._llm_extract(data, engine='qwen')

    def _llm_extract(self, data, engine):
        """Run the per-document-type Qwen extraction (Markdown -> JSON) and store
        the result under its key. (The former GPT auditor step was replaced by the
        code-level validator in validate.py — see _validate_document.)"""
        doc_type = str(data.get('docType', '') or '').strip()
        text = str(data.get('text', '') or '').strip()
        doc_id = data.get('id')
        if not text:
            return self._error(400, 'no OCR text provided to extract')

        import doc_prompts
        system = doc_prompts.get_system_prompt(doc_type, engine)
        if not system:
            return self._json({'response': None,
                               'error': f'No {engine} system prompt configured for document type "{doc_type}".'})
        try:
            # Chunked when the transcript spans multiple pages (a large invoice
            # would otherwise overflow the output-token limit); single request
            # otherwise. See _extract_invoice / _merge_invoice_jsons.
            response, meta = _extract_invoice(system, text)
        except Exception as e:  # model unreachable / error — report, don't crash
            return self._json({'response': None, 'error': f'{engine} error: {e}'})
        if doc_id is not None:
            save_result_field(doc_id, engine, response)
            # Real token usage + measured time for this step (shown in the UI).
            save_result_field(doc_id, f'{engine}_meta', meta)
        return self._json({'response': response, 'meta': meta})

    def _validate_document(self, data):
        """Code-level validation of the extracted invoice JSON (replaces the GPT
        auditor). Reads the stored Qwen JSON (or an inline 'json' payload), runs
        validate.validate_invoice, stores the report, and returns it. The report
        is display-only — it never changes the extracted values."""
        import validate as _validate
        doc_id = data.get('id')
        raw = data.get('json')
        if raw in (None, '') and doc_id is not None:
            raw = (load_result(doc_id) or {}).get('qwen')
        parsed = raw if isinstance(raw, dict) else _parse_json_blob(raw)
        if parsed is None:
            return self._json({'report': None,
                               'error': 'no Qwen JSON available to validate'})
        report = _validate.validate_invoice(parsed)
        if doc_id is not None:
            save_result_field(doc_id, 'validation', json.dumps(report))
        return self._json({'report': report})

    def _whatsapp_webhook(self, body):
        """Inbound Twilio WhatsApp message. Saves any attached files (e.g. PDFs)
        to uploads/ and records each as a document. Always replies 200 + TwiML
        so Twilio doesn't retry."""
        form = parse_qs(body)
        def field(key):
            return form.get(key, [''])[0]

        try:
            num_media = int(field('NumMedia') or 0)
        except ValueError:
            num_media = 0
        caption = field('Body').strip()

        os.makedirs(UPLOADS_DIR, exist_ok=True)
        saved = 0
        for i in range(num_media):
            url = field(f'MediaUrl{i}')
            ctype = field(f'MediaContentType{i}')  # e.g. 'application/pdf'
            if not url:
                continue
            try:
                content = download_media(url)
            except Exception as e:
                print(f'WhatsApp: failed to download media {i}: {e}')
                continue

            subtype = ctype.split('/')[-1] if '/' in ctype else (ctype or 'bin')
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            stored = _safe_filename(f'wa_{stamp}_{i}.{subtype}')
            with open(os.path.join(UPLOADS_DIR, stored), 'wb') as f:
                f.write(content)

            # Document name: the WhatsApp caption if any, else the stored name.
            name = caption or stored
            add_document(name, subtype, file=f'uploads/{stored}', source='whatsapp')
            saved += 1

        print(f'WhatsApp webhook: {saved}/{num_media} media saved (from {field("From")})')
        return self._twiml()

    def _twiml(self):
        """Empty TwiML reply — tells Twilio the message was handled."""
        body = b'<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/xml; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status=200):
        body = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        self._json({'error': message}, status=status)


if __name__ == '__main__':
    os.chdir(ROOT)
    os.makedirs(UPLOADS_DIR, exist_ok=True)  # where WhatsApp PDFs land
    os.makedirs(RESULTS_DIR, exist_ok=True)  # where OCR/Qwen/GPT results land
    init_db()  # create Postgres tables on first hosted boot (no-op in CSV mode)

    # Background Gmail poller: automatically pulls invoices from allowed senders.
    # Interval overridable via GMAIL_POLL_SECONDS (default 10s).
    gmail_poll_seconds = int(os.environ.get('GMAIL_POLL_SECONDS', '10'))

    def _gmail_loop():
        while True:
            res = poll_gmail()
            if res.get('ingested'):
                print(f'Gmail: ingested {res["ingested"]} attachment(s)')
            elif res.get('error') and res['error'] != 'Gmail not configured':
                print(f'Gmail poll: {res["error"]}')
            time.sleep(gmail_poll_seconds)

    try:
        import gmail_client
        if gmail_client.is_configured():
            threading.Thread(target=_gmail_loop, daemon=True).start()
            print(f'Gmail poller started (every {gmail_poll_seconds}s)')
    except Exception as e:
        print('Gmail poller not started:', e)

    # Background auto-OCR: OCR every document that arrives, as soon as it arrives.
    threading.Thread(target=_auto_ocr_loop, daemon=True).start()
    print('Auto-OCR worker started')

    # When PORT is set (e.g. on Render/Fly/Railway) bind publicly; otherwise
    # only bind to localhost for local development.
    port = int(os.environ.get('PORT', '3000'))
    host = '0.0.0.0' if os.environ.get('PORT') else 'localhost'
    display_host = 'localhost' if host == 'localhost' else host
    print(f'Serving http://{display_host}:{port}  (Ctrl+C to stop)')
    ThreadingHTTPServer((host, port), Handler).serve_forever()
