# Support Desk Agent

An AI-powered support desk assistant built with the OpenAI Agents SDK.

## Features

- **Order Lookup** — Check order status by order ID
- **Return Policy** — Look up return policies by item category
- **Ticket Tracking** — Check support ticket statuses
- **Human Escalation** — Escalate unresolved issues to a human agent

## Setup

1. Clone the repo and navigate to the directory.

2. Copy the example env file and add your OpenAI API key:

```bash
cp .env.example .env
```

Edit `.env` and set your `OPENAI_API_KEY`. If using an OpenAI-compatible endpoint, also set `OPENAI_BASE_URL`.

3. Install dependencies:

```bash
pip install -e .
```

## Usage

### Interactive mode

```bash
python main.py
```

### Single question mode

```bash
python main.py "What is the status of order ORD-1001?"
```

### Example queries

- "Where is my order ORD-1002?"
- "What's the return policy for electronics?"
- "Check ticket TKT-5001"
- "I want to speak to a human about a damaged item"
