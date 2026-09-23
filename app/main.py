"""交叉实验顺序平衡 API。

使用 Williams 平衡拉丁方为受试者分配条件顺序，保证：
* 每个条件在每个位置上出现次数相等；
* 每个有向相邻条件对出现次数相等。

完整条件序列仅保存在服务端，通过推进接口按周期受控揭盲：
创建与详情接口只返回受试者名册，不暴露任何后续周期安排。
创建实验时另为每个稳定分配槽位 / 周期生成 128 位 nonce 与 SHA-256 盲态承诺；
承诺列表不提前泄露条件或 nonce，成功推进时才返回对应 nonce 供审计核验。
另提供双人审批（15 分钟授权窗口）的实验级紧急揭盲通道，
审批与最终揭盲在单个 SQLite 写事务内完成，审计记录不含条件内容。
另提供脱敏结果快照：在单个一致性读事务内固定同一时点，按 subject_id
稳定排序生成仅含已揭盲周期与完成时间的不可变 JSON 快照（附 SHA-256
摘要），相同 operation_id 幂等重放同一快照，可按快照 ID 随时取回。
另提供受试者换号：仅允许原受试者从未揭盲时，在单个写事务内把隐藏分配
（序列内容与下标不变）与进度原子转移到新编号，原编号保留墓碑不可复用、
不可查询或推进，并追加不含条件内容的换号审计（按时间倒序分页查询）。
换号不暗改入组 reservation：修改隐藏分配前先以 subject_id 为全局键校验
原 / 新编号均未被任何有效 reservation 占用，任一被占即 409 且零写入，
须先释放相关入组锁再换号。
另提供实验模板克隆：POST /experiments/{id}/clone 基于源实验条件创建独立
新实验，接收新的互异 subject_ids、seed 与可选 washout_seconds；克隆会
重新生成 Williams 分配、稳定槽位与每周期盲态承诺，绝不复用源实验的
nonce/commitment，也不复制任何揭盲 / 完成 / 紧急揭盲进度与状态。
另提供受试者入组互斥：以 subject_id 为全局键（跨实验）登记 experiment
enrollment lease，POST /enrollment-locks 在单个事务内批量申请 1～100 个
受试者，任一受试者已被其他 active 租约占用时整批 409 且零写入，成功返回
不可变 reservation_id；POST /experiments/{id}/enrollment-release 携带
operation_id 与期望版本按 compare-and-set 释放整批锁并支持幂等重放，
释放后受试者可重新入组而旧 reservation 永久不得复活；并提供按受试者查询
当前占用实验与版本、按实验查询当前持锁清单与入组审计的接口。
"""

from __future__ import annotations

import random
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query

from . import db
from .schemas import (
    AdvanceBatchOut,
    AdvanceBatchRequest,
    AdvanceOut,
    AdvanceRequest,
    CommitmentsOut,
    CompleteOut,
    CompleteRequest,
    EmergencyUnblindEventsOut,
    EmergencyUnblindOut,
    EmergencyUnblindRequest,
    EnrollmentLockEventsOut,
    EnrollmentLockOut,
    EnrollmentLockRequest,
    EnrollmentOccupanciesOut,
    EnrollmentReleaseOut,
    EnrollmentReleaseRequest,
    ExperimentClone,
    ExperimentCreate,
    ExperimentEnrollmentLocksOut,
    ExperimentOut,
    ExperimentSummary,
    PauseRequest,
    ProgressOut,
    ResumeRequest,
    SnapshotCreate,
    SnapshotOut,
    StateEventOut,
    StateEventsOut,
    StateTransitionBatchOut,
    StateTransitionBatchRequest,
    SubjectReplaceRequest,
    SubjectReplacementEventOut,
    SubjectReplacementEventsOut,
)
from .williams import group_size, williams_sequences


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 容器 / 进程启动时建表（旧库自动补齐新增表）
    db.init_db()
    yield


