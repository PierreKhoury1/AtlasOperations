"""`python -m hermes` launches the desktop UI; `python -m hermes run "task" [--mode auto|<workflow>]` runs headless."""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hermes")
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="run a task headless in the terminal")
    r.add_argument("task")
    r.add_argument("--mode", default="auto")
    sub.add_parser("ui", help="launch the desktop UI (default)")
    t = sub.add_parser("test", help="ping each configured provider (or one) and report")
    t.add_argument("provider", nargs="?", default="")
    k = sub.add_parser("set-key", help="store an API key in config/providers.json")
    k.add_argument("key")
    k.add_argument("--provider", default="anthropic")
    k.add_argument("--workspace", default="", help="anthropic-workspace-id (identity-linked keys)")
    args = p.parse_args(argv)

    if args.cmd == "set-key":
        from . import config as cfg
        prov = cfg.load("providers", cfg.DEFAULT_PROVIDERS)
        if args.provider not in prov.get("providers", {}):
            print(f"unknown provider {args.provider!r}; have: {', '.join(prov.get('providers', {}))}")
            return 2
        prov["providers"][args.provider]["api_key"] = args.key.strip()
        if args.workspace:
            prov["providers"][args.provider]["workspace_id"] = args.workspace.strip()
        cfg.save("providers", prov)
        print(f"saved key for {args.provider} ({args.key.strip()[:10]}...)")
        return 0

    if args.cmd == "test":
        from . import config as cfg
        from .providers import ProviderPool
        reg = ProviderPool(cfg.load("providers", cfg.DEFAULT_PROVIDERS))
        names = [args.provider] if args.provider else list(reg.cfg.get("providers", {}))
        rc = 0
        for n in names:
            try:
                print(f"{n}: {reg.get(n).test()}")
            except Exception as e:
                print(f"{n}: FAIL {type(e).__name__}: {e}")
                rc = 1
        return rc

    if args.cmd == "run":
        for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        from . import config as cfg
        from .orchestrator import Orchestrator
        from .store import Store

        def emit(ev):
            if ev.kind == "usage":
                return
            print(f"[{ev.agent}] {ev.kind}: {ev.text}", flush=True)

        res = Orchestrator(cfg.load_all(), Store(), emit).run(args.task, args.mode)
        print(f"\n== {res.status} == tokens in {res.tokens_in} out {res.tokens_out}\n{res.run_dir}")
        return 0 if res.status == "done" else 1

    from .ui import main as ui_main
    ui_main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
