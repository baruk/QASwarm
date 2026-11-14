import importlib
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from evals import eval_process, eval_runner, evaluator
from evals.eval_context import EvalContextError, group_eval_cases
from evals.eval_process import EvalProcessCleanupError
from evals.tests.test_eval_context import TemporaryProject, make_case


def judge_result(case):
    return {
        "eval_case_id": case["id"],
        "expected_properties": [
            {"property": prop, "passed": True, "reason": "unit-test stub"}
            for prop in case["expected_properties"]
        ],
        "forbidden_behavior": [
            {"rule": rule, "violated": False, "reason": "unit-test stub"}
            for rule in case["forbidden_behavior"]
        ],
        "status": "PASS", "score": 1.0,
    }


class RunnerTests(TemporaryProject):
    def test_import_does_not_execute_agent_or_evaluator(self):
        with patch.object(eval_runner.subprocess, "Popen") as popen, patch.object(eval_runner.subprocess, "run") as run:
            importlib.reload(eval_runner)
        popen.assert_not_called()
        run.assert_not_called()

    def test_cli_has_no_new_launch_parameters(self):
        with patch("sys.argv", ["eval_runner.py"]):
            self.assertEqual(vars(eval_runner.parse_args()), {"debug": False})
        with patch("sys.argv", ["eval_runner.py", "--debug"]):
            self.assertEqual(vars(eval_runner.parse_args()), {"debug": True})

    def test_dataset_validation_happens_before_any_launch(self):
        dataset = self.project / "dataset.json"
        dataset.write_text(json.dumps([make_case(run_type="unknown")]), encoding="utf-8")
        with patch.object(eval_runner, "run_agent") as run_agent:
            with self.assertRaises(EvalContextError):
                eval_runner.load_eval_groups(dataset)
        run_agent.assert_not_called()

    def test_fresh_resume_fresh_launches_are_distinct_and_receive_correct_context(self):
        calls = []

        def fake_agent(*, page_id, prompt, trace_path, workdir):
            context, _ = json.JSONDecoder().raw_decode(prompt.split("EVAL_CONTEXT:\n", 1)[1])
            calls.append(context)
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            trace_path.write_text("", encoding="utf-8")
            trace_path.with_suffix(".response.md").write_text("unit-test stub", encoding="utf-8")
            manifest_path = workdir / context["manifest"]
            manifest = self.registry.load(manifest_path)
            manifest["workflow_status"] = "REQUIRES_HUMAN_DECISION"
            self.save(manifest_path, manifest)
            return {
                "return_code": 0, "agent_response": "unit-test stub",
                "stderr": "", "error_events": [], "transcript": [],
            }

        groups = group_eval_cases([
            make_case(), make_case(run_type="resume", case_id="EVC-002"),
            make_case(case_id="EVC-003"),
        ])
        def fake_judge(case, response, **kwargs):
            with self.assertRaises(EvalContextError):
                with self.registry.locked():
                    self.fail("Блокировка снята до окончания judge")
            return judge_result(case)

        with patch.object(eval_runner, "run_agent", side_effect=fake_agent), patch.object(
            eval_runner, "evaluate_response", side_effect=fake_judge,
        ) as judge, redirect_stdout(io.StringIO()):
            results = eval_runner.run_eval_groups(groups, project=self.project)
        self.assertEqual([call["run_type"] for call in calls], ["fresh", "resume", "fresh"])
        self.assertEqual(calls[0]["run_id"], calls[1]["run_id"])
        self.assertNotEqual(calls[0]["run_id"], calls[2]["run_id"])
        self.assertEqual(calls[1]["attempt"], 2)
        self.assertEqual([result["status"] for result in results], ["PASS"] * 3)
        self.assertEqual(judge.call_count, 3)
        self.assertTrue(all("evidence" in call.kwargs for call in judge.call_args_list))
        self.assertEqual(len(list(self.registry.directory.rglob("*.json"))), 2)

    def test_resume_without_fresh_is_error_and_never_launches_publisher_workflow(self):
        groups = group_eval_cases([make_case(run_type="resume")])
        with patch.object(eval_runner, "run_agent") as agent, patch.object(eval_runner, "evaluate_response") as judge, redirect_stdout(io.StringIO()):
            result = eval_runner.run_eval_groups(groups, project=self.project)[0]
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("нет fresh-прогона", result["error"])
        agent.assert_not_called()
        judge.assert_not_called()

    def test_resume_of_unregistered_completed_run_is_blocked_before_workflow_launch(self):
        path, manifest = self.registry.prepare(3964929, "fresh")
        self.registry.finish_attempt(path, manifest, 0)
        before = path.read_bytes()
        with patch.object(eval_runner, "run_agent") as agent, patch.object(
            eval_runner, "evaluate_response",
        ) as judge, redirect_stdout(io.StringIO()):
            result = eval_runner.run_eval_groups(group_eval_cases([make_case(run_type="resume")]), project=self.project)[0]
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("eval-контекст", result["error"])
        self.assertEqual(path.read_bytes(), before)
        agent.assert_not_called()
        judge.assert_not_called()

    def test_process_failure_retains_fresh_for_future_resume(self):
        def failed_agent(**kwargs):
            kwargs["trace_path"].parent.mkdir(parents=True, exist_ok=True)
            kwargs["trace_path"].write_text("", encoding="utf-8")
            return {"return_code": 1, "agent_response": "partial", "stderr": "failure", "error_events": [], "transcript": []}

        with patch.object(eval_runner, "run_agent", side_effect=failed_agent), patch.object(eval_runner, "evaluate_response") as judge, redirect_stdout(io.StringIO()):
            result = eval_runner.run_eval_groups(group_eval_cases([make_case()]), project=self.project)[0]
        self.assertEqual(result["status"], "ERROR")
        path, manifest = self.registry.latest_fresh(3964929)
        self.assertEqual(manifest["attempts"][-1]["return_code"], 1)
        self.assertIsNone(manifest["workflow_status"])
        self.assertTrue(path.exists())
        judge.assert_not_called()

    def test_unregistered_workflow_is_error_even_with_zero_exit_and_success_reply(self):
        def fake_agent(**kwargs):
            trace = kwargs["trace_path"]
            trace.parent.mkdir(parents=True, exist_ok=True)
            trace.write_text("", encoding="utf-8")
            trace.with_suffix(".response.md").write_text("Статус: SUCCESS", encoding="utf-8")
            return {
                "return_code": 0, "agent_response": "Статус: SUCCESS",
                "stderr": "", "error_events": [], "transcript": [],
            }

        with patch.object(eval_runner, "run_agent", side_effect=fake_agent), patch.object(
            eval_runner, "evaluate_response",
        ) as judge, redirect_stdout(io.StringIO()):
            result = eval_runner.run_eval_groups(group_eval_cases([make_case()]), project=self.project)[0]
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("eval-контекст", result["error"])
        judge.assert_not_called()
        _, manifest = self.registry.latest_fresh(3964929)
        self.assertEqual(manifest["attempts"][-1]["return_code"], 0)
        self.assertIsNone(manifest["workflow_status"])
        self.assertTrue(all(value == "NOT_CHECKED" for value in manifest["gates"].values()))

    def test_multiline_prompt_is_archived_and_redirected_to_stdin_not_cmd_arguments(self):
        path, manifest = self.registry.prepare(3964929, "fresh")
        prompt = eval_runner.build_agent_prompt(self.registry, path, manifest)
        prompt += '\nКавычки: "значение", символы & | < > % ^ !\r\n' + "длинный текст " * 4000
        received = []
        process = Mock(stdin=None, stdout=io.StringIO(''), stderr=io.StringIO(''))
        process.wait.return_value = 0
        process.poll.return_value = 0

        def capture_launch(command, **kwargs):
            self.assertNotIn(prompt, command)
            self.assertTrue(all("\n" not in argument for argument in command))
            self.assertTrue(hasattr(kwargs["stdin"], "read"))
            received.append(kwargs["stdin"].read())
            return process

        trace = self.project / "attempt-1.jsonl"
        with patch.object(eval_runner.subprocess, "Popen", side_effect=capture_launch), redirect_stdout(io.StringIO()):
            result = eval_runner.run_agent("3964929", prompt, trace, workdir=self.project)
        self.assertEqual(received, [prompt])
        self.assertEqual(result["prompt_path"], trace.with_suffix(".prompt.md"))
        self.assertEqual(result["prompt_path"].read_bytes(), prompt.encode("utf-8"))

    def test_claimed_success_without_current_containers_and_evidence_is_not_judged(self):
        def fake_agent(**kwargs):
            context, _ = json.JSONDecoder().raw_decode(kwargs["prompt"].split("EVAL_CONTEXT:\n", 1)[1])
            path = self.project / context["manifest"]
            manifest = self.registry.load(path)
            manifest["workflow_status"] = "SUCCESS"
            self.save(path, manifest)
            trace = kwargs["trace_path"]
            trace.parent.mkdir(parents=True, exist_ok=True)
            trace.write_text("", encoding="utf-8")
            trace.with_suffix(".response.md").write_text("Статус: SUCCESS", encoding="utf-8")
            return {"return_code": 0, "agent_response": "Статус: SUCCESS"}

        with patch.object(eval_runner, "run_agent", side_effect=fake_agent), patch.object(
            eval_runner, "evaluate_response",
        ) as judge, redirect_stdout(io.StringIO()):
            result = eval_runner.run_eval_groups(group_eval_cases([make_case()]), project=self.project)[0]
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("jira_epic", result["error"])
        self.assertIn("qase_root_suite", result["error"])
        judge.assert_not_called()

    def test_failed_review_gate_rejects_success_even_with_all_artifacts_and_containers(self):
        def fake_agent(**kwargs):
            context, _ = json.JSONDecoder().raw_decode(kwargs["prompt"].split("EVAL_CONTEXT:\n", 1)[1])
            path = self.project / context["manifest"]
            manifest = self.registry.load(path)
            manifest["workflow_status"] = "SUCCESS"
            manifest["gates"] = {key: "PASSED" for key in manifest["gates"]}
            manifest["gates"]["requirement_review"] = "FAILED"
            manifest["containers"]["jira_epic"].update(local_id="T-1", key="SCRUM-42")
            manifest["containers"]["qase_root_suite"].update(local_id="S-1", id=42)
            for mapping, directories in (("artifacts", {
                "requirement_review": "requirement-review", "jira_tasks": "jira-tasks", "test_model": "test-model",
            }), ("reports", {"jira_creation": "jira-creation", "qase_creation": "qase-creation"})):
                for key, directory in directories.items():
                    artifact = self.project / "artifacts" / directory / "v1_261112_1200.md"
                    artifact.parent.mkdir(parents=True, exist_ok=True)
                    artifact.write_text(f"Eval run: {manifest['run_id']}\nSource pageId: 3964929\n", encoding="utf-8")
                    manifest[mapping][key] = self.registry.relative_path(artifact)
            self.save(path, manifest)
            trace = kwargs["trace_path"]
            trace.parent.mkdir(parents=True, exist_ok=True)
            trace.write_text("", encoding="utf-8")
            trace.with_suffix(".response.md").write_text("Статус: SUCCESS", encoding="utf-8")
            return {"return_code": 0, "agent_response": "Статус: SUCCESS"}

        with patch.object(eval_runner, "run_agent", side_effect=fake_agent), patch.object(
            eval_runner, "evaluate_response",
        ) as judge, redirect_stdout(io.StringIO()):
            result = eval_runner.run_eval_groups(group_eval_cases([make_case()]), project=self.project)[0]
        self.assertEqual(result["status"], "ERROR")
        self.assertIn("gate:requirement_review", result["error"])
        judge.assert_not_called()
        _, manifest = self.registry.latest_fresh(3964929)
        self.assertEqual(manifest["gates"]["requirement_review"], "FAILED")
        self.assertEqual(manifest["containers"]["jira_epic"]["key"], "SCRUM-42")

    def test_registered_conflict_stop_reaches_judge_without_later_stage_artifacts(self):
        def fake_agent(**kwargs):
            context, _ = json.JSONDecoder().raw_decode(kwargs["prompt"].split("EVAL_CONTEXT:\n", 1)[1])
            path = self.project / context["manifest"]
            manifest = self.registry.load(path)
            review = self.project / "artifacts/requirement-review/v1_261112_1200.md"
            review.parent.mkdir(parents=True, exist_ok=True)
            review.write_text(
                f"Eval run: {manifest['run_id']}\nSource pageId: 3964929\n"
                "Статус: FINAL\n## Readiness\nDesign allowed: no\nUnresolved conflicts: CNF-1\n",
                encoding="utf-8",
            )
            manifest["artifacts"]["requirement_review"] = self.registry.relative_path(review)
            manifest["workflow_status"] = "REQUIRES_HUMAN_DECISION"
            manifest["gates"]["requirement_review"] = "FAILED"
            manifest["gates"]["final"] = "FAILED"
            self.save(path, manifest)
            trace = kwargs["trace_path"]
            trace.parent.mkdir(parents=True, exist_ok=True)
            trace.write_text("", encoding="utf-8")
            response = "REQUIRES_HUMAN_DECISION: CNF-1 — подтвердите правило по двум источникам. Дизайн и публикация не запускались."
            trace.with_suffix(".response.md").write_text(response, encoding="utf-8")
            return {"return_code": 0, "agent_response": response}

        case = make_case(case_id="EVC-CONFLICT")
        with patch.object(eval_runner, "run_agent", side_effect=fake_agent), patch.object(
            eval_runner, "evaluate_response", return_value=judge_result(case),
        ) as judge, redirect_stdout(io.StringIO()):
            result = eval_runner.run_eval_groups(group_eval_cases([case]), project=self.project)[0]
        self.assertEqual(result["status"], "PASS")
        judge.assert_called_once()
        evidence = judge.call_args.kwargs["evidence"]
        self.assertEqual(set(evidence["files"]), {"requirement_review"})
        self.assertIn("REQUIRES_HUMAN_DECISION", judge.call_args.args[1])
        _, manifest = self.registry.latest_fresh(3964929)
        self.assertEqual(manifest["workflow_status"], "REQUIRES_HUMAN_DECISION")
        self.assertTrue(all(manifest["gates"][key] == "NOT_CHECKED" for key in (
            "jira_tasks", "test_model", "jira_creation", "qase_creation",
        )))
        self.assertEqual(len(list((self.project / "artifacts").rglob("*.md"))), 1)

    def test_full_trace_is_not_truncated_to_diagnostic_tail(self):
        events = [json.dumps({"type": "text", "part": {"text": str(index)}}) for index in range(400)]
        process = Mock(stdout=io.StringIO("\n".join(events) + "\n"), stderr=io.StringIO(""))
        process.wait.return_value = 0
        trace = self.project / "trace.jsonl"
        with patch.object(eval_runner.subprocess, "Popen", return_value=process) as popen, redirect_stdout(io.StringIO()):
            result = eval_runner.run_agent("3964929", "prompt", trace, workdir=self.project)
        self.assertEqual(len(trace.read_text(encoding="utf-8").splitlines()), 400)
        self.assertEqual(len(result["transcript"]), eval_runner.MAX_TRANSCRIPT_LINES)
        self.assertEqual(trace.with_suffix(".response.md").read_text(encoding="utf-8"), result["agent_response"])
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--agent") + 1], "orchestrator")
        self.assertIn("--standalone", command)

    def test_trace_collision_prevents_process_start_and_preserves_file(self):
        trace = self.project / "trace.jsonl"
        trace.write_text("existing trace", encoding="utf-8")
        with patch.object(eval_runner.subprocess, "Popen") as popen:
            with self.assertRaises(FileExistsError):
                eval_runner.run_agent("3964929", "prompt", trace, workdir=self.project)
        popen.assert_not_called()
        self.assertEqual(trace.read_text(encoding="utf-8"), "existing trace")

    def test_prompt_collision_prevents_process_start_and_preserves_file(self):
        trace = self.project / "trace.jsonl"
        prompt_path = trace.with_suffix(".prompt.md")
        prompt_path.write_text("existing prompt", encoding="utf-8")
        with patch.object(eval_runner.subprocess, "Popen") as popen:
            with self.assertRaises(FileExistsError):
                eval_runner.run_agent("3964929", "new prompt", trace, workdir=self.project)
        popen.assert_not_called()
        self.assertEqual(prompt_path.read_text(encoding="utf-8"), "existing prompt")

    def test_debug_transcript_preserves_last_flag_and_identifies_stdin_file(self):
        trace = self.project / "attempt-1.jsonl"
        process = Mock(stdin=None, stdout=io.StringIO(''), stderr=io.StringIO(''))
        process.wait.return_value = 0
        process.poll.return_value = 0
        with patch.object(eval_runner.subprocess, "Popen", return_value=process), patch.object(
            eval_runner, "DEBUG", True,
        ), redirect_stdout(io.StringIO()):
            eval_runner.run_agent("3964929", "full prompt", trace, workdir=self.project)
        log = next((self.project / "evals/results/debug").glob("*.log")).read_text(encoding="utf-8")
        self.assertIn("--format json <stdin>", log)
        self.assertIn(str(trace.with_suffix(".prompt.md")), log)
        self.assertIn("PROMPT:\nfull prompt", log)

    @unittest.skipUnless(os.name == "nt", "Windows batch launcher regression")
    def test_real_windows_batch_launcher_receives_complete_utf8_prompt(self):
        directory = self.project / "launcher with spaces"
        directory.mkdir()
        script = directory / "capture_stdin.py"
        script.write_text(
            "import json, sys\n"
            "payload = {'argv': sys.argv[1:], 'stdin': sys.stdin.buffer.read().decode('utf-8')}\n"
            "print(json.dumps({'type': 'text', 'part': {'text': json.dumps(payload)}}))\n",
            encoding="utf-8",
        )
        launcher = directory / "opencode.cmd"
        launcher.write_text(f'@echo off\n"{sys.executable}" "{script}" %*\n', encoding="utf-8")
        original_open = eval_runner.open_opencode

        def fixture_open(command, workdir, *, stdin):
            return original_open([str(launcher), *command[1:]], workdir, stdin=stdin)

        path, manifest = self.registry.prepare(3964929, "fresh")
        prompt = eval_runner.build_agent_prompt(self.registry, path, manifest)
        prompt += '\nUTF-8: кириллица, "кавычки", & | < > % ^ !\r\n' + "длинный текст " * 4000
        trace = self.project / "attempt-1.jsonl"
        with patch.object(eval_runner, "open_opencode", side_effect=fixture_open), redirect_stdout(io.StringIO()):
            result = eval_runner.run_agent("3964929", prompt, trace, workdir=self.project)
        self.assertEqual(result["return_code"], 0)
        captured = json.loads(result["agent_response"])
        self.assertEqual(captured["stdin"], prompt)
        self.assertEqual(captured["argv"][-2:], ["--format", "json"])
        self.assertNotIn(prompt, captured["argv"])

    def test_stream_failure_and_interruption_stop_process_and_save_partial_response(self):
        event = json.dumps({"type": "text", "part": {"text": "partial response"}})

        class FailingStream(io.StringIO):
            def __next__(stream):
                if stream.tell() == 0:
                    return super(FailingStream, stream).__next__()
                raise failure

        for failure in (OSError("stream failure"), KeyboardInterrupt()):
            process = Mock(stdin=None, stdout=FailingStream(event + "\n"), stderr=io.StringIO(""))
            process.poll.return_value = None
            trace = self.project / f"failure-{type(failure).__name__}.jsonl"
            with self.subTest(failure=type(failure).__name__), patch.object(eval_runner.subprocess, "Popen", return_value=process), patch.object(
                eval_process, "stop_process",
            ) as stop, redirect_stdout(io.StringIO()):
                with self.assertRaises(type(failure)):
                    eval_runner.run_agent("3964929", "prompt", trace, workdir=self.project)
            stop.assert_called_once_with(process)
            self.assertEqual(trace.read_text(encoding="utf-8").strip(), event)
            self.assertEqual(trace.with_suffix(".response.md").read_text(encoding="utf-8"), "partial response")
            self.assertEqual(trace.with_suffix(".prompt.md").read_text(encoding="utf-8"), "prompt")
            self.assertTrue(process.stdout.closed)
            self.assertTrue(process.stderr.closed)

    def test_keyboard_interrupt_finishes_attempt_without_changing_business_gates(self):
        with patch.object(eval_runner, "run_agent", side_effect=KeyboardInterrupt()), redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                eval_runner.run_eval_groups(group_eval_cases([make_case()]), project=self.project)
        _, manifest = self.registry.latest_fresh(3964929)
        self.assertIsNotNone(manifest["attempts"][-1]["finished_at"])
        self.assertIsNone(manifest["attempts"][-1]["return_code"])
        self.assertTrue(all(value == "NOT_CHECKED" for value in manifest["gates"].values()))
        with self.registry.locked():
            pass

    def test_cleanup_failure_stops_dataset_before_next_launch(self):
        groups = group_eval_cases([make_case(), make_case(page_id=123)])
        with patch.object(eval_runner, "run_agent", side_effect=EvalProcessCleanupError("own process still active")) as agent, redirect_stdout(io.StringIO()):
            with self.assertRaises(EvalProcessCleanupError):
                eval_runner.run_eval_groups(groups, project=self.project)
        self.assertEqual(agent.call_count, 1)
        self.assertEqual(len(list(self.registry.directory.rglob("*.json"))), 1)