app = FastAPI(
    title="Crossover Experiment Order Balancer",
    description="基于 Williams 平衡拉丁方的交叉实验顺序分配与按周期受控揭盲服务",
    version="1.12.1",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _state_conflict_detail(exc: db.InvalidStateTransitionError) -> dict:
    """构造暂停 / 恢复状态冲突的 409 响应体（不含任何揭盲信息）。"""
    messages = {
        "already_paused": "实验已处于暂停状态，无需重复暂停",
        "already_active": "实验当前正在运行中，无需恢复",
        "state_version_mismatch": (
            "期望状态版本与服务端当前版本不一致（可能已被其他操作者变更）"
        ),
    }
    detail = {
        "message": messages[exc.reason],
        "reason": exc.reason,
        "current_status": exc.current_status,
        "state_version": exc.state_version,
    }
    if exc.expected_version is not None:
        detail["expected_version"] = exc.expected_version
    return detail


def _validate_conditions(conditions: list[str]) -> None:
    """校验条件列表：非空字符串且互异。"""
    if any(not c.strip() for c in conditions):
        raise HTTPException(status_code=422, detail="conditions 不能为空字符串")
    if len(set(conditions)) != len(conditions):
        raise HTTPException(
            status_code=422, detail="conditions 必须互异（存在重复条件）"
        )


def _validate_subject_ids(subject_ids: list[str]) -> None:
    """校验受试者列表：非空字符串且互异。"""
    if any(not s.strip() for s in subject_ids):
        raise HTTPException(status_code=422, detail="subject_ids 不能为空字符串")
    if len(set(subject_ids)) != len(subject_ids):
        raise HTTPException(
            status_code=422, detail="subject_ids 必须互异（存在重复受试者）"
        )


def _validate_full_groups(count: int, size: int) -> None:
    """受试者数量必须能整分为完整的 Williams 序列组。"""
    if count % size == 0:
        return
    lower = (count // size) * size
    upper = lower + size
    nearest = [lower, upper] if lower > 0 else [upper]
    raise HTTPException(
        status_code=422,
        detail={
            "message": (
                f"受试者数量 {count} 无法整分为完整的序列组"
                f"（每组 {size} 条序列）"
            ),
            "subject_count": count,
            "group_size": size,
            "nearest_acceptable": nearest,
        },
    )


def _build_assignments(
    conditions: list[str], subject_ids: list[str], seed: int
) -> tuple[int, list[dict]]:
    """按种子确定性地生成 Williams 分配。

    Williams 平衡序列（下标形式）映射为实际条件后，用种子打乱“序列池”
    再依次分配给受试者；池中每条序列恰好出现 ``count // size`` 次，
    保证整组完整。完整序列只写入服务端存储，不进入响应。

    :returns: ``(完整序列组大小, 分配列表)``。
    """
    n = len(conditions)
    size = group_size(n)
    count = len(subject_ids)
    _validate_full_groups(count, size)

    index_sequences = williams_sequences(n)
    condition_sequences = [
        [conditions[i] for i in seq] for seq in index_sequences
    ]

    rng = random.Random(seed)
    pool = list(range(size)) * (count // size)
    rng.shuffle(pool)

    assignments = [
        {
            "subject_id": subject_id,
            "sequence_index": pool[i],
            "sequence": condition_sequences[pool[i]],
        }
        for i, subject_id in enumerate(subject_ids)
    ]
    return size, assignments


@app.post(
    "/experiments",
    status_code=201,
    response_model=ExperimentOut,
    summary="创建实验并生成分配结果（不返回条件序列）",
)
def create_experiment(payload: ExperimentCreate) -> dict:
    conditions = payload.conditions
    subject_ids = payload.subject_ids
    _validate_conditions(conditions)
    _validate_subject_ids(subject_ids)

    size, assignments = _build_assignments(conditions, subject_ids, payload.seed)

    experiment_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    db.insert_experiment(
        experiment_id,
        conditions,
        payload.seed,
        size,
        created_at,
        assignments,
        washout_seconds=payload.washout_seconds,
    )

    return {
        "experiment_id": experiment_id,
        "conditions": conditions,
        "seed": payload.seed,
        "group_size": size,
        "num_subjects": len(subject_ids),
        "num_periods": len(conditions),
        "washout_seconds": payload.washout_seconds,
        "status": "active",
        "state_version": 0,
        "created_at": created_at,
        "assignments": [{"subject_id": s} for s in subject_ids],
    }


@app.post(
    "/experiments/{experiment_id}/clone",
    status_code=201,
    response_model=ExperimentOut,
    summary=(
        "基于源实验条件模板克隆独立新实验：重新生成 Williams 分配、稳定槽位"
        "与每周期盲态承诺，不复用源实验的 nonce/commitment 或任何揭盲进度"
    ),
)
def clone_experiment(experiment_id: str, payload: ExperimentClone) -> dict:
    source = db.get_experiment(experiment_id)
    if source is None:
        raise HTTPException(status_code=404, detail="experiment not found")

    conditions = source["conditions"]
    subject_ids = payload.subject_ids
    _validate_subject_ids(subject_ids)

    # 用新种子重新执行 Williams 池打乱；受试者数量不成整组时返回明确错误
    size, assignments = _build_assignments(conditions, subject_ids, payload.seed)

    # 省略 washout_seconds 时沿用源实验洗脱期；显式传值（含 0）时以请求为准
    washout_seconds = (
        source["washout_seconds"]
        if payload.washout_seconds is None
        else payload.washout_seconds
    )

    # 新实验 ID、创建时间、稳定槽位与 128 位 nonce/承诺全部重新生成，
    # 源实验的揭盲 / 完成 / 暂停 / 紧急揭盲 / 快照等任何状态均不复制。
    new_experiment_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    db.insert_experiment(
        new_experiment_id,
        conditions,
        payload.seed,
        size,
        created_at,
        assignments,
        washout_seconds=washout_seconds,
    )

    return {
        "experiment_id": new_experiment_id,
        "conditions": conditions,
        "seed": payload.seed,
        "group_size": size,
        "num_subjects": len(subject_ids),
        "num_periods": len(conditions),
        "washout_seconds": washout_seconds,
        "status": "active",
        "state_version": 0,
        "created_at": created_at,
        "assignments": [{"subject_id": s} for s in subject_ids],
    }


@app.get(
    "/experiments",
    response_model=list[ExperimentSummary],
    summary="列出全部实验",
)
def list_experiments() -> list[dict]:
    return db.list_experiments()


@app.get(
    "/experiments/{experiment_id}",
    response_model=ExperimentOut,
    summary="查询实验（只返回受试者名册，不含条件序列）",
)
def get_experiment(experiment_id: str) -> dict:
    experiment = db.get_experiment(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return experiment


@app.get(
    "/experiments/{experiment_id}/commitments",
    response_model=CommitmentsOut,
    summary="查询盲态分配承诺（只返回槽位、周期与承诺，不返回 nonce 或条件）",
)
def list_commitments(experiment_id: str) -> dict:
    try:
        commitments = db.list_commitments(experiment_id)
    except db.CommitmentsUnavailableError:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "该实验创建时尚未启用盲态承诺，不能事后补造",
                "reason": "commitments_unavailable",
            },
        )
    if commitments is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return {"experiment_id": experiment_id, "commitments": commitments}


@app.post(
    "/experiments/{experiment_id}/advance",
    response_model=AdvanceOut,
    response_model_exclude_none=True,
    summary=(
        "按周期受控揭盲：上一周期已完成且洗脱期届满时，"
        "仅当 expected_period 为下一周期才返回当前条件并原子推进"
    ),
)
def advance_period(experiment_id: str, payload: AdvanceRequest) -> dict:
    try:
        result = db.advance_period(
            experiment_id,
            payload.subject_id,
            payload.expected_period,
            payload.request_id,
        )
    except db.RequestIdConflictError:
        # 同一幂等键却提交了不同的受试者或周期
        raise HTTPException(
            status_code=409,
            detail={
                "message": "request_id 已被其他请求占用",
                "reason": "request_id_conflict",
                "request_id": payload.request_id,
            },
        )
    except db.StalePeriodError as exc:
        # 并发抢先推进或过期周期：不揭露任何条件
        raise HTTPException(
            status_code=409,
            detail={
                "message": "expected_period 与该受试者的下一周期不一致"
                if not exc.complete
                else "该受试者已完成全部周期",
                "reason": "experiment_complete"
                if exc.complete
                else "period_mismatch",
                "next_period": exc.next_period,
            },
        )
    except db.PriorPeriodNotCompletedError:
        # 上一周期尚无完成记录：只说明“未就绪”，不揭露下一周期条件
        raise HTTPException(
            status_code=409,
            detail={
                "message": "上一周期尚未登记完成，不能推进下一周期",
                "reason": "prior_period_not_completed",
            },
        )
    except db.WashoutNotElapsedError as exc:
        # 洗脱期未届满：给出服务端计算的重试间隔，不附带任何周期 / 条件信息
        raise HTTPException(
            status_code=409,
            detail={
                "message": "洗脱期尚未届满，下一周期暂不可推进",
                "reason": "washout_not_elapsed",
                "retry_after_seconds": exc.retry_after_seconds,
            },
        )
    except db.ExperimentPausedError:
        # 实验已暂停：拒绝揭盲，响应不泄露任何条件 / 后续安排
        raise HTTPException(
            status_code=409,
            detail={
                "message": "实验已暂停，暂停期间不能揭盲",
                "reason": "experiment_paused",
            },
        )
    except db.SubjectReplacedError:
        # 该编号已因换号退役：不能推进，响应不泄露任何条件
        raise HTTPException(
            status_code=409,
            detail={
                "message": "该受试者编号已换号退役，不能推进",
                "reason": "subject_replaced",
                "subject_id": payload.subject_id,
            },
        )

    if result is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "实验或受试者不存在",
                "experiment_id": experiment_id,
                "subject_id": payload.subject_id,
            },
        )
    return result


