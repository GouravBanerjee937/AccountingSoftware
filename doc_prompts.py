"""Per-document-type system prompts for the Qwen and GPT extraction steps.

Each document type maps to TWO prompt files under prompts/:
  - a Qwen extraction prompt (engine='qwen')
  - a GPT auditor prompt      (engine='gpt')

The pipeline is: OCR -> Qwen(qwen prompt + OCR) -> GPT(gpt prompt + OCR + Qwen output).
Add more files and entries here as you create prompts for other document types.
"""

import os

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompts')

# Map a Documents-page type -> its Qwen extraction system-prompt filename.
_QWEN_FILES = {
    'Sales Invoice':    'sales_purchase_qwen.txt',
    'Purchase Invoice': 'sales_purchase_qwen.txt',
    'Sales Return':     'credit_debit_qwen.txt',
    'Purchase Return':  'credit_debit_qwen.txt',
    'Credit Note':      'credit_debit_qwen.txt',
    'Debit Note':       'credit_debit_qwen.txt',
    'Receipt':          'receipt_qwen.txt',
    'Payment':          'receipt_qwen.txt',
}

# Map a Documents-page type -> its GPT auditor system-prompt filename.
_GPT_FILES = {
    'Sales Invoice':    'sales_purchase_gpt.txt',
    'Purchase Invoice': 'sales_purchase_gpt.txt',
    'Sales Return':     'credit_debit_gpt.txt',
    'Purchase Return':  'credit_debit_gpt.txt',
    'Credit Note':      'credit_debit_gpt.txt',
    'Debit Note':       'credit_debit_gpt.txt',
    'Receipt':          'receipt_gpt.txt',
    'Payment':          'receipt_gpt.txt',
}


def get_system_prompt(doc_type, engine='qwen'):
    """Return the system prompt text for a document type + engine, or None if
    there isn't one configured / the file is missing.

    engine: 'qwen' for the extraction prompt, 'gpt' for the auditor prompt.
    """
    files = _GPT_FILES if engine == 'gpt' else _QWEN_FILES
    fname = files.get((doc_type or '').strip())
    if not fname:
        return None
    path = os.path.join(_DIR, fname)
    if not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8') as f:
        return f.read()
