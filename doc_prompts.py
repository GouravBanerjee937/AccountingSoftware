"""Per-document-type system prompts for the Qwen and GPT extraction steps.

Each document type maps to TWO prompt files:
  - a Qwen markdown->JSON extraction prompt (engine='qwen'), under prompts_vlm/
  - a GPT auditor prompt                    (engine='gpt'),  under prompts/

The pipeline is: Qwen VLM(image -> Markdown) -> Qwen(qwen prompt + Markdown -> JSON,
which fills the form) -> GPT(gpt prompt + Markdown + Qwen JSON -> QA validation report).
Add more files and entries here as you create prompts for other document types.
"""

import os

_BASE = os.path.dirname(os.path.abspath(__file__))
# Both passes now live in the VLM prompt folder: the Qwen markdown->JSON
# extraction prompts and the GPT QA-validator prompts.
_QWEN_DIR = os.path.join(_BASE, 'prompts_vlm')
_GPT_DIR = os.path.join(_BASE, 'prompts_vlm')

# Map a Documents-page type -> its Qwen markdown->JSON extraction prompt filename.
_QWEN_FILES = {
    'Sales Invoice':    'sales_purchase_json.txt',
    'Purchase Invoice': 'sales_purchase_json.txt',
    'Sales Return':     'credit_debit_json.txt',
    'Purchase Return':  'credit_debit_json.txt',
    'Credit Note':      'credit_debit_json.txt',
    'Debit Note':       'credit_debit_json.txt',
    'Receipt':          'receipt_json.txt',
    'Payment':          'receipt_json.txt',
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

    engine: 'qwen' for the markdown->JSON extraction prompt (prompts_vlm/),
            'gpt'  for the auditor prompt (prompts/).
    """
    if engine == 'gpt':
        files, directory = _GPT_FILES, _GPT_DIR
    else:
        files, directory = _QWEN_FILES, _QWEN_DIR
    fname = files.get((doc_type or '').strip())
    if not fname:
        return None
    path = os.path.join(directory, fname)
    if not os.path.isfile(path):
        return None
    with open(path, encoding='utf-8') as f:
        return f.read()
