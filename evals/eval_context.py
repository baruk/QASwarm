"""Состояние и контекст только неинтерактивных eval-прогонов."""

import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


RUN_TYPES = ("fresh", "resume")
REGISTRY_DIRECTORY = Path("artifacts/eval-runs")
PROJECTS = {
    "jira": {
        "cloud_id": "a6b9202f-c2db-401f-85e1-a2ec85eb1ea2",
        "project_key": "SCRUM",
        "project_id": "10000",
    },
    "qase": {"project_code": "TMS"},
}
ARTIFACT_DIRECTORIES = {
    "requirement_review": "requirement-review",
    "jira_tasks": "jira-tasks",
    "test_model": "test-model",
    "jira_creation": "jira-creation",
    "qase_creation": "qase-creation",
}
RUN_ID_PATTERN = re.compile(r"[0-9]{6}_[0-9]{6}_[0-9a-f]{32}")
ARTIFACT_NAME_PATTERN = re.compile(r"v[1-9][0-9]*_[0-9]{6}_[0-9]{4}\.md")
ATTEMPT_FIELDS = ("number", "run_type", "started_at", "finished_at", "return_code")


class EvalContextError(RuntimeError):
    """Некорректный eval-контекст или небезопасное возобновление."""


def normalize_page_id(page_id):
    value = str(page_id)
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise EvalContextError(f"Некорректный page_id: {page_id!r}")
    return value


def validate_run_type(run_type):
    if run_type not in RUN_TYPES:
        raise EvalContextError(
            f"run_type должен быть fresh или resume, получено: {run_type!r}"
        )
    return run_type


def group_eval_cases(eval_cases):
    """Объединить только соседние кейсы одного состояния, сохранив порядок."""
    if not isinstance(eval_cases, list):
        raise EvalContextError("Golden dataset должен быть списком кейсов")
    groups = []
    for eval_case in eval_cases:
        if (not isinstance(eval_case, dict)
                or not isinstance(eval_case.get("name"), str)
                or not eval_case["name"].strip()):
            raise EvalContextError("Eval Case должен содержать name и input")
        eval_input = eval_case.get("input_data", eval_case.get("input"))
        case_id = eval_case.get("eval_id", eval_case.get("id"))
        if not isinstance(case_id, str) or not case_id.strip():
            raise EvalContextError("Eval Case должен содержать непустой id/eval_id")
        for label, checks in (
            ("expected_properties", eval_case.get("expected_behavior", eval_case.get("expected_properties", []))),
            ("forbidden_behavior", eval_case.get("forbidden_behavior", [])),
        ):
            if (not isinstance(checks, list)
                    or any(not isinstance(check, str) or not check.strip() for check in checks)):
                raise EvalContextError(f"Eval Case {case_id}: {label} должен быть списком текстовых пунктов")
        if not isinstance(eval_input, dict):
            raise EvalContextError(f"Eval Case {case_id}: отсутствует input")
        source = eval_input.get("source")
        if not isinstance(source, str) or source.lower() != "confluence":
            raise EvalContextError(
                f"Eval Case {case_id} использует неподдерживаемый источник"
            )
        page_id = normalize_page_id(eval_input.get("page_id"))
        run_type = validate_run_type(eval_input.get("run_type"))
        key = (page_id, run_type)
        if not groups or groups[-1][0] != key:
            groups.append((key, []))
        groups[-1][1].append(eval_case)
    return groups


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise EvalContextError(f"Некорректное время fresh-прогона: {value!r}") from error
    if result.tzinfo is None:
        raise EvalContextError("Время fresh-прогона должно содержать часовой пояс")
    return result


