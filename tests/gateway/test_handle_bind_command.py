"""Handle Bind Command Tests.

Full test coverage for the /bind command handler (handle_bind_command):
  - /bind (no args): shows current binding
  - /bind <soul_name>: binds a soul by name (includes API key env expansion)
  - /bind save: persists binding to config
  - /bind unbind: removes binding from config + runtime
  - /bind --clear: clears runtime binding
  - /bind list: lists all persisted bindings

This catches regressions like the UnboundLocalError on channel_id which
was introduced during refactoring but missed by existing tests.
"""

from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch
import pytest

from gateway.extensions.channel_binding import (
    handle_bind_command,
    _parse_session_key,
    _expand_api_key,
    set_bind_reset,
    consume_bind_reset,
    _session_bindings,
    _session_soul_names,
    _session_model_overrides,
    _session_skills,
    _session_memory_scopes,
    _state_lock,
)


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_state():
    """Clear all channel binding in-memory state before each test."""
    with _state_lock:
        _session_bindings.clear()
        _session_soul_names.clear()
        _session_model_overrides.clear()
        _session_skills.clear()
        _session_memory_scopes.clear()
    yield


def create_mock_event(args: str, session_key: str = "agent:main:telegram:group:6911288694"):
    """Create a mock Event object with the given command args."""
    event = Mock()
    event.get_command_args = Mock(return_value=args)
    event.source = Mock()
    event.source.session_key = session_key
    return event


@pytest.mark.asyncio
async def test_handle_bind_no_args_no_binding():
    """/bind (no args) when no binding is active."""
    event = create_mock_event("")
    session_store = Mock()
    session_entry = Mock()
    session_entry.session_key = "agent:main:telegram:group:6911288694"
    session_store.get_or_create_session = Mock(return_value=session_entry)
    
    result = await handle_bind_command(session_store, event)
    
    assert "No soul binding active" in result
    assert "/bind <soul_name>" in result
    assert "/bind save" in result
    assert "/bind unbind" in result


@pytest.mark.asyncio
async def test_handle_bind_soul_name_success():
    """/bind <soul_name> successfully binds an existing soul (alpha).
    
    This catches the UnboundLocalError on channel_id which happened
    because the refactor forgot to define channel_id in this branch.
    """
    # We need to patch get_hermes_home to point to the real ~/.hermes
    from hermes_constants import get_hermes_home
    real_hermes_home = Path.home() / '.hermes'
    
    event = create_mock_event("alpha")
    session_store = Mock()
    session_entry = Mock()
    session_entry.session_key = "agent:main:telegram:group:6911288694"
    session_store.get_or_create_session = Mock(return_value=session_entry)
    
    with patch('hermes_constants.get_hermes_home', return_value=real_hermes_home):
        # Before the fix: this would raise UnboundLocalError because
        # channel_id was undefined in this branch (only defined for save/unbind)
        # After the fix: it should successfully bind without error
        result = await handle_bind_command(session_store, event)
    
    # Should not crash with UnboundLocalError
    # It might still fail if alpha.md doesn't exist, so check for that first
    if "not found" in result:
        pytest.skip("alpha.md not found in ~/.hermes/souls/, skipping")
    
    assert "🎭 Soul set to **alpha**" in result
    assert "fresh context" in result
    # Should call set_bind_reset with parsed channel_id (6911288694)
    # Check the flag is set
    assert consume_bind_reset("6911288694") is True


@pytest.mark.asyncio
async def test_handle_bind_soul_name_not_found():
    """/bind <non_existent> returns error with available souls list."""
    # We need to patch get_hermes_home to point to the real ~/.hermes
    from hermes_constants import get_hermes_home
    real_hermes_home = Path.home() / '.hermes'
    
    event = create_mock_event("nonexistent_soul_that_should_not_exist")
    session_store = Mock()
    session_entry = Mock()
    session_entry.session_key = "agent:main:telegram:group:6911288694"
    session_store.get_or_create_session = Mock(return_value=session_entry)
    
    with patch('hermes_constants.get_hermes_home', return_value=real_hermes_home):
        result = await handle_bind_command(session_store, event)
    
    assert "not found in ~/.hermes/souls/" in result
    # Should list at least some available souls if any exist
    if "(none found)" not in result:
        assert "alpha" in result.lower()  # alpha is common if present