class EvaluatorTests(unittest.TestCase):
    def evaluate_events(self, case, events, return_code=0, stderr=""):
        process = Mock(stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO(), returncode=return_code)
        process.communicate.return_value = ("\n".join(json.dumps(event) for event in events), stderr)
        process.poll.return_value = return_code
        with patch.object(evaluator.subprocess, "Popen", return_value=process), redirect_stdout(io.StringIO()):
            return evaluator.evaluate_response(case, "response")

    def text_event(self, text, message_id=None):
        part = {"text": text}
        if message_id is not None:
            part["messageID"] = message_id
        return {"type": "text", "part": part}

    def test_judge_receives_run_type_evidence_and_shared_step_reuse_rule(self):
        prompt = evaluator.build_judge_prompt(make_case(run_type="resume"), "response", {"manifest_path": "current.json"})
        self.assertIn("RUN TYPE:\nresume", prompt)
        self.assertIn("current.json", prompt)
        self.assertIn("shared step", prompt)
        self.assertIn("не является ошибкой", prompt)
        self.assertIn("Не выводи промежуточные текстовые сообщения", prompt)
        self.assertIn("Единственный текстовый ответ — один итоговый JSON-объект", prompt)

    def test_json_example_escapes_case_id(self):
        case = make_case(case_id='EVC-"quoted"\nnext-line')
        prompt = evaluator.build_judge_prompt(case, "response")
        example = prompt.split("Верни ТОЛЬКО валидный JSON следующей структуры:\n", 1)[1]
        result, _ = json.JSONDecoder().raw_decode(example)
        self.assertEqual(result["eval_case_id"], case["id"])

    def test_judge_checks_conflict_escalation_in_trace_without_inventing_case_properties(self):
        prompt = evaluator.build_judge_prompt(make_case(), "response")
        self.assertIn("Для проверяемых свойств остановки на конфликте сверь Review и трассу вызовов", prompt)
        self.assertIn("Запуск дизайнеров", prompt)
        self.assertIn("Ready to create: no", prompt)
        self.assertIn("Сам ID CNF не доказывает реальность противоречия", prompt)
        self.assertIn("Не добавляй проверок вне пунктов Eval Case", prompt)

    def test_failed_property_overrides_llm_claimed_pass(self):
        case = make_case()
        result = judge_result(case)
        result["expected_properties"][0]["passed"] = False
        checked = evaluator.validate_evaluation_result(case, result)
        self.assertEqual(checked["status"], "FAIL")
        self.assertEqual(checked["score"], 0.0)

    def test_forbidden_action_overrides_llm_claimed_pass(self):
        case = make_case()
        result = judge_result(case)
        result["forbidden_behavior"][0]["violated"] = True
        self.assertEqual(evaluator.validate_evaluation_result(case, result)["status"], "FAIL")

    def test_omitted_changed_or_non_boolean_checks_are_rejected(self):
        case = make_case()
        variants = []
        missing = judge_result(case)
        missing["expected_properties"] = []
        variants.append(missing)
        changed = judge_result(case)
        changed["expected_properties"][0]["property"] = "Other"
        variants.append(changed)
        not_boolean = judge_result(case)
        not_boolean["expected_properties"][0]["passed"] = "true"
        variants.append(not_boolean)
        missing_reason = judge_result(case)
        del missing_reason["expected_properties"][0]["reason"]
        variants.append(missing_reason)
        for result in variants:
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                evaluator.validate_evaluation_result(case, result)

    def test_evaluator_uses_separate_read_only_agent_and_stdin(self):
        case = make_case()
        event = {"type": "text", "part": {"text": json.dumps(judge_result(case))}}
        process = Mock(stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO(), returncode=0)
        process.communicate.return_value = (json.dumps(event), "")
        process.poll.return_value = 0
        with patch.object(evaluator.subprocess, "Popen", return_value=process) as popen, redirect_stdout(io.StringIO()):
            result = evaluator.evaluate_response(case, "response")
        self.assertEqual(result["status"], "PASS")
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--agent") + 1], "eval-judge")
        self.assertIn("--standalone", command)
        self.assertIn("RUN TYPE:\nfresh", process.communicate.call_args.args[0])
        self.assertTrue(process.stdin.closed)

    def test_intermediate_commentary_is_not_concatenated_with_final_json(self):
        case = make_case()
        events = [
            {"type": "step_start", "part": {"messageID": "reading"}},
            self.text_event("I'll start by reading the evidence files for this run.", "reading"),
            {"type": "tool_use", "part": {"tool": "read", "state": {"status": "completed"}}},
            {"type": "step_finish", "part": {"messageID": "reading", "reason": "tool-calls"}},
            {"type": "step_start", "part": {"messageID": "final"}},
            self.text_event(json.dumps(judge_result(case)), "final"),
        ]
        self.assertEqual(self.evaluate_events(case, events)["status"], "PASS")

    def test_message_ids_separate_responses_without_step_events(self):
        case = make_case()
        events = [
            self.text_event("Reading evidence.", "reading"),
            self.text_event(json.dumps(judge_result(case)), "final"),
        ]
        self.assertEqual(self.evaluate_events(case, events)["status"], "PASS")

    def test_step_boundaries_separate_responses_without_message_ids(self):
        case = make_case()
        events = [
            {"type": "step_start", "part": {}},
            self.text_event("Reading evidence."),
            {"type": "step_finish", "part": {"reason": "tool-calls"}},
            {"type": "step_start", "part": {}},
            self.text_event(json.dumps(judge_result(case))),
        ]
        self.assertEqual(self.evaluate_events(case, events)["status"], "PASS")

    def test_final_json_fragments_are_joined_without_inventing_newlines(self):
        case = make_case()
        text = json.dumps(judge_result(case))
        split = text.index("Expected") + 3
        events = [self.text_event(text[:split], "final"), self.text_event(text[split:], "final")]
        self.assertEqual(self.evaluate_events(case, events)["status"], "PASS")

    def test_earlier_json_is_not_used_when_final_response_is_missing_or_invalid(self):
        case = make_case()
        earlier = self.text_event(json.dumps(judge_result(case)), "earlier")
        variants = [
            [{"type": "step_start", "part": {"messageID": "final"}}],
            [self.text_event("{", "final")],
            [{"type": "step_finish", "part": {"reason": "tool-calls"}}],
        ]
        for final_events in variants:
            with self.subTest(final_events=final_events), self.assertRaises(RuntimeError):
                self.evaluate_events(case, [earlier, *final_events])

    def test_only_an_entire_final_json_object_is_accepted(self):
        case = make_case()
        text = json.dumps(judge_result(case))
        variants = [
            "I'll start by reading the evidence files.\n" + text,
            text + "\nEvaluation complete.",
            "```json\n" + text + "\n```",
            text + "\n" + text,
            "[" + text + "]",
        ]
        for response in variants:
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                self.evaluate_events(case, [self.text_event(response)])

    def test_json_in_tool_output_is_not_an_evaluation(self):
        case = make_case()
        event = {"type": "tool_use", "part": {"state": {"output": json.dumps(judge_result(case))}}}
        with self.assertRaises(RuntimeError):
            self.evaluate_events(case, [event])

    def test_truncated_last_event_does_not_fall_back_to_earlier_json(self):
        case = make_case()
        stdout = json.dumps(self.text_event(json.dumps(judge_result(case)), "earlier")) + '\n{"type":"text","part":'
        with redirect_stdout(io.StringIO()):
            self.assertEqual(evaluator.collect_final_judge_response(stdout), "")

    def test_nonstandard_constants_and_duplicate_json_keys_are_rejected(self):
        case = make_case()
        text = json.dumps(judge_result(case))
        variants = [
            text.replace('"score": 1.0', '"score": ' + constant)
            for constant in ("NaN", "Infinity", "-Infinity")
        ]
        variants.extend([
            text[:-1] + ', "score": 0.0}',
            text.replace('"passed": true', '"passed": false, "passed": true'),
        ])
        for response in variants:
            with self.subTest(response=response), self.assertRaises(RuntimeError):
                self.evaluate_events(case, [self.text_event(response)])

    def test_invalid_result_schema_is_rejected(self):
        case = make_case()
        variants = []
        for field in ("status", "score"):
            result = judge_result(case)
            del result[field]
            variants.append(result)
        for field, value in (("status", "SUCCESS"), ("status", True), ("score", "1.0"), ("score", True), ("score", 0.5)):
            result = judge_result(case)
            result[field] = value
            variants.append(result)
        extra_root = judge_result(case)
        extra_root["commentary"] = "extra"
        variants.append(extra_root)
        extra_check = judge_result(case)
        extra_check["expected_properties"][0]["commentary"] = "extra"
        variants.append(extra_check)
        for result in variants:
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                self.evaluate_events(case, [self.text_event(json.dumps(result))])

    def test_nonzero_process_exit_is_not_masked_by_valid_json(self):
        case = make_case()
        with self.assertRaisesRegex(RuntimeError, "Return code: 1"):
            self.evaluate_events(case, [self.text_event(json.dumps(judge_result(case)))], return_code=1)


