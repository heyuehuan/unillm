---
title: Credits
group: Developer guide
keywords: [acknowledgements, credits, litellm, copilot, claude code, codex, license]
---

## AI coding tools

Written with help from GitHub Copilot, Claude Code and Codex.

## Team input

Thanks to the team for design input, implementation detail, bug reports and maintenance.
The issues in the repository have the specifics.

## LiteLLM

The proxy structure is adapted from [LiteLLM](https://github.com/BerriAI/litellm), which is
the reference for this style of OpenAI-compatible gateway. Borrowed ideas include the
`model_list` config with per-model parameters, the alias to backend mapping, the master key
and virtual key split, and the `drop_params` behavior for parameters a backend cannot serve.

UniLLM is a much smaller, purpose-built project. It supports three backends instead of a
hundred providers, and it adds the team, attribution and audit model this deployment
needed. The vendored `litellm/` directory in the repository is an upstream reference copy.
It is gitignored and is not part of the package.

## Other components

FastAPI and Uvicorn for the server, SQLAlchemy and Alembic for storage and migrations,
Pydantic for the request models, `cryptography` for SSH signature verification and key
encryption at rest, PyJWT and bcrypt for console authentication, React and Vite for the
console.

## License

See the `LICENSE` file in the repository root.
