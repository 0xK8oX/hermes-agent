"""Channel Binding Health Check Tests.

Validates the full pipeline: binding injection → runtime retrieval.
This acts as a smoke test / health check for the channel binding system.

Three layers tested:
  1. Binding layer — _apply_binding correctly injects soul + model + memory_scope
  2. Runtime layer — _get_model_override + _get_ephemeral return expected values
  3. Live config — validates production config.yaml bindings end-to-end

Run: pytest tests/gateway/test_channel_binding_health_check.py -v
Run (live config only): pytest tests/gateway/test_channel_binding_health_check.py -v -k Live
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.extensions.channel_binding import (
    _apply_binding,
    _get_ephemeral,
    _get_memory_scope,
    _get_model_override,
    _get_skills_override,
    _session_bindings,
    _session_memory_scopes,
    _session_model_overrides,
    _session_skills,
    _session_soul_names,
    _session_souls,
    _state_lock,
    _bound_channel_index,
)


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_state():
    """Clear all channel binding in-memory state before each test."""
    with _state_lock:
        _session_souls.clear()
        _session_soul_names.clear()
        _session_model_overrides.clear()
        _session_skills.clear()
        _session_memory_scopes.clear()
        _session_bindings.clear()
        _bound_channel_index.clear()
    yield
    with _state_lock:
        _session_souls.clear()
        _session_soul_names.clear()
        _session_model_overrides.clear()
        _session_skills.clear()
        _session_memory_scopes.clear()
        _session_bindings.clear()
        _bound_channel_index.clear()


def _real_hermes_home() -> Path:
    """Resolve the real Hermes home directory (always ~/.hermes, not pytest temp)."""
    return Path.home() / ".hermes"


# ── Layer 2: Binding Injection ───────────────────────────────────────

class TestBindingInjection:
    """Verify _apply_binding correctly populates all state dicts."""

    def test_soul_injection(self):
        """_apply_binding with soul populates _session_souls."""
        session_key = "agent:main:whatsapp:group:test-group@g.us"
        _apply_binding(session_key, {
            "soul": "alpha",
            "_content": "You are Alpha, an autonomous Full-Cycle Agent.",
        })

        assert _session_souls[session_key] == "You are Alpha, an autonomous Full-Cycle Agent."
        assert _session_soul_names[session_key] == "alpha"

    def test_model_injection(self):
        """_apply_binding with model/provider populates _session_model_overrides."""
        session_key = "agent:main:whatsapp:group:test-group@g.us"
        _apply_binding(session_key, {
            "soul": "alpha",
            "_content": "Alpha soul",
            "model": "MiniMax-M2.7",
            "provider": "anthropic",
            "base_url": "https://api.minimaxi.com/anthropic",
            "api_key": "sk-test-key",
        })

        override = _session_model_overrides[session_key]
        assert override["model"] == "MiniMax-M2.7"
        assert override["provider"] == "anthropic"
        assert override["base_url"] == "https://api.minimaxi.com/anthropic"
        assert override["api_key"] == "sk-test-key"

    def test_skills_injection(self):
        """_apply_binding with skills populates _session_skills."""
        session_key = "agent:main:whatsapp:group:test-group@g.us"
        _apply_binding(session_key, {
            "soul": "alpha",
            "_content": "Alpha soul",
            "skills": ["github/github-issues", "github/github-pr-workflow"],
        })

        assert _session_skills[session_key] == ["github/github-issues", "github/github-pr-workflow"]

    def test_memory_scope_injection(self):
        """_apply_binding with memory_scope populates _session_memory_scopes."""
        session_key = "agent:main:whatsapp:group:test-group@g.us"
        _apply_binding(session_key, {
            "soul": "alpha",
            "_content": "Alpha soul",
            "memory_scope": "alpha",
        })

        assert _session_memory_scopes[session_key] == "alpha"

    def test_full_binding_injection(self):
        """Full binding with all fields injects everything."""
        session_key = "agent:main:whatsapp:group:full-test@g.us"
        _apply_binding(session_key, {
            "soul": "alpha",
            "_content": "You are Alpha, the全能 assistant.",
            "model": "MiniMax-M2.7",
            "provider": "anthropic",
            "base_url": "https://api.minimaxi.com/anthropic",
            "api_key": "sk-cp-test",
            "skills": ["github/github-issues", "software-development/systematic-debugging"],
            "memory_scope": "alpha",
        })

        # Verify all dicts populated
        assert _session_souls[session_key] == "You are Alpha, the全能 assistant."
        assert _session_model_overrides[session_key]["model"] == "MiniMax-M2.7"
        assert _session_model_overrides[session_key]["provider"] == "anthropic"
        assert _session_skills[session_key] == ["github/github-issues", "software-development/systematic-debugging"]
        assert _session_memory_scopes[session_key] == "alpha"
        assert _session_soul_names[session_key] == "alpha"

    def test_model_only_binding_no_soul(self):
        """Binding with only model (no soul content) still injects model."""
        session_key = "agent:main:telegram:group:model-only"
        _apply_binding(session_key, {
            "soul": "fed",
            "model": "openrouter/free",
            "provider": "custom:openrouter-free-proxy",
            "base_url": "http://localhost:8787",
        })

        assert _session_model_overrides[session_key]["model"] == "openrouter/free"
        assert _session_model_overrides[session_key]["provider"] == "custom:openrouter-free-proxy"
        # Soul might not be set if _content not provided and file not loadable
        # That's OK — model override is independent


# ── Layer 3: Runtime Retrieval (Health Check Core) ───────────────────

class TestRuntimeRetrievalHealthCheck:
    """
    Simulate the full flow: create binding → retrieve runtime values.
    This is the core health check — verifies the data flows correctly
    from binding injection to the getters that gateway/run.py calls.
    """

    def _setup_binding(self, session_key, binding):
        """Helper: apply binding and return nothing (just set up state)."""
        _apply_binding(session_key, binding)

    def test_alpha_binding_health_check(self):
        """
        Full health check for 'alpha' soul:
        - Soul content loaded → agent knows who it is
        - Model override set → agent uses correct model
        - Memory scope set → agent uses correct memory isolation
        """
        session_key = "agent:main:whatsapp:group:alpha-test@g.us"
        self._setup_binding(session_key, {
            "soul": "alpha",
            "_content": "You are Alpha, an autonomous Full-Cycle Agent.",
            "model": "MiniMax-M2.7",
            "provider": "anthropic",
            "base_url": "https://api.minimaxi.com/anthropic",
            "api_key": "sk-cp-test-key",
            "skills": ["github/github-issues", "github/github-pr-workflow"],
            "memory_scope": "alpha",
        })

        # Simulate what gateway/run.py does when creating a new agent:
        # 1. Get ephemeral (soul content)
        soul_content = _get_ephemeral(session_key)
        assert soul_content is not None, "Soul content should not be None"
        assert "Alpha" in soul_content, f"Soul content should mention 'Alpha', got: {soul_content[:100]}"

        # 2. Get model override
        model_override = _get_model_override(session_key)
        assert model_override is not None, "Model override should not be None"
        assert model_override["model"] == "MiniMax-M2.7", (
            f"Expected model 'MiniMax-M2.7', got '{model_override['model']}'"
        )
        assert model_override["provider"] == "anthropic", (
            f"Expected provider 'anthropic', got '{model_override['provider']}'"
        )

        # 3. Get skills override
        skills = _get_skills_override(session_key)
        assert skills is not None, "Skills override should not be None"
        assert "github/github-issues" in skills, f"Expected 'github/github-issues' in skills, got: {skills}"

        # 4. Get memory scope
        memory_scope = _get_memory_scope(session_key)
        assert memory_scope == "alpha", f"Expected memory_scope 'alpha', got '{memory_scope}'"

    def test_dev_binding_health_check(self):
        """Health check for 'dev' soul with different model."""
        session_key = "agent:main:discord:thread:12345:67890"
        self._setup_binding(session_key, {
            "soul": "dev",
            "_content": "You are Sam, a Senior Full-Stack Developer.",
            "model": "glm-5.1",
            "provider": "custom",
            "base_url": "https://open.bigmodel.cn/api/coding/paas/v4",
            "api_key": "test-glm-key",
            "skills": ["github/github-issues", "github/github-code-review"],
            "memory_scope": "dev",
        })

        soul_content = _get_ephemeral(session_key)
        assert soul_content is not None
        assert "Sam" in soul_content

        model_override = _get_model_override(session_key)
        assert model_override is not None
        assert model_override["model"] == "glm-5.1"
        assert model_override["provider"] == "custom"

        memory_scope = _get_memory_scope(session_key)
        assert memory_scope == "dev"

    def test_accountant_binding_health_check(self):
        """Health check for 'accountant' soul."""
        session_key = "agent:main:whatsapp:group:acct-test@g.us"
        self._setup_binding(session_key, {
            "soul": "accountant",
            "_content": "You are the company accountant.",
            "memory_scope": "accountant",
            "skills": ["productivity/professional-pdf-generation"],
        })

        soul_content = _get_ephemeral(session_key)
        assert soul_content is not None
        assert "accountant" in soul_content.lower()

        # Accountant may not have model override — that's fine
        model_override = _get_model_override(session_key)
        # No assertion on model_override being non-None — it's optional

        memory_scope = _get_memory_scope(session_key)
        assert memory_scope == "accountant"

    def test_unbound_session_returns_none(self):
        """Session with no binding should return None for all getters."""
        session_key = "agent:main:whatsapp:group:unbound@g.us"

        assert _get_ephemeral(session_key) is None
        assert _get_model_override(session_key) is None
        assert _get_skills_override(session_key) is None
        assert _get_memory_scope(session_key) is None

    def test_binding_isolation_different_sessions(self):
        """Two different sessions should have independent bindings."""
        key_alpha = "agent:main:whatsapp:group:alpha@g.us"
        key_dev = "agent:main:discord:thread:999:888"

        _apply_binding(key_alpha, {
            "soul": "alpha",
            "_content": "Alpha content",
            "model": "MiniMax-M2.7",
            "provider": "anthropic",
            "memory_scope": "alpha",
        })
        _apply_binding(key_dev, {
            "soul": "dev",
            "_content": "Dev content",
            "model": "glm-5.1",
            "provider": "custom",
            "memory_scope": "dev",
        })

        # Alpha session
        assert _get_ephemeral(key_alpha) == "Alpha content"
        assert _get_model_override(key_alpha)["model"] == "MiniMax-M2.7"
        assert _get_memory_scope(key_alpha) == "alpha"

        # Dev session — completely independent
        assert _get_ephemeral(key_dev) == "Dev content"
        assert _get_model_override(key_dev)["model"] == "glm-5.1"
        assert _get_memory_scope(key_dev) == "dev"

        # Cross-contamination check
        assert _get_ephemeral(key_alpha) != _get_ephemeral(key_dev)
        assert _get_model_override(key_alpha)["model"] != _get_model_override(key_dev)["model"]


# ── Live Config Health Check ─────────────────────────────────────────

class TestLiveConfigHealthCheck:
    """
    Load actual config.yaml bindings and verify each one end-to-end.
    This is the real "health check" — validates production config.

    Skipped gracefully if config not available or soul files missing.
    """

    @staticmethod
    def _read_soul_content_direct(soul_name):
        """Read soul file directly from ~/.hermes/souls/ (bypasses get_hermes_home)."""
        soul_path = _real_hermes_home() / "souls" / f"{soul_name}.md"
        if not soul_path.exists():
            return None
        content = soul_path.read_text()
        # Strip YAML frontmatter
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                content = content[end + 3:].strip()
        return content

    @pytest.fixture
    def live_bindings(self):
        """Load actual channel bindings from config.yaml + inline soul content."""
        import yaml
        config_path = _real_hermes_home() / "config.yaml"
        if not config_path.exists():
            pytest.skip("config.yaml not found")

        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        all_bindings = []
        for platform_key in ("whatsapp", "telegram", "discord", "slack"):
            platform_cfg = cfg.get(platform_key, {})
            if isinstance(platform_cfg, dict):
                extra = platform_cfg.get("extra", {})
                if isinstance(extra, dict):
                    bindings = extra.get("channel_personality_bindings", [])
                    if isinstance(bindings, list):
                        for b in bindings:
                            b["_platform"] = platform_key
                            # Pre-load soul content to bypass get_hermes_home()
                            soul_name = b.get("soul")
                            if soul_name and "_content" not in b:
                                content = self._read_soul_content_direct(soul_name)
                                if content:
                                    b["_content"] = content
                            all_bindings.append(b)

        if not all_bindings:
            pytest.skip("No channel_personality_bindings found in config.yaml")

        return all_bindings

    def test_all_config_bindings_soul_files_exist(self, live_bindings):
        """Every bound soul must have a corresponding file on disk."""
        errors = []
        for binding in live_bindings:
            soul_name = binding.get("soul")
            if not soul_name:
                continue
            soul_path = _real_hermes_home() / "souls" / f"{soul_name}.md"
            if not soul_path.exists():
                errors.append(f"Soul '{soul_name}' → file not found: {soul_path}")

        assert not errors, "\n".join(errors)

    def test_all_config_bindings_soul_content_nonempty(self, live_bindings):
        """Every bound soul must load non-empty content."""
        errors = []
        for binding in live_bindings:
            soul_name = binding.get("soul")
            if not soul_name:
                continue
            content = self._read_soul_content_direct(soul_name)
            if not content or not content.strip():
                errors.append(f"Soul '{soul_name}' has empty content")

        assert not errors, "\n".join(errors)

    def test_all_config_bindings_model_override_retrievable(self, live_bindings):
        """Every binding with a model must produce a valid model override."""
        errors = []
        for binding in live_bindings:
            platform = binding.get("_platform", "whatsapp")
            chat_id = binding.get("id", "unknown")
            session_key = f"agent:main:{platform}:group:{chat_id}"

            _apply_binding(session_key, binding)

            if binding.get("model"):
                override = _get_model_override(session_key)
                if override is None:
                    errors.append(
                        f"[{platform}:{chat_id}] Model '{binding['model']}' defined "
                        f"but _get_model_override returned None"
                    )
                elif override.get("model") != binding["model"]:
                    errors.append(
                        f"[{platform}:{chat_id}] Expected model '{binding['model']}', "
                        f"got '{override.get('model')}'"
                    )

        assert not errors, "\n".join(errors)

    def test_all_config_bindings_soul_retrievable(self, live_bindings):
        """Every binding with a soul must produce retrievable ephemeral content."""
        errors = []
        for binding in live_bindings:
            platform = binding.get("_platform", "whatsapp")
            chat_id = binding.get("id", "unknown")
            session_key = f"agent:main:{platform}:group:{chat_id}"

            _apply_binding(session_key, binding)

            soul_name = binding.get("soul")
            if soul_name:
                ephemeral = _get_ephemeral(session_key)
                if ephemeral is None:
                    errors.append(
                        f"[{platform}:{chat_id}] Soul '{soul_name}' defined "
                        f"but _get_ephemeral returned None"
                    )

        assert not errors, "\n".join(errors)

    def test_all_config_bindings_memory_scope_retrievable(self, live_bindings):
        """Every binding with memory_scope must be retrievable."""
        errors = []
        for binding in live_bindings:
            platform = binding.get("_platform", "whatsapp")
            chat_id = binding.get("id", "unknown")
            session_key = f"agent:main:{platform}:group:{chat_id}"

            _apply_binding(session_key, binding)

            if binding.get("memory_scope"):
                scope = _get_memory_scope(session_key)
                if scope != binding["memory_scope"]:
                    errors.append(
                        f"[{platform}:{chat_id}] Expected memory_scope "
                        f"'{binding['memory_scope']}', got '{scope}'"
                    )

        assert not errors, "\n".join(errors)

    def test_all_config_bindings_summary(self, live_bindings):
        """
        Summary health check — prints binding status for each channel.
        This test always passes — it's a diagnostic, not a gate.
        """
        print(f"\n{'='*70}")
        print("Channel Binding Health Check Summary")
        print(f"{'='*70}")

        for binding in live_bindings:
            platform = binding.get("_platform", "?")
            chat_id = binding.get("id", "?")
            soul = binding.get("soul", "?")
            model = binding.get("model", "(default)")
            provider = binding.get("provider", "(default)")
            mem_scope = binding.get("memory_scope", "(default)")
            skills_count = len(binding.get("skills", []))

            session_key = f"agent:main:{platform}:group:{chat_id}"
            _apply_binding(session_key, binding)

            # Check each component
            soul_ok = _get_ephemeral(session_key) is not None
            model_ok = True
            if binding.get("model"):
                override = _get_model_override(session_key)
                model_ok = override is not None and override.get("model") == binding["model"]

            status = "✅" if (soul_ok and model_ok) else "❌"
            print(f"\n{status} [{platform}] {chat_id}")
            print(f"   Soul: {soul} {'✅' if soul_ok else '❌ (empty)'}")
            print(f"   Model: {model} via {provider} {'✅' if model_ok else '❌ (mismatch)'}")
            print(f"   Memory: {mem_scope}")
            print(f"   Skills: {skills_count} loaded")

        print(f"\n{'='*70}")