class ProcessTests(TemporaryProject):
    def test_real_owned_process_is_reaped_after_exception_without_calling_opencode(self):
        command = [sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(60)"]
        with self.assertRaisesRegex(ValueError, "fixture failure"):
            with eval_process.open_opencode(command, self.project, stdin=eval_process.subprocess.DEVNULL) as process:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                raise ValueError("fixture failure")
        self.assertIsNotNone(process.poll())
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def test_windows_cleanup_targets_only_own_process_tree(self):
        process = Mock(pid=4321)
        process.poll.return_value = None
        with patch.object(eval_process.os, "name", "nt"), patch.object(
            eval_process.subprocess, "run", return_value=SimpleNamespace(returncode=0),
        ) as run:
            eval_process.stop_process(process)
        self.assertEqual(run.call_args.args[0], ["taskkill.exe", "/PID", "4321", "/T", "/F"])
        process.wait.assert_called_once_with(timeout=5)

    def test_windows_cleanup_failure_is_not_silently_ignored(self):
        process = Mock(pid=4321)
        process.poll.return_value = None
        with patch.object(eval_process.os, "name", "nt"), patch.object(
            eval_process.subprocess, "run", return_value=SimpleNamespace(returncode=1),
        ), self.assertRaises(EvalProcessCleanupError):
            eval_process.stop_process(process)

    def test_posix_cleanup_kills_remaining_children_and_reaps_parent(self):
        process = Mock(pid=4321)
        process.wait.side_effect = [eval_process.subprocess.TimeoutExpired("opencode", 5), 0]
        with patch.object(eval_process.os, "name", "posix"), patch.object(
            eval_process.os, "killpg", create=True,
        ) as killpg, patch.object(eval_process.signal, "SIGKILL", 9, create=True):
            eval_process.stop_process(process)
        self.assertEqual(killpg.call_args_list[0].args, (4321, eval_process.signal.SIGTERM))
        self.assertEqual(killpg.call_args_list[1].args, (4321, 9))
        self.assertEqual(process.wait.call_count, 2)


class InstructionTests(unittest.TestCase):
    def test_interactive_default_and_inputs_are_not_extended(self):
        project = Path(__file__).resolve().parents[2]
        config = (project / "opencode.jsonc").read_text(encoding="utf-8")
        self.assertIn('"default_agent": "orchestrator"', config)
        readme = (project / "README.md").read_text(encoding="utf-8")
        inputs = readme.split("### Обязательные входные данные", 1)[1].split("### Проектные значения", 1)[0]
        self.assertNotIn("run_type", inputs)
        self.assertNotIn("run_id", inputs)
        orchestrator = (project / ".opencode/agents/orchestrator.md").read_text(encoding="utf-8")
        self.assertIn("Обычный интерактивный Workflow ниже не меняется", orchestrator)
        self.assertIn("Без явного `EVAL_CONTEXT.mode: noninteractive_eval`", orchestrator)

    def test_missing_eval_context_blocks_fallback_and_contextless_handoff(self):
        project = Path(__file__).resolve().parents[2]
        contract = (project / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("ошибка передачи eval-контекста", contract)
        self.assertIn("не переходи к обычному Workflow", contract)
        self.assertIn("artifact с `Eval run`", contract)
        orchestrator = (project / ".opencode/agents/orchestrator.md").read_text(encoding="utf-8")
        self.assertIn("Первое действие в eval — чтение переданного manifest", orchestrator)
        self.assertIn("не запускай обычный Workflow вместо eval", orchestrator)

    def test_review_conflict_stops_both_branches_before_metadata_and_design(self):
        project = Path(__file__).resolve().parents[2]
        text = (project / ".opencode/agents/orchestrator.md").read_text(encoding="utf-8")
        workflow = text.split("# Workflow\n", 1)[1].split("## Остановка на ревью требований", 1)[0]
        self.assertIn("При любом неразрешённом Conflict", workflow)
        self.assertIn("немедленно", workflow)
        self.assertLess(workflow.index("При любом неразрешённом Conflict"), workflow.index("Выясни проектные значения"))
        stop = text.split("## Остановка на ревью требований", 1)[1].split("## Подтверждение публикации", 1)[0]
        for agent in ("task-designer", "test-designer", "jira-publisher", "qase-publisher"):
            self.assertIn(f"`{agent}`", stop)
        self.assertIn("независимо от критичности", stop)
        self.assertIn("Не формируй частичные спецификации", stop)
        self.assertIn("Запроси решение владельца требований через `question`", stop)
        self.assertIn("оба несовместимых фрагмента и их источники", stop)
        self.assertIn("При отсутствии ответа заверши Workflow с `REQUIRES_HUMAN_DECISION`", stop)
        self.assertIn("после новой версии Review", stop)
        self.assertIn("повторного независимого Requirement Review Gate", stop)

    def test_final_review_with_conflicts_is_not_a_passed_gate_or_successful_handoff(self):
        project = Path(__file__).resolve().parents[2]
        analyst = (project / ".opencode/agents/requirements-analyst.md").read_text(encoding="utf-8")
        skill = (project / ".opencode/skills/requirement-review/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("любой неразрешённый Conflict", analyst)
        self.assertIn("верни `REQUIRES_HUMAN_DECISION`, `design_allowed: no`", analyst)
        self.assertIn("не `SUCCESS` handoff", analyst)
        self.assertIn("Не назначай Gate", analyst)
        for field in ("Design allowed", "Unresolved conflicts", "Blocking open questions", "Required human decisions"):
            self.assertIn(field, skill)
        self.assertIn("Readiness согласуется с Conflicts и Open Questions", skill)
        orchestrator = (project / ".opencode/agents/orchestrator.md").read_text(encoding="utf-8")
        gate = orchestrator.split("## Requirement Review Gate", 1)[1].split("## Task Design Gate", 1)[0]
        self.assertIn("любой такой Conflict означает непройденный Gate", gate)
        self.assertIn("`FINAL` с `Design allowed: no`", gate)

    def test_designers_and_skills_do_not_turn_conflicts_into_partial_ready_lists(self):
        project = Path(__file__).resolve().parents[2]
        for agent, skill_name, service in (("task-designer", "task-design", "Jira"), ("test-designer", "test-design", "Qase")):
            with self.subTest(agent=agent):
                text = (project / f".opencode/agents/{agent}.md").read_text(encoding="utf-8")
                first_step = text.split("# Порядок работы", 1)[1].split("2. Загрузи skill", 1)[0]
                self.assertIn("Любой неразрешённый Conflict", first_step)
                self.assertIn("Не формируй спецификацию даже для незатронутых требований", first_step)
                self.assertIn(f"не выполняй {service}-вызовы", first_step)
                self.assertIn("`artifact: none`", text)
                skill = (project / f".opencode/skills/{skill_name}/SKILL.md").read_text(encoding="utf-8")
                self.assertIn("частичный artifact не формируй", skill)
                self.assertIn("без неразрешённых конфликтов и блокирующих вопросов", skill)

    def test_publishers_refuse_conflict_handoff_despite_ready_items_and_publication_permission(self):
        project = Path(__file__).resolve().parents[2]
        for agent in ("jira-publisher", "qase-publisher"):
            with self.subTest(agent=agent):
                text = (project / f".opencode/agents/{agent}.md").read_text(encoding="utf-8")
                self.assertIn("неразрешённом конфликте требований", text)
                self.assertIn("`REQUIRES_HUMAN_DECISION` без create/update-вызовов", text)
                self.assertIn("не превращай конфликт в `SKIPPED`", text)
                self.assertIn("подтверждение публикации эту блокировку не снимают", text)

    def test_eval_resume_and_noninteractive_exception_preserve_conflict_stop(self):
        project = Path(__file__).resolve().parents[2]
        text = (project / ".opencode/agents/orchestrator.md").read_text(encoding="utf-8")
        fresh = text.split("## Только неинтерактивный eval: fresh", 1)[1].split("## Только неинтерактивный eval: resume", 1)[0]
        self.assertIn("заверши попытку до шагов 4–9", fresh)
        self.assertIn("не создавай даже Epic или корневую suite", fresh)
        resume = text.split("## Только неинтерактивный eval: resume", 1)[1].split("# Workflow", 1)[0]
        self.assertIn("независимо от прежней отметки `PASSED`", resume)
        self.assertIn("не возобновляй дизайн, create или correction", resume)
        confirmation = text.split("## Подтверждение публикации", 1)[1].split("# Маршрутизация", 1)[0]
        self.assertIn("не отменяет остановку на ревью требований", confirmation)
        self.assertIn("Не начатые этапы оставь `NOT_CHECKED`", text)

    def test_final_success_is_forbidden_with_unresolved_requirement_conflicts(self):
        project = Path(__file__).resolve().parents[2]
        text = (project / ".opencode/agents/orchestrator.md").read_text(encoding="utf-8")
        final_gate = text.split("## Final Gate", 1)[1].split("# Параллелизм", 1)[0]
        self.assertIn("неразрешённые конфликты требований", final_gate)
        self.assertIn("Пропуск спорных элементов не позволяет установить `SUCCESS`", final_gate)
        contract = (project / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Любой неразрешённый конфликт требований блокирует", contract)
        self.assertIn("подтверждённая часть требований не разрешает обход", contract)

    def test_eval_judge_is_hidden_and_has_no_write_or_publication_permissions(self):
        project = Path(__file__).resolve().parents[2]
        judge = (project / ".opencode/agents/eval-judge.md").read_text(encoding="utf-8")
        frontmatter = judge.split("---", 2)[1]
        self.assertIn("hidden: true", frontmatter)
        self.assertNotIn('action: "edit"', frontmatter)
        self.assertNotIn('action: "subagent"', frontmatter)
        self.assertNotIn("jira_", frontmatter)
        self.assertNotIn("qase_", frontmatter)
        self.assertIn("Не выводи промежуточный текст", judge)
        self.assertIn("ровно один валидный JSON-объект", judge)


if __name__ == "__main__":
    unittest.main()
