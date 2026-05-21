"""Structural tests: verify hook integration points exist in gateway/run.py.

These tests catch upstream merge regressions where our hook calls are silently
dropped from gateway/run.py. If upstream rewrites a function and our hooks
vanish, these tests fail immediately.
"""

import ast
import inspect
from pathlib import Path

import pytest

import gateway.run


@pytest.fixture(scope="module")
def gateway_run_ast():
    """Parse gateway/run.py into an AST once per test module."""
    source = Path(gateway.run.__file__).read_text(encoding="utf-8")
    return ast.parse(source)


def _find_function(node: ast.AST, name: str) -> ast.FunctionDef | None:
    """Find a top-level or nested function definition by name."""
    for child in ast.walk(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == name:
            return child
    return None


def _contains_call(node: ast.AST, func_name: str) -> bool:
    """Return True if the AST subtree contains a call to `func_name`."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            # Handle simple names: fire_hooks_first(...)
            if isinstance(child.func, ast.Name) and child.func.id == func_name:
                return True
            # Handle attributes: fire_hooks_first(...) is already covered above
            # because we import it directly. But be safe:
            if isinstance(child.func, ast.Attribute) and child.func.attr == func_name:
                return True
    return False


def _contains_str_literal(node: ast.AST, text: str) -> bool:
    """Return True if the AST subtree contains a string literal matching `text`."""
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value == text:
            return True
    return False


class TestRunAgentHookIntegration:
    """Verify _run_agent contains the hook calls our extensions depend on."""

    def test_run_agent_contains_get_ephemeral_call(self, gateway_run_ast):
        """_run_agent MUST call fire_hooks_first('get_ephemeral', ...) —
        otherwise channel-bound souls are never injected."""
        func = _find_function(gateway_run_ast, "_run_agent")
        assert func is not None, "_run_agent function not found in gateway/run.py"
        assert _contains_call(func, "fire_hooks_first"), (
            "Missing fire_hooks_first call in _run_agent. "
            "Channel binding soul injection will break."
        )
        assert _contains_str_literal(func, "get_ephemeral"), (
            "fire_hooks_first is called but not with 'get_ephemeral'. "
            "Soul injection hook is missing."
        )

    def test_run_agent_passes_soul_identity_to_aiagent(self, gateway_run_ast):
        """_run_agent MUST pass soul_identity=... to AIAgent() —
        otherwise the stable system prompt uses DEFAULT_AGENT_IDENTITY."""
        func = _find_function(gateway_run_ast, "_run_agent")
        assert func is not None
        # Look for keyword argument `soul_identity=` in any Call node
        for child in ast.walk(func):
            if isinstance(child, ast.keyword) and child.arg == "soul_identity":
                return
        pytest.fail(
            "AIAgent constructor in _run_agent is missing soul_identity= keyword. "
            "Channel-bound persona will not replace DEFAULT_AGENT_IDENTITY."
        )


class TestCommandDispatchIntegration:
    """Verify custom slash-command dispatch wiring survived the merge."""

    def test_bind_command_dispatch_exists(self, gateway_run_ast):
        """The /bind command dispatch block must exist — otherwise /bind
        produces 'Unknown command' even though the handler function exists."""
        func = _find_function(gateway_run_ast, "_handle_message")
        assert func is not None, "_handle_message not found"
        # We check for the literal string that identifies the dispatch block
        assert _contains_str_literal(func, "bind"), (
            "Missing /bind command dispatch in _handle_message. "
            "Users will see 'Unknown command' when typing /bind."
        )

    def test_bind_handler_imports_handle_bind_command(self, gateway_run_ast):
        """The dispatch block must import handle_bind_command from the
        channel_binding extension."""
        source = Path(gateway.run.__file__).read_text(encoding="utf-8")
        assert "from gateway.extensions.channel_binding import handle_bind_command" in source, (
            "Missing import for handle_bind_command. /bind dispatch is broken."
        )


class TestSkillsOverrideIntegration:
    """Verify skills override hook result reaches the agent."""

    def test_run_agent_has_skills_override_param(self, gateway_run_ast):
        """_run_agent MUST accept skills_override param — otherwise
        get_skills_override result is fetched but never applied."""
        func = _find_function(gateway_run_ast, "_run_agent")
        assert func is not None, "_run_agent not found"
        for arg in func.args.args + func.args.kwonlyargs:
            if arg.arg == "skills_override":
                return
        pytest.fail(
            "_run_agent is missing skills_override parameter. "
            "get_skills_override hook result will be silently ignored."
        )

    def test_skills_override_merged_into_enabled_toolsets(self, gateway_run_ast):
        """skills_override must be merged into enabled_toolsets inside _run_agent."""
        func = _find_function(gateway_run_ast, "_run_agent")
        assert func is not None
        source = Path(gateway.run.__file__).read_text(encoding="utf-8")
        assert "skills_override" in source and "enabled_toolsets" in source, (
            "skills_override is not merged into enabled_toolsets. "
            "Channel-bound skills will not be loaded."
        )


class TestModelOverrideCondition:
    """Verify model-only overrides are accepted (no api_key required)."""

    def test_resolve_session_allows_model_without_api_key(self, gateway_run_ast):
        """_resolve_session_agent_runtime must accept model-only overrides.

        Original bug: `if _ch_runtime.get("api_key")` rejected all overrides
        that only set model (no api_key). Soul frontmatters never set api_key.
        """
        func = _find_function(gateway_run_ast, "_resolve_session_agent_runtime")
        assert func is not None, "_resolve_session_agent_runtime not found"
        source = Path(gateway.run.__file__).read_text(encoding="utf-8")
        # The fix uses: if _ch_runtime.get("api_key") or _ch_overrides.get("model")
        assert 'or _ch_overrides.get("model")' in source, (
            "Model-only channel binding overrides are silently ignored. "
            "Condition must check both api_key AND model."
        )


class TestCacheSignatureIntegration:
    """Verify agent cache signature includes soul_identity."""

    def test_cache_signature_includes_soul_identity(self, gateway_run_ast):
        """_agent_config_signature must include soul_identity param —
        otherwise soul switches reuse cached agents with stale identity."""
        func = _find_function(gateway_run_ast, "_agent_config_signature")
        assert func is not None, "_agent_config_signature not found"
        for arg in func.args.args + func.args.kwonlyargs:
            if arg.arg == "soul_identity":
                return
        pytest.fail(
            "_agent_config_signature is missing soul_identity parameter. "
            "Soul switches will not invalidate the agent cache."
        )


class TestSessionLifecycleHooks:
    """Verify session lifecycle hooks are wired in gateway/run.py."""

    def test_on_new_session_hook_fired(self, gateway_run_ast):
        """on_new_session hook must fire when a new session is created."""
        func = _find_function(gateway_run_ast, "_handle_message_with_agent")
        assert func is not None
        assert _contains_str_literal(func, "on_new_session"), (
            "Missing on_new_session hook call. Channel binding won't auto-apply "
            "config-level bindings for new sessions."
        )

    def test_on_session_reset_hook_fired(self, gateway_run_ast):
        """on_session_reset hook must fire after /new or /reset."""
        func = _find_function(gateway_run_ast, "_handle_reset_command")
        assert func is not None
        assert _contains_str_literal(func, "on_session_reset"), (
            "Missing on_session_reset hook call. Extensions won't be notified "
            "when the user resets a session."
        )

    def test_on_session_cleanup_hook_fired(self, gateway_run_ast):
        """on_session_cleanup hook must fire when sessions expire."""
        # This is usually in a loop or cleanup function; search the whole file
        assert _contains_str_literal(gateway_run_ast, "on_session_cleanup"), (
            "Missing on_session_cleanup hook call. Extension state will leak "
            "when sessions expire."
        )