@app.post(
    "/experiments/{experiment_id}/advance-batch",
    response_model=AdvanceBatchOut,
    response_model_exclude_none=True,
    summary=(
        "批量受控揭盲：单个写事务内校验全部成员的完成登记与洗脱期门禁，"
        "全部通过才按输入顺序揭盲并推进；任一项失败整批零写入并返回 409"
    ),
)
def advance_batch(experiment_id: str, payload: AdvanceBatchRequest) -> dict:
    try:
        result = db.advance_batch(
            experiment_id,
            payload.batch_id,
            [m.model_dump() for m in payload.members],
        )
    except db.BatchIdConflictError:
        # 同一 batch_id 绑定了不同的成员或周期
        raise HTTPException(
            status_code=409,
            detail={
                "message": "batch_id 已被成员或周期不同的其他请求占用",
                "reason": "batch_id_conflict",
                "batch_id": payload.batch_id,
            },
        )
    except db.BatchMembersRejectedError as exc:
        # 整批门禁未通过：只列各失败项的 subject_id 与原因，不含任何条件
        raise HTTPException(
            status_code=409,
            detail={
                "message": "批量中存在未通过门禁的成员，整批未揭盲",
                "reason": "batch_member_rejected",
                "batch_id": payload.batch_id,
                "failures": exc.failures,
            },
        )
    except db.ExperimentPausedError:
        # 实验已暂停：整批拒绝，响应不泄露任何条件 / 后续安排
        raise HTTPException(
            status_code=409,
            detail={
                "message": "实验已暂停，暂停期间不能揭盲",
                "reason": "experiment_paused",
                "batch_id": payload.batch_id,
            },
        )

    if result is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return result


