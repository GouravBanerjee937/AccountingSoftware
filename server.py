"""Small static + JSON API server backing index.html with CSV persistence."""

import base64
import csv
import json
import os
import re
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
DOCUMENTS_HEADER = ['id', 'upload_datetime', 'name', 'doc_type', 'file']

ALLOWED_STATUSES = ('unpaid', 'paid', 'overdue')
DEFAULT_STATUS = 'unpaid'

# Business document categories the user can classify a document as.
DOC_TYPES = ('Sales Invoice', 'Purchase Invoice', 'Sales Return',
             'Purchase Return', 'Credit Note', 'Debit Note')


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
            file TEXT NOT NULL DEFAULT '')""")


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
            cur.execute("""SELECT id, upload_datetime, name, doc_type, file
                           FROM documents ORDER BY id""")
            return [{'id': r[0], 'uploadDateTime': r[1] or '',
                     'name': r[2] or '', 'docType': r[3] or '', 'file': r[4] or ''}
                    for r in cur.fetchall()]
    return [{'id': int(r['id']), 'uploadDateTime': r.get('upload_datetime', ''),
             'name': r.get('name', ''), 'docType': r.get('doc_type', ''),
             'file': r.get('file', '')}
            for r in read_csv(DOCUMENTS_CSV, DOCUMENTS_HEADER)]


def save_documents(documents):
    if USE_PG:
        with _pg() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM documents")
            cur.executemany(
                """INSERT INTO documents (id, upload_datetime, name, doc_type, file)
                   VALUES (%s, %s, %s, %s, %s)""",
                [(d['id'], d['uploadDateTime'], d['name'], d['docType'],
                  d.get('file', '')) for d in documents])
        return
    rows = [{'id': d['id'], 'upload_datetime': d['uploadDateTime'],
             'name': d['name'], 'doc_type': d['docType'],
             'file': d.get('file', '')} for d in documents]
    write_csv(DOCUMENTS_CSV, DOCUMENTS_HEADER, rows)


def _result_path(doc_id):
    return os.path.join(RESULTS_DIR, f'{int(doc_id)}.json')


def load_result(doc_id):
    """Return the stored {ocr, qwen, gpt} dict for a document (empty if none)."""
    path = _result_path(doc_id)
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_result_field(doc_id, key, value):
    """Merge one result field (ocr/qwen/gpt) into the document's result file."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    data = load_result(doc_id)
    data[key] = value
    with open(_result_path(doc_id), 'w', encoding='utf-8') as f:
        json.dump(data, f)


def add_document(name, doc_type, file=''):
    """Append one document row. Upload date/time is stamped here, server-side.
    Shared by the manual 'Add Document' form and the WhatsApp webhook."""
    documents = load_documents()
    next_id = max((d['id'] for d in documents), default=0) + 1
    new_doc = {'id': next_id,
               'uploadDateTime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
               'name': name, 'docType': doc_type, 'file': file}
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
            return self._json(load_documents())
        if path == '/api/documents/result':
            qs = parse_qs(urlparse(self.path).query)
            try:
                doc_id = int(qs.get('id', [''])[0])
            except ValueError:
                return self._error(400, 'invalid id')
            return self._json(load_result(doc_id))
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
        if path == '/api/documents/extract':
            return self._extract_document(data)
        if path == '/api/documents/extract-gpt':
            return self._extract_gpt(data)
        if path == '/api/documents/commit':
            return self._commit_documents(data)
        return self._error(404, 'not found')

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

    # File extensions accepted by the Scan upload (PDFs and images).
    _UPLOAD_EXTS = {'.pdf', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff'}

    def _upload_file(self, raw):
        """Stage one uploaded file (PDF/image) into uploads/. Does NOT create a
        document row yet — that happens on commit. Returns per-file status."""
        qs = parse_qs(urlparse(self.path).query)
        name = (qs.get('name', [''])[0] or 'file').strip()
        ctype = qs.get('type', [''])[0]
        ext = os.path.splitext(name)[1].lower()
        is_ok_type = (ext in self._UPLOAD_EXTS
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

    def _commit_documents(self, data):
        """Create document rows for the successfully-uploaded (staged) files."""
        doc_type = str(data.get('docType', '') or '').strip()
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
            created.append(add_document(name, doc_type, file=rel))
        return self._json({'created': created}, status=201)

    def _ocr_document(self, data):
        """Run OCR on a document's stored file via the PP-OCRv5 demo."""
        try:
            doc_id = int(data['id'])
        except (KeyError, ValueError, TypeError):
            return self._error(400, 'a valid document id is required')
        documents = load_documents()
        doc = next((d for d in documents if d['id'] == doc_id), None)
        if doc is None:
            return self._error(404, 'document not found')
        rel = doc.get('file', '')
        if not rel:
            return self._error(400, 'this document has no file to OCR')
        fpath = os.path.join(DATA_DIR, rel)
        if not os.path.isfile(fpath):
            return self._error(404, 'file not found on server')
        try:
            import paddle_ocr
            text = paddle_ocr.ocr_file(fpath)
        except Exception as e:  # cold start / queue / network — report, don't crash
            return self._json({'text': None, 'error': f'OCR service error: {e}'})
        save_result_field(doc_id, 'ocr', text)
        return self._json({'text': text})

    def _extract_document(self, data):
        """Send OCR text to Qwen with a system prompt chosen by document type."""
        return self._llm_extract(data, engine='qwen')

    def _extract_gpt(self, data):
        """Send OCR text to gpt-4.1-mini with the same per-type system prompt."""
        return self._llm_extract(data, engine='gpt')

    def _llm_extract(self, data, engine):
        """Shared extraction: pick the system prompt by document type, run the
        chosen engine on the OCR text, store the result under its key."""
        doc_type = str(data.get('docType', '') or '').strip()
        text = str(data.get('text', '') or '').strip()
        doc_id = data.get('id')
        if not text:
            return self._error(400, 'no OCR text provided to extract')

        import doc_prompts
        system = doc_prompts.get_system_prompt(doc_type)
        if not system:
            return self._json({'response': None,
                               'error': f'No system prompt configured for document type "{doc_type}".'})
        try:
            if engine == 'gpt':
                import gpt_client
                response = gpt_client.chat(system, text)
            else:
                import qwen_client
                response = qwen_client.chat(system, text)
        except Exception as e:  # model unreachable / error — report, don't crash
            return self._json({'response': None, 'error': f'{engine} error: {e}'})
        if doc_id is not None:
            save_result_field(doc_id, engine, response)
        return self._json({'response': response})

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
            add_document(name, subtype, file=f'uploads/{stored}')
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
    # When PORT is set (e.g. on Render/Fly/Railway) bind publicly; otherwise
    # only bind to localhost for local development.
    port = int(os.environ.get('PORT', '3000'))
    host = '0.0.0.0' if os.environ.get('PORT') else 'localhost'
    display_host = 'localhost' if host == 'localhost' else host
    print(f'Serving http://{display_host}:{port}  (Ctrl+C to stop)')
    ThreadingHTTPServer((host, port), Handler).serve_forever()