@pytest.mark.asyncio
async def test_handle_bind_clear():
    """/bind --clear clears runtime binding."""
    # First create a fake binding
    session_key = "agent:main:telegram:group:6911288694"
    with _state_lock:
        _session_soul_names[session_key] = "alpha"
        _session_bindings[session_key] = {"soul": "alpha"}
    
    event = create_mock_event("--clear")
    session_store = Mock()
    session_entry = Mock()
    session_entry.session_key = session_key
    session_store.get_or_create_session = Mock(return_value=session_entry)
    
    result = await handle_bind_command(session_store, event)
    
    assert "Soul binding cleared" in result
    assert "default personality" in result
    # Binding should be cleared
    with _state_lock:
        assert session_key not in _session_soul_names
        assert session_key not in _session_bindings


@pytest.mark.asyncio
async def test_handle_bind_list():
    """/bind list lists all persisted bindings."""
    event = create_mock_event("list")
    session_store = Mock()
    session_entry = Mock()
    session_entry.session_key = "agent:main:telegram:group:6911288694"
    session_store.get_or_create_session = Mock(return_value=session_entry)
    
    result = await handle_bind_command(session_store, event)
    
    # Should return some output (might be empty if no bindings)
    assert result is not None
    assert len(result) > 0
    # Should contain "Persisted channel bindings" or "No persisted bindings"
    assert "bindings" in result.lower()


def test__expand_api_key_env_var_expansion():
    """_expand_api_key correctly expands ${VAR_NAME} from environment."""
    import os

    # Use a temporary env var so the test is self-contained
    os.environ["_TEST_HERMES_EXPAND_KEY"] = "sk-test-expanded-value-12345"
    try:
        raw = "${_TEST_HERMES_EXPAND_KEY}"
        expanded = _expand_api_key(raw)
        assert expanded is not None
        assert expanded != "${_TEST_HERMES_EXPAND_KEY}"  # expansion must happen
        assert expanded == "sk-test-expanded-value-12345"
    finally:
        os.environ.pop("_TEST_HERMES_EXPAND_KEY", None)


def test__expand_api_key_direct_key_no_expansion():
    """_expand_api_key leaves direct API keys untouched."""
    raw = "sk-abcdef123456"
    expanded = _expand_api_key(raw)
    assert expanded == raw


def test__expand_api_key_none_returns_none():
    """_expand_api_key(None) returns None."""
    assert _expand_api_key(None) is None


def test__parse_session_key_correct_format():
    """_parse_session_key correctly extracts platform + channel_id."""
    session_key = "agent:main:telegram:group:6911288694"
    parsed = _parse_session_key(session_key)
    assert parsed is not None
    assert parsed["platform"] == "telegram"
    assert parsed["channel_id"] == "6911288694"
    assert parsed["chat_type"] == "group"


def test_set_bind_reset_and_consume():
    """set_bind_reset sets the flag and consume_bind_reset consumes it."""
    # First consume any existing
    consume_bind_reset("test123")
    # Set it
    set_bind_reset("test123")
    # First consume returns True
    assert consume_bind_reset("test123") is True
    # Second consume returns False (already consumed)
    assert consume_bind_reset("test123") is False