@app.post(
    "/experiments/{experiment_id}/complete",
    response_model=CompleteOut,
    summary=(
        "登记当前周期完成时间：只有已揭盲且尚未完成的当前周期可登记，"
        "相同 request_id 重放原结果"
    ),
)
def complete_period(experiment_id: str, payload: CompleteRequest) -> dict:
    try:
        result = db.complete_period(
            experiment_id,
            payload.subject_id,
            payload.period,
            payload.request_id,
        )
    except db.RequestIdConflictError:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "request_id 已被其他请求占用",
                "reason": "request_id_conflict",
                "request_id": payload.request_id,
            },
        )
    except db.CompletionOrderError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "该周期已登记完成"
                    if exc.already_completed
                    else "只能登记已揭盲且尚未完成的当前周期"
                ),
                "reason": (
                    "period_already_completed"
                    if exc.already_completed
                    else "period_out_of_order"
                ),
                "current_period": exc.current_period,
            },
        )
    except db.SubjectReplacedError:
        # 该编号已因换号退役：不能登记完成
        raise HTTPException(
            status_code=409,
            detail={
                "message": "该受试者编号已换号退役，不能登记完成",
                "reason": "subject_replaced",
                "subject_id": payload.subject_id,
            },
        )

    if result is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "实验或受试者不存在",
                "experiment_id": experiment_id,
                "subject_id": payload.subject_id,
            },
        )
    return result


