"""样品登记领域服务：样品全生命周期管理，接受采样、拖网、沉积物等模块自动登记。

库存处置（分割留库 / 整支移交）在本服务内统一仲裁，保证同一份母样只有一种
处置结果；分割回执只允许命中该沉积物母样对应的唯一登记项，避免同编号样品
（例如两站录入相同编号、水样与岩芯共用编号）互相覆盖库位。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 沉积物母样的样品类型标识，分割回执只作用于这类登记
SEDIMENT_TYPE_TOKENS = ("沉积物", "岩芯")


class SampleRegistryService(BaseModuleService):
    MODULE = "sample_registry"
    ENTITY = "样品登记簿"
    OPERATION_TERM = "确认登记"
    LIST_FIELDS = ["样品编号", "样品类型", "采集站位", "保存方式", "存放位置", "登记人", "登记日期", "备注说明"]
    REQUIRED_FIELDS = ["样品编号", "样品类型", "采集站位"]
    STATUS_ORDER = ["待登记", "已登记", "已入库", "待核", "已移交"]
    ACTION_RULES = {"确认登记": "已登记", "执行入库": "已入库", "移交样品": "已移交"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 通用动作
    # ------------------------------------------------------------------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        # 移交涉及与分割的跨记录冲突仲裁，统一走事务锁入口
        if action == "移交样品":
            return self.handover_with_conflict_check(entry_id)

        # 幂等检查
        idempotent_key = f"{self.MODULE}:{entry_id}:{action}"
        if store.is_idempotent(idempotent_key):
            return None, f"动作「{action}」已执行，请勿重复操作"

        entry["status"] = target
        entry["pending"] = target in ("待登记", "已登记", "待核")
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS

        store.mark_idempotent(idempotent_key)

        return entry, f"{self.ENTITY}已{action}"

    def is_sample_checked_in(self, sample_no: str) -> bool:
        """检查样品是否已入库。"""
        existing = store.find_one_by(self.MODULE, "样品编号", sample_no)
        if existing and existing.get("status") == "已入库":
            return True
        return False

    def get_samples_by_station(self, station: str) -> list[dict[str, Any]]:
        """获取某站位下的所有样品。"""
        return store.find_by(self.MODULE, "采集站位", station)

    # ------------------------------------------------------------------
    # 母样定位：分割入口的根因修复
    # ------------------------------------------------------------------
    @staticmethod
    def _is_sediment(row: dict[str, Any]) -> bool:
        sample_type = str(row.get("样品类型", ""))
        return any(token in sample_type for token in SEDIMENT_TYPE_TOKENS)

    def locate_mother_registry(
        self, sample_no: str, station: str | None = None
    ) -> tuple[dict[str, Any] | None, str, list[dict[str, Any]]]:
        """按「同编号 + 同站位 + 沉积物类型」定位母样对应的唯一登记项。

        返回 (登记项或None, 结果说明, 同编号候选列表)。
        - 命中唯一沉积物母样登记：返回该登记，状态 ``located``；
        - 同编号存在多个沉积物登记但站位无法唯一区分：返回 None，状态 ``ambiguous``；
        - 同编号但全部不是沉积物（如水样）或站位不匹配：返回 None，状态 ``not_found``，
          调用方必须保持待核，绝不能退回「取第一条」的旧逻辑。
        """
        candidates = store.find_by(self.MODULE, "样品编号", sample_no)
        sediment_rows = [row for row in candidates if self._is_sediment(row)]

        station_key = str(station or "").strip()
        if station_key:
            station_matches = [
                row for row in sediment_rows
                if str(row.get("采集站位", "")).strip() == station_key
            ]
            if len(station_matches) == 1:
                return station_matches[0], "located", candidates
            if len(station_matches) > 1:
                return None, "ambiguous", candidates
            # 站位无匹配：不能因为同编号就改写别的站位/别的类型的库位
            if sediment_rows:
                return None, "ambiguous", candidates
            return None, "not_found", candidates

        if len(sediment_rows) == 1:
            return sediment_rows[0], "located", candidates
        if len(sediment_rows) > 1:
            return None, "ambiguous", candidates
        return None, "not_found", candidates

    # ------------------------------------------------------------------
    # 分割回执：由沉积物分割入口在事务锁内调用
    # ------------------------------------------------------------------
    def apply_split_receipt(
        self,
        mother_entry: dict[str, Any],
        *,
        segments: str,
        storage_location: str,
        source_module: str = "sediment_sample",
        source_id: int | None = None,
    ) -> dict[str, Any]:
        """把母样分割回执落到该母样对应的登记项上，只更新这一条登记。

        幂等：同一分割来源重复提交只保留一次库位变更。
        已整支移交的母样不能再回到可领（不允许分割留库覆盖移交结果）。
        """
        registry_id = int(mother_entry["id"])
        lock = store.lock_for(f"{self.MODULE}:{registry_id}")
        with lock:
            registry = store.find(self.MODULE, registry_id)
            if registry is None:
                raise ValueError(f"母样登记 {registry_id} 不存在或已归档")

            receipt_key = f"split:{source_module}:{source_id}"
            receipts = registry.setdefault("分割回执", [])
            if receipt_key in receipts:
                # 重复提交：不做第二次库位变更，直接返回现有登记
                registry["pending"] = registry.get("status") in ("待登记", "已登记", "待核")
                return dict(registry)

            if registry.get("处置回执") == "整支移交" or registry.get("status") == "已移交":
                raise ConflictError(
                    f"母样「{registry.get('样品编号', '')}」已整支移交，不能再分割留库"
                )

            registry["保存方式"] = "分层保存（已分割）"
            registry["存放位置"] = storage_location
            registry["处置回执"] = "分割留库"
            registry["分割回执"] = receipts + [receipt_key]
            registry["分割段数"] = segments
            remark = f"母样分割回执：已分割为{segments}，留库位置 {storage_location}"
            registry["备注说明"] = self._append_remark(registry.get("备注说明", ""), remark)
            # 分割留库后回到可入库/可领流程，不再待核
            if registry.get("status") == "待核":
                registry["status"] = "已登记"
            registry["pending"] = registry.get("status") in ("待登记", "已登记")
            return dict(registry)

    def mark_pending_verification(
        self, mother_entry: dict[str, Any], reason: str
    ) -> dict[str, Any] | None:
        """同号但无法定位唯一母样时，把沉积物候选登记保持/标记为待核。

        只处置沉积物类型的候选；水样、其他类型及已移交的同号项一律不动，
        库位与状态全部原样保留，等待管理员补证后复核。
        """
        sample_no = mother_entry.get("样品编号", "")
        candidates = store.find_by(self.MODULE, "样品编号", sample_no)
        marked: list[dict[str, Any]] = []
        for row in candidates:
            if not self._is_sediment(row):
                continue
            if row.get("status") == "已移交":
                # 已交出的库存不能再回到可领，也不改成待核
                continue
            with store.lock_for(f"{self.MODULE}:{row['id']}"):
                current = store.find(self.MODULE, int(row["id"]))
                if current is None or current.get("status") == "已移交":
                    continue
                if not self._is_sediment(current):
                    continue
                current["status"] = "待核"
                current["pending"] = True
                current["待核原因"] = reason
                current["备注说明"] = self._append_remark(
                    current.get("备注说明", ""), f"分割待核：{reason}"
                )
                marked.append(dict(current))
        return marked[0] if marked else None

    # ------------------------------------------------------------------
    # 整支移交：与分割冲突仲裁
    # ------------------------------------------------------------------
    def _guard_handover(self, registry: dict[str, Any]) -> tuple[bool, str]:
        """移交前检查该登记是否已被分割留库处置。"""
        if registry.get("处置回执") == "分割留库":
            return False, (
                f"样品「{registry.get('样品编号', '')}」已先分割留库（库位："
                f"{registry.get('存放位置', '')}），不能再按整支移交；请核对母样分割回执"
            )
        if registry.get("分割回执"):
            return False, (
                f"样品「{registry.get('样品编号', '')}」存在分割回执，剩余库存仍在库，"
                "不能按整支交出"
            )
        return True, ""

    def handover_with_conflict_check(
        self, registry_id: int
    ) -> tuple[dict[str, Any] | None, str]:
        """在事务锁内办理整支移交，与并发的分割确认互斥。

        幂等检查放进锁内，保证「重复提交只保留一次库位变更」，
        且已交出的库存不能再被任何动作拉回可领状态。
        """
        registry = store.find(self.MODULE, registry_id)
        if registry is None:
            return None, f"{self.ENTITY} {registry_id} 不存在或已归档"

        sample_no = registry.get("样品编号", "")
        # 同一编号的全部库存登记纳入同一事务，防止与分割入口交叉处置
        lock_keys = [
            f"{self.MODULE}:{row['id']}"
            for row in store.find_by(self.MODULE, "样品编号", sample_no)
        ]

        def _do_handover():
            target = store.find(self.MODULE, registry_id)
            if target is None:
                return None, f"{self.ENTITY} {registry_id} 不存在或已归档"

            idempotent_key = f"{self.MODULE}:{registry_id}:移交样品"
            if store.is_idempotent(idempotent_key):
                return None, "动作「移交样品」已执行，请勿重复操作"

            ok, msg = self._guard_handover(target)
            if not ok:
                return None, msg

            # 整支交出：登记整支移交回执，作为库存处置的最终事实
            target["status"] = "已移交"
            target["pending"] = False
            target["abnormal"] = False
            target["处置回执"] = "整支移交"
            store.mark_idempotent(idempotent_key)
            return target, f"{self.ENTITY}已移交样品"

        return store.run_in_transaction(lock_keys, _do_handover)

    # ------------------------------------------------------------------
    # 库存处置查询：两边要能查到此次究竟以何种方式处置
    # ------------------------------------------------------------------
    def disposal_outcome(self, sample_no: str) -> dict[str, Any]:
        """汇总同一编号样品的库存处置事实，供管理员核对。"""
        rows = store.find_by(self.MODULE, "样品编号", sample_no)
        outcomes = [
            {
                "id": row.get("id"),
                "样品类型": row.get("样品类型", ""),
                "采集站位": row.get("采集站位", ""),
                "status": row.get("status", ""),
                "存放位置": row.get("存放位置", ""),
                "处置回执": row.get("处置回执", ""),
                "分割回执": list(row.get("分割回执", [])),
                "待核原因": row.get("待核原因", ""),
            }
            for row in rows
        ]
        handed_over = [o for o in outcomes if o["处置回执"] == "整支移交"]
        split_kept = [o for o in outcomes if o["处置回执"] == "分割留库"]
        pending = [o for o in outcomes if o["status"] == "待核"]
        conflict = bool(handed_over and split_kept)
        if handed_over:
            final = "整支移交"
        elif split_kept:
            final = "分割留库"
        elif pending:
            final = "待核"
        else:
            final = "未处置"
        return {
            "样品编号": sample_no,
            "最终处置": final,
            "存在冲突": conflict,
            "整支移交": handed_over,
            "分割留库": split_kept,
            "待核项": pending,
            "登记明细": outcomes,
        }

    @staticmethod
    def _append_remark(existing: str, addition: str) -> str:
        existing = str(existing or "").strip()
        if not existing:
            return addition
        if addition in existing:
            return existing
        return f"{existing} | {addition}"


class ConflictError(RuntimeError):
    """分割与移交对同一母样库存处置发生冲突。"""
