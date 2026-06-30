"""Per-document-type system prompts for the Qwen extraction step.

Each document type maps to a prompt file under prompts/. Add more files and
entries here as you create prompts for the other document types.
"""

import os

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompts')

# Map a Documents-page type -> its system-prompt filename.
_FILES = {
    'Purchase Invoice': 'purchase_invoice.txt',
    # 'Sales Invoice':    'sales_invoice.txt',
    # 'Sales Return':     'sales_return.txt',
    # 'Purchase Return':  'purchase_return.txt',
    # 'Credit Note':      'credit_note.txt',
    # 'Debit Note':       'debit_note.txt',
}


def get_system_prompt(doc_type):
    """Return the system prompt text for a document type, or None if there
    isn't one configured / the file is missing."""
    fname = _FILES.get((doc_type or '').strip())
    if not fname:
        return None
    path = os.path.join(_DIR, fname)
    if not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8') as f:
        return f.read()
