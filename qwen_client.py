"""Minimal client for the Qwen (Ollama, OpenAI-compatible) chat endpoint."""

import json
import os
import re
import urllib.request

QWEN_URL = os.environ.get('QWEN_URL', 'http://164.52.211.30:11434/v1')
QWEN_MODEL = os.environ.get('QWEN_MODEL', 'qwen3.5:9b')

_THINK_RE = re.compile(r'<think>.*?</think>', re.DOTALL | re.IGNORECASE)


def chat(system, user, temperature=0):
    """Send a system + user message to Qwen and return the assistant text.
    Any <think>...</think> reasoning block is stripped from the result."""
    body = json.dumps({
        'model': QWEN_MODEL,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
        'temperature': temperature,
        'stream': False,
    }).encode('utf-8')

    req = urllib.request.Request(
        QWEN_URL.rstrip('/') + '/chat/completions',
        data=body,
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())

    content = data['choices'][0]['message']['content']
    return _THINK_RE.sub('', content).strip()
