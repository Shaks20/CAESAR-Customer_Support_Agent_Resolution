"""Regression tests for import-safe startup and working-directory independence."""
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_import_does_not_create_or_connect_to_application_database(tmp_path):
    database_dir = tmp_path / "not-created" / "nested"
    database_url = f"sqlite:///{database_dir / 'app.db'}"
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert not database_dir.exists(), "Importing the API must not create database files or directories."


def test_home_route_works_outside_repository_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Resolve" in response.text
