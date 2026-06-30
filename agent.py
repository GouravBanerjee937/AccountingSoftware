"""In-app assistant brain.

Tool sources, all bridged to OpenAI's function-calling:
  1) Accounting — local Python functions (this app's own API).
  2) Razorpay — the official Razorpay MCP server (stdio).
  3) Shopify  — the shopify-mcp server (stdio), auto-enabled when store creds exist.

Each MCP server is connected as a real MCP client; its tools are loaded
dynamically and merged into one menu for the model.

Requires: OPENAI_API_KEY (+ the relevant service creds), the `openai` and
`mcp` packages.
"""

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client

# Accounting tools live in the sibling invoice-mcp project.
_INVOICE_MCP_DIR = os.environ.get(
    "INVOICE_MCP_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "invoice-mcp"),
)
if _INVOICE_MCP_DIR not in sys.path:
    sys.path.insert(0, _INVOICE_MCP_DIR)
import invoice_server as iv  # noqa: E402

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5")
RZP_BIN = os.environ.get("RAZORPAY_MCP_BIN", "/Users/gourav/Work/razorpay-mcp-bin/razorpay-mcp-server")
SHOPIFY_JS = os.environ.get(
    "SHOPIFY_MCP_JS",
    "/Users/gourav/Work/shopify-mcp-pkg/node_modules/shopify-mcp/dist/index.js",
)
QBO_JS = os.environ.get(
    "QUICKBOOKS_MCP_JS",
    "/Users/gourav/Work/quickbooks-online-mcp-server/dist/index.js",
)
# The original (community) Zoho Books MCP server — pip package "zoho-books-mcp"
# (github.com/kkeeling/zoho-mcp), installed in its own venv at ../zoho-mcp with a
# pinned mcp version, launched over stdio.
ZOHO_MCP_BIN = os.environ.get(
    "ZOHO_MCP_BIN", "/Users/gourav/Work/zoho-mcp/.venv/bin/zoho-books-mcp")
# The official PayPal MCP server — npm package "@paypal/mcp", launched over stdio.
PAYPAL_JS = os.environ.get(
    "PAYPAL_MCP_JS",
    "/Users/gourav/Work/paypal-mcp-pkg/node_modules/@paypal/mcp/dist/index.js",
)
# The official TaxBandits MCP server — a REMOTE/hosted server over SSE (HTTP), not
# a local stdio process. Sandbox base by default; override for production.
TAXBANDITS_SSE_URL = os.environ.get(
    "TAXBANDITS_MCP_URL", "https://testapi-aimcp.taxbandits.com/sse")


def _is_set(v):
    return bool(v) and "REPLACE" not in v


def _razorpay_server():
    kid, sec = os.environ.get("RAZORPAY_KEY_ID"), os.environ.get("RAZORPAY_KEY_SECRET")
    if _is_set(kid) and _is_set(sec):
        return StdioServerParameters(
            command=RZP_BIN, args=["stdio", "--key", kid, "--secret", sec], env=dict(os.environ))
    return None


def _shopify_server():
    domain = os.environ.get("MYSHOPIFY_DOMAIN")
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN")
    cid, csec = os.environ.get("SHOPIFY_CLIENT_ID"), os.environ.get("SHOPIFY_CLIENT_SECRET")
    if not _is_set(domain) or not os.path.exists(SHOPIFY_JS):
        return None
    args = [SHOPIFY_JS, "--domain", domain]
    if _is_set(token):
        args += ["--accessToken", token]
    elif _is_set(cid) and _is_set(csec):
        args += ["--clientId", cid, "--clientSecret", csec]
    else:
        return None
    return StdioServerParameters(command="node", args=args, env=dict(os.environ))


def _qbo_env_configured() -> bool:
    """The QuickBooks server reads its own .env (via dotenv). Only start it when
    that .env has real values, not the PASTE_..._HERE placeholders."""
    env_path = os.path.join(os.path.dirname(QBO_JS), "..", ".env")
    if not os.path.exists(env_path):
        return False
    needed = {"QUICKBOOKS_CLIENT_ID", "QUICKBOOKS_CLIENT_SECRET",
              "QUICKBOOKS_REALM_ID", "QUICKBOOKS_REFRESH_TOKEN"}
    found = set()
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                placeholder = (not v) or any(
                    p in v for p in ("REPLACE", "PASTE", "your_", "will_be_filled"))
                if k in needed and not placeholder:
                    found.add(k)
    except OSError:
        return False
    return needed <= found


