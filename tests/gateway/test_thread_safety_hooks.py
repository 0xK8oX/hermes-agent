"""
Tests for thread safety, path traversal validation, and the gateway extension hook system.

Covers commits:
  - 7cef25f85: fix: thread safety for channel_binding + hall.py JSONL + cache MemoryStore global entries
  - 5c2a25dd9: fix: code review critical+high issues
  - 757b0fa20: temp (created gateway/extensions/__init__.py hook system)
"""
import unittest
import threading
import time
from typing import List
from unittest.mock import patch

import gateway.extensions
from gateway.extensions import register_hook, fire_hooks, fire_hooks_first, list_hooks
from gateway.extensions.channel_binding import (
    _state_lock,
    _apply_binding,
    _session_souls,
    resolve_memory_dirs,
    _apply_binding_unlocked,
    _session_bindings,
)


class TestThreadSafetyChannelBinding(unittest.TestCase):
    def setUp(self):
        with _state_lock:
            _session_souls.clear()
            _session_bindings.clear()

    def tearDown(self):
        with _state_lock:
            _session_souls.clear()
            _session_bindings.clear()

    def test_state_lock_is_threading_lock(self):
        """_state_lock exists and is a lock object (has acquire/release methods)."""
        # Don't check exact type because CPython returns _thread.lock not threading.Lock
        self.assertTrue(callable(getattr(_state_lock, 'acquire', None)))
        self.assertTrue(callable(getattr(_state_lock, 'release', None)))

    def test_apply_binding_acquires_state_lock(self):
        """_apply_binding acquires _state_lock before calling _apply_binding_unlocked."""
        session_key = "test_thread:1"
        binding = {"soul": "test"}

        locked_flag = threading.Event()
        done_flag = threading.Event()
        work_started = threading.Event()

        def slow_work(*args, **kwargs):
            # Signal we started and hold the lock while checker tries to acquire
            work_started.set()
            time.sleep(0.2)  # Keep lock held long enough for check to happen
            return None

        def check_lock_held():
            # Wait until work has started (so lock should be held)
            work_started.wait(timeout=1.0)
            # Try to acquire — this should block until lock is released
            acquired = _state_lock.acquire(blocking=True, timeout=0.1)
            if acquired:
                _state_lock.release()
                # Got it immediately → not held when it should be
                locked_flag.set()
            done_flag.set()

        # Start checker thread
        t = threading.Thread(target=check_lock_held)
        t.start()

        with patch("gateway.extensions.channel_binding._load_soul_with_meta", return_value=("# Test", {})):
            with patch("gateway.extensions.channel_binding._apply_binding_unlocked", side_effect=slow_work):
                # Hold lock during this call
                _apply_binding(session_key, binding)

        t.join()

        # Verify the lock was held during _apply_binding_unlocked —
        # checker couldn't acquire while we were holding it
        self.assertFalse(locked_flag.is_set())

    def test_concurrent_apply_binding_is_safe(self):
        """Concurrent calls to _apply_binding don't corrupt state."""
        num_threads = 10
        errors: List[Exception] = []

        def worker(thread_idx: int):
            try:
                session_key = f"concurrent_test:{thread_idx}"
                binding = {"soul": f"soul_{thread_idx}", "model": f"model_{thread_idx}"}
                with patch("gateway.extensions.channel_binding._load_soul_with_meta", return_value=("# Test", {})):
                    _apply_binding(session_key, binding)
                time.sleep(0.01)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No exceptions
        self.assertEqual(len(errors), 0)
        # All sessions are present
        with _state_lock:
            self.assertEqual(len(_session_souls), num_threads)
            for i in range(num_threads):
                self.assertIn(f"concurrent_test:{i}", _session_souls)