@app.get(
    "/experiments/{experiment_id}/subjects/{subject_id}/progress",
    response_model=ProgressOut,
    summary="查询受试者揭盲进度（已揭盲 / 已完成记录、next_period 与 next_available_at）",
)
def get_progress(experiment_id: str, subject_id: str) -> dict:
    try:
        progress = db.get_progress(experiment_id, subject_id)
    except db.SubjectReplacedError:
        # 该编号已因换号退役：不能查询进度
        raise HTTPException(
            status_code=409,
            detail={
                "message": "该受试者编号已换号退役，不能查询进度",
                "reason": "subject_replaced",
                "subject_id": subject_id,
            },
        )
    if progress is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "实验或受试者不存在",
                "experiment_id": experiment_id,
                "subject_id": subject_id,
            },
        )
    return progress


@app.post(
    "/experiments/{experiment_id}/pause",
    response_model=StateEventOut,
    status_code=200,
    summary=(
        "暂停实验：携带唯一 operation_id 与原因，原子置为 paused 并递增状态版本；"
        "暂停期间单条与批量揭盲均返回 409"
    ),
)
def pause_experiment(experiment_id: str, payload: PauseRequest) -> dict:
    try:
        event = db.pause_experiment(
            experiment_id, payload.operation_id, payload.reason
        )
    except db.OperationIdConflictError:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "operation_id 已被其他状态变更操作占用",
                "reason": "operation_id_conflict",
                "operation_id": payload.operation_id,
            },
        )
    except db.InvalidStateTransitionError as exc:
        raise HTTPException(
            status_code=409, detail=_state_conflict_detail(exc)
        )
    if event is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return event


@app.post(
    "/experiments/{experiment_id}/resume",
    response_model=StateEventOut,
    status_code=200,
    summary=(
        "恢复实验：携带期望状态版本，在单个写事务内 compare-and-set 原子恢复，"
        "版本过期或状态不符返回 409 并回传当前版本"
    ),
)
def resume_experiment(experiment_id: str, payload: ResumeRequest) -> dict:
    try:
        event = db.resume_experiment(
            experiment_id,
            payload.expected_version,
            payload.operation_id,
            payload.reason,
        )
    except db.OperationIdConflictError:
        if payload.operation_id is None:
            # 理论不可达：无 operation_id 不会触发唯一冲突，防御性处理
            raise HTTPException(
                status_code=409,
                detail={"message": "状态变更冲突", "reason": "operation_id_conflict"},
            )
        raise HTTPException(
            status_code=409,
            detail={
                "message": "operation_id 已被其他状态变更操作占用",
                "reason": "operation_id_conflict",
                "operation_id": payload.operation_id,
            },
        )
    except db.InvalidStateTransitionError as exc:
        raise HTTPException(
            status_code=409, detail=_state_conflict_detail(exc)
        )
    if event is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return event


@app.post(
    "/experiments/state-batch",
    response_model=StateTransitionBatchOut,
    summary=(
        "批量状态切换：一次提交 batch_operation_id 与多个实验的暂停 / 恢复操作，"
        "同一事务内先校验全部状态与版本，全部通过才执行并追加审计；"
        "任一冲突整批回滚，重复 batch_operation_id 幂等返回首次结果"
    ),
)
def transition_experiment_states_batch(
    payload: StateTransitionBatchRequest,
) -> dict:
    try:
        return db.apply_state_transition_batch(
            payload.batch_operation_id,
            [operation.model_dump() for operation in payload.operations],
        )
    except db.BatchOperationIdConflictError:
        # 同一 batch_operation_id 绑定了不同的操作列表
        raise HTTPException(
            status_code=409,
            detail={
                "message": "batch_operation_id 已被操作列表不同的其他批量请求占用",
                "reason": "batch_operation_id_conflict",
                "batch_operation_id": payload.batch_operation_id,
            },
        )
    except db.BatchStateTransitionsRejectedError as exc:
        # 整批校验未通过：按输入顺序列出各失败项，整批零写入
        raise HTTPException(
            status_code=409,
            detail={
                "message": "批量状态切换中存在未通过校验的操作，整批未执行",
                "reason": "batch_operation_rejected",
                "batch_operation_id": payload.batch_operation_id,
                "failures": exc.failures,
            },
        )


