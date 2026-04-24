"""Tests for gateway/extensions/__init__.py hook system.

Covers all hook registry functions including error handling and edge cases.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest


class TestRegisterHook:
    """register_hook stores callbacks keyed by event name."""

    def test_registers_single_hook(self):
        from gateway.extensions import register_hook, _HOOKS
        _HOOKS.clear()
        fn = lambda x: x
        register_hook("test_event", fn)
        assert "test_event" in _HOOKS
        assert _HOOKS["test_event"] == [fn]

    def test_registers_multiple_hooks_same_event(self):
        from gateway.extensions import register_hook, _HOOKS
        _HOOKS.clear()
        f1 = lambda: 1
        f2 = lambda: 2
        register_hook("multi", f1)
        register_hook("multi", f2)
        assert _HOOKS["multi"] == [f1, f2]

    def test_thread_safe_registration(self):
        import threading
        from gateway.extensions import register_hook, _HOOKS
        _HOOKS.clear()
        errors = []

        def worker(i):
            try:
                register_hook("concurrent", lambda: i)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(_HOOKS["concurrent"]) == 20


class TestFireHooks:
    """fire_hooks calls all registered handlers and collects non-None results."""

    @pytest.fixture(autouse=True)
    def _clean_hooks(self):
        from gateway.extensions import _HOOKS
        _HOOKS.clear()
        yield
        _HOOKS.clear()

    def test_collects_non_none_results(self):
        from gateway.extensions import register_hook, fire_hooks
        register_hook("ev", lambda x: x * 2)
        register_hook("ev", lambda x: None)
        register_hook("ev", lambda x: x + 1)
        assert fire_hooks("ev", 5) == [10, 6]

    def test_returns_empty_list_when_no_hooks(self):
        from gateway.extensions import fire_hooks
        assert fire_hooks("missing_event", 1, 2, 3) == []

    def test_passes_args_and_kwargs(self):
        from gateway.extensions import register_hook, fire_hooks
        captured = []
        register_hook("capture", lambda *a, **k: captured.append((a, k)))
        fire_hooks("capture", 1, 2, foo="bar")
        assert captured == [((1, 2), {"foo": "bar"})]

    def test_exception_in_one_hook_does_not_break_others(self, caplog):
        from gateway.extensions import register_hook, fire_hooks
        register_hook("partial", lambda: 1)
        register_hook("partial", lambda: (_ for _ in ()).throw(ValueError("boom")))
        register_hook("partial", lambda: 3)
        with caplog.at_level(logging.DEBUG):
            results = fire_hooks("partial")
        assert results == [1, 3]
        assert "Error in hook partial" in caplog.text

    def test_exception_logging_includes_function_name(self, caplog):
        from gateway.extensions import register_hook, fire_hooks
        def named_bad_hook():
            raise RuntimeError("oops")
        register_hook("named", named_bad_hook)
        with caplog.at_level(logging.DEBUG):
            fire_hooks("named")
        assert "named_bad_hook" in caplog.text


class TestFireHooksFirst:
    """fire_hooks_first returns the first non-None result."""

    @pytest.fixture(autouse=True)
    def _clean_hooks(self):
        from gateway.extensions import _HOOKS
        _HOOKS.clear()
        yield
        _HOOKS.clear()

    def test_returns_first_non_none(self):
        from gateway.extensions import register_hook, fire_hooks_first
        register_hook("first", lambda: None)
        register_hook("first", lambda: "winner")
        register_hook("first", lambda: "loser")
        assert fire_hooks_first("first") == "winner"

    def test_returns_none_when_all_hooks_return_none(self):
        from gateway.extensions import register_hook, fire_hooks_first
        register_hook("all_none", lambda: None)
        register_hook("all_none", lambda: None)
        assert fire_hooks_first("all_none") is None

    def test_returns_none_when_no_hooks(self):
        from gateway.extensions import fire_hooks_first
        assert fire_hooks_first("nope") is None

    def test_exception_skips_to_next_hook(self, caplog):
        from gateway.extensions import register_hook, fire_hooks_first
        register_hook("skip", lambda: (_ for _ in ()).throw(KeyError("bad")))
        register_hook("skip", lambda: "ok")
        with caplog.at_level(logging.DEBUG):
            result = fire_hooks_first("skip")
        assert result == "ok"
        assert "Error in hook skip" in caplog.text

    def test_all_exceptions_returns_none(self, caplog):
        from gateway.extensions import register_hook, fire_hooks_first
        register_hook("all_bad", lambda: (_ for _ in ()).throw(TypeError("a")))
        register_hook("all_bad", lambda: (_ for _ in ()).throw(TypeError("b")))
        with caplog.at_level(logging.DEBUG):
            result = fire_hooks_first("all_bad")
        assert result is None


class TestListHooks:
    """list_hooks returns a diagnostic summary."""

    @pytest.fixture(autouse=True)
    def _clean_hooks(self):
        from gateway.extensions import _HOOKS
        _HOOKS.clear()
        yield
        _HOOKS.clear()

    def test_returns_event_names_and_function_names(self):
        from gateway.extensions import register_hook, list_hooks
        def my_handler():
            pass
        register_hook("ev1", my_handler)
        summary = list_hooks()
        assert summary == {"ev1": ["my_handler"]}

    def test_returns_empty_when_no_hooks(self):
        from gateway.extensions import list_hooks
        assert list_hooks() == {}

    def test_handles_lambda_names(self):
        from gateway.extensions import register_hook, list_hooks
        register_hook("ev", lambda: None)
        summary = list_hooks()
        assert "lambda" in summary["ev"][0]


class TestDiscoverExtensions:
    """_discover_extensions imports all modules in gateway/extensions/."""

    def test_skips_underscore_modules(self):
        from gateway.extensions import _discover_extensions
        mock_info = MagicMock()
        mock_info.name = "_private"
        with patch("pkgutil.iter_modules", return_value=[mock_info]):
            with patch("importlib.import_module") as mock_import:
                _discover_extensions()
                mock_import.assert_not_called()

    def test_logs_warning_on_failed_import(self, caplog):
        from gateway.extensions import _discover_extensions
        mock_info = MagicMock()
        mock_info.name = "broken"
        with patch("pkgutil.iter_modules", return_value=[mock_info]):
            with patch("importlib.import_module", side_effect=ImportError("nope")):
                with caplog.at_level(logging.WARNING):
                    _discover_extensions()
        assert "Failed to load broken" in caplog.text

    def test_logs_debug_on_successful_import(self, caplog):
        from gateway.extensions import _discover_extensions
        mock_info = MagicMock()
        mock_info.name = "good"
        with patch("pkgutil.iter_modules", return_value=[mock_info]):
            with patch("importlib.import_module"):
                with caplog.at_level(logging.DEBUG):
                    _discover_extensions()
        assert "Loaded: good" in caplog.text
