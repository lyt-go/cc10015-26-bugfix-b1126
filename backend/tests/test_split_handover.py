# -*- coding: utf-8 -*-
"""沉积物母样分割与整支移交的库存处置测试。

覆盖需求：
1. 两站录入相同编号时，分割只更新该份沉积物母样的登记，水样和其他站位同号项保留原库位；
2. 同号且无法定位唯一母样时分割保持待核，补证后可复核继续；
3. 分割与移交对同一母样冲突仲裁，两边可查到最终处置（整支移交 / 分割留库）；
4. 分割重复提交只保留一次库位变更，已移交库存不能再回到可领；
5. 同一份母样不能同时成为整支已移交库存和分割留库库存；
6. 验收主线：水样 A 与岩芯 B 同用 SD21，交错安排整支移交与分割确认。
"""
from __future__ import annotations

import threading

import pytest

from app.services.sample_registry import SampleRegistryService
from app.services.sediment_sample import SedimentSampleService
from app.store import store


@pytest.fixture(autouse=True)
def reset_store():
    store.reset()


def _registry_rows():
    return store.rows("sample_registry")


def _sediment_rows():
    return store.rows("sediment_sample")


def _add_registry(entry_id, sample_no, sample_type, station, location="待分配",
                  status="已登记"):
    _registry_rows().append({
        "id": entry_id,
        "样品编号": sample_no,
        "样品类型": sample_type,
        "采集站位": station,
        "保存方式": "未分割",
        "存放位置": location,
        "登记人": "测试登记员",
        "登记日期": "2026-11-25",
        "备注说明": "",
        "status": status,
        "pending": True,
        "abnormal": False,
    })


def _add_sediment(entry_id, sample_no, station, segments="5段", status="已登记"):
    _sediment_rows().append({
        "id": entry_id,
        "status": status,
        "pending": status != "已分割",
        "abnormal": False,
        "采样站位": station,
        "采样深度": "0-30cm",
        "柱状长度": segments,
        "岩性描述": "粉砂质泥",
        "样品编号": sample_no,
        "采样人": "岩芯管理员",
        "采样时间": "2026-11-25",
        "备注说明": "",
    })


def test_app_router_imports_cleanly():
    """路由层语法/装配错误应在测试阶段暴露，而不是等到服务启动。"""
    from app.main import app

    path = app.url_path_for("disposal_outcome", sample_no="SD21")
    assert path == "/api/sample-registry/disposal/SD21"


# ============================================================
# 1. 根因修复：同编号两站 + 水样，分割只改该份沉积物母样
# ============================================================
def test_split_only_updates_matching_sediment_mother():
    """水样 A（STAT-W）与岩芯 B（STAT-B）同用 SD21，外加另一站的同号岩芯。"""
    _add_registry(101, "SD21", "水样", "STAT-W", location="水样柜-W01")
    _add_registry(102, "SD21", "沉积物（岩芯）", "STAT-B", location="待分配")
    _add_registry(103, "SD21", "沉积物（岩芯）", "STAT-C", location="岩芯架-C12")
    _add_sediment(201, "SD21", "STAT-B")

    svc = SedimentSampleService()
    entry, msg = svc.run_action(201, "分割样品")
    assert entry is not None, f"分割应成功: {msg}"
    assert entry["status"] == "已分割"

    water = store.find("sample_registry", 101)
    core_b = store.find("sample_registry", 102)
    core_c = store.find("sample_registry", 103)

    # 水样和其他站位同号项全部保有原库位
    assert water["存放位置"] == "水样柜-W01"
    assert water["保存方式"] == "未分割"
    assert water["status"] == "已登记"
    assert core_c["存放位置"] == "岩芯架-C12"
    assert core_c["保存方式"] == "未分割"

    # 只有该份母样的登记被更新
    assert core_b["存放位置"] == "沉积物库-STAT-B"
    assert "分割" in core_b["保存方式"]
    assert core_b["处置回执"] == "分割留库"

    outcome = SampleRegistryService().disposal_outcome("SD21")
    assert outcome["最终处置"] == "分割留库"
    assert outcome["存在冲突"] is False
    assert len(outcome["分割留库"]) == 1
    assert len(outcome["整支移交"]) == 0


def test_split_does_not_touch_first_row_when_water_sample_is_first():
    """旧根因：find_one_by 取第一条；第一条恰好是同号水样时会被错改库位。"""
    _add_registry(201, "SD21", "海水样品", "STAT-W", location="水样柜-W02")
    _add_registry(202, "SD21", "沉积物", "STAT-B")
    _add_sediment(301, "SD21", "STAT-B")

    entry, msg = SedimentSampleService().run_action(301, "分割样品")
    assert entry is not None, msg

    water = store.find("sample_registry", 201)
    assert water["存放位置"] == "水样柜-W02"
    assert water.get("处置回执") is None

    core = store.find("sample_registry", 202)
    assert core["存放位置"] == "沉积物库-STAT-B"