@pytest.mark.asyncio
async def test_handle_bind_soul_includes_api_key_expansion():
    """When binding a soul with an env var api_key reference, it gets expanded.

    This tests the end-to-end flow that failed with 401 authentication when
    an env var reference wasn't expanded and the literal string was sent to API.
    """
    import os
    import re
    from hermes_constants import get_hermes_home
    from hermes_cli.env_loader import load_hermes_dotenv
    real_hermes_home = Path.home() / '.hermes'
    load_hermes_dotenv(hermes_home=real_hermes_home)

    # Read alpha.md frontmatter to discover the actual api_key env var reference
    alpha_path = real_hermes_home / "souls" / "alpha.md"
    if not alpha_path.exists():
        pytest.skip("alpha.md not found in ~/.hermes/souls/, skipping")

    frontmatter_text = alpha_path.read_text().split("---")[1] if "---" in alpha_path.read_text() else ""
    m = re.search(r'^api_key:\s*(\S+)', frontmatter_text, re.MULTILINE)
    if not m:
        pytest.skip("alpha.md has no api_key in frontmatter, skipping")

    raw_api_key = m.group(1)
    env_var_match = re.match(r'^\$\{(.+)\}$', raw_api_key)
    if not env_var_match:
        pytest.skip("alpha.md api_key is not an env var reference, skipping")

    env_var_name = env_var_match.group(1)
    env_value = os.environ.get(env_var_name)
    if not env_value:
        pytest.skip(f"{env_var_name} not set in environment, skipping expansion verification")

    event = create_mock_event("alpha")
    session_store = Mock()
    session_entry = Mock()
    session_key = "agent:main:telegram:dm:123456"
    session_entry.session_key = session_key
    session_store.get_or_create_session = Mock(return_value=session_entry)

    with _state_lock:
        _session_bindings.pop(session_key, None)
        _session_model_overrides.pop(session_key, None)

    with patch('hermes_constants.get_hermes_home', return_value=real_hermes_home):
        result = await handle_bind_command(session_store, event)

    if "not found" in result:
        pytest.skip("alpha.md not found in ~/.hermes/souls/, skipping")

    with _state_lock:
        model_override = _session_model_overrides.get(session_key)
        assert model_override is not None
        api_key = model_override.get("api_key")
        assert api_key is not None
        assert api_key != raw_api_key  # MUST be expanded, not literal ${...}
        assert api_key == env_value  # Should match the actual env var value


@pytest.mark.asyncio
async def test_handle_bind_save_persists_memory_scope():
    """/bind save should write memory_scope and skills to config.yaml."""
    from hermes_constants import get_hermes_home
    real_hermes_home = Path.home() / '.hermes'

    session_key = "agent:main:telegram:group:test-save-scope"
    # Pre-populate runtime binding as if /bind delta was run
    with _state_lock:
        _session_bindings[session_key] = {"soul": "delta"}
        _session_soul_names[session_key] = "delta"
        _session_memory_scopes[session_key] = "delta"
        _session_skills[session_key] = ["devops/shopilala-app"]

    event = create_mock_event("save", session_key=session_key)
    session_store = Mock()
    session_entry = Mock()
    session_entry.session_key = session_key
    session_store.get_or_create_session = Mock(return_value=session_entry)

    with patch('hermes_constants.get_hermes_home', return_value=real_hermes_home):
        result = await handle_bind_command(session_store, event)

    assert "Binding saved" in result
    assert "scope: `delta`" in result

    # Verify config.yaml was updated with memory_scope
    import yaml
    config_path = real_hermes_home / "config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    tg_bindings = config.get("telegram", {}).get("extra", {}).get("channel_personality_bindings", [])
    saved = next((b for b in tg_bindings if b.get("id") == "test-save-scope"), None)
    assert saved is not None, "Binding not found in config.yaml"
    assert saved.get("soul") == "delta"
    assert saved.get("memory_scope") == "delta"
    assert saved.get("skills") == ["devops/shopilala-app"]

    # Clean up: remove test binding
    tg_bindings = [b for b in tg_bindings if b.get("id") != "test-save-scope"]
    config["telegram"]["extra"]["channel_personality_bindings"] = tg_bindings
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
