# Support Desk Agent

An AI-powered support desk assistant with a modern web dashboard, built with the OpenAI Agents SDK and FastAPI.

## Features

- **AI Chat** — Ask the AI agent about orders, tickets, return policies, or escalate issues
- **Order Management** — View, search, filter, create, and delete customer orders
- **Ticket Tracking** — Manage support tickets with status updates and assignment
- **Escalation Handling** — Review and resolve escalated issues
- **Customer Directory** — Browse customer profiles with order/ticket history
- **Return Policies** — View return policy information by category
- **Dashboard** — Real-time stats with charts for orders, tickets, priority distribution, and activity feed
- **Chat History** — Browse, resume, and delete past conversations
- **Voice Input** — Speech-to-text support (Chrome-based browsers)

## Setup

1. Clone the repo and navigate to the directory.

2. Copy the example env file and add your API key:

```bash
cp .env.example .env
```

Edit `.env` with your Gemini API key (or set `LLM_PROVIDER=ollama` for local LLM).

3. Install dependencies:

```bash
pip install -e .
```

## Usage

### Web dashboard

```bash
python app.py
```

Open http://localhost:8000 in your browser.

### CLI interactive mode

```bash
python main.py
```

### Single question mode

```bash
python main.py "What is the status of order ORD-1001?"
```

## Running tests

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | — | Gemini API key (required for Gemini) |
| `LLM_PROVIDER` | `gemini` | `gemini` or `ollama` |
| `OPENAI_BASE_URL` | Gemini OpenAI endpoint | Custom base URL |
| `MODEL` | `gemini-2.5-flash` | Model name |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Ollama endpoint |
| `OLLAMA_MODEL` | `llama3.2` | Ollama model name |
| `PORT` | `8000` | Web server port |
| `DB_PATH` | `support_desk.db` | SQLite database path |
