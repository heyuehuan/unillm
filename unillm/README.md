# UniLLM - Unified LLM Proxy

A minimal OpenAI-compatible API proxy for Vertex AI Gemini.

## Features

- **Minimal footprint**: Only essential functionality for `/chat/completions` and `/completions` endpoints
- **OpenAI-compatible API**: Drop-in replacement for OpenAI SDK
- **Vertex AI Gemini support**: Uses Google Application Default Credentials
- **Simple authentication**: API keys stored in environment variables
- **No database required**: Configuration via YAML files

## Installation

```bash
pip install -r requirements_unillm.txt
```

## Quick Start

### 1. Configure Google Cloud credentials

Make sure you have Google Cloud Application Default Credentials set up:

```bash
gcloud auth application-default login
```

### 2. Create a configuration file

Create a `unillm_config.yaml` file:

```yaml
model_list:
  - model_name: gemini-2.5-flash-lite
    unillm_params:
      model: gemini-2.5-flash-lite
      project: your-gcp-project-id
      location: us-central1

general_settings:
  port: 4000
```

### 3. Set API keys (optional)

Set environment variables for API key authentication:

```bash
# Single master key
export UNILLM_MASTER_KEY="sk-your-master-key"

# Or multiple keys (comma-separated)
export UNILLM_API_KEYS="sk-key1,sk-key2,sk-key3"
```

If no keys are set, all requests will be allowed (development mode).

### 4. Run the proxy

```bash
python -m unillm.proxy.proxy_cli --config unillm_config.yaml --port 4000
```

## Usage

### List Models

```bash
curl -H "Authorization: Bearer sk-your-key" http://localhost:4000/v1/models
```

### Chat Completion

```bash
curl -X POST http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{
    "model": "gemini-2.5-flash-lite",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Text Completion

```bash
curl -X POST http://localhost:4000/v1/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{
    "model": "gemini-2.5-flash-lite",
    "prompt": "Once upon a time",
    "max_tokens": 50
  }'
```

### Using with OpenAI SDK

```python
import openai

client = openai.OpenAI(
    api_key="sk-your-key",
    base_url="http://localhost:4000/v1"
)

response = client.chat.completions.create(
    model="gemini-2.5-flash-lite",
    messages=[{"role": "user", "content": "Hello!"}]
)

print(response.choices[0].message.content)
```

## API Endpoints

| Endpoint                | Method | Description           |
| ----------------------- | ------ | --------------------- |
| `/health`               | GET    | Health check          |
| `/v1/models`            | GET    | List available models |
| `/v1/models/{model_id}` | GET    | Get model info        |
| `/v1/chat/completions`  | POST   | Chat completion       |
| `/v1/completions`       | POST   | Text completion       |

## Configuration

### Model Configuration

```yaml
model_list:
  - model_name: <alias-name> # The name clients will use
    unillm_params:
      model: <gemini-model-name> # Actual Gemini model name
      project: <gcp-project-id> # Google Cloud project ID
      location: <region> # e.g., us-central1
```

### Environment Variables

| Variable            | Description                      |
| ------------------- | -------------------------------- |
| `UNILLM_MASTER_KEY` | Single master API key            |
| `UNILLM_API_KEYS`   | Comma-separated list of API keys |
| `UNILLM_CONFIG`     | Path to config file              |

## CLI Options

```
python -m unillm.proxy.proxy_cli [OPTIONS]

Options:
  --host TEXT     Host to bind to (default: 0.0.0.0)
  --port INTEGER  Port to run on (default: 4000)
  --config PATH   Path to config YAML file
  --debug         Enable debug logging
  --reload        Enable auto-reload for development
```

## Project Structure

```
unillm/
├── __init__.py          # Package initialization
├── _logging.py          # Logging configuration
├── types.py             # Pydantic models
├── llm/
│   ├── __init__.py
│   └── vertex_ai.py     # Vertex AI Gemini handler
└── proxy/
    ├── __init__.py
    ├── auth.py          # Authentication
    ├── proxy_cli.py     # CLI entry point
    └── proxy_server.py  # FastAPI server
```

## Differences from LiteLLM

UniLLM is a minimal refactoring of LiteLLM with the following changes:

- **Removed**: Enterprise features, agents, MCP, database integration, caching, guardrails, etc.
- **Kept**: Core `/chat/completions` and `/completions` endpoints, model listing
- **Simplified**: Authentication using environment variables only
- **Focused**: Only Vertex AI Gemini support via Application Default Credentials

## License

See LICENSE file.
