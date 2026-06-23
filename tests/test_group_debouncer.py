import asyncio
import importlib
import json
import sys
import types
import unittest
from pathlib import Path


class Plain:
    def __init__(self, text):
        self.text = text


class At:
    def __init__(self, target=None):
        self.target = target


class Image:
    pass


class Message:
    def __init__(self, components, self_id=None):
        self.message = components
        self.self_id = self_id


class Event:
    def __init__(self, components, self_id=None):
        self.message_obj = Message(components, self_id=self_id)
        self.message_str = ""


def _install_astrbot_stubs():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event = types.ModuleType("astrbot.api.event")
    star = types.ModuleType("astrbot.api.star")
    components = types.ModuleType("astrbot.core.message.components")

    class Logger:
        def __getattr__(self, _name):
            return lambda *args, **kwargs: None

    class Star:
        def __init__(self, context):
            self.context = context

    def event_message_type(*_args, **_kwargs):
        return lambda func: func

    api.logger = Logger()
    event.AstrMessageEvent = object
    event.filter = types.SimpleNamespace(
        EventMessageType=types.SimpleNamespace(GROUP_MESSAGE="group"),
        event_message_type=event_message_type,
    )
    star.Context = object
    star.Star = Star
    components.Plain = Plain

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
            "astrbot.core": types.ModuleType("astrbot.core"),
            "astrbot.core.message": types.ModuleType("astrbot.core.message"),
            "astrbot.core.message.components": components,
        }
    )


_install_astrbot_stubs()
sys.modules.pop("main", None)
plugin_module = importlib.import_module("main")


class GroupDebouncerRegressionTests(unittest.TestCase):
    def make_plugin(self, **config):
        return plugin_module.GroupDebouncer(context=None, config=config)

    def test_target_time_refreshes_when_reset_timer_is_enabled(self):
        plugin = self.make_plugin(reset_timer=True)
        buffer = plugin_module.MessageBuffer("group", "sender", "Sender")
        buffer.target_time = 3.0

        plugin._update_target_time(buffer, now=2.0, window_seconds=3.0)

        self.assertEqual(buffer.target_time, 5.0)

    def test_at_component_without_self_id_is_not_treated_as_at_bot(self):
        plugin = self.make_plugin()
        event = Event([At(target="another-user")], self_id=None)

        self.assertFalse(plugin._is_at_bot_message(event, "@another-user hello"))

    def test_default_injection_preserves_last_non_plain_component(self):
        plugin = self.make_plugin()
        event = Event([Plain("latest"), Image()], self_id="bot")
        buffer = plugin_module.MessageBuffer("group", "sender", "Sender")
        buffer.add("first", [Plain("first")], 300, "Sender", False)
        buffer.add("latest", event.message_obj.message, 300, "Sender", False)

        plugin._inject_merged_text(event, "first\nlatest", buffer)

        self.assertEqual(event.message_obj.message[0].text, "first\nlatest")
        self.assertIsInstance(event.message_obj.message[1], Image)

    def test_cleanup_removes_expired_unlocked_state(self):
        plugin = self.make_plugin(cleanup_interval_seconds=10, inactive_state_ttl_seconds=60)
        plugin.key_last_seen["group::sender"] = 0.0
        plugin.session_last_seen["group"] = 0.0
        plugin.buffers["group::sender"] = plugin_module.MessageBuffer("group", "sender", "Sender")
        plugin.locks["group::sender"] = asyncio.Lock()
        plugin.repeat_locks["group"] = asyncio.Lock()
        plugin.repeat_states["group"] = plugin_module.RepeatState()

        plugin._cleanup_states(now=100.0)

        self.assertNotIn("group::sender", plugin.buffers)
        self.assertNotIn("group::sender", plugin.locks)
        self.assertNotIn("group", plugin.repeat_states)

    def test_cleanup_keeps_locked_state(self):
        plugin = self.make_plugin(cleanup_interval_seconds=10, inactive_state_ttl_seconds=60)
        key = "group::sender"
        lock = asyncio.Lock()
        asyncio.run(self._lock_and_cleanup(plugin, key, lock))
        self.assertIn(key, plugin.locks)

    async def _lock_and_cleanup(self, plugin, key, lock):
        plugin.key_last_seen[key] = 0.0
        plugin.locks[key] = lock
        await lock.acquire()
        try:
            plugin._cleanup_states(now=100.0)
        finally:
            lock.release()

    def test_debounce_group_deny_list_overrides_allow_list(self):
        plugin = self.make_plugin(
            debounce_enabled_groups="allowed,blocked",
            debounce_disabled_groups="blocked",
        )

        self.assertTrue(plugin._debounce_group_allowed("allowed"))
        self.assertFalse(plugin._debounce_group_allowed("blocked"))
        self.assertFalse(plugin._debounce_group_allowed("outside"))

    def test_heartflow_compatibility_uses_conservative_behavior(self):
        plugin = self.make_plugin(heartflow_compat_mode=True, first_message_no_debounce=True)

        self.assertTrue(plugin.config.heartflow_compat_mode)
        self.assertFalse(plugin._should_attempt_repeat())
        self.assertFalse(plugin._should_bypass_first_message())

    def test_heartflow_compatibility_forces_strict_at_matching(self):
        plugin = self.make_plugin(heartflow_compat_mode=True, strict_at_match=False)
        event = Event([At(target="another-user")], self_id=None)

        self.assertFalse(plugin._is_at_bot_message(event, "@another-user hello"))

    def test_heartflow_compatibility_preserves_last_non_plain_component(self):
        plugin = self.make_plugin(heartflow_compat_mode=True, inject_strategy="plain_replace")
        image = Image()
        event = Event([Plain("latest"), image], self_id="bot")
        buffer = plugin_module.MessageBuffer("group", "sender", "Sender")
        buffer.add("latest", event.message_obj.message, 300, "Sender", False)

        plugin._inject_merged_text(event, "latest", buffer)

        self.assertEqual(event.message_obj.message[1:], [image])

    def test_injection_can_preserve_all_non_plain_components(self):
        plugin = self.make_plugin(
            heartflow_compat_mode=False,
            inject_strategy="preserve_all_non_plain",
        )
        last_image = Image()
        event = Event([Plain("latest"), last_image], self_id="bot")
        buffer = plugin_module.MessageBuffer("group", "sender", "Sender")
        first_image = Image()
        buffer.add("first", [Plain("first"), first_image], 300, "Sender", False)
        buffer.add("latest", event.message_obj.message, 300, "Sender", False)

        plugin._inject_merged_text(event, "first\nlatest", buffer)

        self.assertEqual(event.message_obj.message[1:], [first_image, last_image])

    def test_schema_documents_every_runtime_setting(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        runtime_fields = set(plugin_module.DebouncerConfig.model_fields)

        self.assertSetEqual(set(schema), runtime_fields)


if __name__ == "__main__":
    unittest.main()
