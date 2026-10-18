"""Exercise the documented fresh-database migration and seed path."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_command(args, env, cwd=PROJECT_ROOT):
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True,
                           check=False, timeout=30)
    assert result.returncode == 0, f"Command {args} failed:\n{result.stdout}\n{result.stderr}"


def test_migrations_are_reversible_and_seed_is_idempotent(tmp_path):
    database_path = tmp_path / "migration-test.db"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{database_path}"
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    run_command([sys.executable, "-m", "alembic", "upgrade", "head"], env)
    run_command([sys.executable, "-m", "app.db.seed"], env)
    run_command([sys.executable, "-m", "app.db.seed"], env)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM shipments").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM policy_documents").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM policy_chunks").fetchone()[0] >= 4
        workflow_columns = {row[1] for row in connection.execute("PRAGMA table_info(workflow_runs)").fetchall()}
        assert {"current_step", "state_json", "last_error"}.issubset(workflow_columns)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]

    run_command([sys.executable, "-m", "alembic", "downgrade", "base"], env)
    run_command([sys.executable, "-m", "alembic", "upgrade", "head"], env)
    run_command([sys.executable, "-m", "app.db.seed"], env)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM shipments").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM policy_documents").fetchone()[0] == 2
