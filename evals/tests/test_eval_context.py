import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evals.eval_context import (
    PROJECTS, EvalContextError, EvalRunRegistry, build_agent_prompt,
    collect_evidence, group_eval_cases, validate_eval_handoff,
)


NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)


def make_case(page_id=3964929, run_type="fresh", case_id="EVC-001"):
    return {
        "id": case_id,
        "name": "Eval case",
        "input": {"source": "confluence", "page_id": page_id, "run_type": run_type},
        "expected_properties": ["Expected"],
        "forbidden_behavior": ["Forbidden"],
    }


class TemporaryProject(unittest.TestCase):
    def setUp(self):
        preferred = Path(tempfile.gettempdir()) / "opencode"
        self.temporary = tempfile.TemporaryDirectory(
            prefix="tms-eval-tests-", dir=preferred if preferred.is_dir() else None,
        )
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve()
        self.registry = EvalRunRegistry(self.project)

    def save(self, path, manifest):
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def trace(self, manifest):
        path = self.project / "evals/results/runs" / manifest["run_id"] / f"attempt-{len(manifest['attempts'])}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return path


class GroupingTests(unittest.TestCase):
    def test_fresh_and_resume_are_separate_and_order_is_preserved(self):
        groups = group_eval_cases([
            make_case(), make_case(case_id="EVC-002"), make_case(run_type="resume"),
            make_case(), make_case(page_id=123),
        ])
        self.assertEqual([key for key, _ in groups], [
            ("3964929", "fresh"), ("3964929", "resume"),
            ("3964929", "fresh"), ("123", "fresh"),
        ])
        self.assertEqual(len(groups[0][1]), 2)

    def test_input_data_alias_is_supported(self):
        case = make_case(run_type="resume")
        case["input_data"] = case.pop("input")
        self.assertEqual(group_eval_cases([case])[0][0], ("3964929", "resume"))

    def test_missing_or_invalid_run_type_is_not_silently_fresh(self):
        for value in (None, "", "interactive", "FRESH", False):
            with self.subTest(value=value), self.assertRaises(EvalContextError):
                group_eval_cases([make_case(run_type=value)])

    def test_invalid_source_page_and_dataset_are_rejected(self):
        for value in (None, {}, [None]):
            with self.subTest(value=value), self.assertRaises(EvalContextError):
                group_eval_cases(value)
        for page in ("../other", None, False, 0):
            with self.subTest(page=page), self.assertRaises(EvalContextError):
                group_eval_cases([make_case(page_id=page)])
        case = make_case()
        case["input"]["source"] = None
        with self.assertRaises(EvalContextError):
            group_eval_cases([case])

    def test_invalid_case_id_and_checks_are_rejected_before_execution(self):
        for key, value in (("id", None), ("name", ""), ("expected_properties", "text"), ("forbidden_behavior", [False])):
            case = make_case()
            case[key] = value
            with self.subTest(key=key), self.assertRaises(EvalContextError):
                group_eval_cases([case])