@app.get(
    "/experiments/{experiment_id}/state-events",
    response_model=StateEventsOut,
    summary="查询实验状态变更审计记录（暂停 / 恢复），按时间倒序分页",
)
def list_state_events(
    experiment_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    events = db.list_state_events(experiment_id, limit=limit, offset=offset)
    if events is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return {"experiment_id": experiment_id, "events": events}


@app.post(
    "/experiments/{experiment_id}/emergency-unblind",
    response_model=EmergencyUnblindOut,
    response_model_exclude_none=True,
    summary=(
        "实验级紧急揭盲（双人审批）：首个审批人开启 15 分钟授权窗口，"
        "窗口内第二个不同审批人批准后才一次性返回该受试者完整条件序列；"
        "审批与最终揭盲在单个 SQLite 写事务内完成"
    ),
)
def emergency_unblind(experiment_id: str, payload: EmergencyUnblindRequest) -> dict:
    try:
        result = db.emergency_unblind(
            experiment_id,
            payload.subject_id,
            payload.approver_id,
            payload.operation_id,
            payload.reason,
        )
    except db.EmergencyOperationIdConflictError:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "operation_id 已被其他紧急揭盲申请占用",
                "reason": "operation_id_conflict",
                "operation_id": payload.operation_id,
            },
        )
    except db.EmergencySameApproverError:
        # 双人复核未满足：不揭露任何条件 / 序列
        raise HTTPException(
            status_code=409,
            detail={
                "message": "第二审批人必须与首个审批人不同",
                "reason": "same_approver",
            },
        )
    except db.EmergencyAlreadyUnblindedError:
        # 一次性揭盲不可重复：不揭露任何条件 / 序列
        raise HTTPException(
            status_code=409,
            detail={
                "message": "该受试者已完成紧急揭盲，不可重复申请",
                "reason": "already_unblinded",
                "subject_id": payload.subject_id,
            },
        )
    except db.EmergencyWindowExpiredError:
        # 窗口过期：不揭露任何条件 / 序列，可重新发起首个审批开启新窗口
        raise HTTPException(
            status_code=409,
            detail={
                "message": "授权窗口已过期，本次申请被拒绝；可重新发起首个审批",
                "reason": "window_expired",
            },
        )
    except db.SubjectReplacedError:
        # 该编号已因换号退役：不能发起紧急揭盲，不泄露任何序列
        raise HTTPException(
            status_code=409,
            detail={
                "message": "该受试者编号已换号退役，不能发起紧急揭盲",
                "reason": "subject_replaced",
                "subject_id": payload.subject_id,
            },
        )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "实验或受试者不存在",
                "experiment_id": experiment_id,
                "subject_id": payload.subject_id,
            },
        )
    return result


