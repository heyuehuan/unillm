# UniLLM - Unified LLM Proxy

A minimal OpenAI-compatible API proxy for Vertex AI Gemini.

## Features

- **Minimal footprint**: Only essential functionality for `/chat/completions` and `/completions` endpoints
- **OpenAI-compatible API**: Drop-in replacement for OpenAI SDK
- **Vertex AI Gemini support**: Uses Google Application Default Credentials
- **Simple authentication**: API keys stored in environment variables
- **SSH Key Authentication**: Optional signature verification for enhanced security
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

| Variable            | Description                                            |
| ------------------- | ------------------------------------------------------ |
| `UNILLM_MASTER_KEY` | Single master API key                                  |
| `UNILLM_API_KEYS`   | Comma-separated list of API keys                       |
| `UNILLM_CONFIG`     | Path to config file                                    |
| `UNILLM_SSH_KEYS`   | SSH public keys for signature verification (see below) |

## SSH Key Authentication

UniLLM supports optional SSH key signature verification for enhanced API key security. When enabled, API keys must include a cryptographic signature created with the user's SSH private key.

### How It Works

1. Users sign their API key with their SSH private key
2. The signed API key is sent in the format: `original-api-key||key-name||base64-signature`
3. UniLLM verifies the signature using the registered public key
4. Verified requests include the username in the response

### Configuration

#### 1. Set the SSH mode in your config file

```yaml
general_settings:
  port: 4000
  ssh_required: enforce # Options: none, warning, enforce
```

**Modes:**

- `none` (default): SSH verification disabled, standard API key auth only
- `warning`: Verify SSH signatures if present, add warning to response if invalid/missing
- `enforce`: Require valid SSH signature, reject requests with invalid/missing signatures

#### 2. Register SSH public keys

Set the `UNILLM_SSH_KEYS` environment variable with registered public keys:

```bash
export UNILLM_SSH_KEYS="key_name1:ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA...:username1,key_name2:ssh-rsa AAAAB3NzaC1yc2EAAAA...:username2"
```

Format: `key_name:public_key:username` (comma-separated for multiple keys)

### Creating a Signed API Key

#### Using the UniLLM Client Utility (Recommended)

UniLLM provides a convenient Python client for signing API keys:

```python
from unillm.client import sign_api_key

# Simple usage with defaults (uses system username and auto-detected SSH key)
signed = sign_api_key("sk-your-api-key")
print(signed.full_key)  # "sk-your-api-key||johndoe||base64signature..."

# Custom key name and path
signed = sign_api_key(
    "sk-your-api-key",
    key_name="my-custom-name",
    private_key_path="~/.ssh/id_ed25519"
)

# Use with OpenAI SDK
import openai

client = openai.OpenAI(
    api_key=signed.full_key,
    base_url="http://localhost:4000/v1"
)
```

**Command-line usage:**

```bash
# Sign with defaults
python -m unillm.client.ssh_signer sk-your-api-key

# Custom key name
python -m unillm.client.ssh_signer sk-your-api-key --key-name mykey

# List available SSH keys
python -m unillm.client.ssh_signer --list-keys

# JSON output
python -m unillm.client.ssh_signer sk-your-api-key --output json
```

The client automatically:

- Uses your system username as the default key-name
- Searches common SSH key locations (`~/.ssh/id_ed25519`, `~/.ssh/id_rsa`, etc.)
- Selects the newest key if multiple are found
- Supports Ed25519, RSA, and ECDSA key types

#### Using ssh-keygen (Alternative)

You can also sign manually using ssh-keygen:

```bash
# Sign the API key with SSH private key
SIGNATURE=$(echo -n "sk-your-api-key" | ssh-keygen -Y sign -f ~/.ssh/id_ed25519 -n unillm 2>/dev/null | base64 -w0)

# Create the full signed API key
SIGNED_KEY="sk-your-api-key||my-key-name||${SIGNATURE}"

# Use in requests
curl -X POST http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${SIGNED_KEY}" \
  -d '{"model": "gemini-2.5-flash-lite", "messages": [{"role": "user", "content": "Hello!"}]}'
```

### Response Format

When SSH verification is enabled, responses include additional fields:

**Successful verification:**

```json
{
  "id": "chatcmpl-...",
  "model": "gemini-2.5-flash-lite",
  "choices": [...],
  "user": "john.doe"  // Username from registered key
}
```

**Warning mode with invalid/missing signature:**

```json
{
  "id": "chatcmpl-...",
  "model": "gemini-2.5-flash-lite",
  "choices": [...],
  "warning": "SSH key 'unknown-key' is not recognized. Please validate the key or register it."
}
```

### Supported Key Types

- Ed25519 (recommended)
- RSA (2048-bit or higher)
- ECDSA

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
│   ├── vertex_ai.py     # Vertex AI Gemini handler
│   └── vertex_ai_kms.py # Vertex AI with CMEK support
└── proxy/
    ├── __init__.py
    ├── auth.py          # Authentication
    ├── ssh_auth.py      # SSH key signature verification
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
