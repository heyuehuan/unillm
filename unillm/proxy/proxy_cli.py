#!/usr/bin/env python3
"""
UniLLM Proxy CLI

Command-line interface for running the UniLLM proxy server.
"""

import os
import sys

import click
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


@click.command()
@click.option(
    "--host",
    default="0.0.0.0",
    help="Host to bind the server to",
    show_default=True,
)
@click.option(
    "--port",
    default=4000,
    type=int,
    help="Port to run the server on",
    show_default=True,
)
@click.option(
    "--config",
    type=click.Path(exists=True),
    help="Path to the configuration YAML file",
)
@click.option(
    "--debug",
    is_flag=True,
    default=False,
    help="Enable debug logging",
)
@click.option(
    "--reload",
    is_flag=True,
    default=False,
    help="Enable auto-reload for development",
)
def main(host: str, port: int, config: str, debug: bool, reload: bool):
    """
    UniLLM Proxy Server
    
    A minimal OpenAI-compatible API proxy for Vertex AI Gemini.
    
    Example usage:
    
        # Run with config file
        python -m unillm.proxy.proxy_cli --config config.yaml --port 4000
        
        # Run in debug mode
        python -m unillm.proxy.proxy_cli --config config.yaml --debug
    """
    import uvicorn
    
    from unillm import __version__
    from unillm._logging import set_verbose
    
    # Print banner
    print()
    print("=" * 60)
    print(f"  UniLLM Proxy v{__version__}")
    print("=" * 60)
    print()
    print(f"  Host: {host}")
    print(f"  Port: {port}")
    print(f"  Config: {config or 'None'}")
    print(f"  Debug: {debug}")
    print()
    print("=" * 60)
    print()
    
    # Set config path in environment
    if config:
        os.environ["UNILLM_CONFIG"] = config
    
    # Enable debug logging if requested
    if debug:
        set_verbose(True)
    
    # Run the server
    uvicorn.run(
        "unillm.proxy.proxy_server:app",
        host=host,
        port=port,
        reload=reload,
        log_level="debug" if debug else "info",
    )


if __name__ == "__main__":
    main()