def _quickbooks_server():
    if os.path.exists(QBO_JS) and _qbo_env_configured():
        # The server loads its credentials from its own .env; we just launch it.
        return StdioServerParameters(command="node", args=[QBO_JS], env=dict(os.environ))
    return None


def _zohobooks_server():
    needed = (
        os.environ.get("ZOHO_CLIENT_ID"), os.environ.get("ZOHO_CLIENT_SECRET"),
        os.environ.get("ZOHO_REFRESH_TOKEN"), os.environ.get("ZOHO_ORGANIZATION_ID"),
    )
    if os.path.exists(ZOHO_MCP_BIN) and all(_is_set(v) for v in needed):
        return StdioServerParameters(command=ZOHO_MCP_BIN, args=["--stdio"], env=dict(os.environ))
    return None


def _paypal_server():
    token = os.environ.get("PAYPAL_ACCESS_TOKEN")
    environment = os.environ.get("PAYPAL_ENVIRONMENT", "SANDBOX")
    if _is_set(token) and os.path.exists(PAYPAL_JS):
        return StdioServerParameters(
            command="node",
            args=[PAYPAL_JS, f"--access-token={token}", "--tools=all",
                  f"--paypal-environment={environment}"],
            env=dict(os.environ))
    return None


def _taxbandits_server():
    """Remote SSE server. Auth is passed inline in the URL as
    `?authentication=<User_ID>,<MCP_APIKey>` per TaxBandits' MCP docs."""
    uid = os.environ.get("TAXBANDITS_USER_ID")
    apikey = os.environ.get("TAXBANDITS_MCP_APIKEY")
    if _is_set(uid) and _is_set(apikey):
        return {"transport": "sse",
                "url": f"{TAXBANDITS_SSE_URL}?authentication={uid},{apikey}"}
    return None


# --- Accounting tools (local functions) ---
LOCAL_FUNCS = {
    "list_items": iv.list_items,
    "list_invoices": iv.list_invoices,
    "get_invoice": iv.get_invoice,
    "create_invoice": iv.create_invoice,
}

_STR = {"type": "string"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}


def _fn(name, description, properties, required=None):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required or []}}}


LOCAL_SCHEMAS = [
    _fn("list_items", "READ. List local inventory items and stock levels.",
        {"name": _STR, "min_qty": _INT, "max_qty": _INT, "min_price": _NUM, "max_price": _NUM}),
    _fn("list_invoices", "READ. List local invoice line-items.",
        {"status": _STR, "customer": _STR, "item": _STR,
         "min_amount": _NUM, "max_amount": _NUM, "min_qty": _INT, "max_qty": _INT}),
    _fn("get_invoice", "READ. Get all line-items and total for one local invoice number.",
        {"number": _STR}, ["number"]),
    _fn("create_invoice", "WRITE. Create a local invoice line-item and decrement stock.",
        {"number": _STR, "item_name": _STR, "qty": _INT, "customer_name": _STR,
         "price": _NUM, "customer_email": _STR, "due_date": _STR, "status": _STR, "notes": _STR},
        ["number", "item_name", "qty"]),
]

SYSTEM = (
    "You are the assistant built into a small accounting app. You can help across these areas, "
    "each via its own tools:\n"
    "  1) Local accounting — this app's own inventory items and invoices (list_/get_/create_invoice).\n"
    "  2) Razorpay — payments, orders, refunds, payment links, settlements, payouts (fetch_*/create_* tools).\n"
    "  3) Shopify — your online store's products, orders, customers, inventory (get-*/create-* tools).\n"
    "  4) QuickBooks Online — full accounting: customers, invoices, bills, items, payments, and "
    "financial reports (get_*/search_*/create_* tools for each entity).\n"
    "  5) Zoho Books — accounting: organizations, items, contacts (customers/vendors), and invoices "
    "(list_*/get_*/create_invoice tools).\n"
    "  6) TaxBandits — US IRS e-filing: businesses, W-9 collection, and 1099-NEC forms "
    "(create_business, request_w9_by_email/get_w9/list_w9, create_/validate_/transmit_1099_nec). "
    "This is US tax only — it does NOT handle Indian GST.\n\n"
    "Rules:\n"
    "- These are separate books. Keep the local app, QuickBooks, and Zoho Books straight — don't mix "
    "an invoice from one with another. Ask which system the user means if it's ambiguous.\n"
    "- Answer ONLY from tool results. Never invent numbers, ids, or amounts. Relay tool errors plainly.\n"
    "- To COUNT or TOTAL things (products, orders, customers, payments), call the listing tool with a "
    "HIGH limit (e.g. limit 250) and count the actual rows returned. NEVER guess or estimate a count "
    "— state only a number you can literally see in a tool result. If a list might be truncated by a "
    "limit, say so.\n"
    "- Razorpay amounts are in paise (100 = 1 rupee); convert for the user.\n"
    "- Keep Razorpay vs Shopify straight: Razorpay = money/payments; Shopify = the online store "
    "(products, store orders, customers). Don't confuse a Shopify order with a Razorpay order.\n"
    "- Stay on the user's current intent; a bare number in a payment/price context is an amount.\n"
    "- Write URLs as plain bare URLs, never markdown link formatting.\n"
    "- Be concise and clear."
)


