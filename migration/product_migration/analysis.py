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


IDENTITY_STOPWORDS = {
    "the", "and", "for", "with", "from", "pack", "set", "box", "game",
    "model", "edition", "new", "of", "a", "an", "to", "in"
}


def _identity_tokens(value) -> set[str]:
    text = norm(value)
    if not text:
        return set()
    return {
        token for token in re.findall(r"[a-z0-9]+", text)
        if len(token) >= 2 and token not in IDENTITY_STOPWORDS
    }


def web_description_is_trusted(row: pd.Series) -> bool:
    """
    Treat RMS WebDescription as the authoritative expanded product title when
    it is present.

    Hobby Corner intentionally uses WebDescription to work around the RMS Item
    Description character limit, so legitimate titles may have little or no
    token overlap with the abbreviated RMS Description (for example MTG ->
    Magic the Gathering).

    Product identity safety is enforced separately by hard identifier gates
    such as barcode_identity_conflict and source_product_code_collision. Rows
    with those conflicts are REVIEW and never reach a controlled write batch.
    """
    web = "" if pd.isna(row.get("RMS_WebDescription")) else str(row.get("RMS_WebDescription")).strip()
    return bool(web)


def _valid_gtin(value) -> bool:
    raw = "" if value is None or pd.isna(value) else str(value).strip()
    digits = re.sub(r"\D", "", raw)
    if len(digits) not in {8, 12, 13, 14}:
        return False
    nums = [int(x) for x in digits]
    total = 0
    for idx, digit in enumerate(reversed(nums[:-1])):
        total += digit * (3 if idx % 2 == 0 else 1)
    return ((10 - (total % 10)) % 10) == nums[-1]


def source_identity_review_flags(row: pd.Series) -> list[str]:
    """
    Catch strong source-identity conflicts before product creation.

    The hard gate is barcode identity: when a SKU itself is a valid GTIN and
    the row carries a different valid GTIN/UPC, the row is ambiguous and must
    be reviewed. Legacy Web Description quality is handled separately by
    web_description_is_trusted() so abbreviated but legitimate names are not
    over-blocked.
    """
    flags: list[str] = []

    sku = "" if pd.isna(row.get("RMS_SKU")) else str(row.get("RMS_SKU")).strip()
    upc = "" if pd.isna(row.get("RMS_UPC")) else str(row.get("RMS_UPC")).strip()
    aliases = "" if pd.isna(row.get("RMS_Aliases")) else str(row.get("RMS_Aliases")).strip()

    # Some RMS item lookup codes are UPC-A values stored without the leading 0.
    # When that reconstructs to a valid GTIN and conflicts with another valid
    # barcode attached to the row, stop for review instead of guessing.
    sku_digits = re.sub(r"\D", "", sku)
    inferred_sku_gtin = ""
    if sku.isdigit():
        if len(sku_digits) == 11 and _valid_gtin("0" + sku_digits):
            inferred_sku_gtin = "0" + sku_digits
        elif len(sku_digits) in {8, 12, 13, 14} and _valid_gtin(sku_digits):
            inferred_sku_gtin = sku_digits

    candidate_codes = []
    for raw in [upc, *aliases.split("|")]:
        code = re.sub(r"\D", "", str(raw).strip())
        if not code:
            continue
        if len(code) == 11 and _valid_gtin("0" + code):
            code = "0" + code
        if _valid_gtin(code):
            candidate_codes.append(code)

    if inferred_sku_gtin and any(code != inferred_sku_gtin for code in candidate_codes):
        flags.append("barcode_identity_conflict")

    return flags


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

        entry = {
            "levels": levels,
            "path": " > ".join(levels),
        }
        if key in mapping and norm(mapping[key].get("path")) != norm(entry.get("path")):
            raise ValueError(
                "Conflicting category mapping rows for "
                f"department={dept!r}, category={cat!r}: "
                f"{mapping[key].get('path')!r} vs {entry.get('path')!r}"
            )
        mapping[key] = entry

    return mapping