# ============================================================
# 2. 同号无法定位唯一母样：保持待核，补证后复核继续
# ============================================================
def test_ambiguous_same_number_keeps_pending_verification():
    # 两条同站同号沉积物登记，无法确定唯一母样
    _add_registry(301, "SD22", "沉积物（岩芯）", "STAT-B", location="岩芯架-B1")
    _add_registry(302, "SD22", "沉积物（岩芯）", "STAT-B", location="岩芯架-B2")
    _add_registry(303, "SD22", "水样", "STAT-W", location="水样柜-W03")
    _add_sediment(401, "SD22", "STAT-B")

    entry, msg = SedimentSampleService().run_action(401, "分割样品")
    assert entry is not None  # 入口返回待核记录而非异常
    assert entry["status"] == "待核"
    assert "待核" in msg

    # 库位一律未动
    assert store.find("sample_registry", 301)["存放位置"] == "岩芯架-B1"
    assert store.find("sample_registry", 302)["存放位置"] == "岩芯架-B2"
    water = store.find("sample_registry", 303)
    assert water["存放位置"] == "水样柜-W03"
    assert water["status"] == "已登记"

    # 沉积物候选进入待核，水样不被标待核
    assert store.find("sample_registry", 301)["status"] == "待核"
    assert store.find("sample_registry", 302)["status"] == "待核"

    # 待核期间未标记幂等，管理员补证后可继续复核
    sedi = store.find("sediment_sample", 401)
    sedi["备注说明"] = "补证：母样对应登记#301，附分割回执单"
    # 复核时仍无法唯一区分（数据未修正），继续待核
    entry, msg = SedimentSampleService().run_action(401, "复核分割")
    assert entry["status"] == "待核"

    # 补证：移除重复登记后复核成功
    store.find("sample_registry", 302)["样品编号"] = "SD22-DUP"
    entry, msg = SedimentSampleService().run_action(401, "复核分割")
    assert entry["status"] == "已分割", f"补证后复核应成功: {msg}"
    target = store.find("sample_registry", 301)
    assert target["存放位置"] == "沉积物库-STAT-B"
    assert target["status"] != "待核"


def test_split_without_any_sediment_registry_pending():
    """同编号只登记了水样：查不到沉积物母样，分割待核且水样库位不动。"""
    _add_registry(401, "SD23", "海水样品", "STAT-W", location="水样柜-W04")
    _add_sediment(501, "SD23", "STAT-B")

    entry, msg = SedimentSampleService().run_action(501, "分割样品")
    assert entry["status"] == "待核"
    water = store.find("sample_registry", 401)
    assert water["存放位置"] == "水样柜-W04"
    assert water["status"] == "已登记"

    # 补登沉积物母样后复核成功，水样仍不动
    _add_registry(402, "SD23", "沉积物（岩芯）", "STAT-B")
    entry, msg = SedimentSampleService().run_action(501, "复核分割")
    assert entry["status"] == "已分割", msg
    assert store.find("sample_registry", 401)["存放位置"] == "水样柜-W04"
    assert store.find("sample_registry", 402)["存放位置"] == "沉积物库-STAT-B"


# ============================================================
# 3. 分割/移交冲突仲裁：同一份母样只有一种处置结果
# ============================================================
def test_handover_blocked_after_split_kept():
    _add_registry(501, "SD24", "沉积物（岩芯）", "STAT-B")
    _add_sediment(601, "SD24", "STAT-B")

    # 先分割留库
    entry, _ = SedimentSampleService().run_action(601, "分割样品")
    assert entry["status"] == "已分割"

    # 再办整支移交：必须拒绝，剩余库存仍在库
    reg_svc = SampleRegistryService()
    result, msg = reg_svc.run_action(501, "移交样品")
    assert result is None
    assert "分割" in msg or "整支" in msg
    assert store.find("sample_registry", 501)["status"] != "已移交"

    outcome = reg_svc.disposal_outcome("SD24")
    assert outcome["最终处置"] == "分割留库"
    assert outcome["存在冲突"] is False


def test_split_blocked_after_whole_handover():
    _add_registry(601, "SD25", "沉积物（岩芯）", "STAT-B", status="已入库")
    _add_sediment(701, "SD25", "STAT-B")

    # 先整支移交
    result, msg = SampleRegistryService().run_action(601, "移交样品")
    assert result is not None, msg
    assert result["处置回执"] == "整支移交"
    assert result["pending"] is False

    # 再提交分割：必须拒绝，已交出的库存不能再回到可领
    entry, msg = SedimentSampleService().run_action(701, "分割样品")
    assert entry is None
    assert "冲突" in msg or "移交" in msg

    handed = store.find("sample_registry", 601)
    assert handed["status"] == "已移交"
    assert handed["pending"] is False
    assert handed["处置回执"] == "整支移交"
    assert handed.get("存放位置") != "沉积物库-STAT-B"

    outcome = SampleRegistryService().disposal_outcome("SD25")
    assert outcome["最终处置"] == "整支移交"
    assert outcome["存在冲突"] is False
    assert len(outcome["分割留库"]) == 0


