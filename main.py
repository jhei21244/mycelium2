#!/usr/bin/env python3
"""Mycelium 2 — start the server and optionally submit a demo goal."""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(description="Mycelium 2 — Stigmergic Agent Coordination")
    parser.add_argument("--port", type=int, default=8420, help="Server port")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Server host")
    parser.add_argument("--agents", type=int, default=4, help="Number of agents")
    parser.add_argument("--db", type=str, default="mycelium.db", help="Database path")
    args = parser.parse_args()

    import uvicorn
    from mycelium.server import create_app

    app = create_app(db_path=args.db, agent_count=args.agents)

    print(f"""
    ╔══════════════════════════════════════════════╗
    ║           🍄 Mycelium 2                      ║
    ║     Stigmergic Agent Coordination            ║
    ╠══════════════════════════════════════════════╣
    ║  Dashboard:  http://localhost:{args.port}          ║
    ║  API:        http://localhost:{args.port}/api       ║
    ║  Agents:     {args.agents} workers active              ║
    ╚══════════════════════════════════════════════╝
    """)

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
