from __future__ import annotations

import json
import math
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd


def _norm(value: Any) -> str:
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def _text(value: Any) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def _decimal_string(value: Any) -> Optional[str]:
    if value is None or pd.isna(value) or str(value).strip() == "":
        return None
    try:
        dec = Decimal(str(value).replace("$", "").replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return f"{dec:.2f}"


def _first_existing(row: pd.Series, names: Iterable[str]) -> Any:
    for name in names:
        if name in row.index:
            value = row.get(name)
            if value is not None and not pd.isna(value) and str(value).strip() != "":
                return value
    return None


def build_category_uuid_map(cat_id_df: pd.DataFrame) -> Dict[str, str]:
    headers = [str(c).strip() for c in cat_id_df.columns]
    required = {"Level 1", "UUID"}
    if not required.issubset(headers):
        raise ValueError("Cat ID sheet must contain at least 'Level 1' and 'UUID'.")

    out: Dict[str, str] = {}
    for _, row in cat_id_df.iterrows():
        levels = []
        for col in ("Level 1", "Level 2", "Level 3"):
            if col in cat_id_df.columns:
                val = _text(row.get(col))
                if val:
                    levels.append(val)
        uid = _text(row.get("UUID"))
        if levels and uid:
            out[_norm(" > ".join(levels))] = uid
    return out


def build_brand_uuid_map(brand_id_df: pd.DataFrame) -> Dict[str, str]:
    if "Brand Name" not in brand_id_df.columns or "Brand UUID" not in brand_id_df.columns:
        raise ValueError("Brand ID Map must contain 'Brand Name' and 'Brand UUID'.")

    out: Dict[str, str] = {}
    for _, row in brand_id_df.iterrows():
        name = _text(row.get("Brand Name"))
        uid = _text(row.get("Brand UUID"))
        if name and uid:
            out[_norm(name)] = uid
    return out


def build_supplier_uuid_map(supplier_id_df: pd.DataFrame) -> Dict[str, str]:
    if "Supplier Name" not in supplier_id_df.columns or "Supplier UUID" not in supplier_id_df.columns:
        raise ValueError("Supplier ID Map must contain 'Supplier Name' and 'Supplier UUID'.")

    out: Dict[str, str] = {}
    for _, row in supplier_id_df.iterrows():
        name = _text(row.get("Supplier Name"))
        uid = _text(row.get("Supplier UUID"))
        if name and uid:
            out[_norm(name)] = uid
    return out


def select_test_batch(result: pd.DataFrame, size: int = 8) -> pd.DataFrame:
    """
    Pick a varied, fully mapped READY batch.

    Priority buckets:
      - normal READY products
      - reorder suppressed READY products
      - inactive or DoNotOrder products that still qualified READY
      - Games Workshop review candidates are intentionally excluded from the
        first write test because they require manual identity review.
    """
    eligible = result[
        (result["migration_status"] == "READY")
        & result["Category_Mapped"].astype(bool)
        & result["Supplier_Mapped"].astype(bool)
    ].copy()

    if eligible.empty:
        raise ValueError("No fully mapped READY products are available for a test batch.")

    selected = []
    used = set()

    def take(mask, n):
        nonlocal selected, used
        subset = eligible[mask].copy()
        for idx, row in subset.iterrows():
            key = _text(row.get("RMS_ItemID")) or _text(row.get("RMS_SKU")) or str(idx)
            if key in used:
                continue
            selected.append(row)
            used.add(key)
            if len([1 for _ in selected]) >= size:
                return
            n -= 1
            if n <= 0:
                return

    suppressed = (
        (pd.to_numeric(eligible.get("proposed_reorder_point", 0), errors="coerce").fillna(0) == 0)
        & (pd.to_numeric(eligible.get("proposed_restock_level", 0), errors="coerce").fillna(0) == 0)
    )
    inactive = eligible.get("Inactive", pd.Series(False, index=eligible.index)).astype(str).str.lower().isin(["1", "true", "yes"])
    dno = eligible.get("DoNotOrder", pd.Series(False, index=eligible.index)).astype(str).str.lower().isin(["1", "true", "yes"])

    take(inactive | dno, 1)
    take(suppressed & ~(inactive | dno), 2)
    take(~suppressed, 3)
    take(pd.Series(True, index=eligible.index), size - len(selected))

    if not selected:
        raise ValueError("Unable to select a test batch.")

    return pd.DataFrame(selected).head(size).reset_index(drop=True)


def build_standard_family_payload(
    row: pd.Series,
    category_uuid_map: Dict[str, str],
    supplier_uuid_map: Dict[str, str],
    brand_uuid_map: Optional[Dict[str, str]] = None,
    default_weight_unit: Optional[str] = None,
) -> Dict[str, Any]:
    name = _text(_first_existing(row, ["RMS_Description", "Description"]))
    sku = _text(_first_existing(row, ["RMS_SKU", "ItemLookupCode", "SKU"]))
    category_path = _text(row.get("LS_Category_Path"))
    supplier_name = _text(row.get("LS_Supplier"))
    brand_name = _text(_first_existing(row, ["Brand", "RMS_Brand", "brand", "SubDescription3"]))
    upc = _text(_first_existing(row, ["UPC", "RMS_UPC", "Barcode", "barcode"]))
    picture = _text(_first_existing(row, ["PictureName", "Picture", "RMS_Picture", "image_url", "Image URL", "Image"]))
    weight = _decimal_string(_first_existing(row, ["Weight", "RMS_Weight", "weight"]))
    explicit_weight_unit = _text(_first_existing(row, ["Weight Unit", "WeightUnit", "RMS_WeightUnit", "weight_unit"])).upper()

    if not name:
        raise ValueError("Missing product name.")
    if not sku:
        raise ValueError(f"{name}: missing SKU/code.")

    category_id = category_uuid_map.get(_norm(category_path))
    if not category_id:
        raise ValueError(f"{sku}: no Lightspeed category UUID for '{category_path}'.")

    supplier_id = supplier_uuid_map.get(_norm(supplier_name))
    if supplier_name and not supplier_id:
        raise ValueError(f"{sku}: no Lightspeed supplier UUID for '{supplier_name}'.")

    retail = _decimal_string(_first_existing(row, [
        "RetailPrice", "Retail Price", "Price", "RMS_RetailPrice", "RMS_Price"
    ]))
    cost = _decimal_string(_first_existing(row, [
        "LastCost", "Cost", "RMS_Cost", "supply_price", "Supply Price"
    ]))
    description = _text(_first_existing(row, [
        "ExtendedDescription", "Ext. Description", "Web Description"
    ]))

    codes = [{"type": "CUSTOM", "code": sku}]
    if upc and upc != sku:
        codes.append({"type": "UPC", "code": upc})

    product: Dict[str, Any] = {
        "active": {"in_store": True, "ecwid": False},
        "codes": codes,
    }

    weight_unit = explicit_weight_unit or (default_weight_unit or "")
    if weight is not None and weight_unit:
        valid_units = {"CT", "G", "OZ", "LB", "KG"}
        if weight_unit not in valid_units:
            raise ValueError(f"{sku}: unsupported weight unit '{weight_unit}'.")
        product["measurements"] = {"weight": float(weight), "weight_unit": weight_unit}

    if retail is not None:
        # Hobby Corner currently uses tax-inclusive retail pricing in the source
        # migration data; this can be flipped centrally if store config requires.
        product["prices"] = {"price_excluding_tax": retail}

    if supplier_id:
        supplier_entry: Dict[str, Any] = {"supplier_id": supplier_id}
        if cost is not None:
            supplier_entry["price"] = cost
        supplier_code = _text(_first_existing(row, ["RMS_SupplierCode", "supplier_code", "SupplierCode", "ReorderNumber"]))
        if supplier_code:
            supplier_entry["code"] = supplier_code
        product["suppliers"] = [supplier_entry]

    # Deliberately omit outlet_inventories. This guarantees no RMS on-hand
    # quantity is imported. Physical inventory establishes opening stock.
    payload: Dict[str, Any] = {
        "name": name,
        "classification": "STANDARD",
        "category_id": category_id,
        "track_inventory": True,
        "products": [product],
    }
    if brand_name:
        if not brand_uuid_map:
            raise ValueError(f"{sku}: brand '{brand_name}' present but no brand UUID map was supplied.")
        brand_id = brand_uuid_map.get(_norm(brand_name))
        if not brand_id:
            raise ValueError(f"{sku}: no Lightspeed brand UUID for '{brand_name}'.")
        payload["brand_id"] = brand_id

    if description:
        payload["description"] = description

    return payload


def build_test_preview(
    batch: pd.DataFrame,
    category_uuid_map: Dict[str, str],
    supplier_uuid_map: Dict[str, str],
    brand_uuid_map: Optional[Dict[str, str]] = None,
    default_weight_unit: Optional[str] = None,
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    preview_rows = []
    payloads = []

    run_id = f"TEST-{uuid.uuid4().hex[:10].upper()}"

    for _, row in batch.iterrows():
        payload = build_standard_family_payload(
            row,
            category_uuid_map,
            supplier_uuid_map,
            brand_uuid_map=brand_uuid_map,
            default_weight_unit=default_weight_unit,
        )
        payloads.append(payload)
        product = payload["products"][0]
        supplier_entry = product.get("suppliers", [{}])[0] if product.get("suppliers") else {}
        upc_codes = [x.get("code", "") for x in product.get("codes", []) if x.get("type") == "UPC"]
        measurements = product.get("measurements", {})
        picture = _text(_first_existing(row, ["PictureName", "Picture", "RMS_Picture", "image_url", "Image URL", "Image"]))
        brand_name = _text(_first_existing(row, ["Brand", "RMS_Brand", "brand"]))
        missing_rich_fields = []
        if not brand_name: missing_rich_fields.append("brand")
        if not upc_codes: missing_rich_fields.append("UPC")
        if not supplier_entry.get("code"): missing_rich_fields.append("supplier code")
        if not measurements.get("weight"): missing_rich_fields.append("weight")
        if not picture: missing_rich_fields.append("picture")

        preview_rows.append({
            "Run ID": run_id,
            "RMS Item ID": _text(row.get("RMS_ItemID")),
            "SKU": _text(row.get("RMS_SKU")),
            "Name": payload["name"],
            "LS Category Path": _text(row.get("LS_Category_Path")),
            "Category UUID": payload.get("category_id", ""),
            "Brand": brand_name,
            "Brand UUID": payload.get("brand_id", ""),
            "UPC": upc_codes[0] if upc_codes else "",
            "LS Supplier": _text(row.get("LS_Supplier")),
            "Supplier UUID": (
                payload["products"][0].get("suppliers", [{}])[0].get("supplier_id", "")
                if payload["products"][0].get("suppliers")
                else ""
            ),
            "Retail": payload["products"][0].get("prices", {}).get("price_excluding_tax", ""),
            "Supplier Code": supplier_entry.get("code", ""),
            "Supply Cost": (
                payload["products"][0].get("suppliers", [{}])[0].get("price", "")
                if payload["products"][0].get("suppliers")
                else ""
            ),
            "Weight": measurements.get("weight", ""),
            "Weight Unit": measurements.get("weight_unit", ""),
            "Picture Source": picture,
            "Image Upload Required": bool(picture),
            "Missing Rich Fields": ", ".join(missing_rich_fields),
            "Reorder Point Proposed": row.get("proposed_reorder_point", ""),
            "Restock Level Proposed": row.get("proposed_restock_level", ""),
            "Opening Inventory": 0,
            "Write Enabled": False,
            "Payload JSON": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        })

    return pd.DataFrame(preview_rows), payloads
