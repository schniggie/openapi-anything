"""CLI for openapi-anything: generate wrappers, undeploy them, and run the hub server."""

import argparse
import asyncio
import json
import sys

from openapi_anything.gateway.main import create_app
from openapi_anything.gateway.registry import get_registry
from openapi_anything.service import generate_and_deploy, undeploy


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="openapi-anything",
        description="Agentic system to wrap anything into REST APIs with OpenAPI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    gen_parser = subparsers.add_parser("generate", help="Generate and deploy a wrapper from NL description")
    gen_parser.add_argument("description", help="Natural language description of target to wrap")
    gen_parser.add_argument("--id", help="Optional wrapper ID (auto-generated if omitted)")

    del_parser = subparsers.add_parser("delete", help="Undeploy a wrapper (stop+remove container/image)")
    del_parser.add_argument("wrapper_id", help="ID of the wrapper to undeploy")

    serve_parser = subparsers.add_parser("serve", help="Run the central gateway/hub server")
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8000)

    args = parser.parse_args()

    if args.command == "generate":
        print(f"Generating wrapper for: {args.description}")
        result = asyncio.run(generate_and_deploy(args.description, get_registry(), args.id))
        print(json.dumps({
            "wrapper_id": result.wrapper_id,
            "status": result.status,
            "service_url": result.service_url,
            "openapi_url": result.openapi_url,
            "verification_overall": (result.verification or {}).get("overall"),
            "errors": result.errors,
        }, indent=2))
        sys.exit(0 if result.status == "deployed" else 1)
    elif args.command == "delete":
        summary = asyncio.run(undeploy(args.wrapper_id, get_registry()))
        print(json.dumps(summary, indent=2))
        sys.exit(0 if summary.get("removed") else 1)
    elif args.command == "serve":
        import uvicorn

        app = create_app()
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
