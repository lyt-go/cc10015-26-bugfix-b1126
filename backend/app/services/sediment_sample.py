"""沉积物采样领域服务：分割样品后凭母样分割回执更新样品登记模块。

根因修复：分割入口曾按「样品编号」取样品登记的第一条记录，两站录入相同编号、
或水样与岩芯共用编号时会错误改写其他站位/其他类型样品的库位。现在通过
样品登记服务按「同编号 + 同站位 + 沉积物类型」定位唯一母样登记：
- 命中唯一母样：在事务锁内只更新该份沉积物母样的登记（保存方式、库位）；
- 同号但无法确定唯一母样：分割保持待核，不改动任何库位，管理员补证后可复核；
- 已整支移交的母样：拒绝分割留库，同一份母样不会同时存在两种库存处置。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.sample_registry import ConflictError, SampleRegistryService
from app.store import store


class SedimentSampleService(BaseModuleService):
    MODULE = "sediment_sample"
    ENTITY = "沉积物样品"
    OPERATION_TERM = "执行采样"
    LIST_FIELDS = ["采样站位", "采样深度", "柱状长度", "岩性描述", "样品编号", "采样人", "采样时间", "备注说明"]
    REQUIRED_FIELDS = ["采样站位", "采样深度", "柱状长度"]
    STATUS_ORDER = ["待采样", "已采样", "已登记", "待核", "已分割"]
    # 复核分割：待核记录补证后继续办理，不产生新的业务状态
    ACTION_RULES = {
        "执行采样": "已采样",
        "登记样品": "已登记",
        "分割样品": "已分割",
        "复核分割": "已分割",
    }
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        if action in ("分割样品", "复核分割"):
            return self._split(entry_id, action)

        # 幂等检查
        idempotent_key = f"{self.MODULE}:{entry_id}:{action}"
        if store.is_idempotent(idempotent_key):
            return None, f"动作「{action}」已执行，请勿重复操作"

        entry["status"] = target
        entry["pending"] = target in ("待采样", "已采样")
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS

        store.mark_idempotent(idempotent_key)

        return entry, f"{self.ENTITY}已{action}"

    # ------------------------------------------------------------------
    # 分割 / 复核分割
    # ------------------------------------------------------------------
    def _split(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """办理分割并回执样品登记；待核时不标记幂等，补证后可继续复核。"""
        idempotent_key = f"{self.MODULE}:{entry_id}:分割样品"
        if store.is_idempotent(idempotent_key):
            return None, "动作「分割样品」已执行，请勿重复操作"

        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        sample_no = str(entry.get("样品编号", "")).strip()
        station = str(entry.get("采样站位", "")).strip()
        segments = str(entry.get("柱状长度", "")).strip()
        registry_svc = SampleRegistryService()

        # 同编号全部登记纳入同一事务，与库管的整支移交互斥仲裁
        same_no_rows = store.find_by("sample_registry", "样品编号", sample_no)
        lock_keys = [f"sample_registry:{row['id']}" for row in same_no_rows]

        def _attempt():
            current = store.find(self.MODULE, entry_id)
            registry, outcome, candidates = registry_svc.locate_mother_registry(
                sample_no, station
            )

            if outcome == "located":
                storage_location = f"沉积物库-{station}"
                try:
                    updated = registry_svc.apply_split_receipt(
                        registry,
                        segments=segments,
                        storage_location=storage_location,
                        source_module=self.MODULE,
                        source_id=entry_id,
                    )
                except ConflictError as exc:
                    return "conflict", None, str(exc)
                current["status"] = "已分割"
                current["pending"] = False
                current["分割回执"] = f"已更新登记#{updated['id']}库位 {storage_location}"
                current["待核原因"] = ""
                store.mark_idempotent(idempotent_key)
                return "located", current, (
                    f"分割完成：母样登记#{updated['id']}（{updated.get('样品类型', '')}，"
                    f"站位{updated.get('采集站位', '')}）已更新为分层保存，库位 {storage_location}"
                )

            # 同号但查不到唯一母样：保持待核，不改动任何库位
            if outcome == "ambiguous":
                reason = (
                    f"样品编号「{sample_no}」存在{len(candidates)}条同号登记，"
                    "无法确定唯一沉积物母样，请管理员补证站位与样品类型"
                )
            else:
                reason = (
                    f"样品编号「{sample_no}」在样品登记中查不到沉积物母样"
                    "（可能仅登记了水样或其他站位记录），请补证后再办分割"
                )
            current["status"] = "待核"
            current["pending"] = True
            current["待核原因"] = reason
            registry_svc.mark_pending_verification(current, reason)
            return "pending", current, f"分割保持待核：{reason}"

        outcome, result, message = store.run_in_transaction(lock_keys, _attempt)

        if outcome == "conflict":
            return None, f"库存处置冲突：{message}。请核对母样处置结果后再办理"
        if outcome == "pending" and action == "复核分割":
            # 复核后仍待核：返回明确提示，状态维持待核
            return result, f"复核未通过，仍保持待核：{message.split('：', 1)[-1]}"
        return result, message
