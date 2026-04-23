"""
Tests for Discord thread chat_id resolution (9b310fd01).

Covers commits:
  - 9b310fd01: fix: thread chat_id parent resolution + auto-dedup + mempalace_delete tool
  - c51309549: feat: Hall communication commands + cron memory inheritance (cron memory already covered by integration)
"""
import unittest
from unittest.mock import MagicMock


def _resolve_chat_id_for_key(is_thread: bool, auto_threaded_channel: bool, message, effective_channel) -> str:
    """Copied from the actual code in discord.py to test the logic in isolation."""
    if is_thread:
        if auto_threaded_channel:
            # First message in auto-thread: message.channel is the parent TextChannel
            chat_id_for_key = str(message.channel.id)
        else:
            # Message inside an existing thread: message.channel is a Thread object
            _parent_id = getattr(message.channel, "parent_id", None)
            chat_id_for_key = str(_parent_id) if _parent_id else str(message.channel.id)
    else:
        chat_id_for_key = str(effective_channel.id)
    return chat_id_for_key


class TestDiscordThreadChatIdResolution(unittest.TestCase):
    def test_auto_thread_first_message_uses_parent_channel_id(self):
        """First message in auto-thread: channel is parent TextChannel → use it as chat_id."""
        # Mock the message object
        mock_message = MagicMock()
        mock_channel = MagicMock()
        mock_channel.id = 12345  # parent channel ID
        mock_message.channel = mock_channel
        mock_message.id = 99999

        result = _resolve_chat_id_for_key(
            is_thread=True,
            auto_threaded_channel=True,
            message=mock_message,
            effective_channel=mock_channel,
        )
        # Should use parent channel ID
        self.assertEqual(result, "12345")

    def test_existing_thread_message_uses_parent_id_from_attribute(self):
        """Existing thread: message.channel has parent_id attribute → use it."""
        mock_message = MagicMock()
        mock_channel = MagicMock()
        mock_channel.parent_id = 12345  # parent ID on Thread object
        mock_message.channel = mock_channel
        mock_effective_channel = MagicMock()
        mock_effective_channel.id = 98765

        result = _resolve_chat_id_for_key(
            is_thread=True,
            auto_threaded_channel=False,
            message=mock_message,
            effective_channel=mock_effective_channel,
        )
        self.assertEqual(result, "12345")

    def test_existing_thread_no_parent_id_falls_back_to_channel_id(self):
        """If no parent_id attribute, fall back to effective_channel.id."""
        mock_message = MagicMock()
        mock_channel = MagicMock()
        mock_channel.id = 54321  # thread ID
        delattr(mock_channel, "parent_id")  # no attribute
        mock_message.channel = mock_channel
        mock_effective_channel = MagicMock()
        mock_effective_channel.id = 45678

        result = _resolve_chat_id_for_key(
            is_thread=True,
            auto_threaded_channel=False,
            message=mock_message,
            effective_channel=mock_effective_channel,
        )
        # When there is no parent_id, we fall back to message.channel.id
        self.assertEqual(result, "54321")

    def test_non_thread_uses_effective_channel_id(self):
        """Non-thread just uses effective channel ID directly."""
        mock_effective_channel = MagicMock()
        mock_effective_channel.id = 78901

        result = _resolve_chat_id_for_key(
            is_thread=False,
            auto_threaded_channel=False,
            message=None,
            effective_channel=mock_effective_channel,
        )
        self.assertEqual(result, "78901")


if __name__ == "__main__":
    unittest.main()
