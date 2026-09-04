"""Run the test-suite against a throw-away embedded PostgreSQL (pip install pgserver psycopg[binary]).

    py scripts/test_pg.py [pytest args...]

Starts a local server in a temp dir, exports ATLAS_TEST_DATABASE_URL, runs pytest, tears the server down.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import pgserver

d = tempfile.mkdtemp(prefix="atlas-pg-")
srv = pgserver.get_server(d)
try:
    url = srv.get_uri()
    print("embedded postgres:", url)
    env = dict(os.environ, ATLAS_TEST_DATABASE_URL=url)
    code = subprocess.call([sys.executable, "-m", "pytest", "-q", *sys.argv[1:]], env=env)
finally:
    srv.cleanup()
sys.exit(code)
