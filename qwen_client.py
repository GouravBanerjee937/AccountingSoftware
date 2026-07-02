"""Minimal client for the Qwen model on Ollama.

Uses Ollama's NATIVE /api/chat endpoint with thinking disabled (`think: false`)
and an explicit context window (`num_ctx`). This matches the tuned extraction
pipeline: qwen3.5:9b is a reasoning model, and leaving thinking ON makes a full
invoice extraction take many minutes (it times out); the default 2048-token
context also silently truncates the large system prompt. Both are fixed here.
"""

import json
import os
import re
import urllib.request

QWEN_URL = os.environ.get('QWEN_URL', 'http://164.52.211.30:11434/v1')
QWEN_MODEL = os.environ.get('QWEN_MODEL', 'qwen3.5:9b')
# Context window — must be large enough for the (18-38 KB) system prompt + OCR.
QWEN_NUM_CTX = int(os.environ.get('QWEN_NUM_CTX', '40960'))

_THINK_RE = re.compile(r'<think>.*?</think>', re.DOTALL | re.IGNORECASE)


def _api_chat_url():
    """Derive Ollama's native /api/chat URL from QWEN_URL (which may end in /v1)."""
    u = QWEN_URL.rstrip('/')
    if u.endswith('/api/chat'):
        return u
    if u.endswith('/v1'):
        u = u[:-3].rstrip('/')
    return u + '/api/chat'


def chat(system, user, temperature=0):
    """Send a system + user message to Qwen and return the assistant text.
    Thinking is disabled; any stray <think>...</think> block is stripped."""
    body = json.dumps({
        'model': QWEN_MODEL,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
        'stream': False,
        'think': False,
        'keep_alive': '10m',
        'options': {'temperature': temperature, 'num_ctx': QWEN_NUM_CTX},
    }).encode('utf-8')

    req = urllib.request.Request(
        _api_chat_url(),
        data=body,
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())

    content = data['message']['content']
    return _THINK_RE.sub('', content).strip()
