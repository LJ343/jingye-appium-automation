#!/usr/bin/env python3
# coding: utf-8
"""CLI for local Jingye fact import, dry-run, attachments, OSS and API sync."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import sqlite3
import sys
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Iterator

import requests

from jingye_store import (
    DEFAULT_BASE_DIR,
    DEFAULT_DRIVER_ROSTER_FILENAME,
    DEFAULT_SALES_LOADING_POINT_NAME,
    Candidate,
    atomic_write_bytes,
    build_attachment_artifact,
    canonical_json,
    connect_db,
    iter_candidates,
    import_driver_roster,
    now_text,
    refresh_facts,
    status_summary,
)


LOGGER = logging.getLogger("jingye-platform-sync")
DEFAULT_PROJECT_NAME = "敬业钢铁"
DEFAULT_TEST_PROJECT_NAME = "612演示项目"
DEFAULT_TEST_BASE_URL = "http://192.0.2.2:18100"
DEFAULT_TEST_DB_NAME = "jingye_facts.test.sqlite3"
DEFAULT_SOURCE_SYSTEM = "agent"
DEFAULT_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def load_env_file(path: Path = DEFAULT_ENV_FILE) -> Path | None:
    """Load a simple UTF-8 .env file without overwriting process variables."""
    if not path.is_file():
        return None
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise RuntimeError(f".env第{line_number}行缺少等号")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key[0].isdigit() or not key.replace("_", "").isalnum():
            raise RuntimeError(f".env第{line_number}行变量名无效")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return path


@contextlib.contextmanager
def single_instance_lock(path: Path) -> Iterator[None]:
    """Hold a one-byte advisory lock for all mutating commands."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        raise RuntimeError("已有敬业平台同步任务正在操作同一个事实库") from exc
    try:
        yield
    finally:
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def add_candidate_options(parser: argparse.ArgumentParser, *, include_limit: bool = True) -> None:
    parser.add_argument(
        "--source-system",
        default=os.getenv("JINGYE_SOURCE_SYSTEM", DEFAULT_SOURCE_SYSTEM),
        help="平台分配的来源编码，默认agent",
    )
    parser.add_argument(
        "--project-name",
        default=os.getenv("JINGYE_PROJECT_NAME", DEFAULT_PROJECT_NAME),
        help="启用项目的精确名称",
    )
    parser.add_argument(
        "--sales-loading-point-name",
        default=os.getenv("JINGYE_SALES_LOADING_POINT_NAME", DEFAULT_SALES_LOADING_POINT_NAME),
        help="销售运单统一装货点名称，默认河北敬业钢铁",
    )
    parser.add_argument(
        "--require-attachment",
        action="store_true",
        help="仅选择已经取得OSS URL的记录",
    )
    parser.add_argument("--direction", choices=("PURCHASE", "SALES"), default="")
    parser.add_argument(
        "--waybill",
        action="append",
        default=[],
        help="只选择指定运单号；可以重复提供",
    )
    if include_limit:
        parser.add_argument("--limit", type=int, default=0, help="最多处理多少条；0表示不限制")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="敬业运输本地事实库、磅单附件和运输平台安全同步"
    )
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_BASE_DIR)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--profile",
        choices=("production", "test"),
        default="production",
        help="production使用正式主库；test使用独立测试库和测试平台",
    )
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("refresh", help="重新扫描历史Excel/PDF并更新事实库")
    subparsers.add_parser("status", help="显示本地事实库和上传状态")

    mark_unuploaded = subparsers.add_parser(
        "mark-unuploaded",
        help="确认平台记录及幂等数据已删除后，将指定成功或幂等冲突记录恢复为本地未上传状态",
    )
    mark_unuploaded.add_argument(
        "--waybill",
        action="append",
        required=True,
        help="平台已删除的运单号；可以重复提供",
    )
    mark_unuploaded.add_argument(
        "--confirm-remote-deleted",
        action="store_true",
        help="确认指定记录已经从当前 profile 对应的平台删除",
    )

    drivers_import = subparsers.add_parser(
        "drivers-import", help="导入上传专用的车牌、司机姓名和手机号主数据"
    )
    drivers_import.add_argument(
        "--file", type=Path, default=None, help="CSV路径；默认使用platform_sync/driver_roster.csv"
    )

    dry_run = subparsers.add_parser("dry-run", help="生成并校验候选请求，不冻结、不联网")
    add_candidate_options(dry_run)
    dry_run.add_argument("--output", type=Path, default=None, help="可选的完整JSON报告路径")

    attachments = subparsers.add_parser(
        "attachments", help="把逻辑磅单的一个或多个PDF合成单个PDF或长图"
    )
    attachments.add_argument("--format", choices=("jpg", "pdf"), default="jpg")
    attachments.add_argument("--output-dir", type=Path, default=None)
    attachments.add_argument("--direction", choices=("PURCHASE", "SALES"), default="")
    attachments.add_argument("--waybill", action="append", default=[])
    attachments.add_argument("--multi-only", action="store_true")
    attachments.add_argument("--limit", type=int, default=0)
    attachments.add_argument("--force", action="store_true")

    oss_upload = subparsers.add_parser(
        "oss-upload", help="上传已经生成的本地附件；必须显式--execute"
    )
    oss_upload.add_argument("--execute", action="store_true")
    oss_upload.add_argument("--direction", choices=("PURCHASE", "SALES"), default="")
    oss_upload.add_argument("--waybill", action="append", default=[])
    oss_upload.add_argument("--limit", type=int, default=0)

    prepare = subparsers.add_parser(
        "prepare", help="把候选请求冻结到SQLite outbox，但不发送HTTP"
    )
    add_candidate_options(prepare)
    prepare.add_argument(
        "--replace-rejected-route",
        action="store_true",
        help="仅修订并归档此前因ROUTE_NOT_FOUND拒绝的旧版冻结请求",
    )

    upload = subparsers.add_parser(
        "upload", help="发送冻结请求；必须显式--execute和确认项目名"
    )
    add_candidate_options(upload)
    upload.add_argument("--execute", action="store_true")
    upload.add_argument(
        "--replace-rejected-route",
        action="store_true",
        help="仅修订并归档此前因ROUTE_NOT_FOUND拒绝的旧版冻结请求",
    )
    upload.add_argument("--confirm-project", default="")
    upload.add_argument(
        "--base-url",
        default=os.getenv("JINGYE_PLATFORM_BASE_URL", ""),
        help="部署环境基础地址，不含API路径",
    )
    upload.add_argument("--api-key-env", default="JINGYE_EXTERNAL_API_KEY")
    upload.add_argument("--allow-http", action="store_true", help="仅局域网测试环境允许HTTP")
    upload.add_argument("--timeout", type=float, default=30.0)
    upload.add_argument(
        "--allow-batch",
        action="store_true",
        help="允许未显式指定运单且limit超过2；正式批量上线前才使用",
    )

    query = subparsers.add_parser("query", help="只读查询一个已经冻结的远端请求")
    query.add_argument("--request-id", required=True)
    query.add_argument(
        "--base-url", default=os.getenv("JINGYE_PLATFORM_BASE_URL", "")
    )
    query.add_argument("--source-system", default=os.getenv("JINGYE_SOURCE_SYSTEM", DEFAULT_SOURCE_SYSTEM))
    query.add_argument("--api-key-env", default="JINGYE_EXTERNAL_API_KEY")
    query.add_argument("--allow-http", action="store_true")
    query.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    if args.profile == "test":
        if hasattr(args, "project_name"):
            args.project_name = os.getenv(
                "JINGYE_TEST_PROJECT_NAME", DEFAULT_TEST_PROJECT_NAME
            )
        if hasattr(args, "base_url"):
            args.base_url = os.getenv(
                "JINGYE_TEST_PLATFORM_BASE_URL", DEFAULT_TEST_BASE_URL
            )
    return args


