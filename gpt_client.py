"""Minimal client for OpenAI gpt-4.1-mini chat completions."""

import json
import os
import time
import urllib.request

OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
# A dedicated default so this isn't affected by OPENAI_MODEL used elsewhere.
GPT_MODEL = os.environ.get('GPT_DOC_MODEL', 'gpt-4.1-mini')


def chat_with_usage(system, user, temperature=0):
    """Like chat(), but returns (text, meta) where meta carries the real token
    usage from the OpenAI response and the measured request time in seconds."""
    if not OPENAI_API_KEY:
        raise RuntimeError('OPENAI_API_KEY is not set')
    body = json.dumps({
        'model': GPT_MODEL,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
        'temperature': temperature,
    }).encode('utf-8')

    req = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        data=body,
        headers={
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + OPENAI_API_KEY,
        },
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read())
    seconds = round(time.monotonic() - t0, 2)
    u = data.get('usage') or {}
    meta = {
        'prompt_tokens': u.get('prompt_tokens'),
        'completion_tokens': u.get('completion_tokens'),
        'total_tokens': u.get('total_tokens'),
        'seconds': seconds,
    }
    return data['choices'][0]['message']['content'].strip(), meta


def chat(system, user, temperature=0):
    """Send a system + user message to gpt-4.1-mini and return the text."""
    return chat_with_usage(system, user, temperature)[0]
