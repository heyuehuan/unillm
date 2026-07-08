# unillm package

See the [project README](../README.md) at the repository root for full documentation
(installation, configuration, authentication model, API surface, and deployment notes).

Package layout:

```
unillm/
├── config.py            # secret resolution (JWT / Fernet)
├── types.py             # Pydantic request/response models
├── client/ssh_signer.py # client-side SSH key signing utility
├── db/                  # SQLAlchemy models, CRUD, session management
├── llm/                 # backend handlers: vertex_ai, vertex_ai_kms, vllm
└── proxy/
    ├── proxy_server.py  # FastAPI app, /v1 endpoints, request logging
    ├── api_routes.py    # /api management endpoints (JWT)
    ├── auth.py          # API-key auth + model access control
    ├── ssh_auth.py      # SSH signature verification
    └── proxy_cli.py     # CLI entry point
```