class RegistryTests(TemporaryProject):
    def test_each_fresh_gets_unique_container_names_even_at_same_time(self):
        first_path, first = self.registry.prepare(3964929, "fresh", now=NOW)
        second_path, second = self.registry.prepare(3964929, "fresh", now=NOW)
        self.assertNotEqual(first_path, second_path)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["run_type"], "fresh")
        self.assertEqual(first["containers"]["jira_epic"]["summary"], first["containers"]["qase_root_suite"]["title"])
        self.assertIn("261112_120000", first["containers"]["jira_epic"]["summary"])
        self.assertTrue(all(gate == "NOT_CHECKED" for gate in first["gates"].values()))
        self.assertIsNone(first["workflow_status"])

    def test_resume_preserves_identity_containers_specs_and_reports(self):
        path, fresh = self.registry.prepare(3964929, "fresh", now=NOW)
        fresh["containers"]["jira_epic"]["key"] = "SCRUM-42"
        fresh["containers"]["qase_root_suite"]["id"] = 12
        fresh["artifacts"]["jira_tasks"] = "artifacts/jira-tasks/v1_261112_1200.md"
        fresh["reports"]["jira_creation"] = "artifacts/jira-creation/v1_261112_1200.md"
        fresh["source_version"] = 10
        self.save(path, fresh)
        resumed_path, resumed = self.registry.prepare(3964929, "resume", now=NOW + timedelta(hours=1))
        self.assertEqual(path, resumed_path)
        for field in ("run_id", "started_at", "containers", "artifacts", "reports", "source_version"):
            self.assertEqual(fresh[field], resumed[field])
        self.assertEqual(resumed["run_type"], "fresh")
        self.assertEqual(resumed["attempts"][-1]["run_type"], "resume")
        self.assertEqual(resumed["attempts"][-1]["number"], 2)

    def test_latest_uses_fresh_start_not_resume_time_or_success_status(self):
        older_path, older = self.registry.prepare(3964929, "fresh", now=NOW)
        older["workflow_status"] = "SUCCESS"
        self.save(older_path, older)
        self.registry.prepare(3964929, "resume", now=NOW + timedelta(days=5))
        latest_path, latest = self.registry.prepare(3964929, "fresh", now=NOW + timedelta(hours=1))
        latest["workflow_status"] = "PARTIAL"
        self.save(latest_path, latest)
        self.assertEqual(self.registry.latest_fresh(3964929)[0], latest_path)

    def test_page_and_projects_are_both_part_of_resume_lookup(self):
        correct_path, _ = self.registry.prepare(3964929, "fresh", now=NOW)
        self.registry.prepare(123, "fresh", now=NOW + timedelta(hours=1))
        projects = copy.deepcopy(PROJECTS)
        projects["jira"]["project_key"] = "OTHER"
        EvalRunRegistry(self.project, projects).prepare(3964929, "fresh", now=NOW + timedelta(hours=2))
        self.assertEqual(self.registry.latest_fresh(3964929)[0], correct_path)

    def test_missing_fresh_does_not_create_manifest(self):
        with self.assertRaisesRegex(EvalContextError, "нет fresh-прогона"):
            self.registry.prepare(3964929, "resume", now=NOW)
        self.assertEqual(list(self.registry.directory.rglob("*.json")), [])

    def test_completed_run_without_eval_handoff_cannot_be_resumed_or_republished(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        self.registry.finish_attempt(path, manifest, 0, now=NOW + timedelta(minutes=1))
        before = path.read_bytes()
        with self.assertRaisesRegex(EvalContextError, "eval-контекст"):
            self.registry.prepare(3964929, "resume", now=NOW + timedelta(hours=1))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(list(self.registry.directory.rglob("*.json"))), 1)

    def test_interrupted_attempt_without_final_handoff_remains_resumable(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        self.registry.finish_attempt(path, manifest, None, now=NOW + timedelta(minutes=1))
        resumed_path, resumed = self.registry.prepare(3964929, "resume", now=NOW + timedelta(hours=1))
        self.assertEqual(resumed_path, path)
        self.assertEqual(len(resumed["attempts"]), 2)

    def test_corrupt_manifest_is_not_ignored_in_favour_of_older_success(self):
        path, _ = self.registry.prepare(3964929, "fresh", now=NOW)
        (path.parent / "broken.json").write_text("{", encoding="utf-8")
        with self.assertRaises(EvalContextError):
            self.registry.latest_fresh(3964929)

    def test_incomplete_or_invalid_manifest_cannot_be_resumed(self):
        path, valid = self.registry.prepare(3964929, "fresh", now=NOW)
        for key in ("source_version", "workflow_status", "publication_confirmation", "containers", "reports"):
            changed = copy.deepcopy(valid)
            del changed[key]
            self.save(path, changed)
            with self.subTest(key=key), self.assertRaises(EvalContextError):
                self.registry.prepare(3964929, "resume", now=NOW + timedelta(hours=1))
        for key, value in (("schema_version", True), ("page_id", 3964929), ("workflow_status", "PASS"), ("artifacts", {})):
            changed = copy.deepcopy(valid)
            changed[key] = value
            self.save(path, changed)
            with self.subTest(key=key), self.assertRaises(EvalContextError):
                self.registry.load(path)

    def test_artifact_paths_are_validated_before_resume_launch(self):
        path, valid = self.registry.prepare(3964929, "fresh", now=NOW)
        for value in (42, {}, "opencode.jsonc", "../outside.md"):
            changed = copy.deepcopy(valid)
            changed["artifacts"]["requirement_review"] = value
            self.save(path, changed)
            with self.subTest(value=value), self.assertRaises(EvalContextError):
                self.registry.prepare(3964929, "resume", now=NOW + timedelta(hours=1))

    def test_report_filename_must_match_original_specification(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        manifest["artifacts"]["jira_tasks"] = "artifacts/jira-tasks/v1_261112_1200.md"
        manifest["reports"]["jira_creation"] = "artifacts/jira-creation/v2_261112_1200.md"
        self.save(path, manifest)
        with self.assertRaisesRegex(EvalContextError, "не соответствует"):
            self.registry.prepare(3964929, "resume", now=NOW + timedelta(hours=1))

    def test_manifest_timestamp_cannot_change_resume_order(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        manifest["started_at"] = (NOW + timedelta(days=1)).isoformat()
        self.save(path, manifest)
        with self.assertRaisesRegex(EvalContextError, "run_id не соответствует"):
            self.registry.latest_fresh(3964929)

    def test_equal_start_times_have_deterministic_run_id_tie_breaker(self):
        first, _ = self.registry.prepare(3964929, "fresh", now=NOW)
        second, _ = self.registry.prepare(3964929, "fresh", now=NOW)
        self.assertEqual(self.registry.latest_fresh(3964929)[0], max(first, second))

    def test_returned_manifest_projects_do_not_mutate_registry_identity(self):
        _, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        manifest["projects"]["jira"]["project_key"] = "OTHER"
        self.assertEqual(self.registry.projects, PROJECTS)

    def test_runner_finishes_only_technical_attempt_not_gate_checkpoints(self):
        path, expected = self.registry.prepare(3964929, "fresh", now=NOW)
        checkpoint = self.registry.load(path)
        checkpoint["source_version"] = 10
        checkpoint["gates"]["requirement_review"] = "PASSED"
        checkpoint["workflow_status"] = "PARTIAL"
        self.save(path, checkpoint)
        finished = self.registry.finish_attempt(path, expected, 0, now=NOW + timedelta(minutes=1))
        self.assertEqual(finished["source_version"], 10)
        self.assertEqual(finished["gates"]["requirement_review"], "PASSED")
        self.assertEqual(finished["workflow_status"], "PARTIAL")
        self.assertEqual(finished["attempts"][-1]["return_code"], 0)

    def test_identity_cannot_be_changed_by_orchestrator(self):
        path, expected = self.registry.prepare(3964929, "fresh", now=NOW)
        changed = self.registry.load(path)
        changed["projects"]["jira"]["project_key"] = "OTHER"
        self.save(path, changed)
        with self.assertRaises(EvalContextError):
            self.registry.finish_attempt(path, expected, 0)

    def test_orchestrator_cannot_rewrite_technical_attempt_fields(self):
        path, expected = self.registry.prepare(3964929, "fresh", now=NOW)
        changed = self.registry.load(path)
        changed["attempts"][-1]["finished_at"] = (NOW + timedelta(minutes=1)).isoformat()
        self.save(path, changed)
        with self.assertRaisesRegex(EvalContextError, "технический журнал"):
            self.registry.finish_attempt(path, expected, 0)

    def test_project_lock_rejects_overlapping_eval_and_can_be_reacquired(self):
        with self.registry.locked():
            with self.assertRaises(EvalContextError):
                with EvalRunRegistry(self.project).locked():
                    self.fail("Второй eval получил блокировку")
        with self.registry.locked():
            pass

    def test_prompt_contains_actual_type_and_stable_resume_identity(self):
        path, fresh = self.registry.prepare(3964929, "fresh", now=NOW)
        prompt = build_agent_prompt(self.registry, path, fresh)
        self.assertIn('"mode": "noninteractive_eval"', prompt)
        self.assertIn('"run_type": "fresh"', prompt)
        resumed_path, resumed = self.registry.prepare(3964929, "resume", now=NOW + timedelta(hours=1))
        resume_prompt = build_agent_prompt(self.registry, resumed_path, resumed)
        self.assertIn('"run_type": "resume"', resume_prompt)
        self.assertIn(fresh["run_id"], resume_prompt)
        self.assertIn(self.registry.relative_path(path), resume_prompt)

    def test_eval_prompt_starts_with_fail_closed_guard_and_exact_container_scope(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        prompt = build_agent_prompt(self.registry, path, manifest)
        first_line = prompt.splitlines()[0]
        self.assertIn("eval_runner.py", first_line)
        self.assertIn("обычный Workflow запрещён", first_line)
        self.assertIn("Первое действие — прочитай переданный manifest", prompt)
        self.assertIn(manifest["containers"]["jira_epic"]["summary"], prompt)
        self.assertIn(manifest["containers"]["qase_root_suite"]["title"], prompt)
        self.assertIn("задачи и кейсы других прогонов не являются дублями", prompt)

    def test_conflict_stop_precedes_publication_permission_in_fresh_and_resume_prompts(self):
        path, fresh = self.registry.prepare(3964929, "fresh", now=NOW)
        _, resumed = self.registry.prepare(3964929, "resume", now=NOW + timedelta(hours=1))
        for manifest in (fresh, resumed):
            with self.subTest(run_type=manifest["attempts"][-1]["run_type"]):
                prompt = build_agent_prompt(self.registry, path, manifest)
                self.assertIn("любой неразрешённый конфликт требований", prompt)
                self.assertIn("останавливает весь Workflow с REQUIRES_HUMAN_DECISION", prompt)
                self.assertIn("не запускай дизайнеров и паблишеров", prompt)
                self.assertIn("Верни точные вопросы человеку с ID и источниками", prompt)
                self.assertIn("Это разрешение не является решением конфликта", prompt)
                self.assertLess(prompt.index("После ревью любой"), prompt.index("Даю разрешение на публикацию"))


class HandoffTests(TemporaryProject):
    def test_exit_zero_and_success_text_do_not_replace_registered_eval_context(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        self.registry.finish_attempt(path, manifest, 0, now=NOW + timedelta(minutes=1))
        evidence = collect_evidence(self.registry, path, self.trace(manifest))
        with self.assertRaisesRegex(EvalContextError, "eval-контекст"):
            validate_eval_handoff(evidence)

    def test_registered_blocked_or_partial_workflow_can_be_evaluated_without_containers(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        for status in ("FAILED", "PARTIAL", "REQUIRES_HUMAN_DECISION"):
            manifest["workflow_status"] = status
            self.save(path, manifest)
            with self.subTest(status=status):
                evidence = collect_evidence(self.registry, path, self.trace(manifest))
                validate_eval_handoff(evidence)

    def test_success_requires_current_artifacts_and_container_identifiers(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        manifest["workflow_status"] = "SUCCESS"
        manifest["containers"]["jira_epic"].update(local_id="T-1", key="SCRUM-42")
        manifest["containers"]["qase_root_suite"].update(local_id="S-1", id=42)
        manifest["gates"] = {key: "PASSED" for key in manifest["gates"]}
        for mapping, directory in (("artifacts", {
            "requirement_review": "requirement-review", "jira_tasks": "jira-tasks", "test_model": "test-model",
        }), ("reports", {"jira_creation": "jira-creation", "qase_creation": "qase-creation"})):
            for key, name in directory.items():
                artifact = self.project / "artifacts" / name / "v1_261112_1200.md"
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text(f"Eval run: {manifest['run_id']}\nSource pageId: 3964929\n", encoding="utf-8")
                manifest[mapping][key] = self.registry.relative_path(artifact)
        self.save(path, manifest)
        trace = self.trace(manifest)
        trace.with_suffix(".response.md").write_text("unit-test stub", encoding="utf-8")
        evidence = collect_evidence(self.registry, path, trace)
        self.assertEqual(evidence["missing"], [])
        before = path.read_bytes()
        validate_eval_handoff(evidence)
        self.assertEqual(path.read_bytes(), before)
        for missing in (["jira_tasks"], ["qase_creation"]):
            changed = copy.deepcopy(evidence)
            changed["missing"] = missing
            with self.subTest(missing=missing), self.assertRaises(EvalContextError):
                validate_eval_handoff(changed)
        for container, field in (("jira_epic", "key"), ("qase_root_suite", "id"), ("jira_epic", "local_id")):
            changed = copy.deepcopy(evidence)
            changed["manifest"]["containers"][container][field] = None
            with self.subTest(container=container, field=field), self.assertRaises(EvalContextError):
                validate_eval_handoff(changed)

    def test_success_requires_every_mandatory_gate_without_rewriting_gate_results(self):
        _, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        manifest["workflow_status"] = "SUCCESS"
        manifest["containers"]["jira_epic"].update(local_id="T-1", key="SCRUM-42")
        manifest["containers"]["qase_root_suite"].update(local_id="S-1", id=42)
        manifest["gates"] = {key: "PASSED" for key in manifest["gates"]}
        evidence = {"manifest": manifest, "missing": []}
        for gate in manifest["gates"]:
            for status in ("FAILED", "NOT_CHECKED"):
                changed = copy.deepcopy(evidence)
                changed["manifest"]["gates"][gate] = status
                before = copy.deepcopy(changed)
                with self.subTest(gate=gate, status=status), self.assertRaisesRegex(EvalContextError, f"gate:{gate}"):
                    validate_eval_handoff(changed)
                self.assertEqual(changed, before)

    def test_human_request_after_failed_review_does_not_require_design_or_publication(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        manifest["workflow_status"] = "REQUIRES_HUMAN_DECISION"
        manifest["gates"]["requirement_review"] = "FAILED"
        manifest["gates"]["final"] = "FAILED"
        self.save(path, manifest)
        before = path.read_bytes()
        evidence = collect_evidence(self.registry, path, self.trace(manifest))
        validate_eval_handoff(evidence)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(all(manifest["gates"][key] == "NOT_CHECKED" for key in (
            "jira_tasks", "test_model", "jira_creation", "qase_creation",
        )))
        self.assertIsNone(manifest["containers"]["jira_epic"]["key"])
        self.assertIsNone(manifest["containers"]["qase_root_suite"]["id"])


class EvidenceTests(TemporaryProject):
    def test_archived_prompt_is_optional_for_old_runs_and_included_when_present(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        trace = self.trace(manifest)
        original = collect_evidence(self.registry, path, trace)
        self.assertNotIn("prompt_path", original["attempts"][0])
        prompt_path = trace.with_suffix(".prompt.md")
        prompt_path.write_text(build_agent_prompt(self.registry, path, manifest), encoding="utf-8")
        updated = collect_evidence(self.registry, path, trace)
        self.assertEqual(updated["attempts"][0]["prompt_path"], self.registry.relative_path(prompt_path))
        self.assertEqual(updated["missing"], original["missing"])

    def test_only_current_manifest_paths_are_collected(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        stage = self.project / "artifacts/requirement-review/v1_261112_1200.md"
        stage.parent.mkdir(parents=True)
        stage.write_text(f"Eval run: {manifest['run_id']}\nSource pageId: 3964929\n", encoding="utf-8")
        manifest["artifacts"]["requirement_review"] = stage.relative_to(self.project).as_posix()
        self.save(path, manifest)
        evidence = collect_evidence(self.registry, path, self.trace(manifest))
        self.assertEqual(evidence["files"], {"requirement_review": "artifacts/requirement-review/v1_261112_1200.md"})
        self.assertIn("jira_creation", evidence["missing"])

    def test_paths_outside_artifact_directories_are_rejected(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        manifest["artifacts"]["requirement_review"] = "opencode.jsonc"
        self.save(path, manifest)
        with self.assertRaises(EvalContextError):
            collect_evidence(self.registry, path, self.trace(manifest))

    def test_artifact_from_another_run_is_rejected(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        stage = self.project / "artifacts/requirement-review/v1_261112_1200.md"
        stage.parent.mkdir(parents=True)
        stage.write_text("Eval run: other\nSource pageId: 3964929\n", encoding="utf-8")
        manifest["artifacts"]["requirement_review"] = stage.relative_to(self.project).as_posix()
        self.save(path, manifest)
        with self.assertRaises(EvalContextError):
            collect_evidence(self.registry, path, self.trace(manifest))

    def test_marker_prefix_of_another_page_is_not_a_match(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        stage = self.project / "artifacts/requirement-review/v1_261112_1200.md"
        stage.parent.mkdir(parents=True)
        stage.write_text(f"- Eval run: {manifest['run_id']}\n- Source pageId: 39649290\n", encoding="utf-8")
        manifest["artifacts"]["requirement_review"] = stage.relative_to(self.project).as_posix()
        self.save(path, manifest)
        with self.assertRaises(EvalContextError):
            collect_evidence(self.registry, path, self.trace(manifest))

    def test_bullet_markers_in_header_are_accepted(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        stage = self.project / "artifacts/requirement-review/v1_261112_1200.md"
        stage.parent.mkdir(parents=True)
        stage.write_text(f"- Eval run: {manifest['run_id']}\n- Source pageId: 3964929\n", encoding="utf-8")
        manifest["artifacts"]["requirement_review"] = stage.relative_to(self.project).as_posix()
        self.save(path, manifest)
        self.assertNotIn("requirement_review", collect_evidence(self.registry, path, self.trace(manifest))["missing"])

    def test_inline_code_markers_in_creation_reports_are_accepted(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        for report, spec, spec_directory, report_directory in (
            ("jira_creation", "jira_tasks", "jira-tasks", "jira-creation"),
            ("qase_creation", "test_model", "test-model", "qase-creation"),
        ):
            specification = self.project / "artifacts" / spec_directory / "v1_261112_1200.md"
            specification.parent.mkdir(parents=True, exist_ok=True)
            specification.write_text(f"- Eval run: {manifest['run_id']}\n- Source pageId: 3964929\n", encoding="utf-8")
            manifest["artifacts"][spec] = self.registry.relative_path(specification)
            creation = self.project / "artifacts" / report_directory / specification.name
            creation.parent.mkdir(parents=True, exist_ok=True)
            creation.write_text(
                f"# Creation Report\n\n- Eval run: `{manifest['run_id']}`\n- Source pageId: `3964929`\n",
                encoding="utf-8",
            )
            manifest["reports"][report] = self.registry.relative_path(creation)
        self.save(path, manifest)
        candidates = [path] + [
            self.project / value
            for mapping in (manifest["artifacts"], manifest["reports"])
            for value in mapping.values() if value is not None
        ]
        before = {candidate: candidate.read_bytes() for candidate in candidates}
        evidence = collect_evidence(self.registry, path, self.trace(manifest))
        self.assertTrue({"jira_creation", "qase_creation"} <= evidence["files"].keys())
        self.assertFalse({"jira_creation", "qase_creation"} & set(evidence["missing"]))
        self.assertEqual({candidate: candidate.read_bytes() for candidate in before}, before)

    def test_plain_and_inline_code_markers_can_be_mixed(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        stage = self.project / "artifacts/requirement-review/v1_261112_1200.md"
        stage.parent.mkdir(parents=True)
        manifest["artifacts"]["requirement_review"] = self.registry.relative_path(stage)
        self.save(path, manifest)
        trace = self.trace(manifest)
        for run_wrapper, page_wrapper in (("`", ""), ("", "`"), ("`", "`")):
            for bullet in ("", "- ", "* "):
                with self.subTest(run_wrapper=run_wrapper, page_wrapper=page_wrapper, bullet=bullet):
                    stage.write_text(
                        f"{bullet}Eval run: {run_wrapper}{manifest['run_id']}{run_wrapper}\r\n"
                        f"{bullet}Source pageId: {page_wrapper}3964929{page_wrapper}\r\n",
                        encoding="utf-8",
                    )
                    self.assertNotIn("requirement_review", collect_evidence(self.registry, path, trace)["missing"])

    def test_inline_code_markers_remain_exact_and_require_paired_backticks(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        stage = self.project / "artifacts/requirement-review/v1_261112_1200.md"
        stage.parent.mkdir(parents=True)
        manifest["artifacts"]["requirement_review"] = self.registry.relative_path(stage)
        self.save(path, manifest)
        trace = self.trace(manifest)
        run_id = manifest["run_id"]
        for label, value in (
            ("Eval run", "`other`"),
            ("Eval run", f"`{run_id}extra`"),
            ("Source pageId", "`39649290`"),
            ("Eval run", f"`{run_id}"),
            ("Eval run", f"{run_id}`"),
            ("Source pageId", "`3964929"),
            ("Source pageId", "3964929`"),
            ("Eval run", f"`{run_id}` trailing text"),
            ("Source pageId", "`3964929` trailing text"),
        ):
            markers = {"Eval run": f"`{run_id}`", "Source pageId": "`3964929`"}
            markers[label] = value
            stage.write_text("\n".join(f"- {name}: {marker}" for name, marker in markers.items()), encoding="utf-8")
            with self.subTest(label=label, value=value), self.assertRaisesRegex(EvalContextError, label):
                collect_evidence(self.registry, path, trace)

    def test_corrupt_artifact_encoding_is_a_context_error(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        stage = self.project / "artifacts/requirement-review/v1_261112_1200.md"
        stage.parent.mkdir(parents=True)
        stage.write_bytes(b"\xff\xfe")
        manifest["artifacts"]["requirement_review"] = stage.relative_to(self.project).as_posix()
        self.save(path, manifest)
        with self.assertRaisesRegex(EvalContextError, "Нельзя прочитать artifact"):
            collect_evidence(self.registry, path, self.trace(manifest))

    def test_resume_evidence_includes_previous_attempts_of_only_same_run(self):
        path, fresh = self.registry.prepare(3964929, "fresh", now=NOW)
        original_trace = self.trace(fresh)
        original_trace.with_suffix(".response.md").write_text("original", encoding="utf-8")
        path, resumed = self.registry.prepare(3964929, "resume", now=NOW + timedelta(hours=1))
        current_trace = self.trace(resumed)
        current_trace.with_suffix(".response.md").write_text("resumed", encoding="utf-8")
        _, foreign = self.registry.prepare(3964929, "fresh", now=NOW + timedelta(hours=2))
        self.trace(foreign)
        evidence = collect_evidence(self.registry, path, current_trace)
        self.assertEqual([attempt["number"] for attempt in evidence["attempts"]], [1, 2])
        self.assertEqual(evidence["attempts"][0]["trace_path"], self.registry.relative_path(original_trace))
        self.assertTrue(all(fresh["run_id"] in attempt["trace_path"] for attempt in evidence["attempts"]))
        self.assertNotIn(foreign["run_id"], json.dumps(evidence))

    def test_trace_of_another_attempt_is_rejected(self):
        path, manifest = self.registry.prepare(3964929, "fresh", now=NOW)
        wrong_trace = self.trace(manifest).with_name("attempt-2.jsonl")
        with self.assertRaises(EvalContextError):
            collect_evidence(self.registry, path, wrong_trace)


if __name__ == "__main__":
    unittest.main()
