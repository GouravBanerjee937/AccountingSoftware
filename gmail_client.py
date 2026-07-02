"""Gmail client (stdlib only) for the 'system inbox' that receives invoices.

Auth uses an installed-app OAuth2 refresh token. Configure via env:
  GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN

Scope required when generating the refresh token:
  https://www.googleapis.com/auth/gmail.modify
(read messages + download attachments + apply a label so each email is
ingested only once).
"""

import base64
import json
import os
import urllib.parse
import urllib.request

CLIENT_ID = os.environ.get('GMAIL_CLIENT_ID')
CLIENT_SECRET = os.environ.get('GMAIL_CLIENT_SECRET')
REFRESH_TOKEN = os.environ.get('GMAIL_REFRESH_TOKEN')

API = 'https://gmail.googleapis.com/gmail/v1'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
INGEST_LABEL = 'AccountingIngested'   # marks already-processed emails


def is_configured():
    return bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN)


def get_access_token():
    if not is_configured():
        raise RuntimeError('Gmail not configured (set GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN)')
    data = urllib.parse.urlencode({
        'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET,
        'refresh_token': REFRESH_TOKEN, 'grant_type': 'refresh_token',
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data,
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())['access_token']


def _get(token, path):
    req = urllib.request.Request(API + path, headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _post(token, path, body):
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + token,
                                          'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def get_address(token):
    """The connected mailbox's email address (the 'system inbox')."""
    return _get(token, '/users/me/profile').get('emailAddress')


def list_message_ids(token, query):
    q = urllib.parse.quote(query)
    d = _get(token, f'/users/me/messages?q={q}&maxResults=25')
    return [m['id'] for m in d.get('messages', [])]


def get_message(token, msg_id):
    return _get(token, f'/users/me/messages/{msg_id}?format=full')


def get_attachment_bytes(token, msg_id, att_id):
    d = _get(token, f'/users/me/messages/{msg_id}/attachments/{att_id}')
    return base64.urlsafe_b64decode(d.get('data', '') + '===')


def ensure_label(token, name=INGEST_LABEL):
    for lbl in _get(token, '/users/me/labels').get('labels', []):
        if lbl.get('name') == name:
            return lbl['id']
    created = _post(token, '/users/me/labels',
                    {'name': name, 'labelListVisibility': 'labelShow',
                     'messageListVisibility': 'show'})
    return created['id']


def add_label(token, msg_id, label_id):
    _post(token, f'/users/me/messages/{msg_id}/modify', {'addLabelIds': [label_id]})


def iter_attachments(message):
    """Yield (filename, mime_type, attachment_id) for every part with a file."""
    def walk(part):
        body = part.get('body', {})
        filename = part.get('filename') or ''
        att_id = body.get('attachmentId')
        if filename and att_id:
            yield (filename, part.get('mimeType', ''), att_id)
        for p in (part.get('parts') or []):
            yield from walk(p)
    yield from walk(message.get('payload', {}))
