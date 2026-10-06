"""ML 数据集/模型注册表存取域（自 database.py 拆出，P5）。

manifest/model 的列表/字典字段以 JSON 文本存储：register 序列化、
load/list 反序列化，调用方（ml.dataset / ml.registry）始终拿到原生对象。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .schema import init_database

# ---------------------------------------------------------------------------
# v15（D2）：ML 数据集 manifest 落库
# ---------------------------------------------------------------------------
# manifest 中列表/字典字段以 JSON 文本存储；register 序列化、load/list 反序列化，
# 调用方（ml.dataset）始终拿到/传入原生 Python 对象。
_DATASET_JSON_FIELDS = (
    "symbols",
    "rejected",
    "groups",
    "feature_columns",
    "cost_model",
    "profile_snapshot",
    "data_versions",
    "warnings",
)


def register_dataset(manifest: Mapping[str, Any], path: Path | None = None) -> str:
    """写入/更新 ``ml_datasets``（D2）。按 ``dataset_id`` upsert 幂等，返回 dataset_id。

    ``manifest`` 的列表/字典字段（symbols/rejected/groups/feature_columns/cost_model/
    profile_snapshot/data_versions/warnings）以 JSON 编码落库；标量字段直接存。
    """
    dataset_id = str(manifest["dataset_id"])

    def _dump(key: str) -> str:
        return json.dumps(manifest.get(key), ensure_ascii=False, default=str)

    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            """
            INSERT INTO ml_datasets (
                dataset_id, n_rows, n_positive, positive_rate,
                start_date, end_date, max_trade_date, horizon, aux_horizon,
                index_code, industry_point_in_time, csv_path,
                symbols_json, rejected_json, groups_json, feature_columns_json,
                cost_model_json, profile_snapshot_json, data_versions_json,
                warnings_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(dataset_id) DO UPDATE SET
                n_rows = excluded.n_rows,
                n_positive = excluded.n_positive,
                positive_rate = excluded.positive_rate,
                start_date = excluded.start_date,
                end_date = excluded.end_date,
                max_trade_date = excluded.max_trade_date,
                horizon = excluded.horizon,
                aux_horizon = excluded.aux_horizon,
                index_code = excluded.index_code,
                industry_point_in_time = excluded.industry_point_in_time,
                csv_path = excluded.csv_path,
                symbols_json = excluded.symbols_json,
                rejected_json = excluded.rejected_json,
                groups_json = excluded.groups_json,
                feature_columns_json = excluded.feature_columns_json,
                cost_model_json = excluded.cost_model_json,
                profile_snapshot_json = excluded.profile_snapshot_json,
                data_versions_json = excluded.data_versions_json,
                warnings_json = excluded.warnings_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                dataset_id,
                int(manifest.get("n_rows", 0) or 0),
                int(manifest.get("n_positive", 0) or 0),
                manifest.get("positive_rate"),
                manifest.get("start_date"),
                manifest.get("end_date"),
                manifest.get("max_trade_date"),
                int(manifest.get("horizon", 5) or 5),
                int(manifest.get("aux_horizon", 10) or 10),
                manifest.get("index_code"),
                1 if manifest.get("industry_point_in_time") else 0,
                manifest.get("csv_path"),
                _dump("symbols"),
                _dump("rejected"),
                _dump("groups"),
                _dump("feature_columns"),
                _dump("cost_model"),
                _dump("profile_snapshot"),
                _dump("data_versions"),
                _dump("warnings"),
            ),
        )
    return dataset_id


def _row_to_dataset_manifest(row: Mapping[str, Any]) -> Dict[str, Any]:
    """把 ml_datasets 行解码为原生 manifest dict（JSON 字段反序列化）。"""
    data = dict(row)  # sqlite3.Row 无 .get()，先转 dict

    def _load(key: str) -> Any:
        raw = data.get(f"{key}_json")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    manifest: Dict[str, Any] = {
        "dataset_id": data["dataset_id"],
        "n_rows": data["n_rows"],
        "n_positive": data["n_positive"],
        "positive_rate": data["positive_rate"],
        "start_date": data["start_date"],
        "end_date": data["end_date"],
        "max_trade_date": data["max_trade_date"],
        "horizon": data["horizon"],
        "aux_horizon": data["aux_horizon"],
        "index_code": data["index_code"],
        "industry_point_in_time": bool(data["industry_point_in_time"]),
        "csv_path": data["csv_path"],
        "created_at": data.get("created_at"),
    }
    for key in _DATASET_JSON_FIELDS:
        manifest[key] = _load(key)
    return manifest


