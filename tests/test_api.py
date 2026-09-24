"""API 与 Williams 序列的自动化测试。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.williams import group_size, williams_sequences


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as test_client:
        yield test_client


def assert_balanced(sequences: list[list[str]], n: int) -> None:
    """校验位置平衡与有向相邻对平衡。"""
    size = group_size(n)
    assert len(sequences) % size == 0
    k = len(sequences) // size  # 完整组数
    per_position = k * (1 if n % 2 == 0 else 2)

    # 每个条件在每个位置出现次数相等
    for pos in range(n):
        counts = Counter(seq[pos] for seq in sequences)
        assert len(counts) == n
        assert all(v == per_position for v in counts.values())

    # 每个有向相邻条件对出现次数相等
    pairs: Counter = Counter()
    for seq in sequences:
        for a, b in zip(seq, seq[1:]):
            pairs[(a, b)] += 1
    assert len(pairs) == n * (n - 1)
    assert len(set(pairs.values())) == 1


def create_experiment(
    client: TestClient,
    conditions: list[str] | None = None,
    subject_ids: list[str] | None = None,
    seed: int = 42,
    washout_seconds: int = 0,
):
    conditions = conditions or ["A", "B", "C"]
    subject_ids = subject_ids or [f"s{i}" for i in range(6)]
    resp = client.post(
        "/experiments",
        json={
            "conditions": conditions,
            "subject_ids": subject_ids,
            "seed": seed,
            "washout_seconds": washout_seconds,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json(), subject_ids


def complete_period(
    client: TestClient,
    experiment_id: str,
    subject_id: str,
    period: int,
    request_id: str | None = None,
):
    resp = client.post(
        f"/experiments/{experiment_id}/complete",
        json={
            "subject_id": subject_id,
            "period": period,
            "request_id": request_id or f"{subject_id}-c{period}",
        },
    )
    return resp


def reveal_period(
    client: TestClient,
    experiment_id: str,
    subject_id: str,
    period: int,
    req_prefix: str = "",
):
    return client.post(
        f"/experiments/{experiment_id}/advance",
        json={
            "subject_id": subject_id,
            "expected_period": period,
            "request_id": f"{req_prefix}{subject_id}-p{period}",
        },
    )


def reveal_sequence(
    client: TestClient,
    experiment_id: str,
    subject_id: str,
    num_periods: int,
    req_prefix: str = "",
) -> list[str]:
    """通过推进接口逐周期揭盲，重建该受试者的条件序列。

    默认实验 washout=0，因此每个周期揭盲后登记完成即可立刻推进下一周期。
    """
    sequence: list[str] = []
    for period in range(1, num_periods + 1):
        resp = reveal_period(
            client, experiment_id, subject_id, period, req_prefix
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["period"] == period
        sequence.append(body["condition"])
        if period < num_periods:
            done = complete_period(
                client,
                experiment_id,
                subject_id,
                period,
                f"{req_prefix}{subject_id}-c{period}",
            )
            assert done.status_code == 200, done.text
    return sequence


def assert_no_sequence_leak(payload: object) -> None:
    """响应中任何层级都不得出现条件序列或序列下标。"""
    forbidden = {"sequence", "sequence_index"}
    if isinstance(payload, dict):
        assert forbidden.isdisjoint(payload.keys())
        for value in payload.values():
            assert_no_sequence_leak(value)
    elif isinstance(payload, list):
        for item in payload:
            assert_no_sequence_leak(item)


# ---------- Williams 序列单元测试 ----------

@pytest.mark.parametrize("n", range(2, 9))
def test_williams_sequences_balanced(n: int) -> None:
    sequences = [[str(i) for i in seq] for seq in williams_sequences(n)]
    assert len(sequences) == group_size(n)
    assert_balanced(sequences, n)


# ---------- 创建 / 查询接口（不再下发完整序列） ----------

def test_create_even_conditions(client: TestClient) -> None:
    body, subject_ids = create_experiment(
        client,
        conditions=["A", "B", "C", "D"],
        subject_ids=[f"s{i}" for i in range(8)],
    )
    assert body["group_size"] == 4
    assert body["num_subjects"] == 8
    assert body["num_periods"] == 4
    assert len(body["assignments"]) == 8
    # 名册只含受试者 ID，且顺序与提交一致
    assert [a["subject_id"] for a in body["assignments"]] == subject_ids
    assert set(body["assignments"][0].keys()) == {"subject_id"}
    assert_no_sequence_leak(body)

    # 序列只能通过逐周期揭盲重建，且仍满足 Williams 平衡
    sequences = [
        reveal_sequence(client, body["experiment_id"], s, 4)
        for s in subject_ids
    ]
    assert_balanced(sequences, 4)
    # 2 个完整组：每条 Williams 序列恰好被使用 2 次
    assert sorted(Counter(tuple(seq) for seq in sequences).values()) == [
        2, 2, 2, 2
    ]


def test_create_odd_conditions(client: TestClient) -> None:
    body, subject_ids = create_experiment(
        client,
        conditions=["A", "B", "C"],
        subject_ids=[f"s{i}" for i in range(6)],
        seed=7,
    )
    assert body["group_size"] == 6
    assert body["num_periods"] == 3
    assert_no_sequence_leak(body)
    sequences = [
        reveal_sequence(client, body["experiment_id"], s, 3)
        for s in subject_ids
    ]
    assert_balanced(sequences, 3)


def test_same_input_and_seed_gives_same_assignment(client: TestClient) -> None:
    payload = {
        "conditions": ["A", "B", "C"],
        "subject_ids": [f"s{i}" for i in range(12)],
        "seed": 123,
    }
    first = client.post("/experiments", json=payload).json()
    second = client.post("/experiments", json=payload).json()
    assert first["assignments"] == second["assignments"]
    # 逐周期揭盲重建的序列也完全一致（确定性分配）
    first_seqs = [
        reveal_sequence(client, first["experiment_id"], s, 3, "first-")
        for s in payload["subject_ids"]
    ]
    second_seqs = [
        reveal_sequence(client, second["experiment_id"], s, 3, "second-")
        for s in payload["subject_ids"]
    ]
    assert first_seqs == second_seqs


def test_subject_count_not_multiple_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "B", "C"],
            "subject_ids": [f"s{i}" for i in range(7)],
            "seed": 1,
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["group_size"] == 6
    assert detail["subject_count"] == 7
    assert detail["nearest_acceptable"] == [6, 12]


def test_subject_count_below_one_group_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "B", "C", "D"],
            "subject_ids": ["s1", "s2"],
            "seed": 1,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["nearest_acceptable"] == [4]


def test_duplicate_conditions_rejected(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "A", "B"],
            "subject_ids": [f"s{i}" for i in range(6)],
            "seed": 1,
        },
    )
    assert resp.status_code == 422


def test_duplicate_subjects_rejected(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "B", "C"],
            "subject_ids": ["s1", "s1", "s2", "s3", "s4", "s5"],
            "seed": 1,
        },
    )
    assert resp.status_code == 422


def test_too_few_conditions_rejected(client: TestClient) -> None:
    resp = client.post(
        "/experiments",
        json={"conditions": ["A"], "subject_ids": ["s1"], "seed": 1},
    )
    assert resp.status_code == 422


def test_get_experiment_does_not_leak_sequence(client: TestClient) -> None:
    created, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    experiment_id = created["experiment_id"]

    resp = client.get(f"/experiments/{experiment_id}")
    assert resp.status_code == 200
    assert resp.json() == created
    assert_no_sequence_leak(resp.json())

    listing = client.get("/experiments").json()
    entry = next(e for e in listing if e["experiment_id"] == experiment_id)
    assert entry["num_periods"] == 2
    assert_no_sequence_leak(entry)
    assert "assignments" not in entry


def test_get_unknown_experiment_returns_404(client: TestClient) -> None:
    assert client.get("/experiments/nope").status_code == 404


# ---------- 模板克隆（clone） ----------

_UNSET = object()


def clone_experiment(
    client: TestClient,
    source_id: str,
    subject_ids: list[str],
    seed: int,
    washout_seconds=_UNSET,
):
    payload: dict = {"subject_ids": subject_ids, "seed": seed}
    if washout_seconds is not _UNSET:
        payload["washout_seconds"] = washout_seconds
    return client.post(f"/experiments/{source_id}/clone", json=payload)


def test_clone_creates_independent_experiment_with_fresh_slots_and_commitments(
    client: TestClient,
) -> None:
    body, source_subjects = create_experiment(
        client,
        conditions=["A", "B", "C"],
        subject_ids=[f"s{i}" for i in range(6)],
        seed=42,
    )
    source_id = body["experiment_id"]

    # 源实验推进 / 完成若干周期，克隆绝不能带上这些揭盲进度
    source_seq_s0 = reveal_sequence(client, source_id, "s0", 3, "src-")
    source_commitments = client.get(
        f"/experiments/{source_id}/commitments"
    ).json()["commitments"]
    source_slots = {c["allocation_slot_id"] for c in source_commitments}
    source_s1 = reveal_period(client, source_id, "s1", 1, "src-")
    assert source_s1.status_code == 200
    source_nonce_s1 = source_s1.json()["nonce"]

    clone_subjects = [f"c{i}" for i in range(6)]
    resp = clone_experiment(client, source_id, clone_subjects, seed=99)
    assert resp.status_code == 201, resp.text
    clone = resp.json()
    clone_id = clone["experiment_id"]

    # 响应只返回新实验名册与元数据，不含任何条件序列 / 槽位 / 承诺
    assert clone_id != source_id
    assert clone["conditions"] == ["A", "B", "C"]
    assert clone["seed"] == 99
    assert clone["group_size"] == 6
    assert clone["num_subjects"] == 6
    assert clone["num_periods"] == 3
    assert clone["status"] == "active"
    assert clone["state_version"] == 0
    assert clone["washout_seconds"] == 0
    assert [a["subject_id"] for a in clone["assignments"]] == clone_subjects
    assert all(set(a) == {"subject_id"} for a in clone["assignments"])
    assert_no_sequence_leak(clone)

    # 克隆从零开始：无任何揭盲 / 完成进度（不复制源实验进度）
    for subject_id in clone_subjects:
        progress = client.get(
            f"/experiments/{clone_id}/subjects/{subject_id}/progress"
        ).json()
        assert progress["next_period"] == 1
        assert progress["reveals"] == []
        assert progress["completed"] is False

    # 克隆的 Williams 分配重新生成且仍平衡
    clone_sequences = [
        reveal_sequence(client, clone_id, s, 3, "clone-")
        for s in clone_subjects
    ]
    assert_balanced(clone_sequences, 3)

    # 稳定槽位为全新 ID，与源实验槽位互不相交
    clone_commitments = client.get(
        f"/experiments/{clone_id}/commitments"
    ).json()["commitments"]
    clone_slots = {c["allocation_slot_id"] for c in clone_commitments}
    assert clone_slots.isdisjoint(source_slots)
    assert len(clone_commitments) == 18

    # 克隆每个槽位 / 周期的 nonce 与承诺重新生成；即使条件相同，
    # 承诺也因新的 experiment_id / slot_id / nonce 而不同于源实验
    clone_reveal = client.post(
        f"/experiments/{clone_id}/advance",
        json={
            "subject_id": "c1",
            "expected_period": 1,
            "request_id": "clone-c1-p1",
        },
    )
    assert clone_reveal.status_code == 200
    clone_reveal_body = clone_reveal.json()
    c1_key = {
        (item["subject_id"], item["period"]): item
        for item in clone_commitments
    }[("c1", 1)]
    assert clone_reveal_body["allocation_slot_id"] == c1_key["allocation_slot_id"]
    assert clone_reveal_body["nonce"] != source_nonce_s1
    assert (
        commitment_digest(
            clone_id,
            clone_reveal_body["allocation_slot_id"],
            1,
            clone_reveal_body["condition"],
            clone_reveal_body["nonce"],
        )
        == c1_key["commitment"]
    )
    # 承诺绑定的是新实验 ID：用源实验 ID 重算不应匹配
    assert (
        commitment_digest(
            source_id,
            clone_reveal_body["allocation_slot_id"],
            1,
            clone_reveal_body["condition"],
            clone_reveal_body["nonce"],
        )
        != c1_key["commitment"]
    )

    # 源实验进度不受克隆影响，两个实验相互独立
    source_progress = client.get(
        f"/experiments/{source_id}/subjects/s0/progress"
    ).json()
    assert source_progress["completed"] is True
    assert [r["condition"] for r in source_progress["reveals"]] == source_seq_s0

    ids = {e["experiment_id"] for e in client.get("/experiments").json()}
    assert {source_id, clone_id} <= ids
    # 源受试者名册未被克隆改写
    assert source_subjects == [f"s{i}" for i in range(6)]


def test_clone_same_subjects_and_seed_gives_same_assignment_but_fresh_nonces(
    client: TestClient,
) -> None:
    body, subjects = create_experiment(
        client,
        conditions=["A", "B", "C", "D"],
        subject_ids=[f"s{i}" for i in range(4)],
        seed=42,
    )
    source_id = body["experiment_id"]

    first_resp = clone_experiment(client, source_id, subjects, seed=7)
    second_resp = clone_experiment(client, source_id, subjects, seed=7)
    assert first_resp.status_code == 201, first_resp.text
    assert second_resp.status_code == 201, second_resp.text
    first, second = first_resp.json(), second_resp.json()
    assert first["experiment_id"] != second["experiment_id"]

    # 相同条件 / 名册 / 种子 → 相同的 Williams 分配（逐周期揭盲重建后一致）
    first_seqs = [
        reveal_sequence(client, first["experiment_id"], s, 4, "one-")
        for s in subjects
    ]
    second_seqs = [
        reveal_sequence(client, second["experiment_id"], s, 4, "two-")
        for s in subjects
    ]
    assert first_seqs == second_seqs

    # 但槽位与承诺仍然各自独立重新生成，绝不复用
    first_commitments = client.get(
        f"/experiments/{first['experiment_id']}/commitments"
    ).json()["commitments"]
    second_commitments = client.get(
        f"/experiments/{second['experiment_id']}/commitments"
    ).json()["commitments"]
    assert {c["allocation_slot_id"] for c in first_commitments}.isdisjoint(
        c["allocation_slot_id"] for c in second_commitments
    )
    assert {c["commitment"] for c in first_commitments}.isdisjoint(
        c["commitment"] for c in second_commitments
    )

    def first_nonce(eid: str, subject: str) -> str:
        out = client.post(
            f"/experiments/{eid}/advance",
            json={
                "subject_id": subject,
                "expected_period": 1,
                "request_id": f"n-{eid}-{subject}",
            },
        )
        assert out.status_code == 200, out.text
        return out.json()["nonce"]

    # 另取一对未揭盲的同种子克隆，比较各自周期 1 的 nonce
    third = clone_experiment(client, source_id, subjects, seed=7).json()
    fourth = clone_experiment(client, source_id, subjects, seed=7).json()
    assert third["experiment_id"] != fourth["experiment_id"]
    assert first_nonce(third["experiment_id"], subjects[0]) != first_nonce(
        fourth["experiment_id"], subjects[0]
    )


def test_clone_inherits_or_overrides_source_washout(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "clone-washout.db"))
    t0 = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)

    class Clock:
        def __init__(self) -> None:
            self.now = t0

        def __call__(self) -> datetime:
            return self.now

    clock = Clock()
    monkeypatch.setattr(db, "utc_now", clock)

    with TestClient(app) as local:
        created = local.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 1,
                "washout_seconds": 3600,
            },
        ).json()
        source_id = created["experiment_id"]

        # 省略 washout_seconds → 沿用源实验 3600 秒
        inherited = clone_experiment(local, source_id, ["i1", "i2"], seed=2)
        assert inherited.status_code == 201, inherited.text
        inherited_id = inherited.json()["experiment_id"]
        assert inherited.json()["washout_seconds"] == 3600

        # 显式传 0 → 覆盖为 0
        overridden = clone_experiment(
            local, source_id, ["o1", "o2"], seed=3, washout_seconds=0
        )
        assert overridden.status_code == 201, overridden.text
        overridden_id = overridden.json()["experiment_id"]
        assert overridden.json()["washout_seconds"] == 0

        # 显式传其他值也生效
        custom = clone_experiment(
            local, source_id, ["u1", "u2"], seed=4, washout_seconds=120
        )
        assert custom.status_code == 201, custom.text
        custom_id = custom.json()["experiment_id"]
        assert custom.json()["washout_seconds"] == 120

        # 继承洗脱期的克隆：周期 1 揭盲并完成后，立即推进周期 2 被门禁拦截
        assert local.post(
            f"/experiments/{inherited_id}/advance",
            json={
                "subject_id": "i1",
                "expected_period": 1,
                "request_id": "i-a1",
            },
        ).status_code == 200
        assert local.post(
            f"/experiments/{inherited_id}/complete",
            json={"subject_id": "i1", "period": 1, "request_id": "i-c1"},
        ).status_code == 200
        blocked = local.post(
            f"/experiments/{inherited_id}/advance",
            json={
                "subject_id": "i1",
                "expected_period": 2,
                "request_id": "i-a2",
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["reason"] == "washout_not_elapsed"
        assert blocked.json()["detail"]["retry_after_seconds"] == 3600

        # 覆盖为 0 的克隆：完成后立刻可推进
        assert local.post(
            f"/experiments/{overridden_id}/advance",
            json={
                "subject_id": "o1",
                "expected_period": 1,
                "request_id": "o-a1",
            },
        ).status_code == 200
        assert local.post(
            f"/experiments/{overridden_id}/complete",
            json={"subject_id": "o1", "period": 1, "request_id": "o-c1"},
        ).status_code == 200
        ok = local.post(
            f"/experiments/{overridden_id}/advance",
            json={
                "subject_id": "o1",
                "expected_period": 2,
                "request_id": "o-a2",
            },
        )
        assert ok.status_code == 200
        assert ok.json()["period"] == 2

        # 自定义 120 秒洗脱期的克隆门禁
        assert local.post(
            f"/experiments/{custom_id}/advance",
            json={
                "subject_id": "u1",
                "expected_period": 1,
                "request_id": "u-a1",
            },
        ).status_code == 200
        assert local.post(
            f"/experiments/{custom_id}/complete",
            json={"subject_id": "u1", "period": 1, "request_id": "u-c1"},
        ).status_code == 200
        blocked_custom = local.post(
            f"/experiments/{custom_id}/advance",
            json={
                "subject_id": "u1",
                "expected_period": 2,
                "request_id": "u-a2",
            },
        )
        assert blocked_custom.status_code == 409
        assert blocked_custom.json()["detail"]["retry_after_seconds"] == 120


def test_clone_subject_count_not_multiple_returns_422(client: TestClient) -> None:
    body, _ = create_experiment(
        client,
        conditions=["A", "B", "C"],
        subject_ids=[f"s{i}" for i in range(6)],
        seed=1,
    )
    source_id = body["experiment_id"]

    # 7 人：3 条件每组 6 人，不成整组
    resp = clone_experiment(client, source_id, [f"c{i}" for i in range(7)], seed=2)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["subject_count"] == 7
    assert detail["group_size"] == 6
    assert detail["nearest_acceptable"] == [6, 12]
    assert "sequence" not in resp.text

    # 不足一组
    resp = clone_experiment(client, source_id, ["c1"], seed=3)
    assert resp.status_code == 422
    assert resp.json()["detail"]["nearest_acceptable"] == [6]

    # 受试者数量合法（12 人 = 2 组）时成功
    ok = clone_experiment(client, source_id, [f"c{i}" for i in range(12)], seed=4)
    assert ok.status_code == 201, ok.text
    assert ok.json()["num_subjects"] == 12
    assert ok.json()["group_size"] == 6

    # 422 失败不创建新实验：列表里仍是源实验 + 一次成功克隆
    experiments = client.get("/experiments").json()
    assert [e for e in experiments if e["experiment_id"] == source_id][0][
        "num_subjects"
    ] == 6
    assert len(experiments) == 2


def test_clone_duplicate_or_blank_subjects_returns_422(client: TestClient) -> None:
    body, _ = create_experiment(
        client,
        conditions=["A", "B"],
        subject_ids=["s1", "s2"],
        seed=1,
    )
    source_id = body["experiment_id"]

    resp = clone_experiment(client, source_id, ["c1", "c1"], seed=2)
    assert resp.status_code == 422

    resp = clone_experiment(client, source_id, ["c1", "  "], seed=3)
    assert resp.status_code == 422


def test_clone_unknown_source_returns_404(client: TestClient) -> None:
    resp = clone_experiment(client, "missing-experiment", ["c1", "c2"], seed=1)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "experiment not found"}


def test_clone_payload_validation(client: TestClient) -> None:
    body, _ = create_experiment(
        client,
        conditions=["A", "B"],
        subject_ids=["s1", "s2"],
        seed=1,
    )
    source_id = body["experiment_id"]

    # 缺 subject_ids / seed
    assert client.post(
        f"/experiments/{source_id}/clone", json={"seed": 1}
    ).status_code == 422
    assert client.post(
        f"/experiments/{source_id}/clone", json={"subject_ids": ["c1", "c2"]}
    ).status_code == 422
    # 空受试者列表
    assert client.post(
        f"/experiments/{source_id}/clone",
        json={"subject_ids": [], "seed": 1},
    ).status_code == 422
    # 负数 washout
    assert client.post(
        f"/experiments/{source_id}/clone",
        json={"subject_ids": ["c1", "c2"], "seed": 1, "washout_seconds": -1},
    ).status_code == 422


def test_clone_from_paused_source_starts_active_and_source_stays_paused(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client,
        conditions=["A", "B"],
        subject_ids=["s1", "s2"],
        seed=1,
    )
    source_id = body["experiment_id"]
    assert pause_experiment(
        client, source_id, "pause-before-clone"
    ).status_code == 200

    resp = clone_experiment(client, source_id, ["c1", "c2"], seed=2)
    assert resp.status_code == 201, resp.text
    clone = resp.json()
    # 克隆不继承暂停状态：新实验从 active / 版本 0 开始
    assert clone["status"] == "active"
    assert clone["state_version"] == 0
    assert clone["experiment_id"] != source_id

    # 源实验仍处于暂停，克隆可正常揭盲
    assert client.get(f"/experiments/{source_id}").json()["status"] == "paused"
    assert (
        client.post(
            f"/experiments/{source_id}/advance",
            json={
                "subject_id": "s1",
                "expected_period": 1,
                "request_id": "src-a1",
            },
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/experiments/{clone['experiment_id']}/advance",
            json={
                "subject_id": "c1",
                "expected_period": 1,
                "request_id": "clone-a1",
            },
        ).status_code
        == 200
    )

    # 源实验只有自己的暂停审计，克隆不向其追加任何记录
    events = client.get(f"/experiments/{source_id}/state-events").json()["events"]
    assert [e["action"] for e in events] == ["pause"]


def test_clone_from_legacy_source_without_commitments_generates_fresh_ones(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "legacy-clone.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE experiments (
                id TEXT PRIMARY KEY,
                conditions TEXT NOT NULL,
                seed INTEGER NOT NULL,
                group_size INTEGER NOT NULL,
                num_subjects INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE assignments (
                experiment_id TEXT NOT NULL REFERENCES experiments(id),
                subject_id TEXT NOT NULL,
                sequence_index INTEGER NOT NULL,
                sequence TEXT NOT NULL,
                PRIMARY KEY (experiment_id, subject_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO experiments "
            "(id, conditions, seed, group_size, num_subjects, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-clone", '["A","B"]', 1, 2, 2, "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO assignments "
            "(experiment_id, subject_id, sequence_index, sequence) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-clone", "s1", 0, '["A","B"]'),
        )
        conn.execute(
            "INSERT INTO assignments "
            "(experiment_id, subject_id, sequence_index, sequence) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-clone", "s2", 1, '["B","A"]'),
        )

    with TestClient(app) as local:
        # 源实验是旧库实验，没有创建时承诺
        assert local.get("/experiments/legacy-clone/commitments").status_code == 409

        resp = local.post(
            "/experiments/legacy-clone/clone",
            json={"subject_ids": ["c1", "c2"], "seed": 5},
        )
        assert resp.status_code == 201, resp.text
        clone = resp.json()
        clone_id = clone["experiment_id"]
        assert clone["washout_seconds"] == 0

        # 克隆不背“旧实验”包袱：拥有全新创建时承诺
        commitments = local.get(
            f"/experiments/{clone_id}/commitments"
        ).json()["commitments"]
        assert len(commitments) == 4
        assert len({c["allocation_slot_id"] for c in commitments}) == 2

        # 克隆揭盲返回可核验的新 nonce
        revealed = local.post(
            f"/experiments/{clone_id}/advance",
            json={
                "subject_id": "c1",
                "expected_period": 1,
                "request_id": "clone-a1",
            },
        )
        assert revealed.status_code == 200
        revealed_body = revealed.json()
        key = {
            (c["subject_id"], c["period"]): c for c in commitments
        }[("c1", 1)]
        assert revealed_body["allocation_slot_id"] == key["allocation_slot_id"]
        assert (
            commitment_digest(
                clone_id,
                revealed_body["allocation_slot_id"],
                1,
                revealed_body["condition"],
                revealed_body["nonce"],
            )
            == key["commitment"]
        )

        # 源实验仍无承诺（克隆不回填源实验）
        assert local.get("/experiments/legacy-clone/commitments").status_code == 409


def test_clone_does_not_copy_emergency_unblind_or_replacement_state(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client,
        conditions=["A", "B"],
        subject_ids=["s1", "s2", "s3", "s4"],
        seed=1,
    )
    source_id = body["experiment_id"]

    # 源实验完成一次紧急揭盲
    assert client.post(
        f"/experiments/{source_id}/emergency-unblind",
        json={
            "subject_id": "s2",
            "approver_id": "alice",
            "operation_id": "em-1",
            "reason": "SAE",
        },
    ).status_code == 200
    assert client.post(
        f"/experiments/{source_id}/emergency-unblind",
        json={
            "subject_id": "s2",
            "approver_id": "bob",
            "operation_id": "em-2",
            "reason": "SAE",
        },
    ).status_code == 200

    # 源实验换号 s3 -> s3-new
    assert client.post(
        f"/experiments/{source_id}/subjects/s3/replace",
        json={
            "replacement_subject_id": "s3-new",
            "operation_id": "rep-1",
            "reason": "编号错误",
        },
    ).status_code == 200

    clone_subjects = ["c1", "c2", "c3", "c4"]
    resp = clone_experiment(client, source_id, clone_subjects, seed=8)
    assert resp.status_code == 201, resp.text
    clone_id = resp.json()["experiment_id"]

    # 克隆没有任何紧急揭盲审计
    events = client.get(
        f"/experiments/{clone_id}/emergency-unblind-events"
    ).json()["events"]
    assert events == []
    # 克隆没有任何换号审计 / 墓碑
    replacements = client.get(
        f"/experiments/{clone_id}/subject-replacements"
    ).json()["replacements"]
    assert replacements == []
    # 克隆名册内编号全部可查询进度（没有墓碑效应）
    for subject_id in clone_subjects:
        progress = client.get(
            f"/experiments/{clone_id}/subjects/{subject_id}/progress"
        )
        assert progress.status_code == 200


# ---------- 盲态分配承诺 ----------

def commitment_digest(
    experiment_id: str,
    allocation_slot_id: str,
    period: int,
    condition: str,
    nonce: str,
) -> str:
    payload = [experiment_id, allocation_slot_id, period, condition, nonce]
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_commitments_list_hides_conditions_and_unrevealed_nonces(
    client: TestClient,
) -> None:
    body, subject_ids = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    eid = body["experiment_id"]

    resp = client.get(f"/experiments/{eid}/commitments")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["experiment_id"] == eid
    commitments = payload["commitments"]
    assert len(commitments) == 4
    assert [
        (item["subject_id"], item["period"]) for item in commitments
    ] == [("s1", 1), ("s1", 2), ("s2", 1), ("s2", 2)]
    for item in commitments:
        assert set(item) == {
            "subject_id",
            "allocation_slot_id",
            "period",
            "commitment",
        }
        assert re.fullmatch(r"[0-9a-f]{32}", item["allocation_slot_id"])
        assert re.fullmatch(r"[0-9a-f]{64}", item["commitment"])
    slots = {item["allocation_slot_id"] for item in commitments}
    assert len(slots) == len(subject_ids)
    assert "nonce" not in resp.text
    assert '"condition"' not in resp.text


def test_advance_returns_nonce_that_verifies_creation_time_commitment(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client,
        conditions=["条件甲", "条件乙"],
        subject_ids=["受试者一", "受试者二"],
        seed=11,
    )
    eid = body["experiment_id"]
    commitments = client.get(
        f"/experiments/{eid}/commitments"
    ).json()["commitments"]
    by_key = {
        (item["subject_id"], item["period"]): item for item in commitments
    }

    advance_payload = {
        "subject_id": "受试者一",
        "expected_period": 1,
        "request_id": "reveal-1",
    }
    first = client.post(f"/experiments/{eid}/advance", json=advance_payload)
    assert first.status_code == 200
    revealed = first.json()
    assert revealed["allocation_slot_id"] == by_key[("受试者一", 1)][
        "allocation_slot_id"
    ]
    assert re.fullmatch(r"[0-9a-f]{32}", revealed["nonce"])
    expected_commitment = by_key[("受试者一", 1)]["commitment"]
    assert (
        commitment_digest(
            eid,
            revealed["allocation_slot_id"],
            1,
            revealed["condition"],
            revealed["nonce"],
        )
        == expected_commitment
    )

    # 未揭盲的周期 2 仍不能通过任何推进响应取得 nonce
    blocked = client.post(
        f"/experiments/{eid}/advance",
        json={
            "subject_id": "受试者一",
            "expected_period": 2,
            "request_id": "reveal-2",
        },
    )
    assert blocked.status_code == 409
    assert "nonce" not in blocked.text

    # 幂等重放返回同一个槽位与 nonce
    replay = client.post(f"/experiments/{eid}/advance", json=advance_payload)
    assert replay.status_code == 200
    assert replay.json() == revealed


def test_batch_advance_and_replay_return_commitment_nonces(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=4
    )
    eid = body["experiment_id"]
    commitments = {
        (item["subject_id"], item["period"]): item
        for item in client.get(f"/experiments/{eid}/commitments").json()[
            "commitments"
        ]
    }
    members = [
        {"subject_id": "s2", "expected_period": 1},
        {"subject_id": "s1", "expected_period": 1},
    ]

    first = client.post(
        f"/experiments/{eid}/advance-batch",
        json={"batch_id": "batch-commitment", "members": members},
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    for result in first_body["results"]:
        key = (result["subject_id"], result["period"])
        assert result["allocation_slot_id"] == commitments[key]["allocation_slot_id"]
        assert re.fullmatch(r"[0-9a-f]{32}", result["nonce"])
        assert (
            commitment_digest(
                eid,
                result["allocation_slot_id"],
                result["period"],
                result["condition"],
                result["nonce"],
            )
            == commitments[key]["commitment"]
        )

    replay = client.post(
        f"/experiments/{eid}/advance-batch",
        json={"batch_id": "batch-commitment", "members": members},
    )
    assert replay.status_code == 200
    assert replay.json() == first_body


def test_replacement_moves_slot_ownership_without_recomputing_commitments(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=2
    )
    eid = body["experiment_id"]
    before = client.get(f"/experiments/{eid}/commitments").json()["commitments"]
    original = [item for item in before if item["subject_id"] == "s1"]

    replaced = client.post(
        f"/experiments/{eid}/subjects/s1/replace",
        json={
            "replacement_subject_id": "s1-new",
            "operation_id": "rep-commitment",
            "reason": "编号录入错误",
        },
    )
    assert replaced.status_code == 200

    after = client.get(f"/experiments/{eid}/commitments").json()["commitments"]
    moved = [item for item in after if item["subject_id"] == "s1-new"]
    assert moved == [
        {
            **item,
            "subject_id": "s1-new",
        }
        for item in original
    ]
    assert not [item for item in after if item["subject_id"] == "s1"]

    revealed = client.post(
        f"/experiments/{eid}/advance",
        json={
            "subject_id": "s1-new",
            "expected_period": 1,
            "request_id": "new-slot-reveal",
        },
    )
    assert revealed.status_code == 200
    body_revealed = revealed.json()
    assert body_revealed["allocation_slot_id"] == moved[0]["allocation_slot_id"]
    assert (
        commitment_digest(
            eid,
            body_revealed["allocation_slot_id"],
            1,
            body_revealed["condition"],
            body_revealed["nonce"],
        )
        == moved[0]["commitment"]
    )

    # 原编号仍遵守墓碑语义，且不能查询其已迁移走的承诺归属
    assert client.get(
        f"/experiments/{eid}/subjects/s1/progress"
    ).status_code == 409


def test_commitments_unknown_experiment_returns_404(client: TestClient) -> None:
    assert client.get("/experiments/missing/commitments").status_code == 404


# ---------- 按周期受控揭盲 ----------

def test_advance_reveals_one_period_and_progresses(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B", "C"], subject_ids=[f"s{i}" for i in range(6)]
    )
    eid = body["experiment_id"]

    progress = client.get(f"/experiments/{eid}/subjects/s0/progress").json()
    assert progress["next_period"] == 1
    assert progress["num_periods"] == 3
    assert progress["washout_seconds"] == 0
    assert progress["completed"] is False
    assert progress["next_available_at"] is None
    assert progress["reveals"] == []

    first = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s0", "expected_period": 1, "request_id": "r1"},
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["period"] == 1
    assert first_body["condition"] in {"A", "B", "C"}
    assert first_body["request_id"] == "r1"
    assert "revealed_at" in first_body

    progress = client.get(f"/experiments/{eid}/subjects/s0/progress").json()
    assert progress["next_period"] == 2
    assert progress["next_available_at"] is None
    assert progress["reveals"] == [
        {
            "period": 1,
            "condition": first_body["condition"],
            "revealed_at": first_body["revealed_at"],
            "completed_at": None,
        }
    ]
    assert_no_sequence_leak(progress)


def test_stale_or_skipped_period_returns_409_and_reveals_nothing(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=3
    )
    eid = body["experiment_id"]

    # 跳过周期 1 直接请求周期 2
    resp = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 2, "request_id": "skip"},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "period_mismatch"
    assert detail["next_period"] == 1
    assert "condition" not in detail

    # 失败不推进、不留幂等记录：同一 request_id 在正确周期上仍可首次成功
    ok = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "skip"},
    )
    assert ok.status_code == 200

    # 周期 1 已揭盲后，过期请求周期 1 再次冲突
    stale = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "late"},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["next_period"] == 2

    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["next_period"] == 2
    assert len(progress["reveals"]) == 1


def test_same_request_id_retry_is_idempotent(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=9
    )
    eid = body["experiment_id"]
    payload = {
        "subject_id": "s1",
        "expected_period": 1,
        "request_id": "idem-1",
    }

    first = client.post(f"/experiments/{eid}/advance", json=payload)
    assert first.status_code == 200
    first_body = first.json()

    for _ in range(3):
        retry = client.post(f"/experiments/{eid}/advance", json=payload)
        assert retry.status_code == 200
        assert retry.json() == first_body

    # 重试不重复推进
    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["next_period"] == 2
    assert len(progress["reveals"]) == 1

    # 登记周期 1 完成（washout=0，完成后即可推进）
    done = complete_period(client, eid, "s1", 1, "c1")
    assert done.status_code == 200

    # 推进到周期 2 后，用旧 request_id 重放仍返回首次（周期 1）结果
    second = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 2, "request_id": "idem-2"},
    )
    assert second.status_code == 200
    assert second.json()["period"] == 2

    replay = client.post(f"/experiments/{eid}/advance", json=payload)
    assert replay.status_code == 200
    assert replay.json() == first_body


def test_request_id_reused_with_different_payload_returns_409(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=4
    )
    eid = body["experiment_id"]

    first = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "shared"},
    )
    assert first.status_code == 200

    # 同一 request_id 绑定不同受试者
    other_subject = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s2", "expected_period": 1, "request_id": "shared"},
    )
    assert other_subject.status_code == 409
    assert other_subject.json()["detail"]["reason"] == "request_id_conflict"

    # s2 未因此被推进或揭盲
    progress = client.get(f"/experiments/{eid}/subjects/s2/progress").json()
    assert progress["next_period"] == 1
    assert progress["reveals"] == []


def test_concurrent_advance_has_single_winner(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "conc.db"))
    with TestClient(app) as setup:
        created = setup.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 11,
            },
        ).json()
    eid = created["experiment_id"]

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker(i: int) -> None:
        # 每个线程使用独立 client（独立门户线程），共享同一个 SQLite 文件
        with TestClient(app) as local_client:
            barrier.wait()
            resp = local_client.post(
                f"/experiments/{eid}/advance",
                json={
                    "subject_id": "s1",
                    "expected_period": 1,
                    "request_id": f"race-{i}",
                },
            )
            with statuses_lock:
                statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses.count(200) == 1
    assert statuses.count(409) == n_threads - 1

    with TestClient(app) as verify:
        progress = verify.get(
            f"/experiments/{eid}/subjects/s1/progress"
        ).json()
    assert progress["next_period"] == 2
    assert len(progress["reveals"]) == 1


def test_advance_after_final_period_returns_409(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=8
    )
    eid = body["experiment_id"]

    sequence = reveal_sequence(client, eid, "s1", 2)
    assert len(sequence) == 2

    done = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 3, "request_id": "over"},
    )
    assert done.status_code == 409
    assert done.json()["detail"]["reason"] == "experiment_complete"
    assert done.json()["detail"]["next_period"] == 3

    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["completed"] is True
    assert progress["next_period"] == 3
    assert [r["period"] for r in progress["reveals"]] == [1, 2]


def test_advance_unknown_experiment_or_subject_returns_404(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=6
    )
    eid = body["experiment_id"]
    payload = {
        "subject_id": "ghost",
        "expected_period": 1,
        "request_id": "x",
    }

    assert client.post("/experiments/missing/advance", json=payload).status_code == 404
    assert client.post(f"/experiments/{eid}/advance", json=payload).status_code == 404
    assert client.get(f"/experiments/{eid}/subjects/ghost/progress").status_code == 404


def test_invalid_advance_payload_rejected(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=2
    )
    eid = body["experiment_id"]

    resp = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 0, "request_id": "r"},
    )
    assert resp.status_code == 422

    resp = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": ""},
    )
    assert resp.status_code == 422


# ---------- 周期完成与洗脱期门禁 ----------

def test_washout_configured_on_create_and_defaults_zero(client: TestClient) -> None:
    body, _ = create_experiment(
        client,
        conditions=["A", "B"],
        subject_ids=["s1", "s2"],
        washout_seconds=900,
    )
    assert body["washout_seconds"] == 900
    listing = client.get("/experiments").json()
    assert next(
        e for e in listing if e["experiment_id"] == body["experiment_id"]
    )["washout_seconds"] == 900

    default_body, _ = create_experiment(
        client,
        conditions=["A", "B"],
        subject_ids=["s3", "s4"],
        seed=3,
    )
    assert default_body["washout_seconds"] == 0

    # 负数洗脱期被拒绝
    resp = client.post(
        "/experiments",
        json={
            "conditions": ["A", "B"],
            "subject_ids": ["s5", "s6"],
            "seed": 1,
            "washout_seconds": -1,
        },
    )
    assert resp.status_code == 422


def test_complete_only_allowed_for_revealed_current_period(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=3
    )
    eid = body["experiment_id"]

    # 尚未揭盲任何周期
    early = complete_period(client, eid, "s1", 1, "early")
    assert early.status_code == 409
    detail = early.json()["detail"]
    assert detail["reason"] == "period_out_of_order"
    assert detail["current_period"] == 0

    first = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    )
    assert first.status_code == 200

    # 只能登记当前已揭盲周期，不能提前 / 补登
    skip = complete_period(client, eid, "s1", 2, "skip")
    assert skip.status_code == 409
    assert skip.json()["detail"]["reason"] == "period_out_of_order"
    assert skip.json()["detail"]["current_period"] == 1

    ok = complete_period(client, eid, "s1", 1, "c1")
    assert ok.status_code == 200
    completed_body = ok.json()
    assert completed_body["period"] == 1
    assert completed_body["request_id"] == "c1"
    assert "completed_at" in completed_body

    # 同一周期重复登记（换 request_id）→ 409，completed_at 不被改写
    again = complete_period(client, eid, "s1", 1, "c1-again")
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "period_already_completed"
    assert again.json()["detail"]["current_period"] == 1


def test_complete_request_id_is_idempotent_and_replays(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=9
    )
    eid = body["experiment_id"]
    client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    )

    payload = {"subject_id": "s1", "period": 1, "request_id": "done-1"}
    first = client.post(f"/experiments/{eid}/complete", json=payload)
    assert first.status_code == 200
    for _ in range(3):
        replay = client.post(f"/experiments/{eid}/complete", json=payload)
        assert replay.status_code == 200
        assert replay.json() == first.json()

    # 同一完成幂等键绑定不同周期 / 受试者 → 409
    other_period = client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s1", "period": 2, "request_id": "done-1"},
    )
    assert other_period.status_code == 409
    assert other_period.json()["detail"]["reason"] == "request_id_conflict"
    other_subject = client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s2", "period": 1, "request_id": "done-1"},
    )
    assert other_subject.status_code == 409

    # 推进与完成的 request_id 命名空间互不影响
    second_advance = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 2, "request_id": "a2"},
    )
    assert second_advance.status_code == 200


def test_advance_blocked_until_prior_period_completed(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=4
    )
    eid = body["experiment_id"]
    client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    )

    # washout=0 但上一周期尚未完成 → 409，且不泄露下一周期条件
    blocked = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 2, "request_id": "a2"},
    )
    assert blocked.status_code == 409
    detail = blocked.json()["detail"]
    assert detail["reason"] == "prior_period_not_completed"
    assert "condition" not in detail
    assert "next_period" not in detail

    # 门禁失败不消耗幂等键、不推进：登记完成后原 request_id 首次成功
    done = complete_period(client, eid, "s1", 1, "c1")
    assert done.status_code == 200
    ok = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 2, "request_id": "a2"},
    )
    assert ok.status_code == 200
    assert ok.json()["period"] == 2


def test_washout_gate_uses_server_time_and_returns_retry_after(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "washout.db"))
    t0 = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)

    class Clock:
        def __init__(self) -> None:
            self.now = t0

        def __call__(self) -> datetime:
            return self.now

    clock = Clock()
    monkeypatch.setattr(db, "utc_now", clock)

    with TestClient(app) as local:
        created = local.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 1,
                "washout_seconds": 3600,
            },
        ).json()
        eid = created["experiment_id"]

        first = local.post(
            f"/experiments/{eid}/advance",
            json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
        )
        assert first.status_code == 200

        clock.now += timedelta(seconds=120)
        done = local.post(
            f"/experiments/{eid}/complete",
            json={"subject_id": "s1", "period": 1, "request_id": "c1"},
        )
        assert done.status_code == 200
        completed_at = datetime.fromisoformat(done.json()["completed_at"])

        # 进度只给出下一周期最早可推进时间，不含下一周期条件
        progress = local.get(f"/experiments/{eid}/subjects/s1/progress").json()
        assert progress["washout_seconds"] == 3600
        assert progress["next_period"] == 2
        assert progress["next_available_at"] == (
            completed_at + timedelta(seconds=3600)
        ).isoformat()
        assert progress["reveals"][0]["completed_at"] == done.json()["completed_at"]
        assert_no_sequence_leak(progress)

        advance_payload = {
            "subject_id": "s1",
            "expected_period": 2,
            "request_id": "a2",
        }

        # 刚完成：洗脱期还差 3600 秒
        blocked = local.post(f"/experiments/{eid}/advance", json=advance_payload)
        assert blocked.status_code == 409
        detail = blocked.json()["detail"]
        assert detail["reason"] == "washout_not_elapsed"
        assert detail["retry_after_seconds"] == 3600
        assert "condition" not in detail
        assert "next_period" not in detail

        # 差 1 秒到期
        clock.now = completed_at + timedelta(seconds=3599)
        blocked = local.post(f"/experiments/{eid}/advance", json=advance_payload)
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["retry_after_seconds"] == 1

        # 到期瞬间放行；门禁期间被拒的幂等键仍可首次成功
        clock.now = completed_at + timedelta(seconds=3600)
        ok = local.post(f"/experiments/{eid}/advance", json=advance_payload)
        assert ok.status_code == 200
        assert ok.json()["period"] == 2

        progress = local.get(f"/experiments/{eid}/subjects/s1/progress").json()
        assert progress["next_available_at"] is None


def test_final_period_can_be_completed(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=8
    )
    eid = body["experiment_id"]
    reveal_sequence(client, eid, "s1", 2)

    final = complete_period(client, eid, "s1", 2, "c2")
    assert final.status_code == 200

    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["completed"] is True
    assert progress["next_period"] == 3
    assert progress["next_available_at"] is None
    assert [r["period"] for r in progress["reveals"]] == [1, 2]
    assert all(r["completed_at"] is not None for r in progress["reveals"])


def test_complete_unknown_experiment_or_subject_returns_404(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=6
    )
    eid = body["experiment_id"]
    payload = {"subject_id": "ghost", "period": 1, "request_id": "x"}

    assert client.post(
        "/experiments/missing/complete", json=payload
    ).status_code == 404
    assert client.post(
        f"/experiments/{eid}/complete", json=payload
    ).status_code == 404


def test_invalid_complete_payload_rejected(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=2
    )
    eid = body["experiment_id"]

    resp = client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s1", "period": 0, "request_id": "r"},
    )
    assert resp.status_code == 422
    resp = client.post(
        f"/experiments/{eid}/complete",
        json={"subject_id": "s1", "period": 1, "request_id": ""},
    )
    assert resp.status_code == 422


def test_concurrent_complete_has_single_winner(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "conc-complete.db"))
    with TestClient(app) as setup:
        created = setup.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 12,
            },
        ).json()
        setup.post(
            f"/experiments/{created['experiment_id']}/advance",
            json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
        )
    eid = created["experiment_id"]

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker(i: int) -> None:
        with TestClient(app) as local_client:
            barrier.wait()
            resp = local_client.post(
                f"/experiments/{eid}/complete",
                json={"subject_id": "s1", "period": 1, "request_id": f"c-{i}"},
            )
            with statuses_lock:
                statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses.count(200) == 1
    assert statuses.count(409) == n_threads - 1

    with TestClient(app) as verify:
        progress = verify.get(
            f"/experiments/{eid}/subjects/s1/progress"
        ).json()
    assert progress["next_period"] == 2
    assert progress["reveals"][0]["completed_at"] is not None
    # 完成只登记一次：完成后下一周期立刻可推进（washout=0）
    with TestClient(app) as verify:
        advanced = verify.post(
            f"/experiments/{eid}/advance",
            json={"subject_id": "s1", "expected_period": 2, "request_id": "a2"},
        )
    assert advanced.status_code == 200


def test_concurrent_advance_after_completion_has_single_winner(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "conc-advance.db"))
    with TestClient(app) as setup:
        created = setup.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 13,
            },
        ).json()
        eid = created["experiment_id"]
        setup.post(
            f"/experiments/{eid}/advance",
            json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
        )
        setup.post(
            f"/experiments/{eid}/complete",
            json={"subject_id": "s1", "period": 1, "request_id": "c1"},
        )

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker(i: int) -> None:
        with TestClient(app) as local_client:
            barrier.wait()
            resp = local_client.post(
                f"/experiments/{eid}/advance",
                json={
                    "subject_id": "s1",
                    "expected_period": 2,
                    "request_id": f"race2-{i}",
                },
            )
            with statuses_lock:
                statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses.count(200) == 1
    assert statuses.count(409) == n_threads - 1
    with TestClient(app) as verify:
        progress = verify.get(
            f"/experiments/{eid}/subjects/s1/progress"
        ).json()
    assert progress["next_period"] == 3
    assert len(progress["reveals"]) == 2


# ---------- 批量受控揭盲 ----------

def advance_batch(
    client: TestClient,
    experiment_id: str,
    members: list[dict],
    batch_id: str,
):
    return client.post(
        f"/experiments/{experiment_id}/advance-batch",
        json={"batch_id": batch_id, "members": members},
    )


def test_batch_advances_all_members_in_input_order(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    eid = body["experiment_id"]

    members = [
        {"subject_id": "s2", "expected_period": 1},
        {"subject_id": "s1", "expected_period": 1},
    ]
    resp = advance_batch(client, eid, members, "batch-1")
    assert resp.status_code == 200, resp.text
    batch = resp.json()
    assert batch["experiment_id"] == eid
    assert batch["batch_id"] == "batch-1"
    assert [r["subject_id"] for r in batch["results"]] == ["s2", "s1"]
    for result, member in zip(batch["results"], members):
        assert result["period"] == 1
        assert result["condition"] in {"A", "B"}
        assert result["request_id"].startswith("batch:batch-1:")
        assert "revealed_at" in result

    # 进度按整批推进
    for subject_id in ("s1", "s2"):
        progress = client.get(f"/experiments/{eid}/subjects/{subject_id}/progress").json()
        assert progress["next_period"] == 2
        assert len(progress["reveals"]) == 1
        assert progress["reveals"][0]["completed_at"] is None


def test_batch_allows_adjacent_periods_of_same_subject_in_order(
    client: TestClient,
) -> None:
    # 同一受试者的相邻周期可放在同一批中：周期 1 揭盲后需先完成才能揭盲周期 2，
    # 因此只有在之前已登记周期 1 完成时整批才通过
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    eid = body["experiment_id"]
    first = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    ).json()
    assert complete_period(client, eid, "s1", 1, "c1").status_code == 200

    members = [
        {"subject_id": "s1", "expected_period": 2},
        {"subject_id": "s2", "expected_period": 1},
    ]
    resp = advance_batch(client, eid, members, "batch-multi")
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert [r["subject_id"] for r in results] == ["s1", "s2"]
    assert [r["period"] for r in results] == [2, 1]
    # 顺序揭盲：s1 的周期 2 条件即其完整序列第二项，与历史一致
    assert results[0]["condition"] != first["condition"]


def test_batch_failure_is_atomic_and_lists_failures_without_conditions(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    eid = body["experiment_id"]
    # s1 先揭盲周期 1，但不登记完成
    assert client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    ).status_code == 200

    members = [
        # s2 周期 1：本应通过
        {"subject_id": "s2", "expected_period": 1},
        # s1 周期 2：上一周期未完成 → 拒绝
        {"subject_id": "s1", "expected_period": 2},
        # ghost：受试者不存在 → 拒绝
        {"subject_id": "ghost", "expected_period": 1},
    ]
    resp = advance_batch(client, eid, members, "batch-fail")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "batch_member_rejected"
    assert detail["batch_id"] == "batch-fail"
    failures = detail["failures"]
    assert {f["subject_id"]: f["reason"] for f in failures} == {
        "s1": "prior_period_not_completed",
        "ghost": "subject_not_found",
    }
    # 失败项保持输入顺序（s1 在 ghost 之前）
    assert [f["subject_id"] for f in failures] == ["s1", "ghost"]
    # 任何失败项都不得携带条件信息
    assert_no_sequence_leak(detail)

    # 整批零写入：s2 也未被揭盲
    progress = client.get(f"/experiments/{eid}/subjects/s2/progress").json()
    assert progress["next_period"] == 1
    assert progress["reveals"] == []
    # 失败不消耗 batch_id：修正成员后同 batch_id 首次成功
    ok = advance_batch(
        client,
        eid,
        [{"subject_id": "s2", "expected_period": 1}],
        "batch-fail",
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["results"][0]["subject_id"] == "s2"


def test_batch_reports_period_mismatch_and_experiment_complete(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=8
    )
    eid = body["experiment_id"]
    reveal_sequence(client, eid, "s1", 2)  # s1 已完成全部周期

    resp = advance_batch(
        client,
        eid,
        [
            {"subject_id": "s2", "expected_period": 2},   # 超前 → mismatch
            {"subject_id": "s1", "expected_period": 3},   # 已结束
        ],
        "batch-stale",
    )
    assert resp.status_code == 409
    reasons = {f["subject_id"]: f for f in resp.json()["detail"]["failures"]}
    assert reasons["s2"]["reason"] == "period_mismatch"
    assert reasons["s2"]["next_period"] == 1
    assert reasons["s1"]["reason"] == "experiment_complete"
    assert reasons["s1"]["next_period"] == 3
    assert_no_sequence_leak(resp.json())


def test_batch_washout_failure_returns_retry_after_and_zero_writes(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "batch-washout.db"))
    t0 = datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc)

    class Clock:
        def __init__(self) -> None:
            self.now = t0

        def __call__(self) -> datetime:
            return self.now

    clock = Clock()
    monkeypatch.setattr(db, "utc_now", clock)

    with TestClient(app) as local:
        created = local.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 1,
                "washout_seconds": 3600,
            },
        ).json()
        eid = created["experiment_id"]
        # s1 已揭盲周期 1，并在 2 分钟后登记完成（距洗脱期届满还差 3480 秒）
        local.post(
            f"/experiments/{eid}/advance",
            json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
        )
        clock.now += timedelta(seconds=120)
        assert local.post(
            f"/experiments/{eid}/complete",
            json={"subject_id": "s1", "period": 1, "request_id": "c1"},
        ).status_code == 200

        # 再走 240 秒：完成于 t0+120，门禁时刻 t0+360，距届满还差 3360 秒
        clock.now += timedelta(seconds=240)
        resp = advance_batch(
            local,
            eid,
            [
                {"subject_id": "s1", "expected_period": 2},
                {"subject_id": "s2", "expected_period": 1},
            ],
            "batch-w",
        )
        assert resp.status_code == 409
        failures = resp.json()["detail"]["failures"]
        assert len(failures) == 1
        assert failures[0]["subject_id"] == "s1"
        assert failures[0]["reason"] == "washout_not_elapsed"
        assert failures[0]["retry_after_seconds"] == 3360
        assert_no_sequence_leak(resp.json())

        # 零写入：s2 未揭盲、s1 仍停在周期 2
        progress_s2 = local.get(f"/experiments/{eid}/subjects/s2/progress").json()
        assert progress_s2["reveals"] == []

        # 到期后用同一 batch_id 首次成功（门禁失败不消耗幂等键）
        clock.now += timedelta(seconds=3360)
        ok = advance_batch(
            local,
            eid,
            [
                {"subject_id": "s1", "expected_period": 2},
                {"subject_id": "s2", "expected_period": 1},
            ],
            "batch-w",
        )
        assert ok.status_code == 200, ok.text
        assert [r["subject_id"] for r in ok.json()["results"]] == ["s1", "s2"]


def test_batch_same_batch_id_replays_first_result(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=9
    )
    eid = body["experiment_id"]
    members = [
        {"subject_id": "s1", "expected_period": 1},
        {"subject_id": "s2", "expected_period": 1},
    ]
    first = advance_batch(client, eid, members, "idem-batch")
    assert first.status_code == 200
    first_body = first.json()

    for _ in range(3):
        retry = advance_batch(client, eid, members, "idem-batch")
        assert retry.status_code == 200
        assert retry.json() == first_body

    # 重放不重复推进
    for subject_id in ("s1", "s2"):
        progress = client.get(f"/experiments/{eid}/subjects/{subject_id}/progress").json()
        assert progress["next_period"] == 2
        assert len(progress["reveals"]) == 1

    # 即使成员已推进到更后周期，重放仍返回首次结果，不重复揭盲
    assert complete_period(client, eid, "s1", 1, "c1").status_code == 200
    assert client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 2, "request_id": "a2"},
    ).status_code == 200
    replay = advance_batch(client, eid, members, "idem-batch")
    assert replay.status_code == 200
    assert replay.json() == first_body


def test_batch_same_batch_id_different_members_returns_409(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=4
    )
    eid = body["experiment_id"]

    first = advance_batch(
        client,
        eid,
        [{"subject_id": "s1", "expected_period": 1}],
        "shared-batch",
    )
    assert first.status_code == 200

    # 不同成员集合
    other_members = advance_batch(
        client,
        eid,
        [{"subject_id": "s2", "expected_period": 1}],
        "shared-batch",
    )
    assert other_members.status_code == 409
    detail = other_members.json()["detail"]
    assert detail["reason"] == "batch_id_conflict"
    assert detail["batch_id"] == "shared-batch"

    # 相同成员但周期不同
    other_period = advance_batch(
        client,
        eid,
        [{"subject_id": "s1", "expected_period": 2}],
        "shared-batch",
    )
    assert other_period.status_code == 409
    assert other_period.json()["detail"]["reason"] == "batch_id_conflict"

    # 相同成员但顺序不同也视为不同请求（使用全新实验避免与上面的状态叠加）
    body2, _ = create_experiment(
        client, conditions=["A", "B", "C", "D"],
        subject_ids=[f"t{i}" for i in range(8)], seed=7,
    )
    eid2 = body2["experiment_id"]
    two = [
        {"subject_id": "t1", "expected_period": 1},
        {"subject_id": "t2", "expected_period": 1},
    ]
    assert advance_batch(client, eid2, two, "ordered-batch").status_code == 200
    reordered = advance_batch(
        client, eid2, list(reversed(two)), "ordered-batch"
    )
    assert reordered.status_code == 409
    assert reordered.json()["detail"]["reason"] == "batch_id_conflict"

    # 冲突的失败路径零写入：t1 / t2 只保留首批各一条揭盲，未重复推进
    for subject_id in ("t1", "t2"):
        progress = client.get(
            f"/experiments/{eid2}/subjects/{subject_id}/progress"
        ).json()
        assert progress["next_period"] == 2
        assert len(progress["reveals"]) == 1


def test_batch_members_validation(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=2
    )
    eid = body["experiment_id"]

    # 空成员列表
    resp = advance_batch(client, eid, [], "empty")
    assert resp.status_code == 422

    # 重复的 subject_id / expected_period 对
    resp = advance_batch(
        client,
        eid,
        [
            {"subject_id": "s1", "expected_period": 1},
            {"subject_id": "s1", "expected_period": 1},
        ],
        "dup",
    )
    assert resp.status_code == 422

    # expected_period < 1 / batch_id 为空
    assert advance_batch(
        client, eid, [{"subject_id": "s1", "expected_period": 0}], "bad"
    ).status_code == 422
    assert advance_batch(
        client, eid, [{"subject_id": "s1", "expected_period": 1}], ""
    ).status_code == 422

    # 超过 100 个成员（受试者名册只建了 2 人，校验在入库前发生）
    too_many = [
        {"subject_id": f"u{i}", "expected_period": 1} for i in range(101)
    ]
    assert advance_batch(client, eid, too_many, "too-many").status_code == 422


def test_batch_boundary_size_of_100_accepted(client: TestClient) -> None:
    # 100 个互异成员（2 条件一组 2 人，100 为整组倍数）
    subject_ids = [f"u{i}" for i in range(100)]
    body, _ = create_experiment(
        client,
        conditions=["A", "B"],
        subject_ids=subject_ids,
        seed=1,
    )
    eid = body["experiment_id"]
    members = [{"subject_id": s, "expected_period": 1} for s in subject_ids]
    resp = advance_batch(client, eid, members, "batch-100")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["results"]) == 100
    assert [r["subject_id"] for r in resp.json()["results"]] == subject_ids


def test_batch_unknown_experiment_returns_404(client: TestClient) -> None:
    resp = advance_batch(
        client,
        "missing",
        [{"subject_id": "s1", "expected_period": 1}],
        "b",
    )
    assert resp.status_code == 404


def test_concurrent_batches_are_serialized(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "conc-batch.db"))
    with TestClient(app) as setup:
        created = setup.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 21,
            },
        ).json()
    eid = created["experiment_id"]

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker(i: int) -> None:
        with TestClient(app) as local_client:
            barrier.wait()
            resp = advance_batch(
                local_client,
                eid,
                [{"subject_id": "s1", "expected_period": 1}],
                f"batch-race-{i}",
            )
            with statuses_lock:
                statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # BEGIN IMMEDIATE 串行化：只有一批成功，其余看到 period_mismatch
    assert statuses.count(200) == 1
    assert statuses.count(409) == n_threads - 1
    with TestClient(app) as verify:
        progress = verify.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["next_period"] == 2
    assert len(progress["reveals"]) == 1


# ---------- 旧库启动时自动补齐表结构 ----------

def test_startup_migrates_legacy_database(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    # 模拟旧版本数据库：只有 experiments / assignments 两张表
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE experiments (
                id TEXT PRIMARY KEY,
                conditions TEXT NOT NULL,
                seed INTEGER NOT NULL,
                group_size INTEGER NOT NULL,
                num_subjects INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE assignments (
                experiment_id TEXT NOT NULL REFERENCES experiments(id),
                subject_id TEXT NOT NULL,
                sequence_index INTEGER NOT NULL,
                sequence TEXT NOT NULL,
                PRIMARY KEY (experiment_id, subject_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO experiments "
            "(id, conditions, seed, group_size, num_subjects, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-1", '["A","B","C"]', 1, 6, 6, "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO assignments "
            "(experiment_id, subject_id, sequence_index, sequence) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-1", "s0", 0, '["A","C","B"]'),
        )

    # 应用启动（lifespan）自动补齐表结构，旧数据仍可查询与揭盲
    with TestClient(app) as migrated:
        detail = migrated.get("/experiments/legacy-1").json()
        assert detail["num_subjects"] == 6
        # 旧实验缺少 washout_seconds 列：迁移后按 0 秒处理
        assert detail["washout_seconds"] == 0
        assert_no_sequence_leak(detail)

        progress = migrated.get(
            "/experiments/legacy-1/subjects/s0/progress"
        ).json()
        assert progress["next_period"] == 1
        assert progress["num_periods"] == 3
        assert progress["washout_seconds"] == 0
        assert progress["next_available_at"] is None
        assert progress["reveals"] == []

        commitments = migrated.get("/experiments/legacy-1/commitments")
        assert commitments.status_code == 409
        assert commitments.json()["detail"]["reason"] == "commitments_unavailable"

        resp = migrated.post(
            "/experiments/legacy-1/advance",
            json={
                "subject_id": "s0",
                "expected_period": 1,
                "request_id": "boot-1",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["condition"] == "A"
        assert "nonce" not in resp.json()
        assert "allocation_slot_id" not in resp.json()

        # 未完成上一周期不能推进；旧实验 washout=0，完成后立即放行
        blocked = migrated.post(
            "/experiments/legacy-1/advance",
            json={
                "subject_id": "s0",
                "expected_period": 2,
                "request_id": "boot-2",
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["reason"] == "prior_period_not_completed"

        done = migrated.post(
            "/experiments/legacy-1/complete",
            json={"subject_id": "s0", "period": 1, "request_id": "done-1"},
        )
        assert done.status_code == 200

        second = migrated.post(
            "/experiments/legacy-1/advance",
            json={
                "subject_id": "s0",
                "expected_period": 2,
                "request_id": "boot-2",
            },
        )
        assert second.status_code == 200
        assert second.json()["condition"] == "C"


def _create_legacy_db(db_path, columns: list[str] | None = None) -> None:
    """创建仅含旧版 experiments 表（默认无 washout / status / state_version）。"""
    columns = columns or [
        "id TEXT PRIMARY KEY",
        "conditions TEXT NOT NULL",
        "seed INTEGER NOT NULL",
        "group_size INTEGER NOT NULL",
        "num_subjects INTEGER NOT NULL",
        "created_at TEXT NOT NULL",
    ]
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"CREATE TABLE experiments ({', '.join(columns)})")
        conn.execute(
            "INSERT INTO experiments "
            "(id, conditions, seed, group_size, num_subjects, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-1", '["A","B"]', 1, 2, 2, "2026-01-01T00:00:00+00:00"),
        )


def test_init_db_concurrent_startups_serialize_migration(tmp_path, monkeypatch) -> None:
    """多个 API 进程同时启动迁移同一旧库时串行完成，无一因 duplicate column 失败。"""
    db_path = tmp_path / "legacy-race.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    _create_legacy_db(db_path)

    workers = 6
    barrier = threading.Barrier(workers)
    real_get_connection = db.get_connection

    def slow_connection():
        """让每个线程在 BEGIN IMMEDIATE 处汇合，并在持锁后停留，强行制造排队。"""
        conn = real_get_connection()

        class _ConnProxy:
            def __getattr__(self, name):
                return getattr(conn, name)

            def execute(self, sql, *args):
                if isinstance(sql, str) and sql.startswith("BEGIN IMMEDIATE"):
                    barrier.wait()
                    result = conn.execute(sql, *args)
                    time.sleep(0.2)  # 持有写锁，其余线程在此排队
                    return result
                return conn.execute(sql, *args)

        return _ConnProxy()

    monkeypatch.setattr(db, "get_connection", slow_connection)

    failures: list[BaseException] = []

    def migrate() -> None:
        try:
            db.init_db()
        except BaseException as exc:  # 捕获后在主线程断言，避免线程内静默
            failures.append(exc)

    threads = [threading.Thread(target=migrate) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []

    with sqlite3.connect(db_path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(experiments)")]
        assert columns == [
            "id",
            "conditions",
            "seed",
            "group_size",
            "num_subjects",
            "created_at",
            "washout_seconds",
            "status",
            "state_version",
        ]
        # 旧数据保留，旧库缺省值正确回填
        assert conn.execute(
            "SELECT washout_seconds, status, state_version "
            "FROM experiments WHERE id = 'legacy-1'"
        ).fetchone() == (0, "active", 0)


def test_init_db_failed_migration_leaves_no_half_migrated_schema(
    tmp_path, monkeypatch
) -> None:
    """迁移事务中途失败时整体回滚：不留下只补了部分列 / 表的半迁移结构。"""
    db_path = tmp_path / "legacy-rollback.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    _create_legacy_db(db_path)

    real_column_exists = db._column_exists

    def failing_check(conn, table, column):
        # washout / status 的 ALTER 已随事务发出后，在最后一列检查处模拟中断
        if table == "experiments" and column == "state_version":
            raise RuntimeError("simulated migration interruption")
        return real_column_exists(conn, table, column)

    monkeypatch.setattr(db, "_column_exists", failing_check)
    with pytest.raises(RuntimeError, match="simulated migration interruption"):
        db.init_db()

    with sqlite3.connect(db_path) as conn:
        # 已执行的 ALTER / CREATE TABLE 全部随事务回滚，旧库结构原样保留
        columns = [row[1] for row in conn.execute("PRAGMA table_info(experiments)")]
        assert columns == [
            "id",
            "conditions",
            "seed",
            "group_size",
            "num_subjects",
            "created_at",
        ]
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='enrollment_locks'"
        ).fetchone() is None
        assert conn.execute("SELECT seed FROM experiments").fetchone() == (1,)

    monkeypatch.setattr(db, "_column_exists", real_column_exists)
    # 中断后再次启动：旧库仍可完整迁移成功
    db.init_db()
    with sqlite3.connect(db_path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(experiments)")]
        assert columns[-3:] == ["washout_seconds", "status", "state_version"]
        assert conn.execute(
            "SELECT washout_seconds, status, state_version FROM experiments"
        ).fetchone() == (0, "active", 0)


def test_init_db_repeat_startup_does_not_change_schema(tmp_path, monkeypatch) -> None:
    """已迁移库重复启动不改变任何结构（建表 SQL 与列定义均保持一致）。"""
    db_path = tmp_path / "legacy-idempotent.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    _create_legacy_db(db_path)

    db.init_db()
    with sqlite3.connect(db_path) as conn:
        schema_before = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        columns_before = conn.execute("PRAGMA table_info(experiments)").fetchall()

    for _ in range(3):
        db.init_db()

    with sqlite3.connect(db_path) as conn:
        schema_after = conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        columns_after = conn.execute("PRAGMA table_info(experiments)").fetchall()

    assert schema_after == schema_before
    assert columns_after == columns_before


# ---------- 实验级暂停 / 恢复 ----------

def pause_experiment(
    client: TestClient,
    experiment_id: str,
    operation_id: str,
    reason: str = "suspension review",
):
    return client.post(
        f"/experiments/{experiment_id}/pause",
        json={"operation_id": operation_id, "reason": reason},
    )


def resume_experiment(
    client: TestClient,
    experiment_id: str,
    expected_version: int,
    operation_id: str | None = None,
    reason: str | None = None,
):
    body: dict = {"expected_version": expected_version}
    if operation_id is not None:
        body["operation_id"] = operation_id
    if reason is not None:
        body["reason"] = reason
    return client.post(
        f"/experiments/{experiment_id}/resume", json=body
    )


def test_new_experiment_is_active_at_version_zero(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    assert body["status"] == "active"
    assert body["state_version"] == 0

    detail = client.get(f"/experiments/{body['experiment_id']}").json()
    assert detail["status"] == "active"
    assert detail["state_version"] == 0
    entry = next(
        e for e in client.get("/experiments").json()
        if e["experiment_id"] == body["experiment_id"]
    )
    assert entry["status"] == "active"
    assert entry["state_version"] == 0
    assert_no_sequence_leak(detail)


def test_pause_and_resume_lifecycle(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]

    paused = pause_experiment(client, eid, "op-pause-1", "safety hold")
    assert paused.status_code == 200, paused.text
    event = paused.json()
    assert event["action"] == "pause"
    assert event["from_status"] == "active"
    assert event["to_status"] == "paused"
    assert event["state_version"] == 1
    assert event["operation_id"] == "op-pause-1"
    assert event["reason"] == "safety hold"
    assert "created_at" in event
    assert "id" in event

    detail = client.get(f"/experiments/{eid}").json()
    assert detail["status"] == "paused"
    assert detail["state_version"] == 1

    # 期望版本过期：compare-and-set 失败并回传服务端当前版本与状态
    stale = resume_experiment(client, eid, expected_version=0)
    assert stale.status_code == 409
    stale_detail = stale.json()["detail"]
    assert stale_detail["reason"] == "state_version_mismatch"
    assert stale_detail["state_version"] == 1
    assert stale_detail["current_status"] == "paused"
    assert stale_detail["expected_version"] == 0
    # 版本冲突不改变状态、不产生审计记录
    assert client.get(f"/experiments/{eid}").json()["status"] == "paused"

    resumed = resume_experiment(
        client, eid, expected_version=1, operation_id="op-resume-1",
        reason="review finished",
    )
    assert resumed.status_code == 200, resumed.text
    resume_event = resumed.json()
    assert resume_event["action"] == "resume"
    assert resume_event["from_status"] == "paused"
    assert resume_event["to_status"] == "active"
    assert resume_event["state_version"] == 2

    detail = client.get(f"/experiments/{eid}").json()
    assert detail["status"] == "active"
    assert detail["state_version"] == 2

    # 恢复后揭盲恢复正常
    ok = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    )
    assert ok.status_code == 200


def test_pause_is_idempotent_per_operation_id(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]
    payload = {"operation_id": "op-idem", "reason": "same reason"}

    first = client.post(f"/experiments/{eid}/pause", json=payload)
    assert first.status_code == 200
    for _ in range(3):
        replay = client.post(f"/experiments/{eid}/pause", json=payload)
        assert replay.status_code == 200
        assert replay.json() == first.json()

    # 重放不重复递增版本、不产生额外审计记录
    assert client.get(f"/experiments/{eid}").json()["state_version"] == 1
    events = client.get(f"/experiments/{eid}/state-events").json()["events"]
    assert len(events) == 1


def test_repeated_pause_with_new_operation_returns_409(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]
    assert pause_experiment(client, eid, "op-1").status_code == 200

    again = pause_experiment(client, eid, "op-2", "different hold")
    assert again.status_code == 409
    detail = again.json()["detail"]
    assert detail["reason"] == "already_paused"
    assert detail["state_version"] == 1
    assert detail["current_status"] == "paused"
    # 失败路径不写入审计记录
    events = client.get(f"/experiments/{eid}/state-events").json()["events"]
    assert [e["operation_id"] for e in events] == ["op-1"]


def test_pause_operation_id_conflicts_on_different_reason(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]
    first = pause_experiment(client, eid, "shared-op", "first reason")
    assert first.status_code == 200

    conflict = client.post(
        f"/experiments/{eid}/pause",
        json={"operation_id": "shared-op", "reason": "different reason"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["reason"] == "operation_id_conflict"
    assert conflict.json()["detail"]["operation_id"] == "shared-op"


def test_operation_id_namespace_is_shared_between_pause_and_resume(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]
    assert pause_experiment(client, eid, "shared-op").status_code == 200

    # 暂停的 operation_id 不能用于恢复
    conflict = resume_experiment(client, eid, 1, operation_id="shared-op")
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["reason"] == "operation_id_conflict"

    # 冲突不改变状态：正确版本恢复仍成功
    ok = resume_experiment(client, eid, 1, operation_id="resume-op")
    assert ok.status_code == 200

    # 恢复的 operation_id 也不能复用于再次暂停
    reused = pause_experiment(client, eid, "resume-op")
    assert reused.status_code == 409
    assert reused.json()["detail"]["reason"] == "operation_id_conflict"


def test_advance_and_batch_return_409_while_paused_without_leak(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]
    assert pause_experiment(client, eid, "hold").status_code == 200

    advance = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    )
    assert advance.status_code == 409
    detail = advance.json()["detail"]
    assert detail["reason"] == "experiment_paused"
    # 不泄露任何条件 / 后续安排
    assert "condition" not in detail
    assert "next_period" not in detail
    assert_no_sequence_leak(detail)

    batch = advance_batch(
        client,
        eid,
        [
            {"subject_id": "s1", "expected_period": 1},
            {"subject_id": "s2", "expected_period": 1},
        ],
        "b1",
    )
    assert batch.status_code == 409
    batch_detail = batch.json()["detail"]
    assert batch_detail["reason"] == "experiment_paused"
    assert "failures" not in batch_detail
    assert_no_sequence_leak(batch_detail)

    # 暂停期间零揭盲、零幂等消耗
    for subject_id in ("s1", "s2"):
        progress = client.get(
            f"/experiments/{eid}/subjects/{subject_id}/progress"
        ).json()
        assert progress["next_period"] == 1
        assert progress["reveals"] == []

    # 恢复后原 request_id / batch_id 均为首次成功（暂停 409 不消耗幂等键）
    assert resume_experiment(client, eid, 1).status_code == 200
    ok = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    )
    assert ok.status_code == 200
    ok_batch = advance_batch(
        client, eid, [{"subject_id": "s2", "expected_period": 1}], "b1"
    )
    assert ok_batch.status_code == 200


def test_successful_unblinding_replays_first_result_during_pause(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]

    first = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    )
    assert first.status_code == 200
    first_body = first.json()

    first_batch = advance_batch(
        client, eid, [{"subject_id": "s2", "expected_period": 1}], "b1"
    )
    assert first_batch.status_code == 200
    first_batch_body = first_batch.json()

    assert pause_experiment(client, eid, "hold").status_code == 200

    # 已有幂等成功的重放仍返回首次结果，不受暂停影响
    replay = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    )
    assert replay.status_code == 200
    assert replay.json() == first_body

    batch_replay = advance_batch(
        client, eid, [{"subject_id": "s2", "expected_period": 1}], "b1"
    )
    assert batch_replay.status_code == 200
    assert batch_replay.json() == first_batch_body

    # 重放不产生新的揭盲记录
    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert len(progress["reveals"]) == 1


def test_completion_remains_allowed_while_paused(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]
    assert client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    ).status_code == 200
    assert pause_experiment(client, eid, "hold").status_code == 200

    # 完成登记不是揭盲，暂停期间仍允许
    done = complete_period(client, eid, "s1", 1, "c1")
    assert done.status_code == 200

    # 但下一周期揭盲仍被门禁拦截
    blocked = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 2, "request_id": "a2"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["reason"] == "experiment_paused"


def test_resume_when_already_active_returns_409(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]

    # 运行中以版本 0 恢复：状态已是 active
    resp = resume_experiment(client, eid, 0)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "already_active"
    assert detail["state_version"] == 0
    assert detail["current_status"] == "active"
    assert client.get(f"/experiments/{eid}").json()["state_version"] == 0


def test_resume_with_operation_id_is_idempotent(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]
    assert pause_experiment(client, eid, "p1").status_code == 200

    payload = {"expected_version": 1, "operation_id": "r1", "reason": "ok"}
    first = client.post(f"/experiments/{eid}/resume", json=payload)
    assert first.status_code == 200
    # 恢复后（active, v2）重放仍返回首次结果，而非 409
    for _ in range(3):
        replay = client.post(f"/experiments/{eid}/resume", json=payload)
        assert replay.status_code == 200
        assert replay.json() == first.json()
    assert client.get(f"/experiments/{eid}").json()["state_version"] == 2

    # 相同 operation_id 但期望版本不同 → 冲突
    diff = client.post(
        f"/experiments/{eid}/resume",
        json={"expected_version": 2, "operation_id": "r1"},
    )
    assert diff.status_code == 409
    assert diff.json()["detail"]["reason"] == "operation_id_conflict"


def test_state_events_listed_in_reverse_chronological_order(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]

    assert pause_experiment(client, eid, "p1", "hold-1").status_code == 200
    assert resume_experiment(client, eid, 1, "r1").status_code == 200
    assert pause_experiment(client, eid, "p2", "hold-2").status_code == 200

    resp = client.get(f"/experiments/{eid}/state-events")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["experiment_id"] == eid
    events = payload["events"]
    # 最新在前（按时间倒序）
    assert [(e["action"], e["state_version"], e["reason"]) for e in events] == [
        ("pause", 3, "hold-2"),
        ("resume", 2, None),
        ("pause", 1, "hold-1"),
    ]
    assert all("created_at" in e for e in events)
    assert events[0]["id"] > events[1]["id"] > events[2]["id"]
    assert_no_sequence_leak(payload)

    # limit / offset 分页
    page1 = client.get(f"/experiments/{eid}/state-events?limit=2").json()["events"]
    assert [e["operation_id"] for e in page1] == ["p2", "r1"]
    page2 = client.get(
        f"/experiments/{eid}/state-events?limit=2&offset=2"
    ).json()["events"]
    assert [e["operation_id"] for e in page2] == ["p1"]

    # 非法分页参数
    assert client.get(
        f"/experiments/{eid}/state-events?limit=0"
    ).status_code == 422
    assert client.get(
        f"/experiments/{eid}/state-events?limit=501"
    ).status_code == 422
    assert client.get(
        f"/experiments/{eid}/state-events?offset=-1"
    ).status_code == 422

    # 没有任何状态变更的实验返回空列表
    fresh, _ = create_experiment(
        client,
        conditions=["A", "B"],
        subject_ids=["t1", "t2"],
        seed=2,
    )
    empty = client.get(
        f"/experiments/{fresh['experiment_id']}/state-events"
    ).json()
    assert empty["events"] == []


def test_pause_resume_payload_validation(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]

    assert client.post(
        f"/experiments/{eid}/pause",
        json={"operation_id": "", "reason": "x"},
    ).status_code == 422
    assert client.post(
        f"/experiments/{eid}/pause",
        json={"operation_id": "p", "reason": ""},
    ).status_code == 422
    assert client.post(
        f"/experiments/{eid}/pause", json={"operation_id": "p"}
    ).status_code == 422

    # 恢复：expected_version 必填且非负
    assert client.post(
        f"/experiments/{eid}/resume", json={}
    ).status_code == 422
    assert client.post(
        f"/experiments/{eid}/resume", json={"expected_version": -1}
    ).status_code == 422
    # 空字符串的 operation_id / reason 不合法
    assert client.post(
        f"/experiments/{eid}/resume",
        json={"expected_version": 0, "operation_id": ""},
    ).status_code == 422


def test_pause_resume_events_unknown_experiment_return_404(
    client: TestClient,
) -> None:
    assert client.post(
        "/experiments/missing/pause",
        json={"operation_id": "p", "reason": "x"},
    ).status_code == 404
    assert client.post(
        "/experiments/missing/resume", json={"expected_version": 1}
    ).status_code == 404
    assert client.get(
        "/experiments/missing/state-events"
    ).status_code == 404


def test_concurrent_resume_has_single_winner(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "conc-resume.db"))
    with TestClient(app) as setup:
        created = setup.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 31,
            },
        ).json()
        eid = created["experiment_id"]
        assert setup.post(
            f"/experiments/{eid}/pause",
            json={"operation_id": "p1", "reason": "hold"},
        ).status_code == 200

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker(i: int) -> None:
        with TestClient(app) as local_client:
            barrier.wait()
            resp = local_client.post(
                f"/experiments/{eid}/resume",
                json={"expected_version": 1, "operation_id": f"r-{i}"},
            )
            with statuses_lock:
                statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # BEGIN IMMEDIATE + 版本 compare-and-set：只有一次恢复成功
    assert statuses.count(200) == 1
    assert statuses.count(409) == n_threads - 1

    with TestClient(app) as verify:
        detail = verify.get(f"/experiments/{eid}").json()
        assert detail["status"] == "active"
        assert detail["state_version"] == 2
        events = verify.get(f"/experiments/{eid}/state-events").json()["events"]
    # 只有 pause + 一次 resume 两条审计记录
    assert [(e["action"], e["state_version"]) for e in events] == [
        ("resume", 2),
        ("pause", 1),
    ]


def test_legacy_database_defaults_to_active_version_zero(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "legacy-pause.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE experiments (
                id TEXT PRIMARY KEY,
                conditions TEXT NOT NULL,
                seed INTEGER NOT NULL,
                group_size INTEGER NOT NULL,
                num_subjects INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE assignments (
                experiment_id TEXT NOT NULL REFERENCES experiments(id),
                subject_id TEXT NOT NULL,
                sequence_index INTEGER NOT NULL,
                sequence TEXT NOT NULL,
                PRIMARY KEY (experiment_id, subject_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO experiments "
            "(id, conditions, seed, group_size, num_subjects, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-pause", '["A","B"]', 1, 2, 2, "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO assignments "
            "(experiment_id, subject_id, sequence_index, sequence) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-pause", "s1", 0, '["A","B"]'),
        )
        conn.execute(
            "INSERT INTO assignments "
            "(experiment_id, subject_id, sequence_index, sequence) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-pause", "s2", 1, '["B","A"]'),
        )

    with TestClient(app) as migrated:
        detail = migrated.get("/experiments/legacy-pause").json()
        # 旧实验迁移后按“运行中、版本 0”处理
        assert detail["status"] == "active"
        assert detail["state_version"] == 0

        # 暂停 / 恢复完整链路在迁移库上可用
        assert migrated.post(
            "/experiments/legacy-pause/pause",
            json={"operation_id": "p1", "reason": "hold legacy"},
        ).status_code == 200
        blocked = migrated.post(
            "/experiments/legacy-pause/advance",
            json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["reason"] == "experiment_paused"

        assert migrated.post(
            "/experiments/legacy-pause/resume",
            json={"expected_version": 1},
        ).status_code == 200
        assert migrated.post(
            "/experiments/legacy-pause/advance",
            json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
        ).status_code == 200


# ---------- 批量状态切换（跨实验暂停 / 恢复） ----------

def state_batch(
    client: TestClient,
    batch_operation_id: str,
    operations: list[dict],
):
    return client.post(
        "/experiments/state-batch",
        json={
            "batch_operation_id": batch_operation_id,
            "operations": operations,
        },
    )


def transition_op(
    experiment_id: str,
    action: str,
    expected_version: int,
    operation_id: str,
    reason: str | None = None,
) -> dict:
    op = {
        "experiment_id": experiment_id,
        "action": action,
        "expected_version": expected_version,
        "operation_id": operation_id,
    }
    if reason is not None:
        op["reason"] = reason
    return op


def create_two_experiments(client: TestClient) -> tuple[str, str]:
    first, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    second, _ = create_experiment(
        client,
        conditions=["A", "B", "C"],
        subject_ids=[f"t{i}" for i in range(6)],
        seed=2,
    )
    return first["experiment_id"], second["experiment_id"]


def test_state_batch_pauses_multiple_experiments_in_one_transaction(
    client: TestClient,
) -> None:
    eid1, eid2 = create_two_experiments(client)

    operations = [
        transition_op(eid2, "pause", 0, "b-op-2", "hold-2"),
        transition_op(eid1, "pause", 0, "b-op-1", "hold-1"),
    ]
    resp = state_batch(client, "sb-1", operations)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["batch_operation_id"] == "sb-1"
    # 结果严格按输入顺序返回
    assert [r["experiment_id"] for r in body["results"]] == [eid2, eid1]
    assert [r["operation_id"] for r in body["results"]] == ["b-op-2", "b-op-1"]
    for result, reason in zip(body["results"], ["hold-2", "hold-1"]):
        assert result["action"] == "pause"
        assert result["from_status"] == "active"
        assert result["to_status"] == "paused"
        assert result["state_version"] == 1
        assert result["reason"] == reason
        assert "id" in result
        assert "created_at" in result
    assert_no_sequence_leak(body)

    # 两个实验都已暂停，审计各一条，揭盲被门禁拦截
    for eid, subject_id in ((eid1, "s1"), (eid2, "t0")):
        detail = client.get(f"/experiments/{eid}").json()
        assert detail["status"] == "paused"
        assert detail["state_version"] == 1
        events = client.get(f"/experiments/{eid}/state-events").json()["events"]
        assert len(events) == 1
        blocked = client.post(
            f"/experiments/{eid}/advance",
            json={
                "subject_id": subject_id,
                "expected_period": 1,
                "request_id": "a1",
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["reason"] == "experiment_paused"


def test_state_batch_mixed_pause_and_resume(client: TestClient) -> None:
    eid1, eid2 = create_two_experiments(client)
    # eid2 先暂停到版本 1
    assert pause_experiment(client, eid2, "pre-pause", "hold").status_code == 200

    resp = state_batch(
        client,
        "sb-mixed",
        [
            transition_op(eid1, "pause", 0, "m-op-1"),
            transition_op(eid2, "resume", 1, "m-op-2", "review ok"),
        ],
    )
    assert resp.status_code == 200, resp.text
    results = {r["experiment_id"]: r for r in resp.json()["results"]}
    assert results[eid1]["action"] == "pause"
    assert results[eid1]["to_status"] == "paused"
    assert results[eid1]["state_version"] == 1
    assert results[eid1]["reason"] is None
    assert results[eid2]["action"] == "resume"
    assert results[eid2]["from_status"] == "paused"
    assert results[eid2]["to_status"] == "active"
    assert results[eid2]["state_version"] == 2
    assert results[eid2]["reason"] == "review ok"

    assert client.get(f"/experiments/{eid1}").json()["status"] == "paused"
    detail2 = client.get(f"/experiments/{eid2}").json()
    assert detail2["status"] == "active"
    assert detail2["state_version"] == 2
    # 审计按时间倒序：批量恢复在前，此前的暂停在后
    events = client.get(f"/experiments/{eid2}/state-events").json()["events"]
    assert [(e["action"], e["state_version"]) for e in events] == [
        ("resume", 2),
        ("pause", 1),
    ]

    # 恢复后 eid2 揭盲恢复正常
    ok = client.post(
        f"/experiments/{eid2}/advance",
        json={"subject_id": "t0", "expected_period": 1, "request_id": "a1"},
    )
    assert ok.status_code == 200


def test_state_batch_any_conflict_rolls_back_entire_batch(
    client: TestClient,
) -> None:
    eid1, eid2 = create_two_experiments(client)
    # eid2 已处于暂停（版本 1），批量中再次暂停必然冲突
    assert pause_experiment(client, eid2, "pre", "hold").status_code == 200

    resp = state_batch(
        client,
        "sb-rollback",
        [
            transition_op(eid1, "pause", 0, "rb-1"),
            transition_op(eid2, "pause", 1, "rb-2"),
        ],
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "batch_operation_rejected"
    assert detail["batch_operation_id"] == "sb-rollback"
    assert detail["failures"] == [
        {
            "experiment_id": eid2,
            "operation_id": "rb-2",
            "reason": "already_paused",
            "current_status": "paused",
            "state_version": 1,
            "expected_version": 1,
        }
    ]
    assert_no_sequence_leak(detail)

    # 整批回滚：eid1 未暂停、版本未变、无审计记录
    detail1 = client.get(f"/experiments/{eid1}").json()
    assert detail1["status"] == "active"
    assert detail1["state_version"] == 0
    assert client.get(f"/experiments/{eid1}/state-events").json()["events"] == []
    # eid2 保持暂停，仍只有最初一条审计
    events2 = client.get(f"/experiments/{eid2}/state-events").json()["events"]
    assert [e["operation_id"] for e in events2] == ["pre"]

    # 失败不消耗 batch_operation_id：修正操作列表后同键首次成功
    ok = state_batch(
        client, "sb-rollback", [transition_op(eid1, "pause", 0, "rb-1")]
    )
    assert ok.status_code == 200, ok.text
    assert client.get(f"/experiments/{eid1}").json()["status"] == "paused"


def test_state_batch_version_mismatch_rolls_back(client: TestClient) -> None:
    eid1, eid2 = create_two_experiments(client)

    resp = state_batch(
        client,
        "sb-version",
        [
            transition_op(eid1, "pause", 0, "v-1"),
            transition_op(eid2, "pause", 5, "v-2"),  # 期望版本过期
        ],
    )
    assert resp.status_code == 409
    (failure,) = resp.json()["detail"]["failures"]
    assert failure["experiment_id"] == eid2
    assert failure["operation_id"] == "v-2"
    assert failure["reason"] == "state_version_mismatch"
    assert failure["current_status"] == "active"
    assert failure["state_version"] == 0
    assert failure["expected_version"] == 5

    # 零写入：两个实验都保持 active / 版本 0
    for eid in (eid1, eid2):
        detail = client.get(f"/experiments/{eid}").json()
        assert detail["status"] == "active"
        assert detail["state_version"] == 0
        assert client.get(f"/experiments/{eid}/state-events").json()["events"] == []


def test_state_batch_collects_all_failures_in_input_order(
    client: TestClient,
) -> None:
    eid1, _ = create_two_experiments(client)

    resp = state_batch(
        client,
        "sb-failures",
        [
            transition_op("missing-exp", "pause", 0, "f-1"),
            transition_op(eid1, "resume", 0, "f-2"),  # 运行中不能恢复
        ],
    )
    assert resp.status_code == 409
    failures = resp.json()["detail"]["failures"]
    assert [f["experiment_id"] for f in failures] == ["missing-exp", eid1]
    assert [f["reason"] for f in failures] == [
        "experiment_not_found",
        "already_active",
    ]
    assert failures[0] == {
        "experiment_id": "missing-exp",
        "operation_id": "f-1",
        "reason": "experiment_not_found",
    }
    assert failures[1]["current_status"] == "active"
    assert failures[1]["state_version"] == 0

    # 零写入
    assert client.get(f"/experiments/{eid1}").json()["state_version"] == 0
    assert client.get(f"/experiments/{eid1}/state-events").json()["events"] == []


def test_state_batch_same_batch_operation_id_replays_first_result(
    client: TestClient,
) -> None:
    eid1, eid2 = create_two_experiments(client)
    operations = [
        transition_op(eid1, "pause", 0, "idem-op-1", "hold-1"),
        transition_op(eid2, "pause", 0, "idem-op-2", "hold-2"),
    ]

    first = state_batch(client, "sb-idem", operations)
    assert first.status_code == 200
    first_body = first.json()

    for _ in range(3):
        replay = state_batch(client, "sb-idem", operations)
        assert replay.status_code == 200
        assert replay.json() == first_body

    # 重放不重复递增版本、不重复写审计
    for eid in (eid1, eid2):
        assert client.get(f"/experiments/{eid}").json()["state_version"] == 1
        assert len(client.get(f"/experiments/{eid}/state-events").json()["events"]) == 1

    # 同一 batch_operation_id 绑定不同操作列表 → 409
    subset = state_batch(client, "sb-idem", operations[:1])
    assert subset.status_code == 409
    detail = subset.json()["detail"]
    assert detail["reason"] == "batch_operation_id_conflict"
    assert detail["batch_operation_id"] == "sb-idem"

    # 单项字段不同（如期望版本）也视为不同请求
    changed = [operations[0], transition_op(eid2, "pause", 1, "idem-op-2", "hold-2")]
    assert state_batch(client, "sb-idem", changed).status_code == 409

    # 冲突路径零写入：状态与审计保持不变
    for eid in (eid1, eid2):
        assert client.get(f"/experiments/{eid}").json()["state_version"] == 1
        assert len(client.get(f"/experiments/{eid}/state-events").json()["events"]) == 1


def test_state_batch_item_operation_id_conflict_rolls_back(
    client: TestClient,
) -> None:
    eid1, eid2 = create_two_experiments(client)
    # 单条暂停已占用 eid1 的 used-op
    assert pause_experiment(client, eid1, "used-op", "first hold").status_code == 200

    resp = state_batch(
        client,
        "sb-opconflict",
        [
            transition_op(eid2, "pause", 0, "fresh-op"),
            # operation_id 已被占用且原因不同 → 冲突
            transition_op(eid1, "pause", 0, "used-op", "different reason"),
        ],
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["failures"] == [
        {
            "experiment_id": eid1,
            "operation_id": "used-op",
            "reason": "operation_id_conflict",
        }
    ]

    # 整批回滚：eid2 未暂停、无审计
    detail2 = client.get(f"/experiments/{eid2}").json()
    assert detail2["status"] == "active"
    assert detail2["state_version"] == 0
    assert client.get(f"/experiments/{eid2}/state-events").json()["events"] == []


def test_state_batch_replays_matching_existing_operations(
    client: TestClient,
) -> None:
    eid1, eid2 = create_two_experiments(client)
    paused = pause_experiment(client, eid1, "shared-op", "same reason")
    assert paused.status_code == 200
    first_event = paused.json()

    # 新批次引用已存在且参数完全一致的操作：该项按幂等重放，其余项正常执行
    operations = [
        transition_op(eid1, "pause", 0, "shared-op", "same reason"),
        transition_op(eid2, "pause", 0, "new-op"),
    ]
    resp = state_batch(client, "sb-replay", operations)
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert results[0] == first_event  # 重放首次审计事件，不重复执行
    assert results[1]["operation_id"] == "new-op"
    assert results[1]["to_status"] == "paused"

    # 重放项不重复写审计、不重复递增版本；新项正常落库
    assert client.get(f"/experiments/{eid1}").json()["state_version"] == 1
    assert len(client.get(f"/experiments/{eid1}/state-events").json()["events"]) == 1
    assert client.get(f"/experiments/{eid2}").json()["status"] == "paused"

    # 该批次自身也可整体幂等重放
    replay = state_batch(client, "sb-replay", operations)
    assert replay.status_code == 200
    assert replay.json() == resp.json()


def test_state_batch_payload_validation(client: TestClient) -> None:
    eid1, _ = create_two_experiments(client)
    op = transition_op(eid1, "pause", 0, "v-op")

    # 空操作列表 / 空 batch_operation_id
    assert state_batch(client, "sb-v", []).status_code == 422
    assert state_batch(client, "", [op]).status_code == 422
    # 同一实验在同一批中重复出现
    assert state_batch(
        client, "sb-v", [op, transition_op(eid1, "resume", 0, "v-op-2")]
    ).status_code == 422
    # 非法动作 / 负版本 / 空 operation_id / 空 reason / 空 experiment_id
    assert state_batch(
        client, "sb-v", [transition_op(eid1, "stop", 0, "v-op")]
    ).status_code == 422
    assert state_batch(
        client, "sb-v", [transition_op(eid1, "pause", -1, "v-op")]
    ).status_code == 422
    assert state_batch(
        client, "sb-v", [transition_op(eid1, "pause", 0, "")]
    ).status_code == 422
    assert state_batch(
        client, "sb-v", [transition_op(eid1, "pause", 0, "v-op", "")]
    ).status_code == 422
    assert state_batch(
        client, "sb-v", [transition_op("", "pause", 0, "v-op")]
    ).status_code == 422
    # 超过 100 项（校验在入库前发生）
    too_many = [
        transition_op(f"e{i}", "pause", 0, f"v-op-{i}") for i in range(101)
    ]
    assert state_batch(client, "sb-v", too_many).status_code == 422


def test_concurrent_state_batches_have_single_winner(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "conc-state-batch.db"))
    with TestClient(app) as setup:
        created = [
            setup.post(
                "/experiments",
                json={
                    "conditions": ["A", "B"],
                    "subject_ids": [f"s{k}", f"t{k}"],
                    "seed": 40 + k,
                },
            ).json()
            for k in range(2)
        ]
    eids = [c["experiment_id"] for c in created]

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    statuses: list[int] = []
    statuses_lock = threading.Lock()

    def worker(i: int) -> None:
        with TestClient(app) as local_client:
            barrier.wait()
            resp = state_batch(
                local_client,
                f"sb-race-{i}",
                [
                    transition_op(eid, "pause", 0, f"race-{i}-{k}")
                    for k, eid in enumerate(eids)
                ],
            )
            with statuses_lock:
                statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # BEGIN IMMEDIATE 串行化：只有一批通过全部校验，其余看到版本已变而 409
    assert statuses.count(200) == 1
    assert statuses.count(409) == n_threads - 1

    with TestClient(app) as verify:
        for eid in eids:
            detail = verify.get(f"/experiments/{eid}").json()
            assert detail["status"] == "paused"
            assert detail["state_version"] == 1
            events = verify.get(f"/experiments/{eid}/state-events").json()["events"]
            assert len(events) == 1


def test_legacy_database_supports_state_batch_after_migration(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "legacy-state-batch.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    # 模拟旧版本数据库：只有 experiments / assignments 两张表
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE experiments (
                id TEXT PRIMARY KEY,
                conditions TEXT NOT NULL,
                seed INTEGER NOT NULL,
                group_size INTEGER NOT NULL,
                num_subjects INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE assignments (
                experiment_id TEXT NOT NULL REFERENCES experiments(id),
                subject_id TEXT NOT NULL,
                sequence_index INTEGER NOT NULL,
                sequence TEXT NOT NULL,
                PRIMARY KEY (experiment_id, subject_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO experiments "
            "(id, conditions, seed, group_size, num_subjects, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-sb", '["A","B"]', 1, 2, 2, "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO assignments "
            "(experiment_id, subject_id, sequence_index, sequence) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-sb", "s1", 0, '["A","B"]'),
        )
        conn.execute(
            "INSERT INTO assignments "
            "(experiment_id, subject_id, sequence_index, sequence) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-sb", "s2", 1, '["B","A"]'),
        )

    # 启动自动迁移后，批量状态切换在旧库上可用（旧实验按 active / 版本 0）
    with TestClient(app) as migrated:
        operations = [transition_op("legacy-sb", "pause", 0, "lg-1", "hold")]
        resp = state_batch(migrated, "sb-legacy", operations)
        assert resp.status_code == 200, resp.text
        assert resp.json()["results"][0]["state_version"] == 1
        detail = migrated.get("/experiments/legacy-sb").json()
        assert detail["status"] == "paused"
        assert detail["state_version"] == 1

        # 幂等重放返回首次结果
        replay = state_batch(migrated, "sb-legacy", operations)
        assert replay.status_code == 200
        assert replay.json() == resp.json()

        # 批量恢复同样可用
        resumed = state_batch(
            migrated,
            "sb-legacy-2",
            [transition_op("legacy-sb", "resume", 1, "lg-2")],
        )
        assert resumed.status_code == 200
        assert migrated.get("/experiments/legacy-sb").json()["status"] == "active"


# ---------- 实验级紧急揭盲（双人审批 + 15 分钟授权窗口） ----------

def emergency_unblind(
    client: TestClient,
    experiment_id: str,
    subject_id: str,
    approver_id: str,
    operation_id: str,
    reason: str = "SAE 复核",
):
    return client.post(
        f"/experiments/{experiment_id}/emergency-unblind",
        json={
            "subject_id": subject_id,
            "approver_id": approver_id,
            "operation_id": operation_id,
            "reason": reason,
        },
    )


def test_emergency_unblind_dual_approval_returns_full_sequence_once(
    client: TestClient,
) -> None:
    body, subject_ids = create_experiment(
        client,
        conditions=["A", "B", "C"],
        subject_ids=[f"s{i}" for i in range(6)],
        seed=7,
    )
    eid = body["experiment_id"]

    # 首个审批人开启 15 分钟授权窗口：响应不含任何条件内容
    first = emergency_unblind(client, eid, "s0", "alice", "eu-1")
    assert first.status_code == 200, first.text
    opened = first.json()
    assert opened["status"] == "pending_second_approval"
    assert opened["experiment_id"] == eid
    assert opened["subject_id"] == "s0"
    assert opened["operation_id"] == "eu-1"
    assert opened["first_approver_id"] == "alice"
    assert "sequence" not in opened
    assert "second_approver_id" not in opened
    assert "revealed_at" not in opened
    assert_no_sequence_leak(opened)
    opened_at = datetime.fromisoformat(opened["opened_at"])
    expires_at = datetime.fromisoformat(opened["window_expires_at"])
    assert (expires_at - opened_at).total_seconds() == 15 * 60

    # 窗口内第二个不同审批人批准：一次性返回完整条件序列
    second = emergency_unblind(client, eid, "s0", "bob", "eu-2")
    assert second.status_code == 200, second.text
    unblinded = second.json()
    assert unblinded["status"] == "unblinded"
    assert unblinded["first_approver_id"] == "alice"
    assert unblinded["second_approver_id"] == "bob"
    assert unblinded["opened_at"] == opened["opened_at"]
    assert unblinded["window_expires_at"] == opened["window_expires_at"]
    assert "revealed_at" in unblinded
    sequence = unblinded["sequence"]
    assert len(sequence) == 3
    assert sorted(sequence) == ["A", "B", "C"]

    # 与逐周期推进重建的序列一致：紧急揭盲返回的是真实分配结果
    assert sequence == reveal_sequence(client, eid, "s0", 3)

    # 其他受试者不受影响
    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["reveals"] == []


def test_emergency_unblind_first_approval_reveals_nothing(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]

    resp = emergency_unblind(client, eid, "s1", "alice", "eu-open")
    assert resp.status_code == 200
    assert_no_sequence_leak(resp.json())

    # 紧急揭盲是带外流程：不改变按周期推进的进度
    progress = client.get(f"/experiments/{eid}/subjects/s1/progress").json()
    assert progress["next_period"] == 1
    assert progress["reveals"] == []


def test_emergency_unblind_same_approver_cannot_complete(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]
    assert emergency_unblind(client, eid, "s1", "alice", "eu-1").status_code == 200

    # 同一审批人重复（新 operation_id）：409，不泄露序列
    dup = emergency_unblind(client, eid, "s1", "alice", "eu-2")
    assert dup.status_code == 409
    detail = dup.json()["detail"]
    assert detail["reason"] == "same_approver"
    assert_no_sequence_leak(detail)

    # 冲突不关闭窗口、不消耗 operation_id：窗口内第二个不同审批人仍可完成
    ok = emergency_unblind(client, eid, "s1", "bob", "eu-3")
    assert ok.status_code == 200
    assert ok.json()["status"] == "unblinded"
    assert len(ok.json()["sequence"]) == 2


def test_emergency_unblind_window_expiry_blocks_and_allows_fresh_window(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "emergency-expiry.db"))
    t0 = datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc)

    class Clock:
        def __init__(self) -> None:
            self.now = t0

        def __call__(self) -> datetime:
            return self.now

    clock = Clock()
    monkeypatch.setattr(db, "utc_now", clock)

    with TestClient(app) as local:
        created = local.post(
            "/experiments",
            json={"conditions": ["A", "B"], "subject_ids": ["s1", "s2"], "seed": 1},
        ).json()
        eid = created["experiment_id"]

        opened = local.post(
            f"/experiments/{eid}/emergency-unblind",
            json={
                "subject_id": "s1",
                "approver_id": "alice",
                "operation_id": "eu-1",
                "reason": "SAE",
            },
        )
        assert opened.status_code == 200
        assert opened.json()["window_expires_at"] == (
            t0 + timedelta(minutes=15)
        ).isoformat()

        # 窗口内（14 分钟）：同一审批人仍被拒，不同审批人可完成
        clock.now = t0 + timedelta(minutes=14)
        dup = local.post(
            f"/experiments/{eid}/emergency-unblind",
            json={
                "subject_id": "s1",
                "approver_id": "alice",
                "operation_id": "eu-x",
                "reason": "SAE",
            },
        )
        assert dup.status_code == 409
        assert dup.json()["detail"]["reason"] == "same_approver"

        # 窗口届满后：第二审批人被拒，不泄露序列
        clock.now = t0 + timedelta(minutes=15, seconds=1)
        late = local.post(
            f"/experiments/{eid}/emergency-unblind",
            json={
                "subject_id": "s1",
                "approver_id": "bob",
                "operation_id": "eu-2",
                "reason": "SAE",
            },
        )
        assert late.status_code == 409
        detail = late.json()["detail"]
        assert detail["reason"] == "window_expired"
        assert_no_sequence_leak(detail)

        # 过期失败不消耗 operation_id：同一 operation_id 可重新发起首个审批
        reopened = local.post(
            f"/experiments/{eid}/emergency-unblind",
            json={
                "subject_id": "s1",
                "approver_id": "bob",
                "operation_id": "eu-2",
                "reason": "SAE",
            },
        )
        assert reopened.status_code == 200
        reopened_body = reopened.json()
        assert reopened_body["status"] == "pending_second_approval"
        assert reopened_body["first_approver_id"] == "bob"
        assert reopened_body["window_expires_at"] == (
            t0 + timedelta(minutes=30, seconds=1)
        ).isoformat()
        assert_no_sequence_leak(reopened_body)

        # 新窗口内第二个不同审批人批准：正常揭盲
        done = local.post(
            f"/experiments/{eid}/emergency-unblind",
            json={
                "subject_id": "s1",
                "approver_id": "carol",
                "operation_id": "eu-3",
                "reason": "SAE",
            },
        )
        assert done.status_code == 200
        done_body = done.json()
        assert done_body["status"] == "unblinded"
        assert done_body["first_approver_id"] == "bob"
        assert done_body["second_approver_id"] == "carol"
        assert sorted(done_body["sequence"]) == ["A", "B"]

        # 审计完整记录整个生命周期（时间倒序），不含条件内容
        events = local.get(
            f"/experiments/{eid}/emergency-unblind-events"
        ).json()["events"]
        assert [(e["action"], e["approver_id"]) for e in events] == [
            ("unblinded", "carol"),
            ("window_opened", "bob"),
            ("window_expired", "bob"),
            ("window_opened", "alice"),
        ]
        # 过期审计不绑定 operation_id（失败不消耗幂等键）
        assert events[2]["operation_id"] is None
        assert_no_sequence_leak(events)


def test_emergency_unblind_unknown_experiment_or_subject_returns_404(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]

    assert (
        emergency_unblind(client, "missing", "s1", "alice", "eu-1").status_code
        == 404
    )
    ghost = emergency_unblind(client, eid, "ghost", "alice", "eu-1")
    assert ghost.status_code == 404
    assert_no_sequence_leak(ghost.json())

    # 404 不消耗 operation_id、不产生审计记录
    events = client.get(f"/experiments/{eid}/emergency-unblind-events").json()[
        "events"
    ]
    assert events == []
    ok = emergency_unblind(client, eid, "s1", "alice", "eu-1")
    assert ok.status_code == 200


def test_emergency_unblind_operation_id_replays_first_result(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]

    first = emergency_unblind(client, eid, "s1", "alice", "eu-open")
    assert first.status_code == 200
    for _ in range(3):
        replay = emergency_unblind(client, eid, "s1", "alice", "eu-open")
        assert replay.status_code == 200
        assert replay.json() == first.json()

    done = emergency_unblind(client, eid, "s1", "bob", "eu-complete")
    assert done.status_code == 200
    for _ in range(3):
        replay = emergency_unblind(client, eid, "s1", "bob", "eu-complete")
        assert replay.status_code == 200
        assert replay.json() == done.json()

    # 重放不产生额外审计：window_opened 与 unblinded 各一条
    events = client.get(f"/experiments/{eid}/emergency-unblind-events").json()[
        "events"
    ]
    assert [e["action"] for e in events] == ["unblinded", "window_opened"]

    # 完成后重放首个审批的 operation_id，仍返回首次的待审批回执（首次结果语义）
    replay_open = emergency_unblind(client, eid, "s1", "alice", "eu-open")
    assert replay_open.status_code == 200
    assert replay_open.json() == first.json()
    assert_no_sequence_leak(replay_open.json())


def test_emergency_unblind_operation_id_conflict(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]
    assert (
        emergency_unblind(client, eid, "s1", "alice", "shared-op").status_code
        == 200
    )

    # 同一 operation_id 绑定不同审批人 / 受试者 / 原因 → 409
    for payload in (
        {
            "subject_id": "s1",
            "approver_id": "bob",
            "operation_id": "shared-op",
            "reason": "SAE 复核",
        },
        {
            "subject_id": "s2",
            "approver_id": "alice",
            "operation_id": "shared-op",
            "reason": "SAE 复核",
        },
        {
            "subject_id": "s1",
            "approver_id": "alice",
            "operation_id": "shared-op",
            "reason": "different reason",
        },
    ):
        resp = client.post(f"/experiments/{eid}/emergency-unblind", json=payload)
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["reason"] == "operation_id_conflict"
        assert detail["operation_id"] == "shared-op"
        assert_no_sequence_leak(detail)

    # 冲突不产生审计、不推进会话：窗口内第二个不同审批人仍可正常完成
    events = client.get(f"/experiments/{eid}/emergency-unblind-events").json()[
        "events"
    ]
    assert [e["action"] for e in events] == ["window_opened"]
    ok = emergency_unblind(client, eid, "s1", "bob", "eu-2")
    assert ok.status_code == 200
    assert ok.json()["status"] == "unblinded"


def test_emergency_unblind_is_one_time_per_subject(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]
    assert emergency_unblind(client, eid, "s1", "alice", "eu-1").status_code == 200
    completed = emergency_unblind(client, eid, "s1", "bob", "eu-2")
    assert completed.status_code == 200

    # 完成后任何新申请（无论新旧审批人）均 409，不再泄露序列
    third = emergency_unblind(client, eid, "s1", "carol", "eu-3")
    assert third.status_code == 409
    assert third.json()["detail"]["reason"] == "already_unblinded"
    assert_no_sequence_leak(third.json())
    again = emergency_unblind(client, eid, "s1", "alice", "eu-4")
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "already_unblinded"

    # 失败不消耗 operation_id、不产生新审计
    events = client.get(f"/experiments/{eid}/emergency-unblind-events").json()[
        "events"
    ]
    assert [e["action"] for e in events] == ["unblinded", "window_opened"]

    # 其他受试者不受影响，可独立走完整双人审批
    assert emergency_unblind(client, eid, "s2", "alice", "eu-5").status_code == 200
    done = emergency_unblind(client, eid, "s2", "bob", "eu-6")
    assert done.status_code == 200
    assert done.json()["status"] == "unblinded"


def test_emergency_unblind_allowed_while_experiment_paused(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]
    assert pause_experiment(client, eid, "hold").status_code == 200

    # 紧急揭盲是破窗式带外流程，不受实验暂停状态影响
    assert emergency_unblind(client, eid, "s1", "alice", "eu-1").status_code == 200
    done = emergency_unblind(client, eid, "s1", "bob", "eu-2")
    assert done.status_code == 200
    assert done.json()["status"] == "unblinded"
    assert len(done.json()["sequence"]) == 2


def test_concurrent_emergency_second_approvals_have_single_winner(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "conc-emergency.db"))
    with TestClient(app) as setup:
        created = setup.post(
            "/experiments",
            json={"conditions": ["A", "B"], "subject_ids": ["s1", "s2"], "seed": 17},
        ).json()
        eid = created["experiment_id"]
        assert setup.post(
            f"/experiments/{eid}/emergency-unblind",
            json={
                "subject_id": "s1",
                "approver_id": "alice",
                "operation_id": "eu-open",
                "reason": "SAE",
            },
        ).status_code == 200

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    results: list[dict] = []
    statuses: list[int] = []
    results_lock = threading.Lock()

    def worker(i: int) -> None:
        with TestClient(app) as local_client:
            barrier.wait()
            resp = local_client.post(
                f"/experiments/{eid}/emergency-unblind",
                json={
                    "subject_id": "s1",
                    "approver_id": f"approver-{i}",
                    "operation_id": f"eu-second-{i}",
                    "reason": "SAE",
                },
            )
            with results_lock:
                statuses.append(resp.status_code)
                results.append(resp.json())

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # BEGIN IMMEDIATE 串行化：并发第二审批只产生一次揭盲结果
    assert statuses.count(200) == 1
    assert statuses.count(409) == n_threads - 1
    for body, status in zip(results, statuses):
        if status == 200:
            assert body["status"] == "unblinded"
            assert len(body["sequence"]) == 2
        else:
            assert body["detail"]["reason"] == "already_unblinded"
            assert "sequence" not in body["detail"]

    with TestClient(app) as verify:
        events = verify.get(
            f"/experiments/{eid}/emergency-unblind-events"
        ).json()["events"]
    # 审计只有一条 window_opened 与一条 unblinded
    assert [e["action"] for e in events] == ["unblinded", "window_opened"]


def test_emergency_unblind_events_audit_has_no_condition_content(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]

    # 无记录时返回空列表
    empty = client.get(f"/experiments/{eid}/emergency-unblind-events")
    assert empty.status_code == 200
    assert empty.json() == {"experiment_id": eid, "events": []}

    assert (
        emergency_unblind(client, eid, "s1", "alice", "eu-1", "reason-1").status_code
        == 200
    )
    assert (
        emergency_unblind(client, eid, "s1", "bob", "eu-2", "reason-2").status_code
        == 200
    )
    assert (
        emergency_unblind(client, eid, "s2", "carol", "eu-3", "reason-3").status_code
        == 200
    )

    resp = client.get(f"/experiments/{eid}/emergency-unblind-events")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["experiment_id"] == eid
    events = payload["events"]
    # 按时间倒序（最新在前）
    assert [(e["subject_id"], e["action"], e["approver_id"]) for e in events] == [
        ("s2", "window_opened", "carol"),
        ("s1", "unblinded", "bob"),
        ("s1", "window_opened", "alice"),
    ]
    assert events[0]["id"] > events[1]["id"] > events[2]["id"]
    assert events[1]["operation_id"] == "eu-2"
    assert events[1]["reason"] == "reason-2"
    assert events[0]["window_expires_at"] is not None
    assert events[1]["window_expires_at"] is None
    assert all("created_at" in e for e in events)
    # 审计不含任何条件内容
    assert_no_sequence_leak(payload)
    assert all("condition" not in e for e in events)

    # limit / offset 分页
    page1 = client.get(
        f"/experiments/{eid}/emergency-unblind-events?limit=2"
    ).json()["events"]
    assert [e["operation_id"] for e in page1] == ["eu-3", "eu-2"]
    page2 = client.get(
        f"/experiments/{eid}/emergency-unblind-events?limit=2&offset=2"
    ).json()["events"]
    assert [e["operation_id"] for e in page2] == ["eu-1"]

    # 非法分页参数
    assert (
        client.get(f"/experiments/{eid}/emergency-unblind-events?limit=0").status_code
        == 422
    )
    assert (
        client.get(
            f"/experiments/{eid}/emergency-unblind-events?limit=501"
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/experiments/{eid}/emergency-unblind-events?offset=-1"
        ).status_code
        == 422
    )

    # 实验不存在返回 404
    assert client.get("/experiments/missing/emergency-unblind-events").status_code == 404


def test_emergency_unblind_payload_validation(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]
    base = {
        "subject_id": "s1",
        "approver_id": "alice",
        "operation_id": "op",
        "reason": "r",
    }
    for field in ("subject_id", "approver_id", "operation_id", "reason"):
        missing = {k: v for k, v in base.items() if k != field}
        assert (
            client.post(
                f"/experiments/{eid}/emergency-unblind", json=missing
            ).status_code
            == 422
        )
        empty = {**base, field: ""}
        assert (
            client.post(
                f"/experiments/{eid}/emergency-unblind", json=empty
            ).status_code
            == 422
        )


def test_legacy_database_supports_emergency_unblind_after_migration(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "legacy-emergency.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    # 模拟旧版本数据库：只有 experiments / assignments 两张表
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE experiments (
                id TEXT PRIMARY KEY,
                conditions TEXT NOT NULL,
                seed INTEGER NOT NULL,
                group_size INTEGER NOT NULL,
                num_subjects INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE assignments (
                experiment_id TEXT NOT NULL REFERENCES experiments(id),
                subject_id TEXT NOT NULL,
                sequence_index INTEGER NOT NULL,
                sequence TEXT NOT NULL,
                PRIMARY KEY (experiment_id, subject_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO experiments "
            "(id, conditions, seed, group_size, num_subjects, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-eu", '["A","B"]', 1, 2, 2, "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO assignments "
            "(experiment_id, subject_id, sequence_index, sequence) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-eu", "s1", 0, '["A","B"]'),
        )
        conn.execute(
            "INSERT INTO assignments "
            "(experiment_id, subject_id, sequence_index, sequence) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-eu", "s2", 1, '["B","A"]'),
        )

    # 启动自动建表迁移后，紧急揭盲完整链路在旧库上可用
    with TestClient(app) as migrated:
        opened = migrated.post(
            "/experiments/legacy-eu/emergency-unblind",
            json={
                "subject_id": "s1",
                "approver_id": "alice",
                "operation_id": "eu-1",
                "reason": "SAE",
            },
        )
        assert opened.status_code == 200
        assert opened.json()["status"] == "pending_second_approval"
        assert_no_sequence_leak(opened.json())

        done = migrated.post(
            "/experiments/legacy-eu/emergency-unblind",
            json={
                "subject_id": "s1",
                "approver_id": "bob",
                "operation_id": "eu-2",
                "reason": "SAE",
            },
        )
        assert done.status_code == 200
        # 返回的正是旧库 assignments 中保存的完整序列
        assert done.json()["sequence"] == ["A", "B"]

        events = migrated.get(
            "/experiments/legacy-eu/emergency-unblind-events"
        ).json()["events"]
        assert [e["action"] for e in events] == ["unblinded", "window_opened"]
        assert_no_sequence_leak(events)


# ---------- 脱敏结果快照 ----------

ALL_SNAPSHOT_FIELDS = ["period", "condition", "revealed_at", "completed_at"]


def create_snapshot(
    client: TestClient,
    experiment_id: str,
    fields: list[str] | None = None,
    operation_id: str = "snap-1",
):
    return client.post(
        f"/experiments/{experiment_id}/snapshots",
        json={
            "fields": ALL_SNAPSHOT_FIELDS if fields is None else fields,
            "operation_id": operation_id,
        },
    )


def snapshot_digest(content: dict) -> str:
    """按服务端约定的规范化 JSON（键排序、紧凑分隔符、UTF-8）重算 SHA-256。"""
    import hashlib
    import json

    canonical = json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_snapshot_contains_only_revealed_periods_sorted_by_subject(
    client: TestClient,
) -> None:
    # 名册顺序故意打乱；快照必须按 subject_id 稳定排序
    body, _ = create_experiment(
        client,
        conditions=["A", "B"],
        subject_ids=["s3", "s1", "s4", "s2"],
        seed=5,
    )
    eid = body["experiment_id"]

    # s2：揭盲周期 1 并完成，再揭盲周期 2（未完成 → completed_at 为 null）
    assert reveal_period(client, eid, "s2", 1).status_code == 200
    done = complete_period(client, eid, "s2", 1, "s2-c1")
    assert done.status_code == 200
    s2_p2 = reveal_period(client, eid, "s2", 2)
    assert s2_p2.status_code == 200
    # s1：只揭盲周期 1，不登记完成
    s1_p1 = reveal_period(client, eid, "s1", 1)
    assert s1_p1.status_code == 200
    # s3 / s4：完全不揭盲

    resp = create_snapshot(client, eid, operation_id="snap-full")
    assert resp.status_code == 200, resp.text
    snap = resp.json()
    assert snap["experiment_id"] == eid
    assert snap["operation_id"] == "snap-full"
    assert snap["fields"] == ALL_SNAPSHOT_FIELDS
    assert snap["snapshot_id"]
    assert snap["created_at"] == snap["snapshot"]["generated_at"]
    # 摘要为 64 位十六进制，且与规范化内容的重算结果一致
    assert len(snap["digest"]) == 64
    int(snap["digest"], 16)
    assert snap["digest"] == snapshot_digest(snap["snapshot"])

    content = snap["snapshot"]
    assert content["experiment_id"] == eid
    assert "generated_at" in content
    # 按 subject_id 稳定排序，覆盖全部名册（含零揭盲受试者）
    subjects = content["subjects"]
    assert [s["subject_id"] for s in subjects] == ["s1", "s2", "s3", "s4"]
    # s1：周期 1 已揭盲未完成 → completed_at 为 null
    assert subjects[0]["periods"] == [
        {
            "period": 1,
            "condition": s1_p1.json()["condition"],
            "revealed_at": s1_p1.json()["revealed_at"],
            "completed_at": None,
        }
    ]
    # s2：周期 1 已完成、周期 2 已揭盲未完成
    assert [p["period"] for p in subjects[1]["periods"]] == [1, 2]
    assert subjects[1]["periods"][0]["completed_at"] == done.json()["completed_at"]
    assert subjects[1]["periods"][1]["condition"] == s2_p2.json()["condition"]
    assert subjects[1]["periods"][1]["completed_at"] is None
    # s3 / s4：零揭盲 → 空周期列表
    assert subjects[2]["periods"] == []
    assert subjects[3]["periods"] == []
    # 任何层级都不得出现序列或序列下标
    assert_no_sequence_leak(snap)


def test_snapshot_field_whitelist_filters_period_entries(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=3
    )
    eid = body["experiment_id"]
    assert reveal_period(client, eid, "s1", 1).status_code == 200

    # 只取 period + completed_at：条件与揭盲时间被脱敏
    resp = create_snapshot(client, eid, ["period", "completed_at"], "snap-sub")
    assert resp.status_code == 200, resp.text
    snap = resp.json()
    assert snap["fields"] == ["period", "completed_at"]
    s1 = next(
        s for s in snap["snapshot"]["subjects"] if s["subject_id"] == "s1"
    )
    assert s1["periods"] == [{"period": 1, "completed_at": None}]
    # 未选字段不出现在响应中（不是 null，而是键不存在）
    assert set(s1["periods"][0].keys()) == {"period", "completed_at"}

    # 只取 condition：条目仅含条件
    resp = create_snapshot(client, eid, ["condition"], "snap-cond")
    assert resp.status_code == 200
    s1 = next(
        s for s in resp.json()["snapshot"]["subjects"]
        if s["subject_id"] == "s1"
    )
    assert s1["periods"] == [{"condition": "A"}] or s1["periods"] == [
        {"condition": "B"}
    ]
    # subject_id 作为排序键始终保留
    assert set(s1.keys()) == {"subject_id", "periods"}
    assert_no_sequence_leak(resp.json())


def test_snapshot_same_operation_id_replays_and_is_immutable(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=9
    )
    eid = body["experiment_id"]
    assert reveal_period(client, eid, "s1", 1).status_code == 200

    first = create_snapshot(client, eid, operation_id="snap-idem")
    assert first.status_code == 200
    first_body = first.json()

    for _ in range(3):
        replay = create_snapshot(client, eid, operation_id="snap-idem")
        assert replay.status_code == 200
        assert replay.json() == first_body

    # 快照生成后继续推进：s1 完成周期 1 并揭盲周期 2
    assert complete_period(client, eid, "s1", 1, "c1").status_code == 200
    assert reveal_period(client, eid, "s1", 2).status_code == 200

    # 重放仍返回首次快照（不可变），按 ID 取回的内容也不变
    replay = create_snapshot(client, eid, operation_id="snap-idem")
    assert replay.status_code == 200
    assert replay.json() == first_body
    fetched = client.get(f"/experiments/{eid}/snapshots/{first_body['snapshot_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == first_body
    s1 = next(
        s for s in fetched.json()["snapshot"]["subjects"]
        if s["subject_id"] == "s1"
    )
    assert [p["period"] for p in s1["periods"]] == [1]

    # 新 operation_id 生成新快照：反映最新状态，ID 与摘要不同
    newer = create_snapshot(client, eid, operation_id="snap-new")
    assert newer.status_code == 200
    newer_body = newer.json()
    assert newer_body["snapshot_id"] != first_body["snapshot_id"]
    assert newer_body["digest"] != first_body["digest"]
    s1_new = next(
        s for s in newer_body["snapshot"]["subjects"]
        if s["subject_id"] == "s1"
    )
    assert [p["period"] for p in s1_new["periods"]] == [1, 2]
    assert s1_new["periods"][0]["completed_at"] is not None


def test_snapshot_operation_id_conflict_on_different_fields(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=4
    )
    eid = body["experiment_id"]
    assert create_snapshot(client, eid, ["period"], "shared-snap").status_code == 200

    # 同一 operation_id 绑定不同字段白名单 → 409
    conflict = create_snapshot(client, eid, ["period", "condition"], "shared-snap")
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["reason"] == "operation_id_conflict"
    assert detail["operation_id"] == "shared-snap"
    assert_no_sequence_leak(detail)

    # 字段相同但顺序不同也视为不同请求
    reordered = create_snapshot(
        client, eid, ["completed_at", "period"], "shared-snap-2"
    )
    assert reordered.status_code == 200
    conflict2 = create_snapshot(
        client, eid, ["period", "completed_at"], "shared-snap-2"
    )
    assert conflict2.status_code == 409
    assert conflict2.json()["detail"]["reason"] == "operation_id_conflict"

    # 冲突不消耗、不改写：原 operation_id 重放仍返回首次快照
    replay = create_snapshot(client, eid, ["period"], "shared-snap")
    assert replay.status_code == 200
    assert replay.json()["fields"] == ["period"]


def test_snapshot_get_by_id_returns_content_and_404s(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=6
    )
    eid = body["experiment_id"]
    created = create_snapshot(client, eid, operation_id="snap-get")
    assert created.status_code == 200
    snapshot_id = created.json()["snapshot_id"]

    fetched = client.get(f"/experiments/{eid}/snapshots/{snapshot_id}")
    assert fetched.status_code == 200
    assert fetched.json() == created.json()
    assert_no_sequence_leak(fetched.json())

    # 快照 ID 不存在 / 实验不存在 / 跨实验取快照均 404
    assert client.get(f"/experiments/{eid}/snapshots/nope").status_code == 404
    assert client.get(f"/experiments/missing/snapshots/{snapshot_id}").status_code == 404
    other, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["t1", "t2"], seed=7
    )
    assert (
        client.get(
            f"/experiments/{other['experiment_id']}/snapshots/{snapshot_id}"
        ).status_code
        == 404
    )


def test_snapshot_unknown_experiment_returns_404(client: TestClient) -> None:
    resp = create_snapshot(client, "missing", operation_id="snap-x")
    assert resp.status_code == 404


def test_snapshot_payload_validation(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=2
    )
    eid = body["experiment_id"]

    # 空白名单 / 未知字段 / 重复字段
    assert create_snapshot(client, eid, [], "v1").status_code == 422
    assert create_snapshot(client, eid, ["sequence_index"], "v2").status_code == 422
    assert create_snapshot(client, eid, ["sequence"], "v3").status_code == 422
    assert create_snapshot(client, eid, ["period", "period"], "v4").status_code == 422
    # operation_id 缺失或为空
    assert (
        client.post(
            f"/experiments/{eid}/snapshots", json={"fields": ["period"]}
        ).status_code
        == 422
    )
    assert create_snapshot(client, eid, ["period"], "").status_code == 422
    # 校验失败不消耗 operation_id：修正后同键首次成功
    ok = create_snapshot(client, eid, ["period"], "v4")
    assert ok.status_code == 200


def test_snapshot_is_read_only_and_allowed_while_paused(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=1
    )
    eid = body["experiment_id"]
    assert reveal_period(client, eid, "s1", 1).status_code == 200
    assert pause_experiment(client, eid, "hold").status_code == 200

    # 暂停只冻结揭盲：快照作为只读导出仍可用，且只含暂停前已揭盲的数据
    resp = create_snapshot(client, eid, operation_id="snap-paused")
    assert resp.status_code == 200, resp.text
    s1 = next(
        s for s in resp.json()["snapshot"]["subjects"]
        if s["subject_id"] == "s1"
    )
    assert [p["period"] for p in s1["periods"]] == [1]
    assert_no_sequence_leak(resp.json())

    # 快照是纯只读流程：不推进任何进度、不消耗推进 / 完成幂等键
    assert resume_experiment(client, eid, 1).status_code == 200
    assert complete_period(client, eid, "s1", 1, "c1").status_code == 200
    assert reveal_period(client, eid, "s1", 2).status_code == 200


def test_snapshot_generated_at_fixed_within_transaction(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "snapshot-clock.db"))
    t0 = datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc)

    class Clock:
        def __init__(self) -> None:
            self.now = t0

        def __call__(self) -> datetime:
            return self.now

    clock = Clock()
    monkeypatch.setattr(db, "utc_now", clock)

    with TestClient(app) as local:
        created = local.post(
            "/experiments",
            json={"conditions": ["A", "B"], "subject_ids": ["s1", "s2"], "seed": 1},
        ).json()
        eid = created["experiment_id"]

        first = local.post(
            f"/experiments/{eid}/snapshots",
            json={"fields": ["period"], "operation_id": "snap-t1"},
        )
        assert first.status_code == 200
        # 整份快照固定同一时点：generated_at 与 created_at 一致
        assert first.json()["snapshot"]["generated_at"] == t0.isoformat()
        assert first.json()["created_at"] == t0.isoformat()

        # 时钟前进后新快照使用新时点，旧快照内容不变
        clock.now = t0 + timedelta(hours=1)
        second = local.post(
            f"/experiments/{eid}/snapshots",
            json={"fields": ["period"], "operation_id": "snap-t2"},
        )
        assert second.status_code == 200
        assert second.json()["snapshot"]["generated_at"] == (
            t0 + timedelta(hours=1)
        ).isoformat()
        refetched = local.get(
            f"/experiments/{eid}/snapshots/{first.json()['snapshot_id']}"
        )
        assert refetched.json() == first.json()


def test_concurrent_snapshot_creation_shares_single_snapshot(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "conc-snapshot.db"))
    with TestClient(app) as setup:
        created = setup.post(
            "/experiments",
            json={"conditions": ["A", "B"], "subject_ids": ["s1", "s2"], "seed": 11},
        ).json()
        eid = created["experiment_id"]
        setup.post(
            f"/experiments/{eid}/advance",
            json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
        )

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    bodies: list[dict] = []
    bodies_lock = threading.Lock()

    def worker() -> None:
        with TestClient(app) as local_client:
            barrier.wait()
            resp = local_client.post(
                f"/experiments/{eid}/snapshots",
                json={"fields": ALL_SNAPSHOT_FIELDS, "operation_id": "snap-race"},
            )
            assert resp.status_code == 200
            with bodies_lock:
                bodies.append(resp.json())

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # BEGIN IMMEDIATE 串行化：首个事务生成快照，其余幂等重放同一快照
    assert len(bodies) == n_threads
    assert len({b["snapshot_id"] for b in bodies}) == 1
    assert len({b["digest"] for b in bodies}) == 1
    for b in bodies:
        assert b == bodies[0]


def test_legacy_database_supports_snapshots_after_migration(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "legacy-snapshot.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    # 模拟旧版本数据库：只有 experiments / assignments 两张表
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE experiments (
                id TEXT PRIMARY KEY,
                conditions TEXT NOT NULL,
                seed INTEGER NOT NULL,
                group_size INTEGER NOT NULL,
                num_subjects INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE assignments (
                experiment_id TEXT NOT NULL REFERENCES experiments(id),
                subject_id TEXT NOT NULL,
                sequence_index INTEGER NOT NULL,
                sequence TEXT NOT NULL,
                PRIMARY KEY (experiment_id, subject_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO experiments "
            "(id, conditions, seed, group_size, num_subjects, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-snap", '["A","B"]', 1, 2, 2, "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO assignments "
            "(experiment_id, subject_id, sequence_index, sequence) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-snap", "s1", 0, '["A","B"]'),
        )
        conn.execute(
            "INSERT INTO assignments "
            "(experiment_id, subject_id, sequence_index, sequence) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-snap", "s2", 1, '["B","A"]'),
        )

    # 启动自动建表迁移后，快照链路在旧库上可用
    with TestClient(app) as migrated:
        # 旧库无任何揭盲记录：快照中所有受试者周期列表为空
        empty = create_snapshot(migrated, "legacy-snap", operation_id="lg-snap")
        assert empty.status_code == 200, empty.text
        subjects = empty.json()["snapshot"]["subjects"]
        assert [s["subject_id"] for s in subjects] == ["s1", "s2"]
        assert all(s["periods"] == [] for s in subjects)
        assert_no_sequence_leak(empty.json())

        # 揭盲后新快照只含已揭盲周期；旧快照保持不可变
        assert reveal_period(migrated, "legacy-snap", "s1", 1).status_code == 200
        newer = create_snapshot(migrated, "legacy-snap", operation_id="lg-snap-2")
        assert newer.status_code == 200
        s1 = next(
            s for s in newer.json()["snapshot"]["subjects"]
            if s["subject_id"] == "s1"
        )
        assert s1["periods"][0]["condition"] == "A"
        assert s1["periods"][0]["completed_at"] is None

        refetched = migrated.get(
            f"/experiments/legacy-snap/snapshots/{empty.json()['snapshot_id']}"
        )
        assert refetched.status_code == 200
        assert refetched.json() == empty.json()


# ---------- 受试者换号（subject replacement） ----------

def replace_subject(
    client: TestClient,
    experiment_id: str,
    subject_id: str,
    replacement_subject_id: str,
    operation_id: str,
    reason: str = "编号录入错误",
):
    return client.post(
        f"/experiments/{experiment_id}/subjects/{subject_id}/replace",
        json={
            "replacement_subject_id": replacement_subject_id,
            "operation_id": operation_id,
            "reason": reason,
        },
    )


def test_replace_transfers_assignment_and_progress_atomically(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    eid = body["experiment_id"]

    resp = replace_subject(client, eid, "s1", "s1-new", "rep-1", "编号录入错误")
    assert resp.status_code == 200, resp.text
    event = resp.json()
    assert event["experiment_id"] == eid
    assert event["original_subject_id"] == "s1"
    assert event["replacement_subject_id"] == "s1-new"
    assert event["operation_id"] == "rep-1"
    assert event["reason"] == "编号录入错误"
    assert "created_at" in event
    assert "id" in event
    assert_no_sequence_leak(event)

    # 名册中新编号取代原编号
    roster = client.get(f"/experiments/{eid}").json()
    assert [a["subject_id"] for a in roster["assignments"]] == ["s1-new", "s2"]

    # 新编号从周期 1 开始，可正常推进
    progress = client.get(f"/experiments/{eid}/subjects/s1-new/progress").json()
    assert progress["next_period"] == 1
    assert progress["reveals"] == []
    first = reveal_period(client, eid, "s1-new", 1)
    assert first.status_code == 200

    # 序列内容与下标保持不变：紧急揭盲新编号得到同一完整序列
    assert emergency_unblind(client, eid, "s1-new", "alice", "eu-open").status_code == 200
    done = emergency_unblind(client, eid, "s1-new", "bob", "eu-done")
    assert done.status_code == 200
    assert done.json()["sequence"] == [first.json()["condition"]] + [
        c for c in ["A", "B"] if c != first.json()["condition"]
    ]

    # 快照名册只含新编号
    snap = create_snapshot(client, eid, ["period"], "snap-after-replace")
    assert snap.status_code == 200
    assert [s["subject_id"] for s in snap.json()["snapshot"]["subjects"]] == [
        "s1-new",
        "s2",
    ]


def test_replace_tombstones_original_subject(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    eid = body["experiment_id"]
    assert replace_subject(client, eid, "s1", "s1-new", "rep-1").status_code == 200

    # 原编号不能查询进度
    progress = client.get(f"/experiments/{eid}/subjects/s1/progress")
    assert progress.status_code == 409
    assert progress.json()["detail"]["reason"] == "subject_replaced"

    # 原编号不能推进
    advance = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1", "expected_period": 1, "request_id": "a1"},
    )
    assert advance.status_code == 409
    assert advance.json()["detail"]["reason"] == "subject_replaced"
    assert_no_sequence_leak(advance.json())

    # 原编号不能登记完成
    done = complete_period(client, eid, "s1", 1, "c1")
    assert done.status_code == 409
    assert done.json()["detail"]["reason"] == "subject_replaced"

    # 原编号不能发起紧急揭盲
    emergency = emergency_unblind(client, eid, "s1", "alice", "eu-1")
    assert emergency.status_code == 409
    assert emergency.json()["detail"]["reason"] == "subject_replaced"
    assert_no_sequence_leak(emergency.json())

    # 原编号墓碑不能复用：不能作为新编号，也不能再次换号
    reuse = replace_subject(client, eid, "s2", "s1", "rep-2")
    assert reuse.status_code == 409
    assert reuse.json()["detail"]["reason"] == "replacement_subject_exists"
    again = replace_subject(client, eid, "s1", "s1-other", "rep-3")
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "subject_replaced"

    # 换号审计只有首次一条（失败路径零写入）
    events = client.get(f"/experiments/{eid}/subject-replacements").json()
    assert len(events["replacements"]) == 1


def test_replace_rejects_existing_replacement_id(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    eid = body["experiment_id"]

    # 新编号已在册 → 409 零写入
    resp = replace_subject(client, eid, "s1", "s2", "rep-1")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "replacement_subject_exists"
    assert detail["replacement_subject_id"] == "s2"

    # 新编号与原编号相同也视为已存在 → 409
    same = replace_subject(client, eid, "s1", "s1", "rep-2")
    assert same.status_code == 409
    assert same.json()["detail"]["reason"] == "replacement_subject_exists"

    # 零写入：s1 仍在册可正常推进，失败不消耗 operation_id
    assert reveal_period(client, eid, "s1", 1).status_code == 200
    assert client.get(f"/experiments/{eid}/subject-replacements").json()[
        "replacements"
    ] == []


def test_replace_rejects_already_unblinded_subject(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    eid = body["experiment_id"]

    # 按周期揭盲过的受试者不能换号
    assert reveal_period(client, eid, "s1", 1).status_code == 200
    resp = replace_subject(client, eid, "s1", "s1-new", "rep-1")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "subject_already_unblinded"
    assert detail["subject_id"] == "s1"
    assert_no_sequence_leak(detail)

    # 完成过紧急揭盲的受试者不能换号
    assert emergency_unblind(client, eid, "s2", "alice", "eu-1").status_code == 200
    assert emergency_unblind(client, eid, "s2", "bob", "eu-2").status_code == 200
    resp2 = replace_subject(client, eid, "s2", "s2-new", "rep-2")
    assert resp2.status_code == 409
    assert resp2.json()["detail"]["reason"] == "subject_already_unblinded"

    # 零写入：两人分配与进度均未受影响，失败不消耗 operation_id
    assert client.get(f"/experiments/{eid}/subjects/s1/progress").json()[
        "next_period"
    ] == 2
    assert client.get(f"/experiments/{eid}/subjects/s2/progress").json()[
        "next_period"
    ] == 1
    assert client.get(f"/experiments/{eid}/subject-replacements").json()[
        "replacements"
    ] == []


def test_replace_allowed_with_pending_or_expired_emergency_window(
    tmp_path, monkeypatch
) -> None:
    # 仅“完成揭盲”阻止换号：窗口开启未揭盲 / 已过期均不阻止
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "replace-window.db"))
    t0 = datetime(2026, 9, 22, 8, 0, 0, tzinfo=timezone.utc)

    class Clock:
        def __init__(self) -> None:
            self.now = t0

        def __call__(self) -> datetime:
            return self.now

    clock = Clock()
    monkeypatch.setattr(db, "utc_now", clock)

    with TestClient(app) as local:
        created = local.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 1,
            },
        ).json()
        eid = created["experiment_id"]

        # s1：窗口开启但未完成揭盲 → 允许换号
        assert emergency_unblind(local, eid, "s1", "alice", "eu-1").status_code == 200
        ok = replace_subject(local, eid, "s1", "s1-new", "rep-1")
        assert ok.status_code == 200, ok.text

        # s2：窗口过期后 → 允许换号
        assert emergency_unblind(local, eid, "s2", "alice", "eu-2").status_code == 200
        clock.now = t0 + timedelta(minutes=16)
        expired = emergency_unblind(local, eid, "s2", "bob", "eu-3")
        assert expired.status_code == 409
        assert expired.json()["detail"]["reason"] == "window_expired"
        ok2 = replace_subject(local, eid, "s2", "s2-new", "rep-2")
        assert ok2.status_code == 200, ok2.text


def test_replace_operation_id_replays_first_result(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    eid = body["experiment_id"]

    first = replace_subject(client, eid, "s1", "s1-new", "rep-idem", "录入错误")
    assert first.status_code == 200
    first_body = first.json()

    # 相同 operation_id 重试返回首次结果，不重复执行
    for _ in range(3):
        replay = replace_subject(client, eid, "s1", "s1-new", "rep-idem", "录入错误")
        assert replay.status_code == 200
        assert replay.json() == first_body
    events = client.get(f"/experiments/{eid}/subject-replacements").json()
    assert len(events["replacements"]) == 1

    # 同一 operation_id 绑定不同原编号（路径）/ 新编号 / 原因 → 409
    for path_subject, payload in (
        ("s2", {"replacement_subject_id": "s1-new", "operation_id": "rep-idem",
                "reason": "录入错误"}),
        ("s1", {"replacement_subject_id": "s1-other", "operation_id": "rep-idem",
                "reason": "录入错误"}),
        ("s1", {"replacement_subject_id": "s1-new", "operation_id": "rep-idem",
                "reason": "其他原因"}),
    ):
        conflict = client.post(
            f"/experiments/{eid}/subjects/{path_subject}/replace", json=payload
        )
        assert conflict.status_code == 409
        detail = conflict.json()["detail"]
        assert detail["reason"] == "operation_id_conflict"
        assert detail["operation_id"] == "rep-idem"

    # 冲突路径零写入：审计仍只有一条
    events = client.get(f"/experiments/{eid}/subject-replacements").json()
    assert len(events["replacements"]) == 1


def test_replace_unknown_experiment_or_subject_returns_404(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    eid = body["experiment_id"]

    assert (
        replace_subject(client, "missing", "s1", "s1-new", "rep-1").status_code
        == 404
    )
    assert (
        replace_subject(client, eid, "ghost", "ghost-new", "rep-2").status_code
        == 404
    )
    # 404 零写入、不消耗 operation_id：修正后同键首次成功
    ok = replace_subject(client, eid, "s1", "s1-new", "rep-1")
    assert ok.status_code == 200


def test_replace_payload_validation(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    eid = body["experiment_id"]
    base = {
        "replacement_subject_id": "s1-new",
        "operation_id": "rep-v",
        "reason": "r",
    }
    for field in ("replacement_subject_id", "operation_id", "reason"):
        missing = {k: v for k, v in base.items() if k != field}
        assert client.post(
            f"/experiments/{eid}/subjects/s1/replace", json=missing
        ).status_code == 422
        empty = {**base, field: ""}
        assert client.post(
            f"/experiments/{eid}/subjects/s1/replace", json=empty
        ).status_code == 422
    # 校验失败不消耗 operation_id：修正后同键首次成功
    assert replace_subject(client, eid, "s1", "s1-new", "rep-v").status_code == 200


def test_replace_batch_advance_rejects_tombstoned_member(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    eid = body["experiment_id"]
    assert replace_subject(client, eid, "s1", "s1-new", "rep-1").status_code == 200

    # 批量中含已退役成员：整批 409 零写入，失败原因不含任何条件
    resp = advance_batch(
        client,
        eid,
        [
            {"subject_id": "s1", "expected_period": 1},
            {"subject_id": "s2", "expected_period": 1},
        ],
        "batch-tombstone",
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "batch_member_rejected"
    assert detail["failures"] == [
        {"subject_id": "s1", "reason": "subject_replaced"}
    ]
    assert_no_sequence_leak(detail)
    # 整批零写入：s2 也未被揭盲
    assert client.get(f"/experiments/{eid}/subjects/s2/progress").json()[
        "reveals"
    ] == []


def test_replace_chain_moves_assignment_to_latest_id(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    eid = body["experiment_id"]

    # 链式换号：s1 → s1-new → s1-final（每次原编号都从未揭盲）
    assert replace_subject(client, eid, "s1", "s1-new", "rep-1").status_code == 200
    assert (
        replace_subject(client, eid, "s1-new", "s1-final", "rep-2").status_code
        == 200
    )

    # 中间编号也已退役：不能推进、不能复用
    advance = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1-new", "expected_period": 1, "request_id": "a1"},
    )
    assert advance.status_code == 409
    assert advance.json()["detail"]["reason"] == "subject_replaced"
    reuse = replace_subject(client, eid, "s2", "s1-new", "rep-3")
    assert reuse.status_code == 409
    assert reuse.json()["detail"]["reason"] == "replacement_subject_exists"

    # 最终编号承接同一分配：可正常推进
    assert reveal_period(client, eid, "s1-final", 1).status_code == 200

    # 审计按时间倒序记录两次换号
    events = client.get(f"/experiments/{eid}/subject-replacements").json()
    assert [
        (r["original_subject_id"], r["replacement_subject_id"])
        for r in events["replacements"]
    ] == [("s1-new", "s1-final"), ("s1", "s1-new")]


def test_replace_allowed_while_experiment_paused(client: TestClient) -> None:
    body, _ = create_experiment(
        client, conditions=["A", "B"], subject_ids=["s1", "s2"], seed=5
    )
    eid = body["experiment_id"]
    assert pause_experiment(client, eid, "hold").status_code == 200

    # 换号不是揭盲：暂停期间仍允许
    assert replace_subject(client, eid, "s1", "s1-new", "rep-1").status_code == 200
    # 新编号在暂停期间仍受揭盲门禁约束
    blocked = client.post(
        f"/experiments/{eid}/advance",
        json={"subject_id": "s1-new", "expected_period": 1, "request_id": "a1"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["reason"] == "experiment_paused"


def test_subject_replacements_listed_in_reverse_chronological_order(
    client: TestClient,
) -> None:
    body, _ = create_experiment(
        client,
        conditions=["A", "B", "C", "D"],
        subject_ids=[f"s{i}" for i in range(4)],
        seed=5,
    )
    eid = body["experiment_id"]

    # 无记录时返回空列表
    empty = client.get(f"/experiments/{eid}/subject-replacements")
    assert empty.status_code == 200
    assert empty.json() == {"experiment_id": eid, "replacements": []}

    assert replace_subject(client, eid, "s0", "s0-new", "rep-1", "原因一").status_code == 200
    assert replace_subject(client, eid, "s1", "s1-new", "rep-2", "原因二").status_code == 200
    assert replace_subject(client, eid, "s2", "s2-new", "rep-3", "原因三").status_code == 200

    resp = client.get(f"/experiments/{eid}/subject-replacements")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["experiment_id"] == eid
    replacements = payload["replacements"]
    # 按时间倒序（最新在前），审计不含任何条件内容
    assert [r["operation_id"] for r in replacements] == ["rep-3", "rep-2", "rep-1"]
    assert [r["reason"] for r in replacements] == ["原因三", "原因二", "原因一"]
    assert (
        replacements[0]["id"] > replacements[1]["id"] > replacements[2]["id"]
    )
    assert all("created_at" in r for r in replacements)
    assert_no_sequence_leak(payload)

    # limit / offset 分页
    page1 = client.get(
        f"/experiments/{eid}/subject-replacements?limit=2"
    ).json()["replacements"]
    assert [r["operation_id"] for r in page1] == ["rep-3", "rep-2"]
    page2 = client.get(
        f"/experiments/{eid}/subject-replacements?limit=2&offset=2"
    ).json()["replacements"]
    assert [r["operation_id"] for r in page2] == ["rep-1"]

    # 非法分页参数
    assert client.get(
        f"/experiments/{eid}/subject-replacements?limit=0"
    ).status_code == 422
    assert client.get(
        f"/experiments/{eid}/subject-replacements?limit=501"
    ).status_code == 422
    assert client.get(
        f"/experiments/{eid}/subject-replacements?offset=-1"
    ).status_code == 422

    # 实验不存在返回 404
    assert client.get("/experiments/missing/subject-replacements").status_code == 404


def test_concurrent_replace_has_single_winner(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "conc-replace.db"))
    with TestClient(app) as setup:
        created = setup.post(
            "/experiments",
            json={
                "conditions": ["A", "B"],
                "subject_ids": ["s1", "s2"],
                "seed": 11,
            },
        ).json()
    eid = created["experiment_id"]

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    statuses: list[int] = []
    reasons: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        with TestClient(app) as local_client:
            barrier.wait()
            resp = local_client.post(
                f"/experiments/{eid}/subjects/s1/replace",
                json={
                    "replacement_subject_id": f"s1-new-{i}",
                    "operation_id": f"rep-race-{i}",
                    "reason": "并发换号",
                },
            )
            with lock:
                statuses.append(resp.status_code)
                if resp.status_code == 409:
                    reasons.append(resp.json()["detail"]["reason"])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # BEGIN IMMEDIATE 串行化：只有一次换号成功，其余看到墓碑而 409
    assert statuses.count(200) == 1
    assert statuses.count(409) == n_threads - 1
    assert set(reasons) == {"subject_replaced"}

    with TestClient(app) as verify:
        events = verify.get(f"/experiments/{eid}/subject-replacements").json()
        assert len(events["replacements"]) == 1
        winner = events["replacements"][0]["replacement_subject_id"]
        # 只有赢家的新编号在册并可推进
        roster = verify.get(f"/experiments/{eid}").json()
        assert [a["subject_id"] for a in roster["assignments"]] == [winner, "s2"]
        progress = verify.get(
            f"/experiments/{eid}/subjects/{winner}/progress"
        ).json()
        assert progress["next_period"] == 1


def test_legacy_database_supports_subject_replacement_after_migration(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "legacy-replace.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    # 模拟旧版本数据库：只有 experiments / assignments 两张表
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE experiments (
                id TEXT PRIMARY KEY,
                conditions TEXT NOT NULL,
                seed INTEGER NOT NULL,
                group_size INTEGER NOT NULL,
                num_subjects INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE assignments (
                experiment_id TEXT NOT NULL REFERENCES experiments(id),
                subject_id TEXT NOT NULL,
                sequence_index INTEGER NOT NULL,
                sequence TEXT NOT NULL,
                PRIMARY KEY (experiment_id, subject_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO experiments "
            "(id, conditions, seed, group_size, num_subjects, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-rep", '["A","B"]', 1, 2, 2, "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO assignments "
            "(experiment_id, subject_id, sequence_index, sequence) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-rep", "s1", 0, '["A","B"]'),
        )
        conn.execute(
            "INSERT INTO assignments "
            "(experiment_id, subject_id, sequence_index, sequence) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-rep", "s2", 1, '["B","A"]'),
        )

    # 启动自动建表迁移后，换号链路在旧库上可用
    with TestClient(app) as migrated:
        resp = replace_subject(migrated, "legacy-rep", "s1", "s1-new", "rep-1")
        assert resp.status_code == 200, resp.text
        assert resp.json()["original_subject_id"] == "s1"
        assert resp.json()["replacement_subject_id"] == "s1-new"

        # 新编号承接旧库中的同一序列（逐周期揭盲重建一致）
        assert reveal_sequence(migrated, "legacy-rep", "s1-new", 2) == ["A", "B"]

        # 原编号已退役
        assert migrated.get(
            "/experiments/legacy-rep/subjects/s1/progress"
        ).status_code == 409

        events = migrated.get(
            "/experiments/legacy-rep/subject-replacements"
        ).json()
        assert len(events["replacements"]) == 1
        assert_no_sequence_leak(events)


# ---------- 换号与入组锁的状态一致性 ----------


def test_replace_blocked_when_original_subject_enrollment_locked(
    client: TestClient,
) -> None:
    # 回归：已持有入组锁的编号直接换号会让旧编号“挂着锁”、新编号却可被其他
    # 实验申请，导致同一受试者跨实验重复入组。修复后必须先释放锁再换号。
    e1, e2 = _make_two_experiments(client)
    held = acquire_locks(client, e1, ["a1"], "lock-a1")
    assert held.status_code == 201
    reservation_id = held.json()["reservation_id"]

    resp = replace_subject(client, e1, "a1", "a1-new", "rep-lock-1")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason"] == "enrollment_occupied"
    assert detail["operation_id"] == "rep-lock-1"
    assert detail["conflicts"] == [
        {
            "subject_id": "a1",
            "role": "original",
            "experiment_id": e1,
            "reservation_id": reservation_id,
            "version": 1,
        }
    ]
    assert_no_sequence_leak(detail)

    # 锁与分配保持不变：a1 仍由原 reservation 占用，a1-new 处于 released
    occ = client.get(
        "/enrollment-locks",
        params=[("subject_ids", "a1"), ("subject_ids", "a1-new")],
    ).json()
    assert occ["subjects"][0]["status"] == "held"
    assert occ["subjects"][0]["reservation_id"] == reservation_id
    assert occ["subjects"][1] == {"subject_id": "a1-new", "status": "released"}
    locks = client.get(f"/experiments/{e1}/enrollment-locks").json()
    assert [s["subject_id"] for s in locks["subjects"]] == ["a1"]

    # 隐藏分配未转移：a1 仍在册（进度可查、未揭盲），a1-new 不在册（404）
    assert client.get(f"/experiments/{e1}/subjects/a1/progress").status_code == 200
    assert client.get(f"/experiments/{e1}/subjects/a1-new/progress").status_code == 404

    # 未生成换号墓碑或审计
    assert client.get(f"/experiments/{e1}/subject-replacements").json()[
        "replacements"
    ] == []

    # 失败不消耗 operation_id：释放锁后用同一个键重试即首次成功
    assert release_locks(client, e1, reservation_id, 1, "rel-a1").status_code == 200
    retry = replace_subject(client, e1, "a1", "a1-new", "rep-lock-1")
    assert retry.status_code == 200, retry.text
    assert retry.json()["replacement_subject_id"] == "a1-new"


def test_replace_blocked_when_replacement_subject_enrollment_locked(
    client: TestClient,
) -> None:
    # 新编号被其他实验的有效 reservation 占用时同样拒绝，避免换号后重复入组
    e1, e2 = _make_two_experiments(client)
    held = acquire_locks(client, e2, ["a1-new"], "lock-new")
    assert held.status_code == 201
    reservation_id = held.json()["reservation_id"]

    resp = replace_subject(client, e1, "a1", "a1-new", "rep-lock-2")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "enrollment_occupied"
    assert detail["conflicts"] == [
        {
            "subject_id": "a1-new",
            "role": "replacement",
            "experiment_id": e2,
            "reservation_id": reservation_id,
            "version": 1,
        }
    ]

    # 零写入：a1 名册 / 分配未动，无墓碑或审计，锁仍由 e2 持有且清单不被暗改
    roster = client.get(f"/experiments/{e1}").json()
    assert [a["subject_id"] for a in roster["assignments"]] == ["a1", "a2"]
    assert client.get(f"/experiments/{e1}/subject-replacements").json()[
        "replacements"
    ] == []
    held_locks = client.get(f"/experiments/{e2}/enrollment-locks").json()["subjects"]
    assert [s["subject_id"] for s in held_locks] == ["a1-new"]

    # 失败不消耗 operation_id：e2 释放后同键首次成功
    assert release_locks(client, e2, reservation_id, 1, "rel-new").status_code == 200
    assert replace_subject(client, e1, "a1", "a1-new", "rep-lock-2").status_code == 200


def test_replace_lists_both_conflicts_when_original_and_new_occupied(
    client: TestClient,
) -> None:
    e1, e2 = _make_two_experiments(client)
    old = acquire_locks(client, e1, ["a1"], "lock-old").json()
    new = acquire_locks(client, e2, ["a1-new"], "lock-newer").json()

    resp = replace_subject(client, e1, "a1", "a1-new", "rep-lock-both")
    assert resp.status_code == 409
    conflicts = resp.json()["detail"]["conflicts"]
    # 两个被占编号一次性列出（按 subject_id 排序），角色分别标注
    assert conflicts == [
        {
            "subject_id": "a1",
            "role": "original",
            "experiment_id": e1,
            "reservation_id": old["reservation_id"],
            "version": 1,
        },
        {
            "subject_id": "a1-new",
            "role": "replacement",
            "experiment_id": e2,
            "reservation_id": new["reservation_id"],
            "version": 1,
        },
    ]
    # 整批冲突同样零写入
    assert client.get(f"/experiments/{e1}/subject-replacements").json()[
        "replacements"
    ] == []


def test_replace_after_release_succeeds_and_new_id_controls_enrollment(
    client: TestClient,
) -> None:
    # 正确流程：先释放旧编号占用的锁 → 换号 → 以新编号重新入组
    e1, e2 = _make_two_experiments(client)
    held = acquire_locks(client, e1, ["a1"], "lock-flow").json()
    assert release_locks(client, e1, held["reservation_id"], 1, "rel-flow").status_code == 200

    ok = replace_subject(client, e1, "a1", "a1-new", "rep-flow")
    assert ok.status_code == 200, ok.text

    # 换号后只有新编号能入组：旧编号已退役不再出现在持锁清单
    reacq = acquire_locks(client, e1, ["a1-new"], "lock-new-id")
    assert reacq.status_code == 201, reacq.text
    new_reservation = reacq.json()["reservation_id"]
    assert new_reservation != held["reservation_id"]

    occ = client.get(
        "/enrollment-locks",
        params=[("subject_ids", "a1"), ("subject_ids", "a1-new")],
    ).json()
    # 旧编号在入组模块仍为 released（换号不暗改 reservation），新编号被 e1 占用
    assert occ["subjects"][0] == {"subject_id": "a1", "status": "released"}
    assert occ["subjects"][1]["status"] == "held"
    assert occ["subjects"][1]["experiment_id"] == e1
    assert occ["subjects"][1]["reservation_id"] == new_reservation

    # 新编号被 e1 持锁期间，其他实验不能再占用它
    cross = acquire_locks(client, e2, ["a1-new"], "lock-cross")
    assert cross.status_code == 409
    assert cross.json()["detail"]["conflicts"][0]["experiment_id"] == e1

    # 旧 reservation 保持 released，没有任何受试者被挂回它
    assert client.get(f"/experiments/{e1}/enrollment-locks").json()[
        "subjects"
    ] == [
        {
            "subject_id": "a1-new",
            "reservation_id": new_reservation,
            "version": 1,
            "acquired_at": reacq.json()["created_at"],
        }
    ]


def test_replace_same_operation_id_replays_first_result_after_lock_conflict(
    client: TestClient,
) -> None:
    # 首次因占用被拒（不消耗 operation_id）；释放后同参数成功；
    # 之后即使编号再次被占，同参数重试仍回放首次成功结果
    e1, _ = _make_two_experiments(client)
    held = acquire_locks(client, e1, ["a1"], "lock-rep").json()

    blocked = replace_subject(client, e1, "a1", "a1-new", "rep-replay")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["reason"] == "enrollment_occupied"

    assert release_locks(client, e1, held["reservation_id"], 1, "rel-rep").status_code == 200
    first = replace_subject(client, e1, "a1", "a1-new", "rep-replay")
    assert first.status_code == 200
    first_body = first.json()

    # 重新用新编号入组（制造占用），同参数换号重试仍回放首次结果
    reacq = acquire_locks(client, e1, ["a1-new"], "lock-reacq")
    assert reacq.status_code == 201
    replay = replace_subject(client, e1, "a1", "a1-new", "rep-replay")
    assert replay.status_code == 200
    assert replay.json() == first_body

    # 重放不再次转移 / 不新增审计
    assert len(
        client.get(f"/experiments/{e1}/subject-replacements").json()["replacements"]
    ) == 1


# ---------- 受试者入组互斥（enrollment locks） ----------


def _make_two_experiments(client: TestClient) -> tuple[str, str]:
    """创建两个互不相关的双条件实验，返回 (experiment_id_1, experiment_id_2)。"""
    e1 = create_experiment(
        client, conditions=["A", "B"], subject_ids=["a1", "a2"], seed=1
    )[0]["experiment_id"]
    e2 = create_experiment(
        client, conditions=["A", "B"], subject_ids=["b1", "b2"], seed=2
    )[0]["experiment_id"]
    return e1, e2


def acquire_locks(
    client: TestClient,
    experiment_id: str,
    subject_ids: list[str],
    operation_id: str,
):
    return client.post(
        "/enrollment-locks",
        json={
            "experiment_id": experiment_id,
            "subject_ids": subject_ids,
            "operation_id": operation_id,
        },
    )


def release_locks(
    client: TestClient,
    experiment_id: str,
    reservation_id: str,
    expected_version: int,
    operation_id: str,
):
    return client.post(
        f"/experiments/{experiment_id}/enrollment-release",
        json={
            "reservation_id": reservation_id,
            "expected_version": expected_version,
            "operation_id": operation_id,
        },
    )


def test_enrollment_acquire_returns_immutable_reservation(client: TestClient) -> None:
    e1, _ = _make_two_experiments(client)
    resp = acquire_locks(client, e1, ["s1", "s2", "s3"], "enr-1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert re.fullmatch(r"[0-9a-f]{32}", body["reservation_id"])
    assert body["experiment_id"] == e1
    assert body["operation_id"] == "enr-1"
    assert body["status"] == "held"
    assert body["version"] == 1
    # 受试者严格按输入顺序返回，每个成员携带租约版本 1
    assert body["subjects"] == [
        {"subject_id": "s1", "version": 1},
        {"subject_id": "s2", "version": 1},
        {"subject_id": "s3", "version": 1},
    ]
    assert "created_at" in body
    assert_no_sequence_leak(body)


def test_enrollment_replay_same_operation_id_returns_same_reservation(
    client: TestClient,
) -> None:
    e1, _ = _make_two_experiments(client)
    first = acquire_locks(client, e1, ["s1", "s2"], "enr-replay")
    assert first.status_code == 201
    first_body = first.json()

    # 完全相同的参数重试：返回首次的不可变 reservation（同一 ID、同一时间戳）
    second = acquire_locks(client, e1, ["s1", "s2"], "enr-replay")
    assert second.status_code == 201
    assert second.json() == first_body


def test_enrollment_same_operation_id_different_params_returns_409(
    client: TestClient,
) -> None:
    e1, e2 = _make_two_experiments(client)
    assert acquire_locks(client, e1, ["s1", "s2"], "enr-shared").status_code == 201

    # 同一 operation_id 绑定不同受试者列表 → 409
    resp = acquire_locks(client, e1, ["s9"], "enr-shared")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "operation_id_conflict"
    assert detail["operation_id"] == "enr-shared"

    # 同一 operation_id 绑定不同实验 → 409
    resp = acquire_locks(client, e2, ["s1", "s2"], "enr-shared")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "operation_id_conflict"


def test_enrollment_conflict_any_occupied_returns_409_with_zero_writes(
    client: TestClient,
) -> None:
    e1, e2 = _make_two_experiments(client)
    held = acquire_locks(client, e1, ["s1", "s2"], "enr-e1")
    assert held.status_code == 201
    held_body = held.json()

    # e2 申请的批次中 s2 被 e1 占用：整批 409，冲突体回传当前占用实验与版本
    resp = acquire_locks(client, e2, ["s3", "s2", "s4"], "enr-e2")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "subjects_occupied"
    assert detail["operation_id"] == "enr-e2"
    assert detail["conflicts"] == [
        {
            "subject_id": "s2",
            "experiment_id": e1,
            "reservation_id": held_body["reservation_id"],
            "version": 1,
        }
    ]

    # 零写入：未冲突的 s3 / s4 仍处于 released（未登记），无新 reservation
    occ = client.get(
        "/enrollment-locks",
        params=[("subject_ids", "s2"), ("subject_ids", "s3"), ("subject_ids", "s4")],
    ).json()
    assert occ["subjects"][0]["experiment_id"] == e1
    assert occ["subjects"][1]["status"] == "released"
    assert occ["subjects"][2]["status"] == "released"

    # 失败不消耗 operation_id：释放 s2 后用同一个键重试即首次成功
    assert (
        release_locks(client, e1, held_body["reservation_id"], 1, "rel-e1").status_code
        == 200
    )
    retry = acquire_locks(client, e2, ["s3", "s2", "s4"], "enr-e2")
    assert retry.status_code == 201, retry.text


def test_enrollment_conflict_lists_all_occupied_subjects(client: TestClient) -> None:
    e1, e2 = _make_two_experiments(client)
    first = acquire_locks(client, e1, ["s1", "s2"], "enr-a").json()
    # 同一实验的另一批 reservation 占用 s3（互斥是 subject_id 全局键，
    # 即使占用方与申请方是同一实验，重叠受试者也冲突）
    second = acquire_locks(client, e1, ["s3"], "enr-b").json()

    resp = acquire_locks(client, e2, ["s1", "s3", "s9"], "enr-c")
    assert resp.status_code == 409
    conflicts = resp.json()["detail"]["conflicts"]
    # 全部被占用受试者一次性列出，s9 空闲不出现在冲突中
    assert {c["subject_id"] for c in conflicts} == {"s1", "s3"}
    by_subject = {c["subject_id"]: c for c in conflicts}
    assert by_subject["s1"]["reservation_id"] == first["reservation_id"]
    assert by_subject["s3"]["reservation_id"] == second["reservation_id"]


def test_enrollment_same_experiment_overlapping_subjects_conflicts(
    client: TestClient,
) -> None:
    e1, _ = _make_two_experiments(client)
    assert acquire_locks(client, e1, ["s1", "s2"], "enr-x").status_code == 201
    # 同一实验也不能重复登记同一受试者（一个 subject_id 同时只有一个 active 租约）
    resp = acquire_locks(client, e1, ["s2"], "enr-y")
    assert resp.status_code == 409
    assert resp.json()["detail"]["conflicts"][0]["experiment_id"] == e1


def test_enrollment_release_cas_success_and_clears_locks(client: TestClient) -> None:
    e1, _ = _make_two_experiments(client)
    body = acquire_locks(client, e1, ["s1", "s2"], "enr-rel").json()
    reservation_id = body["reservation_id"]

    resp = release_locks(client, e1, reservation_id, 1, "rel-1")
    assert resp.status_code == 200, resp.text
    released = resp.json()
    assert released["reservation_id"] == reservation_id
    assert released["experiment_id"] == e1
    assert released["action"] == "release"
    assert released["status"] == "released"
    assert released["version"] == 2  # CAS 成功：版本在期望版本上加一
    assert released["operation_id"] == "rel-1"
    assert [s["subject_id"] for s in released["subjects"]] == ["s1", "s2"]
    assert all(s["version"] == 2 for s in released["subjects"])
    assert "released_at" in released

    # 整批锁已释放：占用查询全部 released，实验持锁清单为空
    occ = client.get(
        "/enrollment-locks", params=[("subject_ids", "s1"), ("subject_ids", "s2")]
    ).json()
    assert all(s["status"] == "released" for s in occ["subjects"])
    locks = client.get(f"/experiments/{e1}/enrollment-locks").json()
    assert locks["subjects"] == []


def test_enrollment_release_version_mismatch_returns_409_and_keeps_locks(
    client: TestClient,
) -> None:
    e1, _ = _make_two_experiments(client)
    body = acquire_locks(client, e1, ["s1"], "enr-cas").json()

    resp = release_locks(client, e1, body["reservation_id"], 99, "rel-cas")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "reservation_version_mismatch"
    assert detail["current_version"] == 1
    assert detail["expected_version"] == 99

    # CAS 失败零写入：锁仍由 e1 持有，且不消耗 operation_id
    occ = client.get("/enrollment-locks", params=[("subject_ids", "s1")]).json()
    assert occ["subjects"][0]["status"] == "held"
    assert occ["subjects"][0]["experiment_id"] == e1
    retry = release_locks(client, e1, body["reservation_id"], 1, "rel-cas")
    assert retry.status_code == 200


def test_enrollment_release_idempotent_replay(client: TestClient) -> None:
    e1, _ = _make_two_experiments(client)
    body = acquire_locks(client, e1, ["s1", "s2"], "enr-idem").json()
    first = release_locks(client, e1, body["reservation_id"], 1, "rel-idem")
    assert first.status_code == 200
    # 相同 operation_id 重放：返回首次释放回执（同一 released_at / 版本，不重复释放）
    second = release_locks(client, e1, body["reservation_id"], 1, "rel-idem")
    assert second.status_code == 200
    assert second.json() == first.json()


def test_enrollment_release_with_other_operation_id_returns_409(
    client: TestClient,
) -> None:
    e1, _ = _make_two_experiments(client)
    body = acquire_locks(client, e1, ["s1"], "enr-dbl").json()
    assert release_locks(client, e1, body["reservation_id"], 1, "rel-first").status_code == 200

    # 已释放的 reservation 用新 operation_id 再释放：终态不可逆，409
    resp = release_locks(client, e1, body["reservation_id"], 1, "rel-second")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason"] == "reservation_already_released"

    # 用释放后的新版本号也不能再次释放
    resp = release_locks(client, e1, body["reservation_id"], 2, "rel-third")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "reservation_already_released"


def test_enrollment_reacquire_after_release_and_old_reservation_not_revived(
    client: TestClient,
) -> None:
    e1, e2 = _make_two_experiments(client)
    first = acquire_locks(client, e1, ["s1", "s2"], "enr-old").json()
    assert release_locks(client, e1, first["reservation_id"], 1, "rel-old").status_code == 200

    # 释放后可被其他 active 实验（也可以是同一实验的新 reservation）重新入组
    second = acquire_locks(client, e2, ["s1", "s2"], "enr-new")
    assert second.status_code == 201
    second_body = second.json()
    assert second_body["reservation_id"] != first["reservation_id"]
    assert second_body["version"] == 1

    occ = client.get(
        "/enrollment-locks", params=[("subject_ids", "s1"), ("subject_ids", "s2")]
    ).json()
    assert all(s["experiment_id"] == e2 for s in occ["subjects"])
    assert all(s["reservation_id"] == second_body["reservation_id"] for s in occ["subjects"])

    # 旧 reservation 不得复活：旧申请 operation_id 重放只返回首次回执，
    # 不会把锁从新 reservation 改挂回旧 reservation
    replay = acquire_locks(client, e1, ["s1", "s2"], "enr-old")
    assert replay.status_code == 201
    assert replay.json()["reservation_id"] == first["reservation_id"]
    occ_after = client.get("/enrollment-locks", params=[("subject_ids", "s1")]).json()
    assert occ_after["subjects"][0]["reservation_id"] == second_body["reservation_id"]

    # 旧 reservation 的释放重放同样不复活旧锁、不影响新锁
    replay_release = release_locks(client, e1, first["reservation_id"], 1, "rel-old")
    assert replay_release.status_code == 200
    occ_final = client.get("/enrollment-locks", params=[("subject_ids", "s1")]).json()
    assert occ_final["subjects"][0]["experiment_id"] == e2


def test_enrollment_operation_id_space_shared_acquire_and_release(
    client: TestClient,
) -> None:
    e1, e2 = _make_two_experiments(client)
    body = acquire_locks(client, e1, ["s1"], "shared-op").json()
    # 释放复用申请的 operation_id → 全局空间冲突
    resp = release_locks(client, e1, body["reservation_id"], 1, "shared-op")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "operation_id_conflict"

    # 反向：释放键再被申请复用同样 409
    assert release_locks(client, e1, body["reservation_id"], 1, "release-op").status_code == 200
    resp = acquire_locks(client, e2, ["s9"], "release-op")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == "operation_id_conflict"


def test_enrollment_release_unknown_or_other_experiment_returns_404(
    client: TestClient,
) -> None:
    e1, e2 = _make_two_experiments(client)
    body = acquire_locks(client, e1, ["s1"], "enr-404").json()

    # reservation 不属于路径中的实验（跨实验释放）：404，不泄露存在性
    resp = release_locks(client, e2, body["reservation_id"], 1, "rel-cross")
    assert resp.status_code == 404

    # 不存在的 reservation：404
    resp = release_locks(client, e1, "nope", 1, "rel-missing")
    assert resp.status_code == 404


def test_enrollment_occupancy_query_shows_holder_version_and_order(
    client: TestClient,
) -> None:
    e1, e2 = _make_two_experiments(client)
    held = acquire_locks(client, e1, ["s1"], "enr-q1").json()
    acquire_locks(client, e2, ["s3"], "enr-q2")

    resp = client.get(
        "/enrollment-locks",
        params=[
            ("subject_ids", "s9"),      # 从未登记 → released
            ("subject_ids", "s1"),      # e1 持有
            ("subject_ids", "s3"),      # e2 持有
            ("subject_ids", "s8"),      # 从未登记 → released
        ],
    )
    assert resp.status_code == 200
    subjects = resp.json()["subjects"]
    # 严格按输入顺序返回
    assert [s["subject_id"] for s in subjects] == ["s9", "s1", "s3", "s8"]
    # released 条目只含 subject_id / status，不含占用实验等字段
    assert subjects[0] == {"subject_id": "s9", "status": "released"}
    assert subjects[3] == {"subject_id": "s8", "status": "released"}
    # held 条目显示当前占用实验、reservation 与版本
    assert subjects[1] == {
        "subject_id": "s1",
        "status": "held",
        "experiment_id": e1,
        "reservation_id": held["reservation_id"],
        "version": 1,
        "acquired_at": held["created_at"],
    }
    assert subjects[2]["experiment_id"] == e2
    assert subjects[2]["version"] == 1

    # 释放后查询变为 released
    release_locks(client, e1, held["reservation_id"], 1, "rel-q1")
    after = client.get("/enrollment-locks", params=[("subject_ids", "s1")]).json()
    assert after["subjects"][0] == {"subject_id": "s1", "status": "released"}


def test_enrollment_experiment_locks_listing(client: TestClient) -> None:
    e1, e2 = _make_two_experiments(client)
    first = acquire_locks(client, e1, ["s2", "s1"], "enr-list-1").json()
    acquire_locks(client, e1, ["s3"], "enr-list-2")
    acquire_locks(client, e2, ["s9"], "enr-list-3")

    resp = client.get(f"/experiments/{e1}/enrollment-locks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["experiment_id"] == e1
    # 只含当前持锁，按 subject_id 稳定排序，不含已释放历史
    assert [s["subject_id"] for s in body["subjects"]] == ["s1", "s2", "s3"]
    s2 = next(s for s in body["subjects"] if s["subject_id"] == "s2")
    assert s2["reservation_id"] == first["reservation_id"]
    assert s2["version"] == 1
    assert s2["acquired_at"] == first["created_at"]

    # 释放 s1/s2 后清单只剩 s3
    release_locks(client, e1, first["reservation_id"], 1, "rel-list-1")
    body = client.get(f"/experiments/{e1}/enrollment-locks").json()
    assert [s["subject_id"] for s in body["subjects"]] == ["s3"]


def test_enrollment_events_listed_in_reverse_chronological_order(
    client: TestClient,
) -> None:
    e1, _ = _make_two_experiments(client)
    body = acquire_locks(client, e1, ["s1", "s2"], "enr-ev").json()
    release_locks(client, e1, body["reservation_id"], 1, "rel-ev")

    resp = client.get(f"/experiments/{e1}/enrollment-events")
    assert resp.status_code == 200
    events = resp.json()["events"]
    # 时间倒序：release 在前、acquire 在后
    assert [e["action"] for e in events] == ["release", "acquire"]
    assert events[0]["version"] == 2
    assert events[1]["version"] == 1
    for event in events:
        assert event["experiment_id"] == e1
        assert event["reservation_id"] == body["reservation_id"]
        assert event["subjects"] == ["s1", "s2"]
    assert events[0]["operation_id"] == "rel-ev"
    assert events[1]["operation_id"] == "enr-ev"
    # 审计只含编号 / 版本元数据
    assert_no_sequence_leak(events)

    # 其他实验看不到该实验的审计；未知实验 404
    e1b, _ = _make_two_experiments(client)
    assert client.get(f"/experiments/{e1b}/enrollment-events").json()["events"] == []
    assert client.get("/experiments/unknown/enrollment-events").status_code == 404

    # 分页参数
    paged = client.get(
        f"/experiments/{e1}/enrollment-events", params={"limit": 1, "offset": 1}
    ).json()
    assert [e["action"] for e in paged["events"]] == ["acquire"]


def test_enrollment_unknown_experiment_acquire_returns_404(client: TestClient) -> None:
    resp = acquire_locks(client, "no-such-experiment", ["s1"], "enr-noexp")
    assert resp.status_code == 404
    # 失败零写入：占用查询仍为 released
    occ = client.get("/enrollment-locks", params=[("subject_ids", "s1")]).json()
    assert occ["subjects"][0]["status"] == "released"
    # 未知实验的持锁清单也是 404
    assert client.get("/experiments/no-such-experiment/enrollment-locks").status_code == 404


def test_enrollment_allowed_while_experiment_paused(client: TestClient) -> None:
    e1, _ = _make_two_experiments(client)
    # 暂停只门禁揭盲；入组互斥是独立模块，暂停期间仍可申请与释放
    pause = client.post(
        f"/experiments/{e1}/pause",
        json={"operation_id": "pause-1", "reason": "暂停复核"},
    )
    assert pause.status_code == 200

    body = acquire_locks(client, e1, ["s1"], "enr-paused")
    assert body.status_code == 201, body.text
    reservation_id = body.json()["reservation_id"]
    rel = release_locks(client, e1, reservation_id, 1, "rel-paused")
    assert rel.status_code == 200


def test_enrollment_payload_validation(client: TestClient) -> None:
    e1, _ = _make_two_experiments(client)

    def post(subject_ids, operation_id="enr-val", experiment_id=e1):
        return client.post(
            "/enrollment-locks",
            json={
                "experiment_id": experiment_id,
                "subject_ids": subject_ids,
                "operation_id": operation_id,
            },
        )

    assert post([]).status_code == 422                      # 空批次
    assert post(["s1", "s1"]).status_code == 422           # 批内重复
    assert post([""]).status_code == 422                   # 空字符串
    assert post(["  "]).status_code == 422                 # 纯空白
    assert post([f"s{i}" for i in range(101)]).status_code == 422  # 超过 100
    assert post(["s1"], operation_id="").status_code == 422
    assert post(["s1"], experiment_id="").status_code == 422

    # 100 个为上界，合法
    ok = post([f"s{i}" for i in range(100)], operation_id="enr-100")
    assert ok.status_code == 201, ok.text


def test_enrollment_release_payload_validation(client: TestClient) -> None:
    e1, _ = _make_two_experiments(client)
    body = acquire_locks(client, e1, ["s1"], "enr-rval").json()
    base = f"/experiments/{e1}/enrollment-release"

    assert client.post(
        base,
        json={"reservation_id": body["reservation_id"], "expected_version": 0,
              "operation_id": "rel-bad"},
    ).status_code == 422  # expected_version 必须 >= 1
    assert client.post(
        base,
        json={"reservation_id": "", "expected_version": 1,
              "operation_id": "rel-bad"},
    ).status_code == 422
    assert client.post(
        base,
        json={"reservation_id": body["reservation_id"], "expected_version": 1,
              "operation_id": ""},
    ).status_code == 422


def test_enrollment_occupancy_query_validation(client: TestClient) -> None:
    base = "/enrollment-locks"
    assert client.get(base).status_code == 422                      # 缺少 subject_ids
    assert client.get(
        base, params=[("subject_ids", "s1"), ("subject_ids", "s1")]
    ).status_code == 422                                            # 重复
    assert client.get(base, params=[("subject_ids", "")]).status_code == 422
    assert client.get(
        base, params=[("subject_ids", f"s{i}") for i in range(101)]
    ).status_code == 422                                            # 超过 100
    # limit / offset 非法（FastAPI Query 约束）
    assert client.get(
        f"/experiments/{'x'}/enrollment-events", params={"limit": 0}
    ).status_code == 422


def test_concurrent_enrollment_across_experiments_has_single_winner(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "enroll-race.db"))
    with TestClient(app) as setup:
        e1 = create_experiment(
            setup, conditions=["A", "B"], subject_ids=["a1", "a2"], seed=1
        )[0]["experiment_id"]
        e2 = create_experiment(
            setup, conditions=["A", "B"], subject_ids=["b1", "b2"], seed=2
        )[0]["experiment_id"]

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    statuses: list[int] = []
    winners: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        experiment_id = e1 if i % 2 == 0 else e2
        with TestClient(app) as local:
            barrier.wait()
            resp = acquire_locks(local, experiment_id, ["s1", "s2"], f"race-{i}")
            with lock:
                statuses.append(resp.status_code)
                if resp.status_code == 201:
                    winners.append(resp.json()["reservation_id"])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # BEGIN IMMEDIATE 串行化：只有一次申请成功，其余整批 409
    assert statuses.count(201) == 1
    assert statuses.count(409) == n_threads - 1
    assert len(winners) == 1

    with TestClient(app) as verify:
        # 每个受试者全局只有一行当前占用，且都属于同一个胜出 reservation
        occ = verify.get(
            "/enrollment-locks",
            params=[("subject_ids", "s1"), ("subject_ids", "s2")],
        ).json()
        assert {s["reservation_id"] for s in occ["subjects"]} == set(winners)

    import sqlite3

    with sqlite3.connect(tmp_path / "enroll-race.db") as conn:
        # 每个 subject_id 至多一行锁；只有一份 held reservation
        rows = conn.execute(
            "SELECT subject_id, COUNT(*) FROM enrollment_locks GROUP BY subject_id"
        ).fetchall()
        assert sorted(rows) == [("s1", 1), ("s2", 1)]
        assert conn.execute(
            "SELECT COUNT(*) FROM enrollment_reservations WHERE status = 'held'"
        ).fetchone()[0] == 1


def test_legacy_database_supports_enrollment_locks_after_migration(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "legacy-enroll.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))

    # 模拟旧版本数据库：只有 experiments / assignments 两张表
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE experiments (
                id TEXT PRIMARY KEY,
                conditions TEXT NOT NULL,
                seed INTEGER NOT NULL,
                group_size INTEGER NOT NULL,
                num_subjects INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE assignments (
                experiment_id TEXT NOT NULL REFERENCES experiments(id),
                subject_id TEXT NOT NULL,
                sequence_index INTEGER NOT NULL,
                sequence TEXT NOT NULL,
                PRIMARY KEY (experiment_id, subject_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO experiments "
            "(id, conditions, seed, group_size, num_subjects, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-enr", '["A","B"]', 1, 2, 2, "2026-01-01T00:00:00+00:00"),
        )

    # 启动自动建表迁移后，入组互斥链路在旧库上可用
    with TestClient(app) as migrated:
        resp = acquire_locks(migrated, "legacy-enr", ["s1", "s2"], "enr-legacy")
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["experiment_id"] == "legacy-enr"

        occ = migrated.get(
            "/enrollment-locks",
            params=[("subject_ids", "s1"), ("subject_ids", "s2")],
        ).json()
        assert all(s["status"] == "held" for s in occ["subjects"])

        released = release_locks(
            migrated, "legacy-enr", body["reservation_id"], 1, "rel-legacy"
        )
        assert released.status_code == 200
        assert released.json()["status"] == "released"

        # 释放后可重新入组
        again = acquire_locks(migrated, "legacy-enr", ["s1"], "enr-legacy-2")
        assert again.status_code == 201
        assert again.json()["reservation_id"] != body["reservation_id"]