def test_interleaved_handover_and_split_acceptance_scenario():
    """验收主线：水样 A 和岩芯 B 同用 SD21，先为 B 办分割，再交错移交/确认。"""
    _add_registry(701, "SD21", "水样", "STAT-A", location="水样柜-A01")
    _add_registry(702, "SD21", "沉积物（岩芯）", "STAT-B", status="已入库")
    _add_sediment(801, "SD21", "STAT-B")
    reg_svc = SampleRegistryService()
    sedi_svc = SedimentSampleService()

    # 第一步：为 B 办分割，复核两个库位
    entry, msg = sedi_svc.run_action(801, "分割样品")
    assert entry is not None and entry["status"] == "已分割", msg
    assert store.find("sample_registry", 701)["存放位置"] == "水样柜-A01"
    assert store.find("sample_registry", 702)["存放位置"] == "沉积物库-STAT-B"

    # 第二步：交错安排 B 的整支移交 —— 已分割留库，移交被拒
    result, msg = reg_svc.run_action(702, "移交样品")
    assert result is None
    assert store.find("sample_registry", 702)["status"] != "已移交"

    # 第三步：再次提交分割回执（重复确认），库存不发生第二次变更
    before_location = store.find("sample_registry", 702)["存放位置"]
    before_remark = store.find("sample_registry", 702).get("备注说明", "")
    entry2, msg2 = sedi_svc.run_action(801, "分割样品")
    assert entry2 is None
    assert "重复" in msg2 or "已执行" in msg2
    registry = store.find("sample_registry", 702)
    assert registry["存放位置"] == before_location
    assert registry.get("备注说明", "") == before_remark
    assert len(registry.get("分割回执", [])) == 1

    outcome = reg_svc.disposal_outcome("SD21")
    assert outcome["最终处置"] == "分割留库"
    assert outcome["存在冲突"] is False
    # 两个库位均可核对：水样原位、母样留库位
    locations = {item["id"]: item["存放位置"] for item in outcome["登记明细"]}
    assert locations[701] == "水样柜-A01"
    assert locations[702] == "沉积物库-STAT-B"


def test_interleaved_split_then_handover_wins_when_handover_first():
    """交错顺序反过来：移交先落地时，随后的分割确认必须失败。"""
    _add_registry(801, "SD26", "沉积物（岩芯）", "STAT-B", status="已入库")
    _add_sediment(901, "SD26", "STAT-B")

    result, msg = SampleRegistryService().run_action(801, "移交样品")
    assert result is not None, msg

    entry, msg = SedimentSampleService().run_action(901, "分割样品")
    assert entry is None
    assert "冲突" in msg or "移交" in msg

    # 再次提交分割仍被拒，且不会把已移交库存拉回可领
    entry, msg = SedimentSampleService().run_action(901, "分割样品")
    assert entry is None
    handed = store.find("sample_registry", 801)
    assert handed["status"] == "已移交"
    assert handed["pending"] is False


# ============================================================
# 4. 幂等：重复移交只保留一次处置
# ============================================================
def test_duplicate_handover_is_idempotent():
    _add_registry(901, "SD27", "沉积物（岩芯）", "STAT-B", status="已入库")
    reg_svc = SampleRegistryService()

    first, msg = reg_svc.run_action(901, "移交样品")
    assert first is not None, msg
    second, msg2 = reg_svc.run_action(901, "移交样品")
    assert second is None
    assert "重复" in msg2 or "已执行" in msg2
    assert store.find("sample_registry", 901)["status"] == "已移交"


# ============================================================
# 5. 并发：分割与移交同时发生，只有一方落地
# ============================================================
def test_concurrent_split_and_handover_only_one_outcome():
    _add_registry(1001, "SD28", "沉积物（岩芯）", "STAT-B", status="已入库")
    _add_sediment(1101, "SD28", "STAT-B")

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def do_split():
        barrier.wait()
        entry, msg = SedimentSampleService().run_action(1101, "分割样品")
        outcomes.append("split-ok" if entry is not None else "split-rejected")

    def do_handover():
        barrier.wait()
        entry, msg = SampleRegistryService().run_action(1001, "移交样品")
        outcomes.append("handover-ok" if entry is not None else "handover-rejected")

    t1 = threading.Thread(target=do_split)
    t2 = threading.Thread(target=do_handover)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert len(outcomes) == 2
    ok_count = sum(1 for o in outcomes if o.endswith("-ok"))
    assert ok_count == 1, f"只能有一种处置落地，实际: {outcomes}"

    registry = store.find("sample_registry", 1001)
    summary = SampleRegistryService().disposal_outcome("SD28")
    assert summary["存在冲突"] is False
    if "handover-ok" in outcomes:
        assert registry["status"] == "已移交"
        assert registry["处置回执"] == "整支移交"
        assert summary["最终处置"] == "整支移交"
    else:
        assert registry["处置回执"] == "分割留库"
        assert registry["存放位置"] == "沉积物库-STAT-B"
        assert summary["最终处置"] == "分割留库"
