import unittest
from types import SimpleNamespace

from agent import (
    LLMConfig,
    NextStep,
    _build_repair_message,
    _extract_chat_parse_result,
    _extract_chat_raw_result,
    _is_repairable_llm_exception,
    _openrouter_action_mismatch,
    _openrouter_tools,
    _strategy_for_config,
)


def _count_schema_key(value, key):
    if isinstance(value, dict):
        return (1 if key in value else 0) + sum(
            _count_schema_key(item, key) for item in value.values()
        )
    if isinstance(value, list):
        return sum(_count_schema_key(item, key) for item in value)
    return 0


class ProviderStrategyTests(unittest.TestCase):
    def test_openrouter_openai_model_uses_base_schema(self):
        strategy = _strategy_for_config(
            LLMConfig(provider="openrouter", model="openai/gpt-oss-120b", api_key="x")
        )

        self.assertEqual(strategy.api_mode, "chat_completions_raw")
        self.assertIs(strategy.response_model, NextStep)
        self.assertEqual(strategy.schema_variant, "next_step")
        self.assertEqual(strategy.extra_body["provider"]["require_parameters"], True)
        self.assertEqual(strategy.extra_body["reasoning"]["exclude"], True)

        schema = strategy.response_model.model_json_schema()
        self.assertEqual(_count_schema_key(schema, "oneOf"), 0)
        self.assertEqual(_count_schema_key(schema, "discriminator"), 0)

    def test_openai_provider_uses_responses_api(self):
        strategy = _strategy_for_config(
            LLMConfig(provider="openai", model="gpt-4.1-2025-04-14", api_key="x")
        )

        self.assertEqual(strategy.api_mode, "responses")
        self.assertIs(strategy.response_model, NextStep)
        self.assertIsNone(strategy.extra_body)

    def test_non_openai_openrouter_defaults_to_base_schema(self):
        strategy = _strategy_for_config(
            LLMConfig(provider="openrouter", model="anthropic/claude-sonnet-4", api_key="x")
        )

        self.assertEqual(strategy.api_mode, "chat_completions_raw")
        self.assertIs(strategy.response_model, NextStep)
        self.assertEqual(strategy.schema_variant, "next_step")

    def test_empty_chat_choices_are_reported_not_raised(self):
        result = _extract_chat_parse_result(
            SimpleNamespace(choices=[], usage=SimpleNamespace(prompt_tokens=10)),
            elapsed_ms=25,
        )

        self.assertIsNone(result.step)
        self.assertEqual(result.error, "empty choices")
        self.assertEqual(result.prompt_tokens, 10)
        self.assertEqual(result.elapsed_ms, 25)

    def test_unparsed_chat_content_keeps_raw_excerpt_for_repair(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        parsed=None,
                        content='<|channel|>commentary<|message|>{"outcome":"none_unsupported"}',
                    ),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )

        result = _extract_chat_parse_result(response, elapsed_ms=30)
        self.assertIsNone(result.step)
        self.assertEqual(result.finish_reason, "stop")
        self.assertIn("none_unsupported", result.raw_response_excerpt)

        repair = _build_repair_message({
            "error": result.error,
            "raw_response_excerpt": result.raw_response_excerpt,
        })
        self.assertIn("current_state", repair["content"])
        self.assertIn("Do not include commentary", repair["content"])

    def test_bare_respond_validation_error_is_repairable(self):
        with self.assertRaises(Exception) as raised:
            NextStep.model_validate({
                "outcome": "none_unsupported",
                "message": "Cannot add work centers with available tools.",
                "ground_refs": [],
            })

        self.assertTrue(_is_repairable_llm_exception(raised.exception))

    def test_raw_chat_extracts_markdown_fenced_json(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content='```json\n{"current_state":"ready","plan":["call system"],"task_completed":false,"function":{"type":"system"}}\n```',
                        tool_calls=None,
                    ),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )

        result = _extract_chat_raw_result(response, elapsed_ms=30)
        self.assertIsNotNone(result.step)
        self.assertEqual(result.step.function.type, "system")

    def test_raw_chat_extracts_prose_before_json(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content='We need the system context first.\n{"current_state":"ready","plan":["call system"],"task_completed":false,"function":{"type":"system"}}',
                        tool_calls=None,
                    ),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )

        result = _extract_chat_raw_result(response, elapsed_ms=30)
        self.assertIsNotNone(result.step)
        self.assertEqual(result.step.function.type, "system")

    def test_raw_chat_recovers_action_from_tool_call_arguments(self):
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="tool_calls",
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                function=SimpleNamespace(name="system", arguments="{}")
                            )
                        ],
                    ),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )

        result = _extract_chat_raw_result(response, elapsed_ms=30)
        self.assertIsNotNone(result.step)
        self.assertEqual(result.step.function.type, "system")
        self.assertEqual(result.finish_reason, "tool_calls")

    def test_openrouter_action_mismatch_catches_system_for_search_plan(self):
        step = NextStep.model_validate({
            "current_state": "Need to locate the affected equipment.",
            "plan": ["Search equipment for ESD-005."],
            "task_completed": False,
            "function": {"type": "system"},
        })

        self.assertIn("function.type is system", _openrouter_action_mismatch(step))

    def test_openrouter_action_mismatch_allows_real_system_context_call(self):
        step = NextStep.model_validate({
            "current_state": "Need current user context.",
            "plan": ["Call system to confirm current user and today."],
            "task_completed": False,
            "function": {"type": "system"},
        })

        self.assertIsNone(_openrouter_action_mismatch(step))

    def test_openrouter_tools_expose_api_actions_without_type_argument(self):
        tools = _openrouter_tools()
        names = {tool["function"]["name"] for tool in tools}
        self.assertIn("equipment_search", names)
        self.assertIn("notif_create", names)

        equipment_search = next(
            tool for tool in tools if tool["function"]["name"] == "equipment_search"
        )
        params = equipment_search["function"]["parameters"]
        self.assertNotIn("type", params.get("properties", {}))
        self.assertFalse(params.get("additionalProperties", True))


if __name__ == "__main__":
    unittest.main()