@app.get(
    "/experiments/{experiment_id}/emergency-unblind-events",
    response_model=EmergencyUnblindEventsOut,
    summary="查询紧急揭盲审计记录（不含条件内容），按时间倒序分页",
)
def list_emergency_unblind_events(
    experiment_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    events = db.list_emergency_unblind_events(
        experiment_id, limit=limit, offset=offset
    )
    if events is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return {"experiment_id": experiment_id, "events": events}


@app.post(
    "/experiments/{experiment_id}/snapshots",
    response_model=SnapshotOut,
    response_model_exclude_unset=True,
    summary=(
        "生成脱敏结果快照：一致性读事务内固定同一时点，按 subject_id 稳定排序，"
        "仅输出字段白名单内的已揭盲周期与完成时间（未完成周期为 null），"
        "相同 operation_id 幂等返回同一快照"
    ),
)
def create_snapshot(experiment_id: str, payload: SnapshotCreate) -> dict:
    try:
        result = db.create_result_snapshot(
            experiment_id, payload.fields, payload.operation_id
        )
    except db.SnapshotOperationIdConflictError:
        # 同一 operation_id 绑定了不同的字段白名单
        raise HTTPException(
            status_code=409,
            detail={
                "message": "operation_id 已被字段白名单不同的其他快照请求占用",
                "reason": "operation_id_conflict",
                "operation_id": payload.operation_id,
            },
        )
    if result is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return result


@app.get(
    "/experiments/{experiment_id}/snapshots/{snapshot_id}",
    response_model=SnapshotOut,
    response_model_exclude_unset=True,
    summary="按快照 ID 获取不可变快照内容（含 SHA-256 摘要）",
)
def get_snapshot(experiment_id: str, snapshot_id: str) -> dict:
    snapshot = db.get_result_snapshot(experiment_id, snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="snapshot not found")
    return snapshot


@app.post(
    "/experiments/{experiment_id}/subjects/{subject_id}/replace",
    response_model=SubjectReplacementEventOut,
    summary=(
        "受试者换号：仅当原受试者从未揭盲、且原 / 新编号均未被有效入组锁"
        "占用时，在单个写事务内把隐藏分配（序列内容与下标不变）与进度原子"
        "转移到新编号，原编号保留墓碑（不可复用、不可查询进度或推进），"
        "并追加不含条件内容的审计记录"
    ),
)
def replace_subject(
    experiment_id: str, subject_id: str, payload: SubjectReplaceRequest
) -> dict:
    try:
        result = db.replace_subject(
            experiment_id,
            subject_id,
            payload.replacement_subject_id,
            payload.operation_id,
            payload.reason,
        )
    except db.ReplacementOperationIdConflictError:
        # 同一 operation_id 绑定了不同的原 / 新编号或原因
        raise HTTPException(
            status_code=409,
            detail={
                "message": "operation_id 已被其他换号操作占用",
                "reason": "operation_id_conflict",
                "operation_id": payload.operation_id,
            },
        )
    except db.SubjectReplacedError:
        # 原编号已退役（墓碑）：不能再次换号
        raise HTTPException(
            status_code=409,
            detail={
                "message": "该受试者编号已换号退役，不能再次换号",
                "reason": "subject_replaced",
                "subject_id": subject_id,
            },
        )
    except db.ReplacementSubjectExistsError:
        # 新编号已存在（在册或已退役），不能复用
        raise HTTPException(
            status_code=409,
            detail={
                "message": "新受试者编号已存在，不能复用",
                "reason": "replacement_subject_exists",
                "replacement_subject_id": payload.replacement_subject_id,
            },
        )
    except db.SubjectAlreadyUnblindedError:
        # 原编号已发生过揭盲：不允许换号，不泄露任何条件
        raise HTTPException(
            status_code=409,
            detail={
                "message": "该受试者已发生揭盲，不允许换号",
                "reason": "subject_already_unblinded",
                "subject_id": subject_id,
            },
        )
    except db.ReplacementEnrollmentOccupiedError as exc:
        # 原编号或新编号被有效入组锁占用：先释放相关锁再换号，不暗改清单
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "原编号或新编号被有效入组 reservation 占用，"
                    "请先释放相关入组锁再换号"
                ),
                "reason": "enrollment_occupied",
                "operation_id": payload.operation_id,
                "conflicts": exc.conflicts,
            },
        )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "实验或受试者不存在",
                "experiment_id": experiment_id,
                "subject_id": subject_id,
            },
        )
    return result