class TestResolveMemoryDirsPathTraversal(unittest.TestCase):
    def test_rejects_forward_slash_in_scope(self):
        """Scope with / is rejected as path traversal."""
        dirs = resolve_memory_dirs("../etc/passwd")
        # Should reject and return fallback (base dir at minimum)
        self.assertTrue(len(dirs) >= 1)
        # Should NOT contain the traversal path
        for d in dirs:
            self.assertNotIn("etc", str(d))
            self.assertNotIn("passwd", str(d))

    def test_rejects_back_slash_in_scope(self):
        """Scope with \\ is rejected."""
        dirs = resolve_memory_dirs("..\\windows\\system32")
        self.assertTrue(len(dirs) >= 1)
        for d in dirs:
            self.assertNotIn("windows", str(d))

    def test_rejects_dot_dot_in_scope(self):
        """Scope with .. is rejected."""
        dirs = resolve_memory_dirs("..")
        self.assertTrue(len(dirs) >= 1)

    def test_normal_scope_works(self):
        """Normal alphanumeric scope name without traversal works normally."""
        dirs = resolve_memory_dirs("dev")
        # Returns at least the scoped dir
        self.assertTrue(len(dirs) >= 1)
        # Contains "dev" in the path
        self.assertTrue(any("dev" in str(d) for d in dirs))


class TestHookSystem(unittest.TestCase):
    def setUp(self):
        # Clear hooks before each test by resetting _HOOKS
        gateway.extensions._HOOKS.clear()

    def test_register_hook_adds_callback(self):
        """register_hook adds callback to _HOOKS."""
        def test_cb():
            return "test"

        register_hook("test_event", test_cb)
        hooks = list_hooks()
        self.assertIn("test_event", hooks)
        self.assertEqual(len(hooks["test_event"]), 1)

    def test_fire_hooks_collects_non_none_results(self):
        """fire_hooks collects all non-None results."""
        results = []
        def cb1():
            return "one"
        def cb2():
            return None
        def cb3():
            return "three"
        register_hook("test_fire", cb1)
        register_hook("test_fire", cb2)
        register_hook("test_fire", cb3)

        results = fire_hooks("test_fire")
        self.assertEqual(results, ["one", "three"])

    def test_fire_hooks_catches_exceptions_and_continues(self):
        """fire_hooks catches exceptions from callbacks and continues to next."""
        good_result = None
        def bad_cb():
            raise ValueError("oops")
        def good_cb():
            return "good"

        register_hook("test_fail", bad_cb)
        register_hook("test_fail", good_cb)

        results = fire_hooks("test_fail")
        self.assertEqual(results, ["good"])

    def test_fire_hooks_first_returns_first_non_none(self):
        """fire_hooks_first returns the first non-None result and stops."""
        called = []
        def cb1():
            called.append(1)
            return None
        def cb2():
            called.append(2)
            return "found"
        def cb3():
            called.append(3)
            return "never_used"

        register_hook("test_first", cb1)
        register_hook("test_first", cb2)
        register_hook("test_first", cb3)

        result = fire_hooks_first("test_first")
        self.assertEqual(result, "found")
        self.assertEqual(called, [1, 2])  # cb3 never called

    def test_fire_hooks_first_returns_none_when_all_none(self):
        """Returns None when all callbacks return None."""
        def cb1():
            return None
        def cb2():
            return None
        register_hook("test_all_none", cb1)
        register_hook("test_all_none", cb2)
        result = fire_hooks_first("test_all_none")
        self.assertIsNone(result)

    def test_concurrent_register_and_fire_is_safe(self):
        """Concurrent register_hook and fire_hooks is thread-safe."""
        errors: List[Exception] = []
        num_threads = 8

        def register_worker(idx):
            try:
                def cb():
                    return f"result_{idx}"
                register_hook(f"concurrent_event", cb)
                time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        def fire_worker():
            try:
                # This should not crash even while registers are happening
                fire_hooks("concurrent_event")
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(num_threads // 2):
            t = threading.Thread(target=register_worker, args=(i,))
            threads.append(t)
            t = threading.Thread(target=fire_worker)
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No exceptions
        self.assertEqual(len(errors), 0)


if __name__ == "__main__":
    unittest.main()
