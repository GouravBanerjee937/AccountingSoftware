# AccountingSoftware — AI assistant for accounting + Razorpay + Shopify

A small accounting web app with a built‑in **AI chat assistant**. Ask in plain English about your
**inventory & invoices**, your **Razorpay payments**, and your **Shopify store** — the assistant
picks the right tool and answers.

## How it works
```
You (chat box in the app, after login)
  → server.py  /api/ask   (port 3000)
     → agent.py  (the MCP client + brain; OpenAI model)
         → tools merged from 3 sources:
            • local accounting functions (from the invoice-mcp repo)
            • Razorpay MCP server   (separate download — see below)
            • Shopify MCP server    (separate install — see below)
  → answer shown back in the chat box
```

## The two MCP servers are NOT in this repo
They are third‑party software you install yourself (each person installs their own copy):

| Server | Get it from |
|--------|-------------|
| **Razorpay MCP server** (official) | https://github.com/razorpay/razorpay-mcp-server (download a release binary, or use Docker) |
| **Shopify MCP server** (community) | `npm install shopify-mcp` — source: https://github.com/GeLi2001/shopify-mcp |

> Each person also brings **their own** OpenAI key and their **own** Razorpay / Shopify accounts.
> Use Razorpay **Test mode** and a Shopify **development store** while testing.

## Prerequisites
- macOS or Linux
- **Node.js 18+** and **Python 3.10+**
- An **OpenAI API key**
- *(optional)* a Razorpay account (test keys) for payment tools
- *(optional)* a Shopify store + Admin API token for store tools

## Setup
1. **Clone this repo AND the accounting-tools repo side by side** (same parent folder):
   ```bash
   git clone https://github.com/GouravBanerjee937/AccountingSoftware.git
   git clone https://github.com/GouravBanerjee937/invoice-mcp.git
   ```
2. **Python deps:**
   ```bash
   cd AccountingSoftware
   python3 -m venv .venv
   ./.venv/bin/pip install openai mcp
   ```
3. **Install the two MCP servers** (links in the table above) and note where they land:
   - Razorpay: the downloaded `razorpay-mcp-server` binary path.
   - Shopify: `npm install shopify-mcp`, then note `node_modules/shopify-mcp/dist/index.js`.
4. **Fill in your keys + paths:**
   ```bash
   cp set_keys.sh.example set_keys.sh    # set_keys.sh is git-ignored — never committed
   # edit set_keys.sh with your OpenAI key, Razorpay/Shopify creds, and the two server paths
   ```
5. **Run:**
   ```bash
   bash run_server.sh
   # open http://localhost:3000  → log in (any username/password) → use the chat box
   ```

## Configuration (env vars, set in `set_keys.sh`)
| Variable | Required | What |
|----------|----------|------|
| `OPENAI_API_KEY` | ✅ | Your OpenAI key (the assistant's brain) |
| `OPENAI_MODEL` | – | Model id (default `gpt-5.5`; `gpt-4o` / `gpt-4o-mini` also work) |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | for payments | Razorpay **test** keys |
| `RAZORPAY_MCP_BIN` | for payments | Full path to the Razorpay MCP server binary |
| `MYSHOPIFY_DOMAIN` | for store | e.g. `your-store.myshopify.com` |
| `SHOPIFY_ACCESS_TOKEN` | for store | Shopify Admin API token (`shpat_…`) |
| `SHOPIFY_MCP_JS` | for store | Full path to `node_modules/shopify-mcp/dist/index.js` |
| `INVOICE_MCP_DIR` | – | Path to the invoice-mcp repo (defaults to `../invoice-mcp`) |

Razorpay/Shopify are each **optional** — leave their keys blank and that section simply stays off.

## Safety notes
- **Never commit `set_keys.sh`** — it holds real keys and is git-ignored.
- The assistant only does what its tools allow; risky/money actions should be reviewed.
- This is a learning/POC project, not hardened for production.