def _mcp_text(result) -> str:
    parts = [c.text for c in (getattr(result, "content", []) or []) if getattr(c, "type", None) == "text"]
    return "\n".join(parts) if parts else json.dumps({"ok": True})


def ask(question: str, history=None) -> dict:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key or "REPLACE_ME" in key:
        return {"answer": None,
                "error": "Your OpenAI key isn't set yet. Add it in set_keys.sh, then restart."}
    try:
        return asyncio.run(_ask_async(question, history))
    except Exception as e:
        return {"answer": None, "error": f"assistant error: {e}"}


async def _ask_async(question, history):
    from openai import OpenAI
    client = OpenAI()

    servers = [("razorpay", _razorpay_server()), ("shopify", _shopify_server()),
               ("quickbooks", _quickbooks_server()), ("zohobooks", _zohobooks_server()),
               ("paypal", _paypal_server()), ("taxbandits", _taxbandits_server())]
    servers = [(label, p) for label, p in servers if p is not None]

    async with AsyncExitStack() as stack:
        tools = list(LOCAL_SCHEMAS)
        route = {}  # exposed tool name -> (MCP session, real tool name on that server)
        used = set(LOCAL_FUNCS)  # names already taken (local tools win the bare name)
        for label, params in servers:
            try:
                # Remote servers (TaxBandits) arrive as an {"transport":"sse"} marker;
                # everyone else is a local stdio process.
                if isinstance(params, dict) and params.get("transport") == "sse":
                    read, write = await stack.enter_async_context(sse_client(params["url"]))
                else:
                    read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                for t in (await session.list_tools()).tools:
                    # Several accounting backends share names (get_invoice, create_invoice…).
                    # Prefix with the server label whenever the bare name is already taken,
                    # so the model can address each system unambiguously.
                    exposed = t.name if t.name not in used else f"{label}__{t.name}"
                    used.add(exposed)
                    route[exposed] = (session, t.name)
                    schema = t.inputSchema or {"type": "object", "properties": {}}
                    if "type" not in schema:
                        schema = {"type": "object", "properties": schema.get("properties", {})}
                    tools.append({"type": "function", "function": {
                        "name": exposed, "description": (t.description or t.name)[:1024],
                        "parameters": schema}})
            except Exception as e:
                # One bad server shouldn't kill the others; note it and continue.
                tools.append(_fn(f"_{label}_unavailable",
                                 f"The {label} connection failed to start: {e}", {}))

        messages = [{"role": "system", "content": SYSTEM}]
        for turn in (history or []):
            if isinstance(turn, dict) and turn.get("role") in ("user", "assistant") \
                    and isinstance(turn.get("content"), str):
                messages.append({"role": turn["role"], "content": turn["content"]})
        messages.append({"role": "user", "content": question})

        for _ in range(8):
            resp = client.chat.completions.create(
                model=OPENAI_MODEL, messages=messages, tools=tools, tool_choice="auto")
            m = resp.choices[0].message
            if not m.tool_calls:
                return {"answer": m.content or "", "error": None}

            messages.append({
                "role": "assistant", "content": m.content or "",
                "tool_calls": [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name,
                                             "arguments": tc.function.arguments}}
                               for tc in m.tool_calls],
            })
            for tc in m.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                try:
                    if name in LOCAL_FUNCS:
                        content = json.dumps(LOCAL_FUNCS[name](**args), default=str)
                    elif name in route:
                        session, real_name = route[name]
                        content = _mcp_text(await session.call_tool(real_name, args))
                    else:
                        content = json.dumps({"error": f"tool {name} is not available right now"})
                except Exception as e:
                    content = json.dumps({"error": f"tool {name} failed: {e}"})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

        return {"answer": "Sorry — I couldn't finish that within a reasonable number of steps.",
                "error": None}
