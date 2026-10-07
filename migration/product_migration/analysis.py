from __future__ import annotations

import re
from dataclasses import asdict
from typing import Iterable, Optional

import pandas as pd

from rules import decide_product


CATEGORY_REQUIRED = {"RMS Department", "RMS Category"}
CATEGORY_LS_PREFIX = "LS Level"

SUPPLIER_SOURCE_HEADERS = [
    "RMS Supplier",
    "RMS Supplier Name",
    "Old Supplier",
    "Supplier Alias",
    "Source Supplier",
]
SUPPLIER_TARGET_HEADERS = [
    "LS Supplier",
    "Lightspeed Supplier",
    "Supplier Name",
    "Primary Supplier",
]


def norm(value) -> str:
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def parse_dt(value):
    if pd.isna(value) or value == "":
        return None
    ts = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(ts):
        return None
    return ts.to_pydatetime()


def numeric(value, default=0.0) -> float:
    val = pd.to_numeric(value, errors="coerce")
    return default if pd.isna(val) else float(val)


def boolish(value) -> bool:
    if isinstance(value, bool):
        return value
    if pd.isna(value):
        return False
    return str(value).strip().casefold() in {"1", "true", "yes", "y"}


def find_category_mapping_sheet(worksheets: dict[str, pd.DataFrame]) -> tuple[str, pd.DataFrame]:
    for name, df in worksheets.items():
        headers = {str(c).strip() for c in df.columns}
        if CATEGORY_REQUIRED.issubset(headers) and any(
            str(c).strip().startswith(CATEGORY_LS_PREFIX) for c in df.columns
        ):
            return name, df
    raise ValueError(
        "Could not find category mapping tab. Expected headers including "
        "'RMS Department', 'RMS Category', and at least one 'LS Level ...' column."
    )


def find_supplier_mapping_sheet(worksheets: dict[str, pd.DataFrame]) -> tuple[str, pd.DataFrame, str, str]:
    # Preferred: an explicit RMS/old-supplier -> Lightspeed supplier alias map.
    for name, df in worksheets.items():
        headers = [str(c).strip() for c in df.columns]
        source = next((h for h in SUPPLIER_SOURCE_HEADERS if h in headers), None)
        target = next((h for h in SUPPLIER_TARGET_HEADERS if h in headers), None)
        if source and target and source != target:
            return name, df, source, target

    # Current LS: importer structure: "Supplier ID Map" is the canonical
    # Lightspeed supplier list with Supplier Name + Supplier UUID. In this
    # mode RMS supplier names must match a canonical LS supplier name after
    # normalization; unmatched names are sent to REVIEW rather than guessed.
    preferred = worksheets.get("Supplier ID Map")
    if preferred is not None:
        headers = [str(c).strip() for c in preferred.columns]
        if "Supplier Name" in headers:
            return "Supplier ID Map", preferred, "Supplier Name", "Supplier Name"

    for name, df in worksheets.items():
        headers = [str(c).strip() for c in df.columns]
        if "Supplier Name" in headers and "Supplier UUID" in headers:
            return name, df, "Supplier Name", "Supplier Name"

    raise ValueError(
        "Could not find a supplier mapping or canonical supplier list. Expected either "
        "an RMS/old-supplier -> LS supplier map, or a sheet with 'Supplier Name' "
        "and 'Supplier UUID' (such as 'Supplier ID Map')."
    )


def build_category_map(df: pd.DataFrame) -> dict[tuple[str, str], dict]:
    # Google Sheets can contain duplicate visible headers (for example several
    # columns all named "LS Level3"). Access by positional index so pandas does
    # not return a Series for duplicate column names.
    columns = [str(c).strip() for c in df.columns]
    try:
        dept_idx = columns.index("RMS Department")
        cat_idx = columns.index("RMS Category")
    except ValueError as exc:
        raise ValueError(
            "Category mapping sheet must contain 'RMS Department' and 'RMS Category'."
        ) from exc

    ls_indexes = [
        i for i, c in enumerate(columns)
        if c.startswith(CATEGORY_LS_PREFIX)
    ]

    mapping = {}
    for row in df.itertuples(index=False, name=None):
        dept = row[dept_idx] if dept_idx < len(row) else ""
        cat = row[cat_idx] if cat_idx < len(row) else ""
        key = (norm(dept), norm(cat))
        if not any(key):
            continue

        levels = []
        for i in ls_indexes:
            value = row[i] if i < len(row) else ""
            if pd.isna(value):
                continue
            text = str(value).strip()
            if text:
                levels.append(text)

        mapping[key] = {
            "levels": levels,
            "path": " > ".join(levels),
        }

    return mapping


def build_supplier_map(df: pd.DataFrame, source_col: str, target_col: str) -> dict[str, str]:
    out = {}
    for _, row in df.iterrows():
        source = norm(row.get(source_col))
        target = "" if pd.isna(row.get(target_col)) else str(row.get(target_col)).strip()
        if source and target:
            out[source] = target
    return out


def analyze_candidates(
    candidates: pd.DataFrame,
    category_map: dict,
    supplier_map: dict,
) -> pd.DataFrame:
    rows = []

    for _, row in candidates.iterrows():
        decision = decide_product(
            quantity=numeric(row.get("RMS_Quantity")),
            inactive=boolish(row.get("Inactive")),
            do_not_order=boolish(row.get("DoNotOrder")),
            last_sold=parse_dt(row.get("LastSold")),
            last_received=parse_dt(row.get("LastReceived")),
            reorder_point=numeric(row.get("RMS_ReorderPoint")),
            restock_level=numeric(row.get("RMS_RestockLevel")),
            supplier_name=row.get("RMS_Supplier"),
            sku=row.get("RMS_SKU"),
        )

        result = dict(row)
        result.update(asdict(decision))

        cat_key = (norm(row.get("RMS_Department")), norm(row.get("RMS_Category")))
        cat = category_map.get(cat_key)
        result["LS_Category_Path"] = cat["path"] if cat else ""
        result["Category_Mapped"] = bool(cat)

        supplier = supplier_map.get(norm(row.get("RMS_Supplier")), "")
        result["LS_Supplier"] = supplier
        result["Supplier_Mapped"] = bool(supplier)

        mapping_reasons = []
        if not cat:
            mapping_reasons.append("Unmapped category")
        if not supplier and str(row.get("RMS_Supplier", "")).strip():
            mapping_reasons.append("Unmapped supplier")

        if mapping_reasons and decision.migration_status != "EXCLUDE":
            result["migration_status"] = "REVIEW"
            existing = result.get("review_reason", "")
            result["review_reason"] = "; ".join(
                [x for x in [existing, *mapping_reasons] if x]
            )

        # Critical cutover rule: RMS quantity must never become Lightspeed opening stock.
        result["Opening_Inventory_To_Import"] = 0

        rows.append(result)

    return pd.DataFrame(rows)


def split_outputs(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {
        "READY": df[df["migration_status"] == "READY"].copy(),
        "REVIEW": df[df["migration_status"] == "REVIEW"].copy(),
        "EXCLUDE": df[df["migration_status"] == "EXCLUDE"].copy(),
    }


def summary(df: pd.DataFrame) -> pd.DataFrame:
    status = df["migration_status"].value_counts(dropna=False).rename_axis("Status").reset_index(name="Products")
    return status
