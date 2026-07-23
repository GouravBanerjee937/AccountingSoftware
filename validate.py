"""Code-level validation of an extracted invoice JSON (replaces the GPT auditor).

validate_invoice(data) -> {
  'is_valid': bool,
  'errors':   [ {'field': <path>, 'issue': <message>, 'expected'?, 'actual'?} ],
  'summary':  <str>,
}

The report is DISPLAY-ONLY. It never modifies the extracted values — the form
textboxes are always populated from the Qwen JSON. Each issue is shown as a
suggestion next to the key it concerns.

Rules (against prompts_vlm/sales_purchase_json.txt field paths):
  1. line_items[]: quantity > 0; rate, tax_amount, amount >= 0.
  2. line_items[].discount in 0..100.
  3. summary.grand_total > 0.
  4. tax_tables[]: cgst_amount, sgst_amount, igst_amount >= 0;
     IGST and CGST/SGST must not both be present in one row (inter- vs intra-state);
     cgst_amount == sgst_amount and cgst_percentage == sgst_percentage.
  5. grand_total == sub_total + tax_total + other_charges + round_off,
     where tax_total = Sum(cgst_amount + sgst_amount + igst_amount).
  6. reverse_charge in {Yes, Y}  ->  grand_total excludes tax_total.

Money comparisons are EXACT at 2 decimals (paisa), tolerant only of float noise.
"""

import re

_MONEY_RE = re.compile(r'[^0-9.\-]')


def _num(v):
    """Coerce to float, or None when not numeric. Handles '1,234.50', '₹200', ''."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = _MONEY_RE.sub('', v.strip())
        if s in ('', '-', '.', '-.'):
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _r2(x):
    """Round to 2 decimals (money precision)."""
    return round(float(x), 2)


def _eq(a, b):
    """Exact money equality at 2 decimals — absorbs float representation noise only."""
    return _r2(a) == _r2(b)


def _money(x):
    """Format a number as a 2-decimal money string, e.g. 1180.0 -> '1180.00'."""
    return f'{_r2(x):.2f}'


def _item_label(item, idx):
    for k in ('item_name', 'description', 'description_detailed'):
        v = item.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    sno = item.get('sno')
    if sno not in (None, ''):
        return f'item #{sno}'
    return f'item #{idx + 1}'


def validate_invoice(data):
    """Run all rules over an extracted invoice dict and return a report."""
    errors = []

    def add(field, issue, expected=None, actual=None):
        e = {'field': field, 'issue': issue}
        if expected is not None:
            e['expected'] = expected
        if actual is not None:
            e['actual'] = actual
        errors.append(e)

    if not isinstance(data, dict):
        return {
            'is_valid': False,
            'errors': [{'field': '', 'issue': 'Could not parse the extracted JSON.'}],
            'summary': 'No structured data to validate.',
        }

    # ---- Rule 1 & 2: line items ----
    line_items = data.get('line_items')
    if isinstance(line_items, list):
        for i, it in enumerate(line_items):
            if not isinstance(it, dict):
                continue
            label = _item_label(it, i)
            base = f'line_items[{i}]'
            q = _num(it.get('quantity'))
            if q is not None and q <= 0:                       # quantity > 0
                add(f'{base}.quantity',
                    f'The {label} · quantity cannot be negative', actual=it.get('quantity'))
            for key in ('rate', 'tax_amount', 'amount'):        # >= 0
                v = _num(it.get(key))
                if v is not None and v < 0:
                    add(f'{base}.{key}',
                        f'The {label} · {key} cannot be negative', actual=it.get(key))
            d = _num(it.get('discount'))                        # 0..100
            if d is not None and (d < 0 or d > 100):
                add(f'{base}.discount',
                    f'The {label} · discount must be between 0 and 100', actual=it.get('discount'))

    # ---- Rule 3: grand_total > 0 ----
    summary = data.get('summary')
    if not isinstance(summary, dict):
        summary = {}
    gt = _num(summary.get('grand_total'))
    if gt is not None and gt <= 0:
        add('summary.grand_total', 'The grand total cannot be negative',
            actual=summary.get('grand_total'))

    # ---- Rule 4: tax_tables ----
    tax_total = 0.0
    tax_tables = data.get('tax_tables')
    if isinstance(tax_tables, list):
        for j, tt in enumerate(tax_tables):
            if not isinstance(tt, dict):
                continue
            base = f'tax_tables[{j}]'
            cg, sg, ig = _num(tt.get('cgst_amount')), _num(tt.get('sgst_amount')), _num(tt.get('igst_amount'))
            tax_total += (cg or 0) + (sg or 0) + (ig or 0)
            for key, v in (('cgst_amount', cg), ('sgst_amount', sg), ('igst_amount', ig)):
                if v is not None and v < 0:
                    add(f'{base}.{key}', f'The {key} cannot be negative', actual=tt.get(key))
            has_igst = (ig or 0) > 0
            has_cgst_sgst = (cg or 0) > 0 or (sg or 0) > 0
            if has_igst and has_cgst_sgst:
                add(base, 'IGST and CGST/SGST cannot both be present in the same tax row')
            if has_cgst_sgst:                                   # cgst must equal sgst
                if cg is not None and sg is not None and not _eq(cg, sg):
                    add(f'{base}.cgst_amount',
                        'The cgst_amount must be equal to the sgst_amount',
                        expected=_r2(sg), actual=_r2(cg))
                cgp, sgp = _num(tt.get('cgst_percentage')), _num(tt.get('sgst_percentage'))
                if cgp is not None and sgp is not None and cgp != sgp:
                    add(f'{base}.cgst_percentage',
                        'The cgst_percentage must be equal to the sgst_percentage',
                        expected=sgp, actual=cgp)

    # ---- Rule 5 & 6: grand_total reconciliation ----
    other_total = 0.0
    other_charges = data.get('other_charges')
    if isinstance(other_charges, list):
        for oc in other_charges:
            if isinstance(oc, dict):
                other_total += _num(oc.get('amount')) or 0
    sub_total = _num(summary.get('sub_total')) or 0
    round_off = _num(summary.get('round_off')) or 0
    reverse = str(data.get('reverse_charge') or '').strip().lower() in ('yes', 'y')
    if gt is not None:
        expected_gt = sub_total + other_total + round_off + (0.0 if reverse else tax_total)
        if not _eq(gt, expected_gt):
            if reverse:
                add('summary.grand_total',
                    f'The grand total value must be {_money(expected_gt)}',
                    expected=_r2(expected_gt), actual=_r2(gt))
            else:
                expected_tax = gt - sub_total - other_total - round_off
                add('summary.grand_total',
                    f'The grand total value must be {_money(expected_gt)} '
                    f'— or the tax total value must be {_money(expected_tax)}',
                    expected=_r2(expected_gt), actual=_r2(gt))

    n = len(errors)
    return {
        'is_valid': n == 0,
        'errors': errors,
        'summary': ('All checks passed.' if n == 0 else
                    f'{n} issue{"" if n == 1 else "s"} found — values left as extracted; review the suggestions.'),
    }
