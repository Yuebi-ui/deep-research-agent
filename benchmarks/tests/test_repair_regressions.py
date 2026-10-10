"""Regression coverage for memory ablation gates, config attestation and DB path.

These tests stay independent of optional GPU, LangChain, Redis and Chroma
installations so the repository's offline benchmark suite can always run.
"""
from __future__ import annotations

import ast
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from deep_research.benchmark.fingerprint import (
    build_config_snapshot,
    config_fingerprint,
    resolved_memory_flags,
)
from deep_research.benchmark.preflight import Expectations, run_preflight

PRESETS = json.loads((ROOT / "benchmarks/configs/runtime_ablation.v1.json").read_text("utf-8"))["variants"]
IDENTITY = {"provider": "fake", "model": "fake-embedding", "dimension": 4, "schema_version": 1}


class _Probes:
    def __init__(self, worker_env):
        self.worker_env = worker_env

    def api_health(self):
        return {"status": "ok"}

    def redis_ping(self):
        return True

    def vllm_models(self):
        return {"model": "fake", "max_model_len": 8192}

    def worker_process(self):
        return {"pid": 1, "alive": True, "env": self.worker_env, "started_at": 100}

    def api_process(self):
        return {"pid": 2, "alive": True, "env": {}, "started_at": 100}

    def memory_schema(self):
        return []

    def newest_source_mtime(self):
        return 1


class MemoryAblationRegressionTests(unittest.TestCase):
    def test_four_ablations_have_distinct_config_fingerprints(self):
        hashes = {
            name: config_fingerprint(build_config_snapshot({}, env=flags, embedding=IDENTITY))
            for name, flags in PRESETS.items()
        }
        self.assertEqual(len(hashes), 4)
        self.assertEqual(len(set(hashes.values())), 4, hashes)
        self.assertFalse(resolved_memory_flags(PRESETS["memory_off"])["DR_MEMORY_READ_ENABLED"])
        self.assertFalse(resolved_memory_flags(PRESETS["memory_off"])["DR_MEMORY_WRITE_ENABLED"])
        dirs = [flags["DR_MEMORY_DATA_DIR"] for flags in PRESETS.values()]
        self.assertEqual(len(set(dirs)), len(dirs), "no two variants should share a Chroma directory")
        for name, flags in PRESETS.items():
            self.assertTrue(flags["DR_MEMORY_DATA_DIR"].endswith(name))

    def test_flag_change_is_hashed_even_when_other_flags_stay_the_same(self):
        original = dict(PRESETS["stage_recall"])
        turned_off = {**original, "DR_MEMORY_WRITE_ENABLED": "off"}
        snap = lambda env: build_config_snapshot({}, env=env, embedding=IDENTITY)
        self.assertNotEqual(config_fingerprint(snap(original)), config_fingerprint(snap(turned_off)))
        self.assertNotEqual(config_fingerprint(snap(original)), config_fingerprint(snap({**original, "DR_MEMORY_DATA_DIR": "other"})))

    def test_preflight_checks_worker_not_runner_flag_label(self):
        exp = Expectations(memory_preset="memory_off", require_worker=False, allow_stale_services=True)
        self.assertTrue(run_preflight(exp, _Probes(PRESETS["memory_off"]))["passed"])
        with patch.dict(os.environ, PRESETS["memory_off"], clear=False):
            bad = run_preflight(exp, _Probes(PRESETS["stage_plus_episodic"]))
        self.assertFalse(bad["passed"])
        self.assertIn("memory_ablation_preset", bad["failed"])
        self.assertEqual(bad["observations"]["memory_flags_source"], "worker_env")
        # No worker env available is not proof of correctness.
        no_env = run_preflight(exp, _Probes(None))
        self.assertIn("memory_ablation_preset", no_env["failed"])

    def test_master_gates_disable_both_reads_and_writes(self):
        from deep_research.memory.flags import memory_read_enabled, memory_write_enabled
        from deep_research.memory.stage_retrieval import stage_enabled, retrieve_stage_context
        from backend.runtime.memory_outbox import MemoryOutboxProcessor, enabled

        class NeverConstruct:
            def __getattr__(self, name):
                raise AssertionError("a memory store must not be touched")

        with patch.dict(os.environ, PRESETS["memory_off"], clear=False):
            self.assertFalse(memory_read_enabled())
            self.assertFalse(memory_write_enabled())
            self.assertFalse(stage_enabled())
            self.assertFalse(enabled())
            self.assertEqual(retrieve_stage_context("a query", "supervisor", manager=NeverConstruct()), "")
            self.assertEqual(MemoryOutboxProcessor._write_episodes([{"secret": "must-not-write"}]), 0)
            self.assertIsNone(MemoryOutboxProcessor._write_report("question", "long report " * 50))
        with patch.dict(os.environ, {"DR_MEMORY_WRITE_ENABLED": "on", "DR_MEMORY_OUTBOX_ENABLED": "off"}):
            self.assertFalse(enabled(), "turning off the outbox means legacy fallback, not write-off")
            self.assertTrue(memory_write_enabled())

    def test_original_manager_methods_short_circuit_without_optional_dependencies(self):
        """Execute the *source* methods while stores intentionally raise on access.

        Extracting just these methods avoids needing GPU/Chroma/LangChain to
        verify the short-circuit, while full-dependency CI can test the class.
        """
        path = ROOT / "deep_research/memory/manager.py"
        source = ast.parse(path.read_text("utf-8"), filename=str(path))
        cls = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == "MemoryManager")
        functions = [copy.deepcopy(n) for n in cls.body if isinstance(n, ast.FunctionDef)
                     and n.name in {"retrieve_context", "store_from_report"}]
        self.assertEqual(len(functions), 2)
        module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
        scope = {"memory_read_enabled": lambda: False, "memory_write_enabled": lambda: False}
        exec(compile(module, str(path), "exec"), scope)

        class NoStore:
            def __getattr__(self, name):
                raise AssertionError(f"memory store unexpectedly accessed: {name}")

        obj = SimpleNamespace(_store=NoStore(), _structured=NoStore(), _sections=NoStore())
        self.assertEqual(scope["retrieve_context"](obj, "question"), "")
        self.assertIsNone(scope["store_from_report"](obj, "question", "long report " * 50))
        self.assertEqual(scope["retrieve_context"](obj, ""), "")


