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
    "  3) Shopify — your online store's products, orders, customers, inventory (get-*/create-* tools).\n\n"
    "Rules:\n"
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

    servers = [("razorpay", _razorpay_server()), ("shopify", _shopify_server())]
    servers = [(label, p) for label, p in servers if p is not None]

    async with AsyncExitStack() as stack:
        tools = list(LOCAL_SCHEMAS)
        route = {}  # tool name -> the MCP session that owns it
        for label, params in servers:
            try:
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                for t in (await session.list_tools()).tools:
                    route[t.name] = session
                    schema = t.inputSchema or {"type": "object", "properties": {}}
                    if "type" not in schema:
                        schema = {"type": "object", "properties": schema.get("properties", {})}
                    tools.append({"type": "function", "function": {
                        "name": t.name, "description": (t.description or t.name)[:1024],
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
                        content = _mcp_text(await route[name].call_tool(name, args))
                    else:
                        content = json.dumps({"error": f"tool {name} is not available right now"})
                except Exception as e:
                    content = json.dumps({"error": f"tool {name} failed: {e}"})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})

        return {"answer": "Sorry — I couldn't finish that within a reasonable number of steps.",
                "error": None}
