from __future__ import annotations

import re
from collections import Counter, defaultdict
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
    "Mapped Lightspeed Supplier Name",
    "Supplier Name",
    "Primary Supplier",
]

DEFAULT_UNMAPPED_PRIMARY_SUPPLIER = "Other"

# RMS categories intentionally left without a Lightspeed product-category mapping.
# These are empty/legacy/special-system categories and should never be treated as
# migration mapping errors. The LS: importer Mapping sheet remains authoritative
# for all normal product categories.
INTENTIONAL_CATEGORY_EXCLUSIONS = {
    ("games", "rental"),
    ("gift cards", ""),
    ("media", "videos"),
    ("other", "discount"),
    ("other", "online only"),
    ("rewards", "store credit voucher"),
    ("special orders", ""),
}

INTENTIONAL_EXCLUDED_DEPARTMENTS = {
    "homebrew",
}


def is_intentional_category_exclusion(department, category) -> bool:
    dept = norm(department)
    cat = norm(category)
    return dept in INTENTIONAL_EXCLUDED_DEPARTMENTS or (dept, cat) in INTENTIONAL_CATEGORY_EXCLUSIONS


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


def build_department_lv1_map(category_map: dict[tuple[str, str], dict]) -> dict[str, str]:
    """
    Infer the Lightspeed Level-1 parent for each RMS department from the
    authoritative mapping table.

    Preference order:
      1. unique LV1 used by the department;
      2. an LV1 whose normalized name matches the RMS department;
      3. the most frequently used LV1, but only when there is a single winner.

    Ambiguous ties are left unresolved rather than guessed.
    """
    roots_by_department: dict[str, list[str]] = defaultdict(list)
    for (dept_key, _cat_key), mapped in category_map.items():
        if not dept_key:
            continue
        levels = mapped.get("levels") or []
        if levels:
            roots_by_department[dept_key].append(str(levels[0]).strip())

    out: dict[str, str] = {}
    for dept_key, roots in roots_by_department.items():
        roots = [x for x in roots if x]
        if not roots:
            continue
        normalized_unique = {}
        for root in roots:
            normalized_unique.setdefault(norm(root), root)
        if len(normalized_unique) == 1:
            out[dept_key] = next(iter(normalized_unique.values()))
            continue
        if dept_key in normalized_unique:
            out[dept_key] = normalized_unique[dept_key]
            continue

        counts = Counter(norm(root) for root in roots)
        top_count = max(counts.values())
        winners = [key for key, count in counts.items() if count == top_count]
        if len(winners) == 1:
            out[dept_key] = normalized_unique[winners[0]]

    return out


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
    """
    Build primary-supplier migration aliases.

    RMS_Supplier is sourced from Item.SupplierID (the RMS primary supplier).
    This mapping must never be applied to secondary SupplierList relationships.
    """
    out = {}
    for _, row in df.iterrows():
        source_raw = "" if pd.isna(row.get(source_col)) else str(row.get(source_col)).strip()
        target = "" if pd.isna(row.get(target_col)) else str(row.get(target_col)).strip()
        disposition = norm(row.get("Disposition")) if "Disposition" in df.columns else ""

        if disposition == "do not migrate":
            continue
        if not target:
            continue

        # The review sheet represents blank RMS primary suppliers with a readable
        # sentinel label. Convert it back to the actual blank-source key.
        source = "" if norm(source_raw) == "(blank rms supplier)" else norm(source_raw)
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

        rms_department = row.get("RMS_Department")
        rms_category = row.get("RMS_Category")
        dept_key = norm(rms_department)
        cat_key = (dept_key, norm(rms_category))
        cat = category_map.get(cat_key)
        intentional_category_exclusion = is_intentional_category_exclusion(
            rms_department, rms_category
        )

        category_path = cat["path"] if cat else ""
        fallback_applied = False

        # Any classification that is not explicitly mapped goes to the
        # dedicated review bucket. This includes no department, blank category,
        # or a nonblank department/category pair missing from Mapping.
        # Intentionally excluded legacy/system categories remain EXCLUDE.
        if not intentional_category_exclusion and not category_path:
            category_path = "General > No Category"
            fallback_applied = True

        result["LS_Category_Path"] = category_path
        result["Category_Mapped"] = bool(category_path)
        result["Category_Fallback_Applied"] = fallback_applied
        result["Category_Fallback_Needs_Create"] = False
        result["Category_Intentionally_Excluded"] = intentional_category_exclusion
        result["Migration_Review_Flags"] = (
            "unmapped_category_fallback" if fallback_applied else ""
        )

        if intentional_category_exclusion:
            result["migration_status"] = "EXCLUDE"
            existing = result.get("review_reason", "")
            result["review_reason"] = "; ".join(
                [x for x in [existing, "Intentional non-migrating RMS category"] if x]
            )

        # RMS_Supplier is the primary supplier from Item.SupplierID. Do not use
        # secondary SupplierList relationships for supplier mapping.
        primary_supplier_key = norm(row.get("RMS_Supplier"))
        supplier = supplier_map.get(primary_supplier_key, "")
        if not supplier:
            supplier = DEFAULT_UNMAPPED_PRIMARY_SUPPLIER
            result["Supplier_Fallback_Applied"] = True
        else:
            result["Supplier_Fallback_Applied"] = False
        result["LS_Supplier"] = supplier
        result["Supplier_Mapped"] = bool(supplier)

        mapping_reasons = []
        if not category_path and not intentional_category_exclusion:
            mapping_reasons.append("Unmapped category")
        # Unmapped primary suppliers are intentionally redirected to the
        # canonical Lightspeed "Other" supplier so they do not block migration.

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