def load_dataset_manifest(
    dataset_id: str, path: Path | None = None
) -> Optional[Dict[str, Any]]:
    """读取某数据集 manifest（JSON 字段解码）；不存在返回 ``None``。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM ml_datasets WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()
    return _row_to_dataset_manifest(row) if row else None


def list_datasets(path: Path | None = None) -> List[Dict[str, Any]]:
    """列出全部数据集 manifest（按 created_at 降序、再按 dataset_id），JSON 字段解码。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM ml_datasets ORDER BY created_at DESC, dataset_id DESC"
        ).fetchall()
    return [_row_to_dataset_manifest(row) for row in rows]


_MODEL_JSON_FIELDS = (
    "feature_groups",
    "selected_params",
    "metrics",
    "warnings",
)

# 标量列（与 ML_MODELS_COLUMNS 对应，JSON 列单独处理）
_MODEL_SCALAR_FIELDS = (
    "model_id", "kind", "target", "horizon", "dataset_id", "n_features", "threshold",
    "oos_brier", "oos_auc", "oos_logloss", "oos_ece", "oos_mae", "oos_r2",
    "base_rate", "base_rate_brier", "coverage_at_threshold", "win_rate_at_threshold",
    "n_oos", "n_train", "n_rows", "n_positive", "max_trade_date",
    "status", "stale", "artifact_path", "sklearn_version", "trained_at",
)


