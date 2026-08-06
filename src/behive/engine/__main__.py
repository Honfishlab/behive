"""Allow running: python -m behive.engine [command] 'topic' [options]"""
import sys
import os
import argparse
import logging

from behive.config import load_project_env

load_project_env()

log = logging.getLogger(__name__)

# Install compat shims
try:
    from behive.compat.shims import install_shims, install_ops_shim
    install_shims()
    install_ops_shim()
except ImportError:
    pass


def main() -> None:
    """CLI entry point for running research from the command line."""
    parser = argparse.ArgumentParser(
        prog="behive.engine",
        description="BeHive research pipeline engine",
    )
    parser.add_argument("command", nargs="?", default="run",
                        help="Command: run (default)")
    parser.add_argument("topic", nargs="*",
                        help="Research topic/query")
    parser.add_argument("--mission-id", dest="mission_id", default=None,
                        help="Mission ID (auto-generated if not provided)")
    parser.add_argument("--depth", type=int, default=3,
                        help="Research depth 1-5 (default: 3)")
    parser.add_argument("--deep", action="store_true",
                        help="Enable deep research mode")
    parser.add_argument("--force", action="store_true",
                        help="Force re-run even if mission exists")
    parser.add_argument("--scale", type=int, default=200,
                        help="Max sources to scout (default: 200)")

    args = parser.parse_args()

    topic = ' '.join(args.topic) if args.topic else None

    phase_commands = {"resume", "harvest", "process", "synth", "analyze"}
    if args.command in phase_commands:
        mission_id = args.mission_id or topic
        if not mission_id:
            parser.error(f"{args.command} requires --mission-id")
        from behive.engine.orchestrator import cmd_analyze, cmd_harvest, cmd_process, cmd_resume, cmd_synth
        {"resume": cmd_resume, "harvest": cmd_harvest,
         "process": cmd_process, "synth": cmd_synth, "analyze": cmd_analyze}[args.command](mission_id)
        return

    if not topic:
        parser.print_help()
        sys.exit(1)

    # Set mission ID in env so orchestrator picks it up
    if args.mission_id:
        os.environ["BEHIVE_MISSION_ID"] = args.mission_id

    from behive.engine.orchestrator import cmd_run
    cmd_run(
        topic=topic,
        deep=args.deep or args.depth >= 4,
        force=args.force,
        scale=args.scale,
    )


if __name__ == "__main__":
    main()
