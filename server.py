"""Small static + JSON API server backing index.html with CSV persistence."""

import csv
import json
import os
from datetime import datetime
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR lets a hosted deploy point at a persistent disk; defaults to this folder.
DATA_DIR = os.environ.get('DATA_DIR', ROOT)
ITEMS_CSV = os.path.join(DATA_DIR, 'items.csv')
INVOICES_CSV = os.path.join(DATA_DIR, 'invoices.csv')

# Optional shared secret. When API_SECRET is set (e.g. in production), every
# write request must send a matching "X-API-Key" header. Unset locally => open,
# so your existing local setup keeps working unchanged.
API_SECRET = os.environ.get('API_SECRET')

ITEMS_HEADER = ['id', 'name', 'qty', 'price']
# Original 4 columns kept first (backward compatible); customer/status fields appended.
INVOICES_HEADER = ['number', 'item_name', 'qty', 'price',
                   'customer_name', 'customer_email', 'due_date', 'status', 'notes']

ALLOWED_STATUSES = ('unpaid', 'paid', 'overdue')
DEFAULT_STATUS = 'unpaid'


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


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/api/items':
            return self._json(load_items())
        if path == '/api/invoices':
            return self._json(load_invoices())
        return super().do_GET()

    def _authorized(self):
        """Allow if no secret is configured (local), else require a matching key."""
        if not API_SECRET:
            return True
        return self.headers.get('X-API-Key') == API_SECRET

    def do_POST(self):
        if not self._authorized():
            return self._error(401, 'unauthorized: missing or wrong X-API-Key')
        path = urlparse(self.path).path
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length).decode('utf-8') if length else ''
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
        return self._error(404, 'not found')

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
    init_db()  # create Postgres tables on first hosted boot (no-op in CSV mode)
    # When PORT is set (e.g. on Render/Fly/Railway) bind publicly; otherwise
    # only bind to localhost for local development.
    port = int(os.environ.get('PORT', '3000'))
    host = '0.0.0.0' if os.environ.get('PORT') else 'localhost'
    display_host = 'localhost' if host == 'localhost' else host
    print(f'Serving http://{display_host}:{port}  (Ctrl+C to stop)')
    ThreadingHTTPServer((host, port), Handler).serve_forever()
