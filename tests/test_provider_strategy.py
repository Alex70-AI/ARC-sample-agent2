import unittest
from types import SimpleNamespace

from agent import (
    LLMConfig,
    NextStep,
    Req_NotifCreate,
    Req_Respond,
    Req_WikiUpdate,
    Req_WOUpdate,
    TaskContractState,
    _build_repair_message,
    _extract_chat_parse_result,
    _extract_chat_raw_result,
    _missing_arg_fields,
    _is_repairable_llm_exception,
    _openrouter_action_mismatch,
    _openrouter_tools,
    _prepare_for_dispatch,
    _required_and_optional_fields,
    _strategy_for_config,
    _write_readiness,
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

    def test_write_field_extraction_uses_dto_schema(self):
        fn = Req_NotifCreate(
            floc="EXP-ESD-005",
            short_desc="Damaged insulation",
            long_desc="Damaged insulation near ESD-005.",
        )

        required, optional = _required_and_optional_fields(fn)
        self.assertEqual(required, ["floc", "short_desc", "long_desc"])
        self.assertIn("risk_assessment", optional)
        self.assertEqual(_missing_arg_fields(fn, optional), ["risk_assessment"])

    def test_write_readiness_blocks_omitted_optional_without_evidence(self):
        step = NextStep.model_validate({
            "current_state": "Ready to create the maintenance record.",
            "plan": ["Create the notification."],
            "task_completed": False,
            "function": {
                "type": "notif_create",
                "floc": "EXP-ESD-005",
                "short_desc": "Damaged insulation",
                "long_desc": "Damaged insulation near ESD-005.",
            },
        })
        contract = TaskContractState(task_text="Raise a notification for damaged insulation.")

        readiness = _write_readiness(step, step.function, contract)
        self.assertFalse(readiness.ready)
        self.assertIn("risk_assessment", readiness.missing_fields)
        self.assertIn("risk_assessment", readiness.suggested_search_terms)

    def test_write_readiness_blocks_loaded_relevant_optional_field(self):
        step = NextStep.model_validate({
            "current_state": "Ready to create the maintenance record.",
            "plan": ["Create the notification."],
            "task_completed": False,
            "function": {
                "type": "notif_create",
                "floc": "EXP-ESD-005",
                "short_desc": "Damaged insulation",
                "long_desc": "Damaged insulation near ESD-005.",
            },
        })
        contract = TaskContractState(
            task_text="Raise a critical notification for damaged insulation.",
            loaded_docs={
                "maintenance.md": "Notification records include a risk_assessment when risk is part of the condition."
            },
        )

        readiness = _write_readiness(step, step.function, contract)
        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.missing_fields, ["risk_assessment"])

    def test_write_readiness_allows_explicit_optional_omission_justification(self):
        step = NextStep.model_validate({
            "current_state": "Ready to update equipment. current_value is optional and not needed because the task only asks to set running status.",
            "plan": ["Update running_status; omit current_value because it is irrelevant."],
            "task_completed": False,
            "function": {
                "type": "equipment_update",
                "floc": "PUMP-001",
                "running_status": "maintenance",
            },
        })
        contract = TaskContractState(
            task_text="Set PUMP-001 running status to maintenance.",
            loaded_docs={"policy.md": "Equipment update can change running_status or current_value."},
        )

        readiness = _write_readiness(step, step.function, contract)
        self.assertTrue(readiness.ready)

    def test_wo_update_requires_cached_baseline(self):
        contract = TaskContractState(task_text="Close the work order.")
        fn = Req_WOUpdate(wo_id=9812212, status="completed")

        guard = _prepare_for_dispatch(fn, contract)

        self.assertFalse(guard.ready)
        self.assertIn("baseline", guard.reason)
        self.assertEqual(
            guard.details["required_next_action"],
            {"type": "wo_get", "wo_id": 9812212},
        )

    def test_wo_update_strips_unchanged_fields_against_baseline(self):
        contract = TaskContractState(task_text="Close the work order.")
        contract.work_orders_by_id["9812212"] = {
            "id": 9812212,
            "short_desc": "Change pump motor",
            "long_desc": "Replace condensate drain pump motor.",
            "status": "exec",
            "execution_date": "2025-11-02",
            "floc": "UTIL-P-001-M-001",
        }
        fn = Req_WOUpdate(
            wo_id=9812212,
            short_desc="Change pump motor",
            long_desc="Replace condensate drain pump motor.",
            status="completed",
            execution_date="2025-11-02",
            floc="UTIL-P-001-M-001",
        )

        guard = _prepare_for_dispatch(fn, contract)

        self.assertTrue(guard.ready)
        self.assertEqual(
            guard.function.model_dump(exclude_none=True),
            {"type": "wo_update", "wo_id": 9812212, "status": "completed"},
        )

    def test_duplicate_wiki_update_blocks_overlapping_content_for_same_path(self):
        contract = TaskContractState(task_text="Update the SOP.")
        contract.accepted_writes.append({
            "type": "wiki_update",
            "target_key": ["wiki_update", "sop/block-valve-replacement.md"],
            "effective_payload": {
                "start_row": 6,
                "end_row": 6,
                "content": "- Inform Control Room before work commences.",
                "updated_by": "A",
            },
            "result": {},
        })
        fn = Req_WikiUpdate(
            path="sop/block-valve-replacement.md",
            start_row=8,
            end_row=8,
            content="- Inform Control Room before work commences.\n",
            updated_by="A",
        )

        guard = _prepare_for_dispatch(fn, contract)

        self.assertFalse(guard.ready)
        self.assertIn("duplicate successful write", guard.reason)

    def test_respond_validation_requires_ref_to_successful_write(self):
        contract = TaskContractState(task_text="Close the work order.")
        contract.accepted_writes.append({
            "type": "wo_update",
            "args": {"wo_id": 9812212, "status": "completed"},
            "target_key": ["wo_update", 9812212],
            "effective_payload": {"status": "completed"},
            "result": {"work_order": {"id": 9812212, "status": "completed"}},
        })
        fn = Req_Respond(
            message="Closed the work order.",
            outcome="ok_answer",
            ground_refs=[],
        )

        guard = _prepare_for_dispatch(fn, contract)

        self.assertFalse(guard.ready)
        self.assertEqual(
            guard.details["missing_refs"],
            [{"type": "work_order", "id": "9812212"}],
        )


if __name__ == "__main__":
    unittest.main()