@app.get(
    "/experiments/{experiment_id}/subject-replacements",
    response_model=SubjectReplacementEventsOut,
    summary="查询受试者换号审计记录（不含条件内容），按时间倒序分页",
)
def list_subject_replacements(
    experiment_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    replacements = db.list_subject_replacements(
        experiment_id, limit=limit, offset=offset
    )
    if replacements is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return {"experiment_id": experiment_id, "replacements": replacements}


# ---------- 受试者入组互斥（enrollment locks） ----------


@app.post(
    "/enrollment-locks",
    status_code=201,
    response_model=EnrollmentLockOut,
    summary=(
        "批量申请受试者入组租约：单个写事务内以 subject_id 为全局键登记，"
        "1～100 个受试者全部未被其他 active 租约占用才写入不可变 reservation；"
        "任一被占用整批 409 且零写入"
    ),
)
def create_enrollment_locks(payload: EnrollmentLockRequest) -> dict:
    try:
        result = db.create_enrollment_reservation(
            payload.experiment_id, payload.subject_ids, payload.operation_id
        )
    except db.EnrollmentOperationIdConflictError:
        # operation_id 在申请 / 释放共享的全局空间内已被其他参数占用
        raise HTTPException(
            status_code=409,
            detail={
                "message": "operation_id 已被其他入组申请 / 释放操作占用",
                "reason": "operation_id_conflict",
                "operation_id": payload.operation_id,
            },
        )
    except db.EnrollmentSubjectsOccupiedError as exc:
        # 整批零写入：列出每个被占用受试者当前的租约归属与版本
        raise HTTPException(
            status_code=409,
            detail={
                "message": "批量中存在已被其他 active 实验占用的受试者，整批未登记",
                "reason": "subjects_occupied",
                "operation_id": payload.operation_id,
                "conflicts": exc.conflicts,
            },
        )
    if result is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return result


@app.post(
    "/experiments/{experiment_id}/enrollment-release",
    response_model=EnrollmentReleaseOut,
    summary=(
        "按 compare-and-set 释放整批入组锁：仅当 reservation 仍为 held 且"
        "expected_version 与当前版本一致时才释放；释放后可重新入组，"
        "旧 reservation 不得复活；相同 operation_id 幂等重放首次释放结果"
    ),
)
def release_enrollment_locks(
    experiment_id: str, payload: EnrollmentReleaseRequest
) -> dict:
    try:
        result = db.release_enrollment_reservation(
            experiment_id,
            payload.reservation_id,
            payload.expected_version,
            payload.operation_id,
        )
    except db.EnrollmentOperationIdConflictError:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "operation_id 已被其他入组申请 / 释放操作占用",
                "reason": "operation_id_conflict",
                "operation_id": payload.operation_id,
            },
        )
    except db.EnrollmentReservationReleasedError:
        # 旧 reservation 已释放：终态不可逆，不能复活也不能再次释放
        raise HTTPException(
            status_code=409,
            detail={
                "message": "该 reservation 已释放，旧 reservation 不得复活或再次释放",
                "reason": "reservation_already_released",
                "reservation_id": payload.reservation_id,
            },
        )
    except db.EnrollmentVersionMismatchError as exc:
        # CAS 失败：回传服务端当前版本，调用方应重新查询后再操作
        raise HTTPException(
            status_code=409,
            detail={
                "message": "expected_version 与 reservation 当前版本不一致",
                "reason": "reservation_version_mismatch",
                "reservation_id": payload.reservation_id,
                "current_version": exc.current_version,
                "expected_version": exc.expected_version,
            },
        )
    if result is None:
        raise HTTPException(status_code=404, detail="reservation not found")
    return result


@app.get(
    "/enrollment-locks",
    response_model=EnrollmentOccupanciesOut,
    response_model_exclude_none=True,
    summary=(
        "按 subject_id 全局查询当前入组占用：返回每个受试者当前占用实验、"
        "reservation 与租约版本；从未登记或已释放的受试者状态为 released"
    ),
)
def get_enrollment_occupancies(
    subject_ids: list[str] = Query(
        ...,
        min_length=1,
        description=(
            "受试者 ID 查询参数（可重复传入，如 ?subject_ids=s1&subject_ids=s2），"
            "1～100 个、互异、非空；结果按该顺序返回"
        ),
    ),
) -> dict:
    # Query(min_length/max_length) 作用于列表元素而非列表本身，列表长度 /
    # 空字符串 / 互异在此显式校验，与 POST 请求体保持一致的 422 语义
    if len(subject_ids) > 100:
        raise HTTPException(status_code=422, detail="subject_ids 最多 100 个")
    if any(not s.strip() for s in subject_ids):
        raise HTTPException(status_code=422, detail="subject_ids 不能为空字符串")
    if len(set(subject_ids)) != len(subject_ids):
        raise HTTPException(
            status_code=422, detail="subject_ids 必须互异（存在重复受试者）"
        )
    return {"subjects": db.list_enrollment_occupancies(subject_ids)}


@app.get(
    "/experiments/{experiment_id}/enrollment-locks",
    response_model=ExperimentEnrollmentLocksOut,
    summary="查询某实验当前持有的全部入组锁（不含已释放的历史 reservation）",
)
def list_experiment_enrollment_locks(experiment_id: str) -> dict:
    subjects = db.list_experiment_enrollment_locks(experiment_id)
    if subjects is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return {"experiment_id": experiment_id, "subjects": subjects}


@app.get(
    "/experiments/{experiment_id}/enrollment-events",
    response_model=EnrollmentLockEventsOut,
    summary="查询入组申请 / 释放审计记录，按时间倒序分页",
)
def list_enrollment_lock_events(
    experiment_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    events = db.list_enrollment_lock_events(
        experiment_id, limit=limit, offset=offset
    )
    if events is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return {"experiment_id": experiment_id, "events": events}
