"""run_python runs inside bubblewrap on Linux (atlas/tools.py sandbox_argv): no network, no secrets file, /work only."""
import os

import pytest

import atlas.tools as T


def test_off_means_no_wrapper(monkeypatch):
    monkeypatch.setenv("ATLAS_SANDBOX", "off")
    assert T.sandbox_argv("/work/s.py", "/tmp/run", "/usr/bin/python3") is None


def test_auto_without_bwrap_runs_plain(monkeypatch):
    monkeypatch.setenv("ATLAS_SANDBOX", "auto")
    monkeypatch.setattr(T, "_bwrap_path", lambda: "")
    assert T.sandbox_argv("/work/s.py", "/tmp/run", "/usr/bin/python3") is None


def test_required_without_bwrap_refuses(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_SANDBOX", "bwrap")
    monkeypatch.setattr(T, "_bwrap_path", lambda: "")
    with pytest.raises(RuntimeError):
        T.sandbox_argv("/work/s.py", "/tmp/run", "/usr/bin/python3")
    w = T.WorkspaceTools(tmp_path)
    assert "run_python is off" in w.run_python("print(1)")


def test_bwrap_argv_isolates_everything_but_the_run_folder(monkeypatch):
    monkeypatch.setenv("ATLAS_SANDBOX", "auto")
    monkeypatch.setattr(T, "_bwrap_path", lambda: "/usr/bin/bwrap")
    monkeypatch.setattr(os.path, "exists", lambda p: p in ("/usr", "/lib", "/etc/ld.so.cache"))
    argv = T.sandbox_argv("/work/s.py", "/srv/atlas/data/runs/7", "/srv/atlas/venv/bin/python")
    assert argv[0] == "/usr/bin/bwrap" and "--unshare-all" in argv and "--clearenv" in argv
    assert argv[-3:] == ["/srv/atlas/venv/bin/python", "-I", "/work/s.py"]
    assert argv[argv.index("--bind") + 1:argv.index("--bind") + 3] == ["/srv/atlas/data/runs/7", "/work"]
    ro = [argv[i + 1] for i, a in enumerate(argv) if a == "--ro-bind"]
    assert "/usr" in ro and "/lib" in ro and "/lib64" not in ro            # only what exists is bound
    assert "/etc" not in ro and "/srv/atlas/data" not in ro and "/home" not in ro
    assert "--tmpfs" in argv and "--die-with-parent" in argv


def test_plain_run_still_works_here(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_SANDBOX", "off")
    w = T.WorkspaceTools(tmp_path)
    assert "45" in w.run_python("print(sum(range(10)))")