def _write_json(path, value, *, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        with path.open("x", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


class EvalRunRegistry:
    """Один manifest на fresh; resume дополняет попытки того же manifest."""

    def __init__(self, project_path, projects=None):
        self.project_path = Path(project_path).resolve()
        self.directory = self.project_path / REGISTRY_DIRECTORY
        self.projects = json.loads(json.dumps(PROJECTS if projects is None else projects))

    @contextmanager
    def locked(self):
        """Сериализовать eval в проекте; ОС освобождает блокировку после сбоя."""
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / ".runner.lock").open("a+b") as file:
            if file.tell() == 0:
                file.write(b"\0")
                file.flush()
            file.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise EvalContextError("В этом проекте уже выполняется другой eval") from error
            try:
                yield
            finally:
                if os.name == "nt":
                    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    def relative_path(self, path):
        try:
            return Path(path).resolve().relative_to(self.project_path).as_posix()
        except ValueError as error:
            raise EvalContextError("Eval-файл находится вне проекта") from error

    def artifact_path(self, key, value):
        if not isinstance(value, str) or not value:
            raise EvalContextError(f"Недопустимый путь {key}: {value!r}")
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.project_path / candidate
        candidate = candidate.resolve()
        self.relative_path(candidate)
        expected_directory = self.project_path / "artifacts" / ARTIFACT_DIRECTORIES[key]
        if (candidate.parent != expected_directory.resolve()
                or not ARTIFACT_NAME_PATTERN.fullmatch(candidate.name)):
            raise EvalContextError(f"Недопустимый путь {key}: {value!r}")
        return candidate

    def load(self, path):
        path = Path(path).resolve()
        try:
            path.relative_to(self.directory.resolve())
            with path.open(encoding="utf-8") as file:
                manifest = json.load(file)
        except (OSError, ValueError) as error:
            raise EvalContextError(f"Нельзя прочитать manifest {path}: {error}") from error
        try:
            if not isinstance(manifest, dict):
                raise EvalContextError("Manifest должен быть JSON-объектом")
            if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1 or manifest["run_type"] != "fresh":
                raise EvalContextError("Manifest должен описывать исходный fresh-прогон")
            page_id = normalize_page_id(manifest["page_id"])
            if manifest["page_id"] != page_id:
                raise EvalContextError("page_id в manifest должен быть канонической строкой")
            run_id = manifest["run_id"]
            if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
                raise EvalContextError("Некорректный run_id")
            if path != self.directory / page_id / f"{run_id}.json":
                raise EvalContextError("Путь manifest не соответствует page_id и run_id")
            started = _timestamp(manifest["started_at"])
            if not run_id.startswith(f"{started.astimezone(timezone.utc):%y%m%d_%H%M%S}_"):
                raise EvalContextError("run_id не соответствует времени исходного fresh-прогона")
            title = f"EVAL page-{page_id} {run_id}"
            if manifest["containers"]["jira_epic"]["summary"] != title:
                raise EvalContextError("Изменено имя Epic исходного fresh-прогона")
            if manifest["containers"]["qase_root_suite"]["title"] != title:
                raise EvalContextError("Изменено имя корневой suite исходного fresh-прогона")
            for container, local_id in (("jira_epic", "T-1"), ("qase_root_suite", "S-1")):
                if manifest["containers"][container]["local_id"] not in (None, local_id):
                    raise EvalContextError(f"Некорректный Local ID контейнера {container}")
            epic_key = manifest["containers"]["jira_epic"]["key"]
            root_id = manifest["containers"]["qase_root_suite"]["id"]
            if epic_key is not None and (not isinstance(epic_key, str) or not epic_key):
                raise EvalContextError("Некорректный ключ Epic")
            if root_id is not None and (type(root_id) is not int or root_id <= 0):
                raise EvalContextError("Некорректный ID корневой suite")
            if not isinstance(manifest["projects"], dict):
                raise EvalContextError("Отсутствуют целевые проекты")
            for key in ("cloud_id", "project_key", "project_id"):
                if not isinstance(manifest["projects"]["jira"][key], str) or not manifest["projects"]["jira"][key]:
                    raise EvalContextError(f"Некорректный Jira-проект: {key}")
            if not isinstance(manifest["projects"]["qase"]["project_code"], str) or not manifest["projects"]["qase"]["project_code"]:
                raise EvalContextError("Некорректный Qase-проект")
            for key in ("artifacts", "reports", "gates", "project_context"):
                if not isinstance(manifest[key], dict):
                    raise EvalContextError(f"Некорректное поле manifest: {key}")
            for mapping, keys in (
                ("artifacts", {"requirement_review", "jira_tasks", "test_model"}),
                ("reports", {"jira_creation", "qase_creation"}),
            ):
                if set(manifest[mapping]) != keys:
                    raise EvalContextError(f"Неполный набор путей {mapping}")
                for key, value in manifest[mapping].items():
                    if value is not None:
                        self.artifact_path(key, value)
            for report, specification in (("jira_creation", "jira_tasks"), ("qase_creation", "test_model")):
                report_path = manifest["reports"][report]
                spec_path = manifest["artifacts"][specification]
                if report_path is not None and (
                    spec_path is None
                    or self.artifact_path(report, report_path).name != self.artifact_path(specification, spec_path).name
                ):
                    raise EvalContextError(f"Creation Report {report} не соответствует исходной спецификации")
            for key in ("source_version", "publication_confirmation", "workflow_status"):
                if key not in manifest:
                    raise EvalContextError(f"Отсутствует checkpoint {key}")
            if manifest["workflow_status"] not in (None, "SUCCESS", "PARTIAL", "FAILED", "REQUIRES_HUMAN_DECISION"):
                raise EvalContextError("Некорректный workflow_status")
            if not isinstance(manifest["attempts"], list) or not manifest["attempts"]:
                raise EvalContextError("Отсутствует журнал попыток")
            for number, attempt in enumerate(manifest["attempts"], 1):
                if type(attempt["number"]) is not int or attempt["number"] != number:
                    raise EvalContextError("Нарушена последовательность попыток")
                validate_run_type(attempt["run_type"])
                if attempt["run_type"] != ("fresh" if number == 1 else "resume"):
                    raise EvalContextError("Исходная попытка должна быть fresh, последующие — resume")
                _timestamp(attempt["started_at"])
                if number == 1 and attempt["started_at"] != manifest["started_at"]:
                    raise EvalContextError("Первая попытка не соответствует началу fresh-прогона")
                if attempt["finished_at"] is not None:
                    _timestamp(attempt["finished_at"])
                if attempt["return_code"] is not None and type(attempt["return_code"]) is not int:
                    raise EvalContextError("Некорректный код завершения попытки")
            if set(manifest["gates"]) != {*ARTIFACT_DIRECTORIES, "final"}:
                raise EvalContextError("Некорректный набор Gates в manifest")
            if any(value not in ("NOT_CHECKED", "PASSED", "FAILED") for value in manifest["gates"].values()):
                raise EvalContextError("Некорректное значение Gate в manifest")
        except (KeyError, TypeError) as error:
            raise EvalContextError(f"Неполный manifest {path}: {error}") from error
        return manifest

    def latest_fresh(self, page_id):
        page_id = normalize_page_id(page_id)
        candidates = []
        for path in (self.directory / page_id).glob("*.json"):
            manifest = self.load(path)
            if manifest["projects"] == self.projects:
                candidates.append((path, manifest))
        if not candidates:
            raise EvalContextError(
                f"Для pageId={page_id} и заданных Jira/Qase-проектов нет fresh-прогона; "
                "resume не переключается на fresh"
            )
        return max(candidates, key=lambda item: (
            _timestamp(item[1]["started_at"]), item[1]["run_id"]
        ))

    def prepare(self, page_id, run_type, *, now=None):
        page_id = normalize_page_id(page_id)
        validate_run_type(run_type)
        now = datetime.now(timezone.utc) if now is None else now
        started_at = _timestamp(now.isoformat()).astimezone(timezone.utc).isoformat()
        attempt = {
            "number": 1,
            "run_type": run_type,
            "started_at": started_at,
            "finished_at": None,
            "return_code": None,
        }
        if run_type == "resume":
            path, manifest = self.latest_fresh(page_id)
            previous = manifest["attempts"][-1]
            if (previous["finished_at"] is not None
                    and previous["return_code"] == 0
                    and manifest["workflow_status"] is None):
                raise EvalContextError(
                    "Предыдущая попытка завершилась без зарегистрированного handoff: "
                    "eval-контекст не подтверждён. Resume и повторная публикация "
                    "заблокированы; требуется разбор исходного прогона."
                )
            attempt["number"] = len(manifest["attempts"]) + 1
            manifest["attempts"].append(attempt)
            _write_json(path, manifest)
            return path, manifest

        run_id = f"{now.astimezone(timezone.utc):%y%m%d_%H%M%S}_{uuid.uuid4().hex}"
        title = f"EVAL page-{page_id} {run_id}"
        path = self.directory / page_id / f"{run_id}.json"
        manifest = {
            "schema_version": 1,
            "run_type": "fresh",
            "run_id": run_id,
            "page_id": page_id,
            "started_at": started_at,
            "projects": json.loads(json.dumps(self.projects)),
            "containers": {
                "jira_epic": {"local_id": None, "summary": title, "key": None},
                "qase_root_suite": {"local_id": None, "title": title, "id": None},
            },
            "artifacts": {
                "requirement_review": None, "jira_tasks": None, "test_model": None,
            },
            "reports": {"jira_creation": None, "qase_creation": None},
            "source_version": None,
            "project_context": {},
            "gates": {key: "NOT_CHECKED" for key in (*ARTIFACT_DIRECTORIES, "final")},
            "publication_confirmation": None,
            "workflow_status": None,
            "attempts": [attempt],
        }
        _write_json(path, manifest, exclusive=True)
        return path, manifest

    def finish_attempt(self, path, expected_manifest, return_code, *, now=None):
        """Не перезаписывать checkpoints и Gate-решения, записанные оркестратором."""
        manifest = self.load(path)
        for key in ("run_id", "page_id", "started_at", "projects", "run_type"):
            if manifest[key] != expected_manifest[key]:
                raise EvalContextError(f"Оркестратор изменил идентичность прогона: {key}")
        attempt_number = expected_manifest["attempts"][-1]["number"]
        if len(manifest["attempts"]) != attempt_number:
            raise EvalContextError("Неожиданно изменился журнал попыток")
        for actual, expected in zip(manifest["attempts"], expected_manifest["attempts"]):
            if any(actual[field] != expected[field] for field in ATTEMPT_FIELDS):
                raise EvalContextError("Оркестратор изменил технический журнал попыток")
        attempt = manifest["attempts"][-1]
        now = datetime.now(timezone.utc) if now is None else now
        if return_code is not None and type(return_code) is not int:
            raise EvalContextError("Некорректный код завершения попытки")
        attempt["finished_at"] = _timestamp(now.isoformat()).astimezone(timezone.utc).isoformat()
        attempt["return_code"] = return_code
        _write_json(Path(path), manifest)
        return manifest


def build_agent_prompt(registry, path, manifest):
    attempt = manifest["attempts"][-1]
    context = {
        "mode": "noninteractive_eval",
        "run_type": attempt["run_type"],
        "page_id": manifest["page_id"],
        "run_id": manifest["run_id"],
        "attempt": attempt["number"],
        "registry_directory": registry.relative_path(path.parent),
        "manifest": registry.relative_path(path),
    }
    jira = manifest["projects"]["jira"]
    qase = manifest["projects"]["qase"]
    return (
        "Это неинтерактивный eval-запуск из eval_runner.py. "
        "При отсутствии/неполноте EVAL_CONTEXT или недоступном manifest "
        "остановись до Workflow: обычный Workflow запрещён.\n"
        f"EVAL_CONTEXT:\n{json.dumps(context, ensure_ascii=False, indent=2)}\n"
        f"Выполни флоу агента по требованиям Confluence pageId={manifest['page_id']}.\n"
        "Первое действие — прочитай переданный manifest и сверь его с EVAL_CONTEXT. "
        "Не восстанавливай режим/проекты из artifacts других прогонов. "
        "Используй правила eval из инструкции оркестратора только для этого запуска.\n"
        "После ревью любой неразрешённый конфликт требований или блокирующий вопрос "
        "останавливает весь Workflow с REQUIRES_HUMAN_DECISION. Верни точные вопросы "
        "человеку с ID и источниками; не запускай дизайнеров и паблишеров, не создавай "
        "частичные спецификации, Epic, suite или другие сущности даже для незатронутых "
        "требований. FINAL означает завершённость анализа, не разрешение продолжения. "
        "Запиши фактическую блокировку в manifest; не начатые Gates — NOT_CHECKED. "
        "Это правило действует и для fresh, и для resume.\n"
        f"Точный Summary Jira Epic: {json.dumps(manifest['containers']['jira_epic']['summary'], ensure_ascii=False)}.\n"
        f"Точный Title корневой suite Qase: {json.dumps(manifest['containers']['qase_root_suite']['title'], ensure_ascii=False)}.\n"
        "При fresh спецификации включают один Epic с дочерними задачами и одну "
        "корневую suite со всем поддеревом кейсов. Дедупликация ограничена текущими "
        "контейнерами: задачи и кейсы других прогонов не являются дублями. "
        "Если текущий контейнер ещё не создан, не ищи дубли его детей по всему проекту.\n"
        "При resume самостоятельно проверь реестр и найди последний fresh для этой "
        "страницы и этих проектов; переданный manifest должен совпасть с найденным. "
        "Не создавай новый fresh вместо resume.\n"
        "Даю разрешение на публикацию результатов для паблишеров в заданном "
        "неинтерактивном окружении согласно существующим правилам подтверждения. "
        "Публикуй только проверенные спецификации после Gates; зафиксируй их состав "
        "и количество сущностей. Это разрешение не является решением конфликта "
        "требований и не отменяет остановку на ревью. EVAL_CONTEXT не устанавливает "
        "результат Gates.\n"
        f"Jira: cloudId: {jira['cloud_id']}, projectKey: {jira['project_key']}, "
        f"projectId: {jira['project_id']}, "
        "issueTypeName: Задание, issueTypeId: 10003, unspecifiedPriority: omit "
        "(Jira default Medium/3), links: Blocks/10000, Relates/10003. "
        "Тип Epic, обязательные поля и способ назначения родителя выясни чтением.\n"
        f"Qase: projectCode: {qase['project_code']}, priority: HIGH=1, MEDIUM=2, LOW=3, "
        "type: omit, behavior: omit, modelTypeTag: model-type:<value>, customFields: none. "
        "Существующие shared steps можно переиспользовать при совпадении содержания; "
        "новый namespace для них не нужен.\n"
    )


def collect_evidence(registry, path, trace_path):
    """Передать judge только файлы текущего прогона, без поиска старых artifacts."""
    manifest = registry.load(path)
    evidence = {"manifest_path": registry.relative_path(path), "manifest": manifest}
    trace_path = Path(trace_path).resolve()
    expected_trace = (
        registry.project_path / "evals/results/runs" / manifest["run_id"]
        / f"attempt-{manifest['attempts'][-1]['number']}.jsonl"
    )
    if trace_path != expected_trace:
        raise EvalContextError("Трасса не относится к текущей попытке eval")
    evidence["trace_path"] = registry.relative_path(trace_path)
    evidence["files"] = {}
    evidence["missing"] = []
    if not trace_path.is_file():
        evidence["missing"].append("trace")
    evidence["attempts"] = []
    for attempt in manifest["attempts"]:
        history_trace = expected_trace.with_name(f"attempt-{attempt['number']}.jsonl")
        response = history_trace.with_suffix(".response.md")
        prompt = history_trace.with_suffix(".prompt.md")
        if any(candidate.resolve() != candidate for candidate in (history_trace, response, prompt)):
            raise EvalContextError("Доказательства попытки указывают вне текущего eval-прогона")
        evidence["attempts"].append({
            "number": attempt["number"],
            "run_type": attempt["run_type"],
            "trace_path": registry.relative_path(history_trace),
            "response_path": registry.relative_path(response),
        })
        if prompt.is_file():
            evidence["attempts"][-1]["prompt_path"] = registry.relative_path(prompt)
        if not history_trace.is_file():
            evidence["missing"].append(f"attempt-{attempt['number']}:trace")
        if not response.is_file():
            evidence["missing"].append(f"attempt-{attempt['number']}:response")
    for key in ARTIFACT_DIRECTORIES:
        mapping = manifest["reports"] if key.endswith("creation") else manifest["artifacts"]
        value = mapping.get(key)
        if value is None:
            evidence["missing"].append(key)
            continue
        candidate = registry.artifact_path(key, value)
        evidence["files"][key] = registry.relative_path(candidate)
        if not candidate.is_file():
            evidence["missing"].append(key)
            continue
        try:
            with candidate.open(encoding="utf-8") as file:
                header = file.read(8192)
        except (OSError, UnicodeError) as error:
            raise EvalContextError(f"Нельзя прочитать artifact {key}: {error}") from error
        for label, expected in (("Eval run", manifest["run_id"]), ("Source pageId", manifest["page_id"])):
            # Inline code не меняет ID; бэктики должны быть парными, совпадение — полным.
            value = re.escape(expected)
            marker = rf"(?m)^[ \t]*(?:[-*][ \t]+)?{re.escape(label)}:[ \t]*(?:{value}|`{value}`)[ \t]*\r?$"
            if not re.search(marker, header):
                raise EvalContextError(f"Файл {key} не подтверждает принадлежность текущему eval: {label}")
    return evidence


def validate_eval_handoff(evidence):
    """Полнота и непротиворечивость handoff; не назначает Gates и не доказывает публикацию."""
    manifest = evidence["manifest"]
    if manifest["workflow_status"] is None:
        raise EvalContextError(
            "Оркестратор не зарегистрировал итоговый eval handoff в manifest: "
            "eval-контекст не подтверждён. Код 0 и текст SUCCESS не позволяют "
            "оценивать обычный Workflow как fresh/resume."
        )
    if manifest["workflow_status"] != "SUCCESS":
        # Корректно зафиксированные блокировки/частичные результаты оцениваются
        # judge; отсутствие ещё не созданных контейнеров само по себе не ошибка.
        return
    missing = list(evidence["missing"])
    # Наличие всех PASSED необходимо для SUCCESS, но не заменяет независимых
    # проверок оркестратора. Runner лишь отклоняет противоречивый handoff.
    missing.extend(f"gate:{key}" for key, value in manifest["gates"].items() if value != "PASSED")
    for container, local_id, identifier in (
        ("jira_epic", "T-1", "key"), ("qase_root_suite", "S-1", "id"),
    ):
        value = manifest["containers"][container]
        if value["local_id"] != local_id or value[identifier] is None:
            missing.append(container)
    if missing:
        raise EvalContextError(
            "SUCCESS имеет неполный eval handoff: отсутствуют текущие доказательства, "
            "идентификаторы контейнеров или пройденные Gates: " + ", ".join(missing)
        )
