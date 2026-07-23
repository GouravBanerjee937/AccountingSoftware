"""Minimal client for the Qwen model on an OpenAI-compatible server (vLLM).

Talks to the OpenAI /v1/chat/completions endpoint. qwen3.5 is a reasoning model,
so any stray <think>...</think> block is stripped from the reply. A large
max_tokens is used so full invoice extractions aren't truncated.
"""

import json
import os
import re
import time
import urllib.request


def usage_meta(data, seconds):
    """Build a metrics dict from a chat-completions response's real `usage`
    block plus the measured wall-clock seconds. Token fields are None (not
    guessed) if the server didn't return usage."""
    u = (data or {}).get('usage') or {}
    return {
        'prompt_tokens': u.get('prompt_tokens'),
        'completion_tokens': u.get('completion_tokens'),
        'total_tokens': u.get('total_tokens'),
        'seconds': seconds,
    }

QWEN_URL = os.environ.get('QWEN_URL', 'http://164.52.193.161:8001/v1')
QWEN_MODEL = os.environ.get('QWEN_MODEL', 'Qwen/Qwen3.5-9B')
# Max output tokens — big enough for a full multi-item extraction JSON.
QWEN_MAX_TOKENS = int(os.environ.get('QWEN_MAX_TOKENS', '16384'))

_THINK_RE = re.compile(r'<think>.*?</think>', re.DOTALL | re.IGNORECASE)


def _chat_completions_url():
    """Build the /v1/chat/completions URL from QWEN_URL (which normally ends in /v1)."""
    u = QWEN_URL.rstrip('/')
    if u.endswith('/chat/completions'):
        return u
    if not u.endswith('/v1'):
        u = u + '/v1'
    return u + '/chat/completions'


def chat_with_usage(system, user, temperature=0):
    """Like chat(), but returns (text, meta) where meta carries the real token
    usage from the response and the measured request time in seconds."""
    body = json.dumps({
        'model': QWEN_MODEL,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
        'temperature': temperature,
        'max_tokens': QWEN_MAX_TOKENS,
        'stream': False,
    }).encode('utf-8')

    req = urllib.request.Request(
        _chat_completions_url(),
        data=body,
        headers={'Content-Type': 'application/json'},
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=900) as resp:
        data = json.loads(resp.read())
    seconds = round(time.monotonic() - t0, 2)

    content = data['choices'][0]['message']['content']
    text = _THINK_RE.sub('', content or '').strip()
    return text, usage_meta(data, seconds)


def chat(system, user, temperature=0):
    """Send a system + user message to Qwen and return the assistant text.
    Any stray <think>...</think> block is stripped."""
    return chat_with_usage(system, user, temperature)[0]