def build_supplier_map(df: pd.DataFrame, source_col: str, target_col: str) -> dict[str, str]:
    """
    Build primary-supplier migration aliases.

    RMS_Supplier is sourced from Item.SupplierID (the RMS primary supplier).
    This mapping must never be applied to secondary SupplierList relationships.

    Safety rule:
    - A blank-disposition mapping to "Other" is treated as unresolved legacy
      review data, not as an approved supplier conversion.
    - Explicitly approved mappings (for example Disposition=MAP) are honored.
    - The blank-RMS-supplier review row may still map to Other.
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

        source = "" if norm(source_raw) == "(blank rms supplier)" else norm(source_raw)

        # Do not silently collapse a named RMS supplier to Other unless that
        # choice was explicitly reviewed/approved in the mapping sheet.
        if norm(target) == "other" and source and not disposition:
            continue

        if source in out and norm(out[source]) != norm(target):
            raise ValueError(
                f"Conflicting supplier mapping rows for {source_raw!r}: "
                f"{out[source]!r} vs {target!r}"
            )
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
        # A display fallback such as "General > No Category" is not an
        # approved RMS->Lightspeed mapping. Keep it visible as the proposed
        # destination, but do not let it satisfy the mapped/write gate.
        result["Category_Mapped"] = bool(cat and cat.get("path"))
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
        result["Supplier_Fallback_Applied"] = False
        result["LS_Supplier"] = supplier
        # A blank RMS primary supplier remains unresolved even when the review
        # mapping proposes "Other". It must be explicitly reviewed.
        result["Supplier_Mapped"] = bool(supplier) and bool(primary_supplier_key)

        mapping_reasons = []
        if fallback_applied and not intentional_category_exclusion:
            mapping_reasons.append(
                "Unmapped category - proposed fallback is General > No Category"
            )
        elif not result["Category_Mapped"] and not intentional_category_exclusion:
            mapping_reasons.append("Unmapped category")

        if not primary_supplier_key:
            mapping_reasons.append("Blank RMS supplier")
        elif not supplier:
            mapping_reasons.append("Unmapped supplier")

        web_description = "" if pd.isna(row.get("RMS_WebDescription")) else str(row.get("RMS_WebDescription")).strip()
        web_description_trusted = web_description_is_trusted(row)
        result["Web_Description_Trusted"] = web_description_trusted
        if web_description and not web_description_trusted:
            existing_flags = [
                x for x in str(result.get("Migration_Review_Flags", "")).split(",")
                if x
            ]
            result["Migration_Review_Flags"] = ",".join(
                dict.fromkeys([*existing_flags, "web_description_untrusted"])
            )

        identity_flags = source_identity_review_flags(row)
        if identity_flags:
            existing_flags = [
                x for x in str(result.get("Migration_Review_Flags", "")).split(",")
                if x
            ]
            result["Migration_Review_Flags"] = ",".join(
                dict.fromkeys([*existing_flags, *identity_flags])
            )
            mapping_reasons.extend(
                {
                    "barcode_identity_conflict": "Conflicting valid barcode identity on RMS row",
                }[flag]
                for flag in identity_flags
            )

        if mapping_reasons and decision.migration_status != "EXCLUDE":
            result["migration_status"] = "REVIEW"
            existing = result.get("review_reason", "")
            result["review_reason"] = "; ".join(
                [x for x in [existing, *mapping_reasons] if x]
            )

        # Critical cutover rule: RMS quantity must never become Lightspeed opening stock.
        result["Opening_Inventory_To_Import"] = 0

        rows.append(result)

    analyzed = pd.DataFrame(rows)
    if analyzed.empty:
        return analyzed

    # Lightspeed product codes must be unique across the catalog. Detect
    # collisions within the source candidate set before selecting a write batch.
    # Only non-EXCLUDE rows participate because excluded legacy duplicates do
    # not create a catalog conflict at cutover.
    candidate_mask = analyzed["migration_status"].isin(["READY", "REVIEW"])
    code_owners: dict[str, set[int]] = {}
    row_codes: dict[int, set[str]] = {}

    for idx, row in analyzed[candidate_mask].iterrows():
        codes: set[str] = set()
        sku = "" if pd.isna(row.get("RMS_SKU")) else str(row.get("RMS_SKU")).strip()
        upc = "" if pd.isna(row.get("RMS_UPC")) else str(row.get("RMS_UPC")).strip()
        aliases = "" if pd.isna(row.get("RMS_Aliases")) else str(row.get("RMS_Aliases")).strip()
        for code in [sku, upc, *aliases.split("|")]:
            clean = str(code).strip()
            if clean:
                codes.add(clean.casefold())
        row_codes[idx] = codes
        for code in codes:
            code_owners.setdefault(code, set()).add(idx)

    collision_codes = {
        code: owners for code, owners in code_owners.items()
        if len(owners) > 1
    }
    for idx, codes in row_codes.items():
        conflicts = sorted(code for code in codes if code in collision_codes)
        if not conflicts:
            continue
        analyzed.at[idx, "migration_status"] = "REVIEW"
        existing_flags = [
            x for x in str(analyzed.at[idx, "Migration_Review_Flags"] or "").split(",")
            if x
        ]
        analyzed.at[idx, "Migration_Review_Flags"] = ",".join(
            dict.fromkeys([*existing_flags, "source_product_code_collision"])
        )
        existing_reason = str(analyzed.at[idx, "review_reason"] or "").strip()
        collision_reason = "Source product code collision: " + ", ".join(conflicts)
        analyzed.at[idx, "review_reason"] = "; ".join(
            [x for x in [existing_reason, collision_reason] if x]
        )

    return analyzed


def split_outputs(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {
        "READY": df[df["migration_status"] == "READY"].copy(),
        "REVIEW": df[df["migration_status"] == "REVIEW"].copy(),
        "EXCLUDE": df[df["migration_status"] == "EXCLUDE"].copy(),
    }


def summary(df: pd.DataFrame) -> pd.DataFrame:
    status = df["migration_status"].value_counts(dropna=False).rename_axis("Status").reset_index(name="Products")
    return status
