"""兑换策略：固定次数 / 最大化 / 保留余额 / 分享率优先。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


def pick_item(items: List[dict], kind: str, prefer: str = "cheap") -> Optional[dict]:
    """
    从解析出的兑换项里挑一条。
    prefer: cheap=最便宜；max=单次获得流量最大；efficient=每魔力获得流量最高。
    置灰（disabled）行是站点官方标记的不可兑换项，直接跳过。
    """
    pool = [
        it for it in items
        if it.get("kind") == kind and it.get("cost") and it.get("size_bytes") and not it.get("disabled")
    ]
    if not pool:
        return None
    if prefer == "max":
        return max(pool, key=lambda x: (x["size_bytes"], -x["cost"]))
    if prefer == "efficient":
        return max(pool, key=lambda x: (x["size_bytes"] / x["cost"], x["size_bytes"]))
    return min(pool, key=lambda x: (x["cost"], -x["size_bytes"]))


def affordable_times(bonus: Optional[float], cost: Optional[float], keep_bonus: float) -> int:
    if bonus is None or cost is None or cost <= 0:
        return 0
    usable = float(bonus) - float(keep_bonus or 0)
    if usable <= 0:
        return 0
    return int(usable // cost)


def plan_counts(
    *,
    bonus: Optional[float],
    ratio: Optional[float],
    upload_item: Optional[dict],
    download_item: Optional[dict],
    enable_upload: bool,
    enable_download: bool,
    strategy: str,
    fixed_upload: int,
    fixed_download: int,
    max_upload: int,
    max_download: int,
    keep_bonus: float,
    ratio_threshold: float,
    priority: str,
    max_spend: float,
) -> Tuple[int, int, str]:
    """
    返回 (upload_times, download_times, reason)。
    解析不到价格时次数为 0。
    """
    strategy = (strategy or "keep").strip().lower()
    priority = (priority or "auto").strip().lower()
    keep_bonus = float(keep_bonus or 0)
    max_spend = float(max_spend or 0)
    ratio_threshold = float(ratio_threshold or 0)

    up_cost = float(upload_item["cost"]) if upload_item else None
    down_cost = float(download_item["cost"]) if download_item else None
    if enable_upload and not upload_item:
        enable_upload = False
    if enable_download and not download_item:
        enable_download = False

    if not enable_upload and not enable_download:
        return 0, 0, "未开启任何兑换，或页面解析不到对应项目"

    remaining = None if bonus is None else max(0.0, float(bonus) - keep_bonus)
    if remaining is not None and max_spend > 0:
        remaining = min(remaining, max_spend)

    if remaining is not None and remaining <= 0:
        return 0, 0, "魔力值不足或已达保留下限"

    def cap(kind_times: int, kind_max: int) -> int:
        if kind_max and kind_max > 0:
            return min(kind_times, int(kind_max))
        return kind_times

    # 分享率优先：低于阈值兑上传，否则兑下载
    if priority == "auto":
        if ratio is None:
            prefer_kind = "upload"
        elif ratio_threshold > 0 and ratio < ratio_threshold:
            prefer_kind = "upload"
        else:
            prefer_kind = "download"
    elif priority == "download":
        prefer_kind = "download"
    else:
        prefer_kind = "upload"

    upload_n = 0
    download_n = 0

    if strategy == "fixed":
        upload_n = cap(int(fixed_upload or 0), max_upload) if enable_upload else 0
        download_n = cap(int(fixed_download or 0), max_download) if enable_download else 0
        upload_n, download_n = _fit_budget(upload_n, download_n, remaining, up_cost, down_cost, prefer_kind)
        return upload_n, download_n, f"固定次数策略（优先 {prefer_kind}）"

    # maximize / keep 都按余额最大化，keep 已在 remaining 里扣过保留值
    if enable_upload and enable_download:
        first, second = (prefer_kind, "download" if prefer_kind == "upload" else "upload")
    elif enable_upload:
        first, second = "upload", None
    else:
        first, second = "download", None

    budget = remaining
    for kind in (first, second):
        if not kind or budget is None:
            break
        cost = up_cost if kind == "upload" else down_cost
        kind_max = max_upload if kind == "upload" else max_download
        n = affordable_times(budget + keep_bonus, cost, keep_bonus) if cost else 0
        # affordable_times 用的是 bonus-keep；这里 budget 已扣 keep，等价于 budget//cost
        if cost:
            n = int(budget // cost)
        n = cap(n, kind_max)
        spend = n * cost if (n and cost) else 0
        if kind == "upload":
            upload_n = n
        else:
            download_n = n
        if spend:
            budget -= spend

    reason = "最大化兑换" if strategy == "max" else "保留余额后最大化"
    return upload_n, download_n, f"{reason}（优先 {prefer_kind}）"


def _fit_budget(
    upload_n: int,
    download_n: int,
    remaining: Optional[float],
    up_cost: Optional[float],
    down_cost: Optional[float],
    prefer_kind: str,
) -> Tuple[int, int]:
    if remaining is None:
        return upload_n, download_n
    budget = remaining
    order = ["upload", "download"] if prefer_kind == "upload" else ["download", "upload"]
    out = {"upload": upload_n, "download": download_n}
    used = {"upload": 0, "download": 0}
    for kind in order:
        cost = up_cost if kind == "upload" else down_cost
        want = out[kind]
        if not cost or want <= 0:
            used[kind] = 0
            continue
        n = min(want, int(budget // cost))
        used[kind] = max(0, n)
        budget -= used[kind] * cost
    return used["upload"], used["download"]