class DatabasePathRegressionTests(unittest.TestCase):
    def test_configured_relative_sqlite_path_is_root_based_not_cwd_based(self):
        from backend.db.engine import resolve_database_path, resolve_database_url
        from deep_research.settings import reset_engine_settings
        from sqlalchemy.engine.url import make_url

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cfg = base / "config.yml"
            cfg.write_text("stages:\n  prod:\n    database:\n      backend: sqlite\n      sqlite:\n        path: data/alternate/tasks-custom.db\n", "utf-8")
            alt_dir = base / "launch_from_here"
            alt_dir.mkdir()
            overrides = {"CONFIG_PATH": str(cfg), "STAGE": "prod", "APP_ENV": "test"}
            prior_cwd = Path.cwd()
            try:
                with patch.dict(os.environ, overrides, clear=False):
                    reset_engine_settings()
                    expected = ROOT / "data/alternate/tasks-custom.db"
                    self.assertEqual(resolve_database_path(), expected)
                    self.assertEqual(Path(make_url(resolve_database_url()).database), expected)
                    os.chdir(alt_dir)
                    self.assertEqual(resolve_database_path(), expected)
                    self.assertEqual(Path(make_url(resolve_database_url()).database), expected)
                    # Across a new Python process from an unrelated cwd.
                    env = dict(os.environ, PYTHONPATH=str(ROOT))
                    proc = subprocess.run(
                        [sys.executable, "-c", "from backend.db.engine import resolve_database_path; print(resolve_database_path())"],
                        env=env, cwd=alt_dir, check=True, capture_output=True, text=True,
                    )
                    self.assertEqual(proc.stdout.strip(), str(expected))
                    # The AutoDL helper calls this exact same resolver.
                    sh = subprocess.run(
                        ["bash", "-c", "source scripts/autodl/_common.sh; resolve_task_db_path"],
                        env=env, cwd=ROOT, check=True, capture_output=True, text=True,
                    )
                    self.assertEqual(sh.stdout.strip(), str(expected))
            finally:
                os.chdir(prior_cwd)
                reset_engine_settings()

    def test_absolute_path_and_default_data_dir(self):
        from backend.db.engine import resolve_database_path
        from deep_research.settings import get_engine_settings, reset_engine_settings

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cfg = base / "config.yml"
            db = base / "store with spaces" / "tasks.db"
            cfg.write_text("stages:\n  prod:\n    database:\n      backend: sqlite\n      sqlite:\n        path: '" + str(db) + "'\n", "utf-8")
            with patch.dict(os.environ, {"CONFIG_PATH": str(cfg), "STAGE": "prod"}):
                reset_engine_settings()
                self.assertEqual(resolve_database_path(), db)
            with patch.dict(os.environ, {"CONFIG_PATH": str(base / "missing.yml"), "DR_DATA_DIR": "data/custom-default"}):
                reset_engine_settings()
                self.assertEqual(resolve_database_path(), ROOT / "data/custom-default/tasks.db")
                self.assertEqual(get_engine_settings().resolved_memory_data_dir,
                                 ROOT / "data/custom-default")
            reset_engine_settings()

    def test_memory_data_dir_override_is_isolated_from_task_data_dir(self):
        from deep_research.settings import get_engine_settings, reset_engine_settings

        with patch.dict(os.environ, {
            "DR_DATA_DIR": "data/production-tasks",
            "DR_MEMORY_DATA_DIR": "data/ablation/memory_off",
        }):
            reset_engine_settings()
            settings = get_engine_settings()
            self.assertEqual(settings.resolved_data_dir, ROOT / "data/production-tasks")
            self.assertEqual(settings.resolved_memory_data_dir, ROOT / "data/ablation/memory_off")
        reset_engine_settings()

    def test_same_sqlite_file_is_used_by_connections_started_in_different_cwds(self):
        from backend.db.engine import resolve_database_url
        from deep_research.settings import reset_engine_settings
        from sqlalchemy import create_engine, text

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            db = base / "db" / "one-source.db"
            db.parent.mkdir()
            cfg = base / "config.yml"
            cfg.write_text("stages:\n  prod:\n    database:\n      backend: sqlite\n      sqlite:\n        path: '" + str(db) + "'\n", encoding="utf-8")
            other_cwd = base / "other"
            other_cwd.mkdir()
            prior_cwd = Path.cwd()
            try:
                with patch.dict(os.environ, {"CONFIG_PATH": str(cfg), "STAGE": "prod"}):
                    reset_engine_settings()
                    engine = create_engine(resolve_database_url())
                    with engine.begin() as conn:
                        conn.execute(text("CREATE TABLE boundary_check (value INTEGER NOT NULL)"))
                        conn.execute(text("INSERT INTO boundary_check VALUES (42)"))
                    engine.dispose()
                    os.chdir(other_cwd)
                    second = create_engine(resolve_database_url())
                    with second.connect() as conn:
                        self.assertEqual(conn.execute(text("SELECT value FROM boundary_check")).scalar_one(), 42)
                    second.dispose()
                    self.assertEqual(len(list(base.rglob("one-source.db"))), 1)
            finally:
                os.chdir(prior_cwd)
                reset_engine_settings()


if __name__ == "__main__":
    unittest.main()