def register_model(model: Mapping[str, Any], path: Path | None = None) -> str:
    """写入/更新 ``ml_models``（D3）。按 ``model_id`` upsert 幂等，返回 model_id。

    ``model`` 的 list/dict 字段（feature_groups/selected_params/metrics/warnings）以 JSON
    编码落库；标量字段直接存。新建默认 ``status='candidate'``（调用方可显式覆盖）。
    """
    model_id = str(model["model_id"])

    def _dump(key: str) -> str:
        return json.dumps(model.get(key), ensure_ascii=False, default=str)

    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            """
            INSERT INTO ml_models (
                model_id, kind, target, horizon, dataset_id, n_features, threshold,
                oos_brier, oos_auc, oos_logloss, oos_ece, oos_mae, oos_r2,
                base_rate, base_rate_brier, coverage_at_threshold, win_rate_at_threshold,
                n_oos, n_train, n_rows, n_positive, max_trade_date,
                status, stale, artifact_path, sklearn_version,
                feature_groups_json, selected_params_json, metrics_json, warnings_json,
                trained_at, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            ON CONFLICT(model_id) DO UPDATE SET
                kind = excluded.kind,
                target = excluded.target,
                horizon = excluded.horizon,
                dataset_id = excluded.dataset_id,
                n_features = excluded.n_features,
                threshold = excluded.threshold,
                oos_brier = excluded.oos_brier,
                oos_auc = excluded.oos_auc,
                oos_logloss = excluded.oos_logloss,
                oos_ece = excluded.oos_ece,
                oos_mae = excluded.oos_mae,
                oos_r2 = excluded.oos_r2,
                base_rate = excluded.base_rate,
                base_rate_brier = excluded.base_rate_brier,
                coverage_at_threshold = excluded.coverage_at_threshold,
                win_rate_at_threshold = excluded.win_rate_at_threshold,
                n_oos = excluded.n_oos,
                n_train = excluded.n_train,
                n_rows = excluded.n_rows,
                n_positive = excluded.n_positive,
                max_trade_date = excluded.max_trade_date,
                status = excluded.status,
                stale = excluded.stale,
                artifact_path = excluded.artifact_path,
                sklearn_version = excluded.sklearn_version,
                feature_groups_json = excluded.feature_groups_json,
                selected_params_json = excluded.selected_params_json,
                metrics_json = excluded.metrics_json,
                warnings_json = excluded.warnings_json,
                trained_at = excluded.trained_at,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                model_id,
                model.get("kind"),
                model.get("target"),
                int(model.get("horizon", 5) or 5),
                model.get("dataset_id"),
                int(model.get("n_features", 0) or 0),
                model.get("threshold"),
                model.get("oos_brier"),
                model.get("oos_auc"),
                model.get("oos_logloss"),
                model.get("oos_ece"),
                model.get("oos_mae"),
                model.get("oos_r2"),
                model.get("base_rate"),
                model.get("base_rate_brier"),
                model.get("coverage_at_threshold"),
                model.get("win_rate_at_threshold"),
                int(model.get("n_oos", 0) or 0),
                int(model.get("n_train", 0) or 0),
                int(model.get("n_rows", 0) or 0),
                int(model.get("n_positive", 0) or 0),
                model.get("max_trade_date"),
                str(model.get("status", "candidate")),
                1 if model.get("stale") else 0,
                model.get("artifact_path"),
                model.get("sklearn_version"),
                _dump("feature_groups"),
                _dump("selected_params"),
                _dump("metrics"),
                _dump("warnings"),
                model.get("trained_at"),
            ),
        )
    return model_id


def _row_to_model(row: Mapping[str, Any]) -> Dict[str, Any]:
    """把 ml_models 行解码为原生 dict（JSON 字段反序列化）。"""
    data = dict(row)  # sqlite3.Row 无 .get()，先转 dict

    def _load(key: str) -> Any:
        raw = data.get(f"{key}_json")
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    model: Dict[str, Any] = {field: data.get(field) for field in _MODEL_SCALAR_FIELDS}
    model["stale"] = bool(data.get("stale"))
    model["created_at"] = data.get("created_at")
    model["updated_at"] = data.get("updated_at")
    for key in _MODEL_JSON_FIELDS:
        model[key] = _load(key)
    return model


def load_model(model_id: str, path: Path | None = None) -> Optional[Dict[str, Any]]:
    """读取某模型行（JSON 字段解码）；不存在返回 ``None``。"""
    target = init_database(path)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM ml_models WHERE model_id = ?", (model_id,)
        ).fetchone()
    return _row_to_model(row) if row else None


def list_models(
    path: Path | None = None, status: Optional[str] = None
) -> List[Dict[str, Any]]:
    """列出模型（按 trained_at 降序）；``status`` 非空时过滤。"""
    target = init_database(path)
    query = "SELECT * FROM ml_models"
    params: Tuple[Any, ...] = ()
    if status:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY trained_at DESC, model_id DESC"
    with sqlite3.connect(target, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(query, params).fetchall()
    return [_row_to_model(row) for row in rows]


def set_model_status(
    model_id: str,
    status: str,
    *,
    stale: Optional[bool] = None,
    warnings: Optional[Sequence[str]] = None,
    path: Path | None = None,
) -> None:
    """更新模型 ``status``（晋升/退役/标 demo），可同步 ``stale`` 与 ``warnings``。"""
    target = init_database(path)
    sets = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
    params: List[Any] = [status]
    if stale is not None:
        sets.append("stale = ?")
        params.append(1 if stale else 0)
    if warnings is not None:
        sets.append("warnings_json = ?")
        params.append(json.dumps(list(warnings), ensure_ascii=False, default=str))
    params.append(model_id)
    with sqlite3.connect(target, timeout=30) as connection:
        connection.execute(
            f"UPDATE ml_models SET {', '.join(sets)} WHERE model_id = ?", tuple(params)
        )


def retire_promoted_models(
    target_name: str,
    horizon: int,
    *,
    exclude_model_id: Optional[str] = None,
    path: Path | None = None,
) -> int:
    """把同 (target, horizon) 的其它 promoted/demo 模型退役，保证每个目标只有一个在位。"""
    db = init_database(path)
    with sqlite3.connect(db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT model_id FROM ml_models WHERE target = ? AND horizon = ? "
            "AND status IN ('promoted', 'demo')",
            (target_name, int(horizon)),
        ).fetchall()
        retired = [
            r["model_id"] for r in rows if r["model_id"] != exclude_model_id
        ]
        for mid in retired:
            connection.execute(
                "UPDATE ml_models SET status = 'retired', updated_at = CURRENT_TIMESTAMP "
                "WHERE model_id = ?",
                (mid,),
            )
    return len(retired)


def load_promoted_model(
    target_name: Optional[str] = None,
    *,
    include_demo: bool = False,
    path: Path | None = None,
) -> Optional[Dict[str, Any]]:
    """取在位模型（``status='promoted'``，可选含 ``demo``）；无则 ``None``。

    多个时取 trained_at 最新的一个。``target_name`` 非空时按目标过滤（D5 scoring 默认
    取 ``win5`` 分类模型）。
    """
    db = init_database(path)
    statuses = ("promoted", "demo") if include_demo else ("promoted",)
    placeholders = ",".join("?" for _ in statuses)
    query = f"SELECT * FROM ml_models WHERE status IN ({placeholders})"
    params: List[Any] = list(statuses)
    if target_name:
        query += " AND target = ?"
        params.append(target_name)
    query += " ORDER BY trained_at DESC, model_id DESC LIMIT 1"
    with sqlite3.connect(db, timeout=30) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(query, tuple(params)).fetchone()
    return _row_to_model(row) if row else None