def effective_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    base_dir = args.base_dir.resolve()
    runtime_dir = base_dir / "platform_sync"
    if args.db:
        db_path = args.db.resolve()
    elif args.profile == "test":
        db_path = runtime_dir / os.getenv(
            "JINGYE_TEST_DB_NAME", DEFAULT_TEST_DB_NAME
        )
    else:
        db_path = runtime_dir / "jingye_facts.sqlite3"
    return base_dir, runtime_dir, db_path


def candidate_rows(connection: sqlite3.Connection, args: argparse.Namespace) -> list[Candidate]:
    candidates = list(
        iter_candidates(
            connection,
            args.source_system,
            args.project_name,
            sales_loading_point_name=args.sales_loading_point_name,
            require_attachment=args.require_attachment,
            direction=args.direction,
            waybill_numbers=args.waybill,
        )
    )
    if getattr(args, "limit", 0) > 0:
        accepted: list[Candidate] = []
        rejected: list[Candidate] = []
        for item in candidates:
            if item.payload is not None and len(accepted) < args.limit:
                accepted.append(item)
            elif item.payload is None:
                rejected.append(item)
        candidates = accepted + rejected
    return candidates


def dry_run_report(connection: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    candidates = candidate_rows(connection, args)
    accepted = [item for item in candidates if item.payload is not None]
    rejected = [item for item in candidates if item.payload is None]
    directions = Counter(item.direction for item in accepted)
    loading_points = [str(item.payload.get("loadingPointName") or "") for item in accepted]
    unloading_points = [str(item.payload.get("unloadingPointName") or "") for item in accepted]
    reasons = Counter(item.rejection_reason for item in rejected)
    payload_hashes = [
        hashlib.sha256(canonical_json(item.payload).encode("utf-8")).hexdigest()
        for item in accepted
    ]
    submissions_by_waybill = {
        int(row["waybill_id"]): row
        for row in connection.execute(
            "SELECT waybill_id, request_id, payload_json, payload_sha256, status "
            "FROM submissions"
        )
    }
    accepted_without_submission = sum(
        item.waybill_id not in submissions_by_waybill for item in accepted
    )
    accepted_successful = [
        item
        for item in accepted
        if item.waybill_id in submissions_by_waybill
        and submissions_by_waybill[item.waybill_id]["status"] == "SUCCESS"
    ]
    accepted_success_payload_drift = sum(
        bool(
            successful_submission_changed_fields(
                submissions_by_waybill[item.waybill_id], item.payload
            )
        )
        for item in accepted_successful
    )
    return {
        "generated_at": now_text(),
        "dry_run": True,
        "external_writes": 0,
        "summary": {
            "selected": len(candidates),
            "accepted": len(accepted),
            "rejected": len(rejected),
            "accepted_without_submission": accepted_without_submission,
            "accepted_successful": len(accepted_successful),
            "accepted_success_payload_drift": accepted_success_payload_drift,
            "directions": dict(sorted(directions.items())),
            "loading_point_snapshots": sum(bool(value) for value in loading_points),
            "unloading_point_snapshots": sum(bool(value) for value in unloading_points),
            "distinct_loading_points": len({value for value in loading_points if value}),
            "distinct_unloading_points": len({value for value in unloading_points if value}),
            "route_codes_sent": sum("routeCode" in item.payload for item in accepted),
            "driver_snapshots": sum(
                bool(item.payload.get("driverName") and item.payload.get("driverPhone"))
                for item in accepted
            ),
            "rejection_reasons": dict(sorted(reasons.items())),
            "unique_payload_hashes": len(set(payload_hashes)),
        },
        "payloads": [item.payload for item in accepted],
        "rejections": [
            {
                "direction": item.direction,
                "waybillNo": item.waybill_no,
                "reason": item.rejection_reason,
            }
            for item in rejected
        ],
    }


def route_rejection_upgrade_is_safe(
    current: sqlite3.Row, new_payload: dict[str, Any]
) -> bool:
    if current["status"] != "REJECTED":
        return False
    if not str(current["response_message"] or "").startswith("ROUTE_NOT_FOUND"):
        return False
    if current["platform_waybill_id"]:
        return False
    old_payload = json.loads(current["payload_json"])
    if not old_payload.pop("routeCode", None):
        return False
    if "routeCode" in new_payload:
        return False
    for field in ("loadingPointName", "unloadingPointName"):
        if field in new_payload:
            old_payload[field] = new_payload[field]
    return canonical_json(old_payload) == canonical_json(new_payload)


def successful_submission_changed_fields(
    current: sqlite3.Row, candidate_payload: dict[str, Any]
) -> list[str]:
    """Return top-level fields that drifted from an immutable successful payload."""
    candidate_payload_json = canonical_json(candidate_payload)
    if (
        current["payload_sha256"]
        == hashlib.sha256(candidate_payload_json.encode("utf-8")).hexdigest()
        and current["payload_json"] == candidate_payload_json
    ):
        return []
    try:
        frozen_payload = json.loads(current["payload_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return ["<invalid-frozen-payload>"]
    if not isinstance(frozen_payload, dict):
        return ["<invalid-frozen-payload>"]
    missing = object()
    return sorted(
        str(field)
        for field in set(frozen_payload) | set(candidate_payload)
        if frozen_payload.get(field, missing) != candidate_payload.get(field, missing)
    ) or ["<serialization>"]


def warn_successful_submission_drift(
    current: sqlite3.Row,
    candidate_payload: dict[str, Any],
    *,
    waybill_no: str,
) -> list[str]:
    changed_fields = successful_submission_changed_fields(current, candidate_payload)
    if changed_fields:
        LOGGER.warning(
            "平台成功记录保持冻结并跳过：waybill=%s frozenRequestId=%s "
            "candidateRequestId=%s changedFields=%s",
            waybill_no,
            current["request_id"],
            candidate_payload.get("requestId"),
            ",".join(changed_fields),
        )
    return changed_fields


def prepare_submission(
    connection: sqlite3.Connection,
    candidate: Candidate,
    *,
    replace_rejected_route: bool = False,
) -> sqlite3.Row:
    if candidate.payload is None:
        raise ValueError(candidate.rejection_reason or "候选请求无效")
    payload_json = canonical_json(candidate.payload)
    digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    request_id = str(candidate.payload["requestId"])
    ticket = candidate.payload.get("loadingTicket") or candidate.payload.get("unloadingTicket")
    current = connection.execute(
        "SELECT * FROM submissions WHERE request_id=? OR waybill_id=?",
        (request_id, candidate.waybill_id),
    ).fetchone()
    if current is not None:
        if current["status"] == "SUCCESS":
            warn_successful_submission_drift(
                current,
                candidate.payload,
                waybill_no=candidate.waybill_no,
            )
            return current
        if current["request_id"] != request_id:
            raise RuntimeError("同一运单已用不同requestId冻结，禁止改号")
        if current["status"] == "UNUPLOADED":
            connection.execute(
                """
                UPDATE submissions SET
                    source_system=?, external_waybill_no=?, external_ticket_no=?,
                    payload_json=?, payload_sha256=?, status='PREPARED', attempts=0,
                    http_status=NULL, response_code=NULL, response_message=NULL,
                    response_json=NULL, platform_waybill_id=NULL,
                    last_attempt_at=NULL, success_at=NULL
                WHERE request_id=?
                """,
                (
                    candidate.payload["sourceSystem"],
                    candidate.payload["externalWaybillNo"],
                    ticket["externalTicketNo"],
                    payload_json,
                    digest,
                    request_id,
                ),
            )
            connection.commit()
            return connection.execute(
                "SELECT * FROM submissions WHERE request_id=?", (request_id,)
            ).fetchone()
        if current["payload_sha256"] != digest or current["payload_json"] != payload_json:
            if not replace_rejected_route or not route_rejection_upgrade_is_safe(
                current, candidate.payload
            ):
                raise RuntimeError("同一requestId已冻结不同请求内容，禁止覆盖")
            revision_no = int(
                connection.execute(
                    "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM submission_revisions WHERE request_id=?",
                    (request_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO submission_revisions(
                    request_id, waybill_id, revision_no, payload_json, payload_sha256,
                    status, http_status, response_code, response_message, response_json,
                    archived_at, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    current["waybill_id"],
                    revision_no,
                    current["payload_json"],
                    current["payload_sha256"],
                    current["status"],
                    current["http_status"],
                    current["response_code"],
                    current["response_message"],
                    current["response_json"],
                    now_text(),
                    "API_V3_REMOVE_ROUTE_ADD_POINT_NAMES",
                ),
            )
            connection.execute(
                """
                UPDATE submissions SET
                    payload_json=?, payload_sha256=?, status='PREPARED',
                    http_status=NULL, response_code=NULL, response_message=NULL,
                    response_json=NULL, platform_waybill_id=NULL, success_at=NULL
                WHERE request_id=?
                """,
                (payload_json, digest, request_id),
            )
            connection.commit()
            return connection.execute(
                "SELECT * FROM submissions WHERE request_id=?", (request_id,)
            ).fetchone()
        return current
    connection.execute(
        """
        INSERT INTO submissions(
            request_id, waybill_id, source_system, external_waybill_no,
            external_ticket_no, payload_json, payload_sha256,
            status, first_prepared_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PREPARED', ?)
        """,
        (
            request_id,
            candidate.waybill_id,
            candidate.payload["sourceSystem"],
            candidate.payload["externalWaybillNo"],
            ticket["externalTicketNo"],
            payload_json,
            digest,
            now_text(),
        ),
    )
    connection.commit()
    return connection.execute(
        "SELECT * FROM submissions WHERE request_id=?", (request_id,)
    ).fetchone()


def mark_submissions_unuploaded(
    connection: sqlite3.Connection, args: argparse.Namespace
) -> dict[str, Any]:
    if not args.confirm_remote_deleted:
        raise RuntimeError("必须显式提供--confirm-remote-deleted确认平台记录已删除")

    wanted = list(dict.fromkeys(str(value or "").strip().upper() for value in args.waybill))
    if not wanted or any(not value for value in wanted):
        raise RuntimeError("必须提供非空运单号")

    selected: list[sqlite3.Row] = []
    already_unuploaded: list[str] = []
    for waybill_no in wanted:
        rows = connection.execute(
            """
            SELECT s.*
            FROM submissions s
            JOIN waybills w ON w.id=s.waybill_id
            WHERE w.waybill_no=?
            """,
            (waybill_no,),
        ).fetchall()
        if not rows:
            raise RuntimeError(f"运单{waybill_no}没有本地冻结或上传记录")
        if len(rows) != 1:
            raise RuntimeError(f"运单{waybill_no}匹配到多条本地提交记录，禁止自动重置")
        current = rows[0]
        if current["status"] == "UNUPLOADED":
            already_unuploaded.append(waybill_no)
            continue
        resettable_conflict = (
            current["status"] == "REJECTED"
            and str(current["response_message"] or "").startswith("DUPLICATE_CONFLICT")
        )
        if current["status"] != "SUCCESS" and not resettable_conflict:
            raise RuntimeError(
                f"运单{waybill_no}当前状态为{current['status']}，"
                "不是可重置的SUCCESS或DUPLICATE_CONFLICT"
            )
        selected.append(current)

    reset_rows: list[dict[str, str]] = []
    with connection:
        for current in selected:
            revision_no = int(
                connection.execute(
                    "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM submission_revisions WHERE request_id=?",
                    (current["request_id"],),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO submission_revisions(
                    request_id, waybill_id, revision_no, payload_json, payload_sha256,
                    status, http_status, response_code, response_message, response_json,
                    archived_at, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    current["request_id"],
                    current["waybill_id"],
                    revision_no,
                    current["payload_json"],
                    current["payload_sha256"],
                    current["status"],
                    current["http_status"],
                    current["response_code"],
                    current["response_message"],
                    current["response_json"],
                    now_text(),
                    (
                        "REMOTE_IDEMPOTENCY_CONFIRMED_PURGED"
                        if current["status"] == "REJECTED"
                        else "REMOTE_RECORD_CONFIRMED_DELETED"
                    ),
                ),
            )
            connection.execute(
                """
                UPDATE submissions SET
                    status='UNUPLOADED', attempts=0,
                    http_status=NULL, response_code=NULL, response_message=NULL,
                    response_json=NULL, platform_waybill_id=NULL,
                    last_attempt_at=NULL, success_at=NULL
                WHERE request_id=?
                """,
                (current["request_id"],),
            )
            reset_rows.append(
                {
                    "waybillNo": current["external_waybill_no"],
                    "requestId": current["request_id"],
                    "state": "UNUPLOADED",
                }
            )
    return {
        "external_writes": 0,
        "stats": {
            "reset_to_unuploaded": len(reset_rows),
            "already_unuploaded": len(already_unuploaded),
        },
        "results": reset_rows,
    }


def prepare_candidates(connection: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    rows = candidate_rows(connection, args)
    stats = Counter()
    errors: list[dict[str, str]] = []
    for candidate in rows:
        successful_submission = connection.execute(
            'SELECT * FROM submissions WHERE waybill_id=? AND status=\'SUCCESS\'',
            (candidate.waybill_id,),
        ).fetchone()
        if successful_submission is not None:
            if candidate.payload is not None and warn_successful_submission_drift(
                successful_submission,
                candidate.payload,
                waybill_no=candidate.waybill_no,
            ):
                stats['successful_payload_drift'] += 1
            stats['already_successful'] += 1
            continue
        if candidate.payload is None:
            stats["rejected_locally"] += 1
            errors.append({"waybillNo": candidate.waybill_no, "error": candidate.rejection_reason})
            continue
        try:
            previous_hash = connection.execute(
                "SELECT payload_sha256, status FROM submissions WHERE request_id=?",
                (candidate.payload["requestId"],),
            ).fetchone()
            prepared = prepare_submission(
                connection,
                candidate,
                replace_rejected_route=args.replace_rejected_route,
            )
            if previous_hash is None:
                stats["newly_frozen"] += 1
            elif previous_hash["status"] == "UNUPLOADED":
                stats["reprepared_unuploaded"] += 1
            elif previous_hash["payload_sha256"] != prepared["payload_sha256"]:
                stats["revised_rejected_route"] += 1
            else:
                stats["already_frozen"] += 1
        except Exception as exc:
            stats["freeze_conflicts"] += 1
            errors.append({"waybillNo": candidate.waybill_no, "error": str(exc)})
    return {"stats": dict(stats), "errors": errors}


def selected_attachment_rows(connection: sqlite3.Connection, args: argparse.Namespace) -> list[sqlite3.Row]:
    clauses = ["lt.validation_status='VALID'"]
    parameters: list[Any] = []
    if args.direction:
        clauses.append("w.direction=?")
        parameters.append(args.direction)
    if args.waybill:
        placeholders = ",".join("?" for _ in args.waybill)
        clauses.append(f"w.waybill_no IN ({placeholders})")
        parameters.extend(str(value).strip().upper() for value in args.waybill)
    if getattr(args, "multi_only", False):
        clauses.append("lt.method='CHAIN_AGGREGATE'")
    rows = connection.execute(
        f"""
        SELECT w.direction, w.waybill_no, lt.*
        FROM logical_tickets lt JOIN waybills w ON w.id=lt.waybill_id
        WHERE {' AND '.join(clauses)}
        ORDER BY w.direction, w.waybill_no
        """,
        tuple(parameters),
    ).fetchall()
    if args.limit > 0:
        rows = rows[: args.limit]
    return rows


def build_attachments(
    connection: sqlite3.Connection, runtime_dir: Path, args: argparse.Namespace
) -> dict[str, Any]:
    output_dir = args.output_dir.resolve() if args.output_dir else runtime_dir / "attachments"
    stats = Counter()
    artifacts: list[dict[str, Any]] = []
    for row in selected_attachment_rows(connection, args):
        try:
            result = build_attachment_artifact(
                connection,
                int(row["waybill_id"]),
                output_dir,
                args.format,
                force=args.force,
            )
            stats["cached" if result["cached"] else "created"] += 1
            stats["pages"] += int(result["pages"] or 0)
            stats["bytes"] += int(result["bytes"] or 0)
            artifacts.append(
                {
                    "direction": row["direction"],
                    "waybillNo": row["waybill_no"],
                    **result,
                }
            )
        except Exception as exc:
            stats["failed"] += 1
            artifacts.append(
                {
                    "direction": row["direction"],
                    "waybillNo": row["waybill_no"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {"stats": dict(stats), "artifacts": artifacts}


def require_env(name: str) -> str:
    value = str(os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量{name}")
    return value


def upload_oss(connection: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    if not args.execute:
        raise RuntimeError("oss-upload不会默认执行；确认外部写入后必须显式提供--execute")
    try:
        import oss2
    except ImportError as exc:
        raise RuntimeError("缺少oss2依赖；请先安装oss2") from exc

    access_key_id = require_env("OSS_ACCESS_KEY_ID")
    access_key_secret = require_env("OSS_ACCESS_KEY_SECRET")
    endpoint = require_env("OSS_ENDPOINT")
    bucket_name = require_env("OSS_BUCKET_NAME")
    url_prefix = require_env("OSS_URL_PREFIX").rstrip("/")
    object_prefix = str(
        os.getenv("OSS_OBJECT_PREFIX") or os.getenv("OSS_FILE_HOST") or ""
    ).strip("/")
    auth = oss2.Auth(access_key_id, access_key_secret)
    bucket = oss2.Bucket(auth, endpoint, bucket_name)

    clauses = [
        "lt.validation_status='VALID'",
        "lt.artifact_path IS NOT NULL",
        "lt.oss_url IS NULL",
    ]
    parameters: list[Any] = []
    if args.direction:
        clauses.append("w.direction=?")
        parameters.append(args.direction)
    if args.waybill:
        placeholders = ",".join("?" for _ in args.waybill)
        clauses.append(f"w.waybill_no IN ({placeholders})")
        parameters.extend(str(value).strip().upper() for value in args.waybill)
    rows = connection.execute(
        f"""
        SELECT w.direction, w.waybill_no, lt.*
        FROM logical_tickets lt JOIN waybills w ON w.id=lt.waybill_id
        WHERE {' AND '.join(clauses)}
        ORDER BY w.direction, w.waybill_no
        """,
        tuple(parameters),
    ).fetchall()
    if args.limit > 0:
        rows = rows[: args.limit]
    stats = Counter()
    results: list[dict[str, str]] = []
    for row in rows:
        path = Path(row["artifact_path"])
        extension = path.suffix.lower().lstrip(".") or "bin"
        object_name = f"jingye/{row['artifact_sha256']}.{extension}"
        object_key = f"{object_prefix}/{object_name}" if object_prefix else object_name
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        try:
            if not bucket.object_exists(object_key):
                response = bucket.put_object_from_file(
                    object_key, str(path), headers={"Content-Type": content_type}
                )
                if not 200 <= int(response.status) < 300:
                    raise RuntimeError(f"OSS HTTP状态{response.status}")
                stats["uploaded"] += 1
            else:
                stats["already_exists"] += 1
            url = url_prefix + "/" + urllib.parse.quote(object_key, safe="/")
            connection.execute(
                "UPDATE logical_tickets SET oss_url=?, updated_at=? WHERE waybill_id=?",
                (url, now_text(), row["waybill_id"]),
            )
            connection.commit()
            results.append({"waybillNo": row["waybill_no"], "url": url})
        except Exception as exc:
            stats["failed"] += 1
            results.append({"waybillNo": row["waybill_no"], "error": str(exc)})
    return {"stats": dict(stats), "results": results}


def validate_remote_options(
    base_url: str, allow_http: bool, api_key_env: str
) -> tuple[str, str]:
    base_url = str(base_url or "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("缺少--base-url或JINGYE_PLATFORM_BASE_URL")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError("base-url不是有效HTTP(S)地址")
    if parsed.scheme != "https" and not allow_http:
        raise RuntimeError("非HTTPS地址必须显式提供--allow-http，且只允许局域网测试环境")
    return base_url, require_env(api_key_env)


def response_details(response: requests.Response) -> tuple[Any, str, str, dict[str, Any]]:
    try:
        value = response.json()
        code = value.get("code") if isinstance(value, dict) else None
        message = str(value.get("message") or "") if isinstance(value, dict) else ""
        data = value.get("data") if isinstance(value, dict) and isinstance(value.get("data"), dict) else {}
        return code, message, canonical_json(value), data
    except Exception:
        body = response.text[:2000]
        return None, body, body, {}


def record_attempt(
    connection: sqlite3.Connection,
    request_id: str,
    method: str,
    state: str,
    http_status: int | None,
    response_code: Any,
    message: str,
    response_json: str,
    *,
    platform_waybill_id: Any = None,
    update_submission: bool = True,
) -> None:
    connection.execute(
        """
        INSERT INTO upload_attempts(
            request_id, attempted_at, method, http_status,
            response_code, state, response_message, response_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            now_text(),
            method,
            http_status,
            response_code if isinstance(response_code, int) else None,
            state,
            message[:4000],
            response_json[:20000],
        ),
    )
    if update_submission:
        connection.execute(
            """
            UPDATE submissions SET
                status=?, attempts=attempts + CASE WHEN ?='POST' THEN 1 ELSE 0 END,
                http_status=?, response_code=?, response_message=?, response_json=?,
                platform_waybill_id=COALESCE(?, platform_waybill_id),
                last_attempt_at=?, success_at=CASE WHEN ?='SUCCESS' THEN ? ELSE success_at END
            WHERE request_id=?
            """,
            (
                state,
                method,
                http_status,
                response_code if isinstance(response_code, int) else None,
                message[:4000],
                response_json[:20000],
                str(platform_waybill_id) if platform_waybill_id is not None else None,
                now_text(),
                state,
                now_text(),
                request_id,
            ),
        )
    connection.commit()


def remote_get(
    session: requests.Session,
    base_url: str,
    request_id: str,
    source_system: str,
    timeout: float,
) -> requests.Response:
    encoded_id = urllib.parse.quote(request_id, safe="")
    return session.get(
        f"{base_url}/api/open/v1/transport-records/{encoded_id}",
        params={"sourceSystem": source_system},
        timeout=(5.0, timeout),
    )


def query_one(connection: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    base_url, api_key = validate_remote_options(args.base_url, args.allow_http, args.api_key_env)
    session = requests.Session()
    session.headers.update({"X-Api-Key": api_key, "User-Agent": "qclaw-jingye-sync/1.0"})
    response = remote_get(
        session, base_url, args.request_id, args.source_system, args.timeout
    )
    code, message, response_json, data = response_details(response)
    row = connection.execute(
        "SELECT request_id, status FROM submissions WHERE request_id=?", (args.request_id,)
    ).fetchone()
    if row is not None:
        remote_found = 200 <= response.status_code < 300 and code == 0
        preserve_rejected = row["status"] == "REJECTED"
        if remote_found and preserve_rejected:
            state = "QUERY_FOUND_REJECTED"
        else:
            state = "SUCCESS" if remote_found else "QUERY_FAILED"
        record_attempt(
            connection,
            args.request_id,
            "GET",
            state,
            response.status_code,
            code,
            message,
            response_json,
            platform_waybill_id=data.get("waybillId"),
            update_submission=not preserve_rejected,
        )
    return {
        "requestId": args.request_id,
        "httpStatus": response.status_code,
        "code": code,
        "message": message,
        "data": data,
    }


def upload_candidates(connection: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any]:
    if not args.execute:
        raise RuntimeError("upload不会默认发送；必须显式提供--execute")
    if args.confirm_project != args.project_name:
        raise RuntimeError("--confirm-project必须与--project-name完全一致")
    if not args.waybill and not args.allow_batch:
        if args.limit <= 0 or args.limit > 2:
            raise RuntimeError("测试上传必须指定--waybill，或设置1到2的--limit；批量需显式--allow-batch")
    base_url, api_key = validate_remote_options(args.base_url, args.allow_http, args.api_key_env)
    candidates = candidate_rows(connection, args)
    valid_candidates = [item for item in candidates if item.payload is not None]
    if not valid_candidates:
        raise RuntimeError("没有满足条件的可上传候选请求")

    session = requests.Session()
    session.headers.update(
        {
            "Content-Type": "application/json; charset=utf-8",
            "X-Api-Key": api_key,
            "User-Agent": "qclaw-jingye-sync/1.0",
        }
    )
    endpoint = f"{base_url}/api/open/v1/transport-records"
    stats = Counter()
    results: list[dict[str, Any]] = []
    for candidate in valid_candidates:
        row = prepare_submission(
            connection,
            candidate,
            replace_rejected_route=args.replace_rejected_route,
        )
        request_id = row["request_id"]
        if row["status"] == "SUCCESS":
            stats["skipped_success"] += 1
            changed_fields = successful_submission_changed_fields(row, candidate.payload)
            if changed_fields:
                stats["success_payload_drift"] += 1
                results.append(
                    {
                        "requestId": request_id,
                        "state": "SKIPPED_SUCCESS_PAYLOAD_DRIFT",
                        "changedFields": changed_fields,
                    }
                )
            else:
                results.append({"requestId": request_id, "state": "SKIPPED_SUCCESS"})
            continue
        if row["status"] == "REJECTED":
            stats["skipped_rejected"] += 1
            results.append({"requestId": request_id, "state": "SKIPPED_REJECTED"})
            continue

        if row["attempts"] > 0 or row["status"] in {"RETRY", "UNKNOWN"}:
            try:
                query_response = remote_get(
                    session,
                    base_url,
                    request_id,
                    row["source_system"],
                    args.timeout,
                )
                query_code, query_message, query_json, query_data = response_details(query_response)
                if 200 <= query_response.status_code < 300 and query_code == 0:
                    record_attempt(
                        connection,
                        request_id,
                        "GET",
                        "SUCCESS",
                        query_response.status_code,
                        query_code,
                        query_message,
                        query_json,
                        platform_waybill_id=query_data.get("waybillId"),
                    )
                    stats["recovered_by_query"] += 1
                    results.append({"requestId": request_id, "state": "SUCCESS_BY_QUERY"})
                    continue
                if query_response.status_code in {401, 403}:
                    record_attempt(
                        connection,
                        request_id,
                        "GET",
                        "RETRY",
                        query_response.status_code,
                        query_code,
                        query_message,
                        query_json,
                    )
                    stats["auth_failure"] += 1
                    results.append({"requestId": request_id, "state": "AUTH_FAILURE"})
                    break
            except requests.RequestException as exc:
                LOGGER.warning("重试前查询失败，将继续复用冻结请求体：%s", type(exc).__name__)

        try:
            response = session.post(
                endpoint,
                data=str(row["payload_json"]).encode("utf-8"),
                timeout=(5.0, args.timeout),
            )
            code, message, response_json, data = response_details(response)
            if 200 <= response.status_code < 300 and code == 0:
                state = "SUCCESS"
                stats["success"] += 1
            elif response.status_code >= 500 or response.status_code in {408, 429}:
                state = "RETRY"
                stats["retryable"] += 1
            else:
                state = "REJECTED"
                stats["rejected"] += 1
            record_attempt(
                connection,
                request_id,
                "POST",
                state,
                response.status_code,
                code,
                message,
                response_json,
                platform_waybill_id=data.get("waybillId"),
            )
            results.append(
                {
                    "requestId": request_id,
                    "state": state,
                    "httpStatus": response.status_code,
                    "code": code,
                    "message": message,
                }
            )
            if response.status_code in {401, 403}:
                break
        except requests.RequestException as exc:
            message = f"{type(exc).__name__}: {exc}"
            record_attempt(
                connection,
                request_id,
                "POST",
                "UNKNOWN",
                None,
                None,
                message,
                "",
            )
            stats["unknown"] += 1
            results.append({"requestId": request_id, "state": "UNKNOWN", "message": message})
    return {"stats": dict(stats), "results": results}


def main() -> int:
    load_env_file()
    args = parse_args()
    configure_logging(args.verbose)
    base_dir, runtime_dir, db_path = effective_paths(args)
    lock_path = runtime_dir / "jingye_platform_sync.lock"
    mutating = args.command in {
        "refresh",
        "drivers-import",
        "mark-unuploaded",
        "attachments",
        "oss-upload",
        "prepare",
        "upload",
        "query",
    }
    lock_context = single_instance_lock(lock_path) if mutating else contextlib.nullcontext()
    with lock_context:
        connection = connect_db(db_path)
        try:
            if args.command == "refresh":
                print_json(
                    refresh_facts(
                        connection,
                        base_dir,
                        project_name=(
                            os.getenv(
                                "JINGYE_TEST_PROJECT_NAME",
                                DEFAULT_TEST_PROJECT_NAME,
                            )
                            if args.profile == "test"
                            else os.getenv(
                                "JINGYE_PROJECT_NAME", DEFAULT_PROJECT_NAME
                            )
                        ),
                        sales_loading_point_name=os.getenv(
                            "JINGYE_SALES_LOADING_POINT_NAME",
                            DEFAULT_SALES_LOADING_POINT_NAME,
                        ),
                    )
                )
            elif args.command == "drivers-import":
                roster_path = (
                    args.file.resolve()
                    if args.file
                    else runtime_dir / DEFAULT_DRIVER_ROSTER_FILENAME
                )
                print_json(import_driver_roster(connection, roster_path))
            elif args.command == "status":
                print_json(status_summary(connection))
            elif args.command == "mark-unuploaded":
                print_json(mark_submissions_unuploaded(connection, args))
            elif args.command == "dry-run":
                report = dry_run_report(connection, args)
                if args.output:
                    atomic_write_bytes(
                        args.output.resolve(),
                        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
                    )
                    report["output"] = str(args.output.resolve())
                print_json(report["summary"] | {"external_writes": 0, **({"output": report["output"]} if "output" in report else {})})
            elif args.command == "attachments":
                print_json(build_attachments(connection, runtime_dir, args))
            elif args.command == "oss-upload":
                print_json(upload_oss(connection, args))
            elif args.command == "prepare":
                print_json(prepare_candidates(connection, args))
            elif args.command == "upload":
                print_json(upload_candidates(connection, args))
            elif args.command == "query":
                print_json(query_one(connection, args))
            else:
                raise AssertionError(f"未知命令：{args.command}")
        finally:
            connection.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        LOGGER.error("%s: %s", type(exc).__name__, exc)
        raise SystemExit(1)
