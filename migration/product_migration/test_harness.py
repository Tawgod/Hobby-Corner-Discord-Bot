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


def _first_positive_decimal(row: pd.Series, names: Iterable[str]) -> Optional[str]:
    """
    Return the first positive money value from the configured source precedence.
    RMS frequently stores 0 in SupplierList/LastCost as a missing placeholder,
    so zero must not mask a later real cost.
    """
    for name in names:
        if name not in row.index:
            continue
        raw = row.get(name)
        if raw is None or pd.isna(raw) or str(raw).strip() == "":
            continue
        try:
            dec = Decimal(str(raw).replace("$", "").replace(",", "").strip())
        except (InvalidOperation, ValueError):
            continue
        if dec > 0:
            return f"{dec:.2f}"
    return None


def _split_aliases(value: Any) -> List[str]:
    raw = _text(value)
    if not raw:
        return []
    parts = [p.strip() for p in raw.split('|') if p and p.strip()]
    seen = set()
    out = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _product_code_type(value: Any) -> str:
    code = _text(value)
    if not code.isdigit():
        return "CUSTOM"
    length = len(code)
    if length == 12:
        return "UPC"
    if length in {8, 13}:
        return "EAN"
    if length == 14:
        return "ITF"
    return "CUSTOM"


def _first_existing(row: pd.Series, names: Iterable[str]) -> Any:
    for name in names:
        if name in row.index:
            value = row.get(name)
            if value is not None and not pd.isna(value) and str(value).strip() != "":
                return value
    return None


def _trusted_name_parts(row: pd.Series) -> Tuple[str, str, bool]:
    """
    Return (name, source_label, web_trusted).

    Prefer the legacy web title only when analysis has corroborated it against
    the RMS item/extended description. Otherwise use the RMS Description so a
    stale NitroSell title cannot rename the product.
    """
    web = _text(_first_existing(row, [
        "Web Description", "RMS_WebDescription", "WebDescription"
    ]))
    rms = _text(_first_existing(row, ["RMS_Description", "Description"]))

    trusted_value = row.get("Web_Description_Trusted") if "Web_Description_Trusted" in row.index else None
    if trusted_value is None or pd.isna(trusted_value) or str(trusted_value).strip() == "":
        try:
            from analysis import web_description_is_trusted
            web_trusted = bool(web and web_description_is_trusted(row))
        except Exception:
            web_trusted = False
    else:
        web_trusted = str(trusted_value).strip().casefold() in {"1", "true", "yes", "y"}

    if web and web_trusted:
        return web, "RMS WebDescription (trusted)", True
    if rms:
        return rms, "RMS Description", False
    if web:
        # Last-resort only for malformed source rows missing RMS Description.
        return web, "RMS WebDescription (uncorroborated fallback)", False
    return "", "", False


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
            key = _norm(" > ".join(levels))
            if key in out and out[key] != uid:
                raise ValueError(
                    f"Conflicting Cat ID UUIDs for {' > '.join(levels)!r}: "
                    f"{out[key]!r} vs {uid!r}"
                )
            out[key] = uid
    return out


def build_brand_uuid_map(brand_id_df: pd.DataFrame) -> Dict[str, str]:
    if "Brand Name" not in brand_id_df.columns or "Brand UUID" not in brand_id_df.columns:
        raise ValueError("Brand ID Map must contain 'Brand Name' and 'Brand UUID'.")

    out: Dict[str, str] = {}
    for _, row in brand_id_df.iterrows():
        name = _text(row.get("Brand Name"))
        uid = _text(row.get("Brand UUID"))
        if name and uid:
            key = _norm(name)
            if key in out and out[key] != uid:
                raise ValueError(
                    f"Conflicting Brand ID Map UUIDs for {name!r}: "
                    f"{out[key]!r} vs {uid!r}"
                )
            out[key] = uid
    return out


def build_supplier_uuid_map(supplier_id_df: pd.DataFrame) -> Dict[str, str]:
    if "Supplier Name" not in supplier_id_df.columns or "Supplier UUID" not in supplier_id_df.columns:
        raise ValueError("Supplier ID Map must contain 'Supplier Name' and 'Supplier UUID'.")

    out: Dict[str, str] = {}
    for _, row in supplier_id_df.iterrows():
        name = _text(row.get("Supplier Name"))
        uid = _text(row.get("Supplier UUID"))
        if name and uid:
            key = _norm(name)
            if key in out and out[key] != uid:
                raise ValueError(
                    f"Conflicting Supplier ID Map UUIDs for {name!r}: "
                    f"{out[key]!r} vs {uid!r}"
                )
            out[key] = uid
    return out


def resolve_category_uuids_from_api(
    category_uuid_map: Dict[str, str],
    category_paths: Iterable[str],
    *,
    api_domain: str,
    token: str,
    create_missing_fallbacks: bool = False,
    timeout: int = 45,
) -> Tuple[Dict[str, str], Dict[str, str], List[str], List[Dict[str, str]]]:
    """
    Resolve exact Lightspeed category paths and optionally create missing
    two-level fallback categories such as "Games > Other Games".

    Creation is restricted to a missing child beneath an already-existing LV1
    category. No root categories are created and no existing categories are
    updated or deleted.
    """
    import requests

    domain = _text(api_domain)
    auth_token = _text(token)
    if not domain or not auth_token:
        raise ValueError("Lightspeed API domain/token are required to resolve category UUIDs.")

    if domain.startswith("http://") or domain.startswith("https://"):
        base_host = domain.rstrip("/")
    else:
        base_host = f"https://{domain}.retail.lightspeed.app"

    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "HobbyCorner-ProductMigration/1.0",
    }

    def list_categories() -> List[Dict[str, Any]]:
        res = requests.get(
            f"{base_host}/api/2026-07/product_categories",
            headers=headers,
            params={"include": "family", "page_size": 1000},
            timeout=timeout,
        )
        if not res.ok:
            raise RuntimeError(
                f"Lightspeed category lookup failed {res.status_code}: {res.text[:1000]}"
            )
        body = res.json()
        data = body.get("data", {}) if isinstance(body, dict) else {}
        rows = data.get("categories", []) if isinstance(data, dict) else []
        return [x for x in rows if isinstance(x, dict)]

    def path_for(item: Dict[str, Any]) -> str:
        category_path = item.get("category_path") or []
        names = [
            _text(part.get("name"))
            for part in category_path
            if isinstance(part, dict) and _text(part.get("name"))
        ]
        own_name = _text(item.get("name"))
        if own_name and (not names or _norm(names[-1]) != _norm(own_name)):
            names.append(own_name)
        return " > ".join(names)

    wanted = {
        _norm(path): _text(path)
        for path in category_paths
        if _text(path) and _norm(path) not in category_uuid_map
    }
    if not wanted:
        return dict(category_uuid_map), {}, [], []

    categories = list_categories()
    out = dict(category_uuid_map)
    resolved: Dict[str, str] = {}
    created: List[Dict[str, str]] = []

    by_path: Dict[str, List[Dict[str, Any]]] = {}
    roots_by_name: Dict[str, List[Dict[str, Any]]] = {}
    children_by_parent_and_name: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for item in categories:
        path = path_for(item)
        if path:
            by_path.setdefault(_norm(path), []).append(item)
        item_name = _text(item.get("name"))
        parent_id = _text(item.get("parent_category_id"))
        if not parent_id and item_name:
            roots_by_name.setdefault(_norm(item_name), []).append(item)
        if parent_id and item_name:
            children_by_parent_and_name.setdefault(
                (parent_id, _norm(item_name)), []
            ).append(item)

    still_missing: Dict[str, str] = {}
    for key, display_path in wanted.items():
        matches = by_path.get(key, [])
        if len(matches) == 1 and _text(matches[0].get("id")):
            uid = _text(matches[0].get("id"))
            out[key] = uid
            resolved[display_path] = uid
            continue

        # Some Lightspeed responses vary in how category_path is populated.
        # For a two-level path, fall back to exact parent/child matching by IDs.
        parts = [p.strip() for p in display_path.split(">") if p.strip()]
        if len(parts) == 2:
            parent_name, child_name = parts
            parent_matches = roots_by_name.get(_norm(parent_name), [])
            if len(parent_matches) == 1:
                parent_id = _text(parent_matches[0].get("id"))
                child_matches = children_by_parent_and_name.get(
                    (parent_id, _norm(child_name)), []
                )
                if len(child_matches) == 1 and _text(child_matches[0].get("id")):
                    uid = _text(child_matches[0].get("id"))
                    out[key] = uid
                    resolved[display_path] = uid
                    continue

        still_missing[key] = display_path

    if create_missing_fallbacks and still_missing:
        for key, display_path in list(still_missing.items()):
            parts = [p.strip() for p in display_path.split(">") if p.strip()]
            if len(parts) != 2 or not parts[1].startswith("Other "):
                continue

            parent_name, child_name = parts
            parent_matches = roots_by_name.get(_norm(parent_name), [])
            if len(parent_matches) != 1:
                continue
            parent_id = _text(parent_matches[0].get("id"))
            if not parent_id:
                continue

            create = requests.post(
                f"{base_host}/api/2026-07/product_categories/bulk",
                headers=headers,
                json={
                    "categories": [{
                        "name": child_name,
                        "parent_category_id": parent_id,
                    }]
                },
                timeout=timeout,
            )
            if not create.ok:
                raise RuntimeError(
                    f"Category create failed for '{display_path}' "
                    f"{create.status_code}: {create.text[:1000]}"
                )

            body = create.json()
            created_rows = body.get("data", []) if isinstance(body, dict) else []
            created_id = ""
            if isinstance(created_rows, list) and len(created_rows) == 1:
                created_id = _text(created_rows[0].get("id"))
            if not created_id:
                # Read-back below is authoritative even if response shape varies.
                pass
            created.append({
                "path": display_path,
                "id": created_id,
                "parent_id": parent_id,
            })

        # Re-read and verify every requested path exactly.
        categories = list_categories()
        by_path = {}
        for item in categories:
            path = path_for(item)
            if path:
                by_path.setdefault(_norm(path), []).append(item)

        for key, display_path in list(still_missing.items()):
            matches = by_path.get(key, [])
            if len(matches) == 1 and _text(matches[0].get("id")):
                uid = _text(matches[0].get("id"))
                out[key] = uid
                resolved[display_path] = uid
                still_missing.pop(key, None)

    unresolved = sorted(still_missing.values())
    return out, resolved, unresolved, created


def resolve_supplier_uuids_from_api(
    supplier_uuid_map: Dict[str, str],
    supplier_names: Iterable[str],
    *,
    api_domain: str,
    token: str,
    timeout: int = 45,
) -> Tuple[Dict[str, str], Dict[str, str], List[str]]:
    """
    Resolve missing supplier UUIDs by exact Lightspeed supplier name.

    This is read-only. It never creates or edits suppliers. Exact normalized
    name matches only; ambiguous or missing names are returned unresolved.
    """
    import requests

    domain = _text(api_domain)
    auth_token = _text(token)
    if not domain or not auth_token:
        raise ValueError("Lightspeed API domain/token are required to resolve missing supplier UUIDs.")

    if domain.startswith("http://") or domain.startswith("https://"):
        base_host = domain.rstrip("/")
    else:
        base_host = f"https://{domain}.retail.lightspeed.app"

    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Accept": "application/json",
        "User-Agent": "HobbyCorner-ProductMigration/1.0",
    }

    wanted = {
        _norm(name): _text(name)
        for name in supplier_names
        if _text(name) and _norm(name) not in supplier_uuid_map
    }
    if not wanted:
        return dict(supplier_uuid_map), {}, []

    res = requests.get(
        f"{base_host}/api/2026-07/suppliers",
        headers=headers,
        params={"page_size": 1000, "deleted": "false"},
        timeout=timeout,
    )
    if not res.ok:
        raise RuntimeError(
            f"Lightspeed supplier lookup failed {res.status_code}: {res.text[:1000]}"
        )

    body = res.json()
    raw = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(raw, dict):
        rows = raw.get("suppliers") or raw.get("items") or []
    elif isinstance(raw, list):
        rows = raw
    else:
        rows = []

    by_name: Dict[str, List[str]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name") or item.get("supplier_name"))
        uid = _text(item.get("id") or item.get("supplier_id"))
        if name and uid:
            by_name.setdefault(_norm(name), []).append(uid)

    out = dict(supplier_uuid_map)
    resolved: Dict[str, str] = {}
    unresolved: List[str] = []

    for key, display_name in wanted.items():
        matches = sorted(set(by_name.get(key, [])))
        if len(matches) == 1:
            out[key] = matches[0]
            resolved[display_name] = matches[0]
        else:
            unresolved.append(display_name)

    return out, resolved, unresolved


def select_test_batch(result: pd.DataFrame, size: int = 12, exclude_skus: Optional[Iterable[str]] = None) -> pd.DataFrame:
    """
    Pick a representative, fully mapped READY batch.

    The original selector could cluster around one supplier/product type
    (for example Testors paints). This version deliberately diversifies by:
      - Lightspeed category path
      - supplier
      - image present / image absent
      - web description present / absent
      - reorder active / reorder suppressed
      - inactive / DoNotOrder where otherwise eligible

    Games Workshop review candidates remain excluded from the first controlled
    write path because reused part numbers require manual identity review.
    """
    eligible = result[
        (result["migration_status"] == "READY")
        & result["Category_Mapped"].astype(bool)
        & result["Supplier_Mapped"].astype(bool)
    ].copy()

    excluded = {
        _text(x).casefold()
        for x in (exclude_skus or [])
        if _text(x)
    }

    if eligible.empty:
        raise ValueError("No new fully mapped READY products are available for a test batch after excluding prior test SKUs.")

    def txt_series(names):
        for name in names:
            if name in eligible.columns:
                return eligible[name].fillna("").astype(str).str.strip()
        return pd.Series("", index=eligible.index)

    eligible["_sku"] = txt_series(["RMS_SKU", "ItemLookupCode", "SKU"])
    if excluded:
        eligible = eligible[
            ~eligible["_sku"].astype(str).str.strip().str.casefold().isin(excluded)
        ].copy()
    eligible["_category"] = eligible.get(
        "LS_Category_Path", pd.Series("", index=eligible.index)
    ).fillna("").astype(str).str.strip()
    eligible["_supplier"] = eligible.get(
        "LS_Supplier", pd.Series("", index=eligible.index)
    ).fillna("").astype(str).str.strip()
    eligible["_picture"] = txt_series(["PictureName", "Picture", "RMS_Picture"])
    eligible["_webdesc"] = txt_series(
        ["Web Description", "RMS_WebDescription", "WebDescription"]
    )

    # Guarantee web-description coverage in controlled previews when the source
    # contains any eligible products with a web description. This gives us a
    # direct test that Web Description becomes the Lightspeed family name.
    webdesc_candidates = eligible[eligible["_webdesc"].astype(str).str.strip() != ""]

    rp = pd.to_numeric(
        eligible.get("proposed_reorder_point", pd.Series(0, index=eligible.index)),
        errors="coerce",
    ).fillna(0)
    rl = pd.to_numeric(
        eligible.get("proposed_restock_level", pd.Series(0, index=eligible.index)),
        errors="coerce",
    ).fillna(0)
    eligible["_reorder_suppressed"] = (rp == 0) & (rl == 0)
    eligible["_inactive"] = eligible.get(
        "Inactive", pd.Series(False, index=eligible.index)
    ).astype(str).str.lower().isin(["1", "true", "yes", "y"])
    eligible["_dno"] = eligible.get(
        "DoNotOrder", pd.Series(False, index=eligible.index)
    ).astype(str).str.lower().isin(["1", "true", "yes", "y"])

    # Prefer rows that add new coverage dimensions to the batch.
    selected_idx = []
    used_skus = set()
    seen_categories = set()
    seen_suppliers = set()
    seen_image_state = set()
    seen_webdesc_state = set()
    seen_reorder_state = set()
    seen_special_state = set()

    remaining = list(eligible.index)

    if size > 0 and not webdesc_candidates.empty:
        first_web_idx = sorted(
            webdesc_candidates.index,
            key=lambda idx: str(webdesc_candidates.loc[idx, "_sku"]).casefold(),
        )[0]
        row = eligible.loc[first_web_idx]
        selected_idx.append(first_web_idx)
        used_skus.add(str(row["_sku"]).strip())
        seen_categories.add(str(row["_category"]).strip().casefold())
        seen_suppliers.add(str(row["_supplier"]).strip().casefold())
        seen_image_state.add(bool(str(row["_picture"]).strip()))
        seen_webdesc_state.add(True)
        seen_reorder_state.add(bool(row["_reorder_suppressed"]))
        seen_special_state.add(bool(row["_inactive"] or row["_dno"]))
        remaining.remove(first_web_idx)

    while remaining and len(selected_idx) < size:
        best_idx = None
        best_score = None

        for idx in remaining:
            row = eligible.loc[idx]
            sku = str(row["_sku"]).strip()
            if not sku or sku in used_skus:
                continue

            category = str(row["_category"]).strip().casefold()
            supplier = str(row["_supplier"]).strip().casefold()
            image_state = bool(str(row["_picture"]).strip())
            webdesc_state = bool(str(row["_webdesc"]).strip())
            reorder_state = bool(row["_reorder_suppressed"])
            special_state = bool(row["_inactive"] or row["_dno"])

            score = (
                (6 if category and category not in seen_categories else 0)
                + (4 if supplier and supplier not in seen_suppliers else 0)
                + (3 if image_state not in seen_image_state else 0)
                + (3 if webdesc_state not in seen_webdesc_state else 0)
                + (2 if reorder_state not in seen_reorder_state else 0)
                + (2 if special_state not in seen_special_state else 0)
            )

            # Deterministic tie-breaker keeps the test reproducible.
            tie = (score, sku.casefold())
            if best_score is None or tie > best_score:
                best_score = tie
                best_idx = idx

        if best_idx is None:
            break

        row = eligible.loc[best_idx]
        selected_idx.append(best_idx)
        used_skus.add(str(row["_sku"]).strip())
        seen_categories.add(str(row["_category"]).strip().casefold())
        seen_suppliers.add(str(row["_supplier"]).strip().casefold())
        seen_image_state.add(bool(str(row["_picture"]).strip()))
        seen_webdesc_state.add(bool(str(row["_webdesc"]).strip()))
        seen_reorder_state.add(bool(row["_reorder_suppressed"]))
        seen_special_state.add(bool(row["_inactive"] or row["_dno"]))
        remaining.remove(best_idx)

    if not selected_idx:
        raise ValueError("Unable to select a representative test batch.")

    return (
        eligible.loc[selected_idx]
        .drop(columns=[
            "_sku", "_category", "_supplier", "_picture", "_webdesc",
            "_reorder_suppressed", "_inactive", "_dno"
        ], errors="ignore")
        .reset_index(drop=True)
    )

def build_standard_family_payload(
    row: pd.Series,
    category_uuid_map: Dict[str, str],
    supplier_uuid_map: Dict[str, str],
    brand_uuid_map: Optional[Dict[str, str]] = None,
    default_weight_unit: Optional[str] = None,
) -> Dict[str, Any]:
    # Use the richer legacy web title only when its identity is corroborated;
    # otherwise fall back to the RMS Description.
    name, _, _ = _trusted_name_parts(row)
    sku = _text(_first_existing(row, ["RMS_SKU", "ItemLookupCode", "SKU"]))
    category_path = _text(row.get("LS_Category_Path"))
    supplier_name = _text(row.get("LS_Supplier"))
    brand_name = _text(_first_existing(row, ["Brand", "RMS_Brand", "brand", "SubDescription3"]))
    upc = _text(_first_existing(row, ["RMS_UPC", "UPC", "Barcode", "barcode"]))
    aliases = _split_aliases(_first_existing(row, ["RMS_Aliases", "Aliases", "aliases"]))
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
    cost = _first_positive_decimal(row, [
        "RMS_SupplierListCost", "SupplierListCost",
        "LastCost", "Cost", "RMS_Cost",
        "ReplacementCost", "RMS_ReplacementCost",
        "supply_price", "Supply Price"
    ])
    description = _text(_first_existing(row, [
        "ExtendedDescription", "Ext. Description", "Web Description"
    ]))

    codes = [{"type": "CUSTOM", "code": sku}]
    seen_codes = {sku}
    if upc and upc not in seen_codes:
        codes.append({"type": _product_code_type(upc), "code": upc})
        seen_codes.add(upc)
    for alias in aliases:
        if alias in seen_codes:
            continue
        # Preserve alternate RMS aliases with barcode types based on code length.
        # Lightspeed accepts UPC/EAN/ITF/CUSTOM codes and requires code uniqueness.
        codes.append({"type": _product_code_type(alias), "code": alias})
        seen_codes.add(alias)

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
        # Controlled Lightspeed creates have accepted Hobby Corner's RMS RetailPrice
        # as the store's tax-exclusive sell price. The 2026-10 API permits only the
        # store-appropriate including- or excluding-tax field.
        product["prices"] = {"price_excluding_tax": retail}

    if supplier_id:
        supplier_entry: Dict[str, Any] = {"supplier_id": supplier_id}
        if cost is not None:
            supplier_entry["price"] = cost
        supplier_code = _text(_first_existing(row, [
            "RMS_SupplierItemCode", "ReorderNumber", "supplier_code", "SupplierCode"
        ]))
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
        brand_name = _text(_first_existing(row, ["Brand", "RMS_Brand", "brand", "SubDescription3"]))
        _, name_source, web_trusted = _trusted_name_parts(row)
        missing_rich_fields = []
        if not brand_name: missing_rich_fields.append("brand")
        if not upc_codes: missing_rich_fields.append("UPC")
        if not supplier_entry.get("code"): missing_rich_fields.append("supplier code")
        if supplier_entry and not supplier_entry.get("price"): missing_rich_fields.append("supply cost")
        if not measurements.get("weight"): missing_rich_fields.append("weight")
        if not picture: missing_rich_fields.append("picture")

        preview_rows.append({
            "Run ID": run_id,
            "RMS Item ID": _text(row.get("RMS_ItemID")),
            "SKU": _text(row.get("RMS_SKU")),
            "Name": payload["name"],
            "Name Source": name_source,
            "Web Description Trusted": web_trusted,
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
            "Review Flags": _text(row.get("Migration_Review_Flags")),
            "Reorder Point Proposed": row.get("proposed_reorder_point", ""),
            "Restock Level Proposed": row.get("proposed_restock_level", ""),
            "Opening Inventory": 0,
            "Write Enabled": False,
            "Payload JSON": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        })

    return pd.DataFrame(preview_rows), payloads


def payload_contains_opening_inventory(payload: Dict[str, Any]) -> bool:
    """Return True if any nested payload key could write outlet inventory."""
    def walk(value: Any) -> bool:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).strip().casefold() in {
                    "outlet_inventories",
                    "outlet_inventory",
                    "inventory",
                    "inventory_level",
                    "on_hand",
                }:
                    return True
                if walk(child):
                    return True
        elif isinstance(value, list):
            return any(walk(child) for child in value)
        return False

    return walk(payload)


def find_picture_path(picture_name: Any, search_roots: Iterable[str]) -> Optional[str]:
    """
    Resolve PictureName against one or more local/Drive folders.

    Search-root order is authoritative. This is important because Hobby Corner
    can have the same filename in multiple image folders; Lightspeed Ready
    should win over Pictures, Pictures over Attention, etc.

    Within each root:
      1. exact filename
      2. case-insensitive full filename
      3. same filename stem across common image extensions

    If multiple stem-only matches exist inside the same priority root, do not
    guess. Duplicate matches in lower-priority roots do not invalidate a
    higher-priority match.
    """
    import os

    name = _text(picture_name)
    if not name:
        return None

    basename = os.path.basename(name)
    wanted = basename.casefold()
    wanted_stem = os.path.splitext(wanted)[0]
    image_extensions = {
        ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"
    }

    for root in search_roots:
        root = _text(root)
        if not root or not os.path.isdir(root):
            continue

        exact = os.path.join(root, basename)
        if os.path.isfile(exact):
            return exact

        full_matches = []
        stem_matches = []

        try:
            for current_root, _, files in os.walk(root):
                for filename in files:
                    candidate = os.path.join(current_root, filename)
                    if not os.path.isfile(candidate):
                        continue

                    folded = filename.casefold()
                    if folded == wanted:
                        full_matches.append(candidate)
                        continue

                    stem, ext = os.path.splitext(folded)
                    if stem == wanted_stem and ext in image_extensions:
                        stem_matches.append(candidate)
        except OSError:
            continue

        unique_full = list(dict.fromkeys(full_matches))
        if unique_full:
            # Same filename in this priority root is acceptable; folder order
            # inside the root is deterministic and preferable to falling
            # through to a lower-priority source.
            return unique_full[0]

        unique_stem = list(dict.fromkeys(stem_matches))
        if len(unique_stem) == 1:
            return unique_stem[0]
        if len(unique_stem) > 1:
            # Ambiguous only within the same priority root.
            return None

    return None

def assess_image_quality(
    picture_path: Optional[str],
    *,
    min_width: int = 500,
    min_height: int = 500,
) -> Dict[str, Any]:
    """
    Inspect a local product image without blocking its use.

    Images below the configurable dimensions are still eligible for upload, but
    receive the background review flag 'poor_quality_image' so they can be
    replaced later.
    """
    result = {
        "width": None,
        "height": None,
        "low_quality": False,
        "review_flags": [],
        "error": "",
    }
    if not picture_path:
        return result

    try:
        from PIL import Image
        with Image.open(picture_path) as img:
            width, height = img.size
        result["width"] = int(width)
        result["height"] = int(height)
        result["low_quality"] = width < min_width or height < min_height
        if result["low_quality"]:
            result["review_flags"].append("poor_quality_image")
    except Exception as exc:
        # Failure to inspect quality should not prevent using the existing file.
        result["error"] = str(exc)
        result["review_flags"].append("image_quality_unverified")

    return result

def execute_controlled_product_write(
    payload: Dict[str, Any],
    *,
    api_domain: str,
    token: str,
    picture_path: Optional[str] = None,
    timeout: int = 45,
) -> Dict[str, Any]:
    """
    Create one STANDARD product family, upload one image if supplied, and read it back.

    Safety rules:
      - exact SKU duplicate gate runs first
      - payloads containing outlet/on-hand inventory keys are rejected
      - image upload happens only after successful product creation
      - no reorder/restock write is attempted here
    """
    import os
    import requests

    if payload_contains_opening_inventory(payload):
        raise ValueError("Blocked write: payload contains an inventory/on-hand field.")

    products = payload.get("products") or []
    if len(products) != 1:
        raise ValueError("Controlled writer currently requires exactly one product per family.")

    codes = products[0].get("codes") or []
    sku = next(
        (
            _text(code.get("code"))
            for code in codes
            if _text(code.get("type")).upper() == "CUSTOM" and _text(code.get("code"))
        ),
        "",
    )
    if not sku:
        raise ValueError("Controlled writer could not resolve the product SKU.")

    domain = _text(api_domain)
    auth_token = _text(token)
    if not domain or not auth_token:
        raise ValueError("Lightspeed API domain/token are required.")

    if domain.startswith("http://") or domain.startswith("https://"):
        base_host = domain.rstrip("/")
    else:
        base_host = f"https://{domain}.retail.lightspeed.app"

    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Accept": "application/json",
        "User-Agent": "HobbyCorner-ProductMigration/1.0",
    }

    result: Dict[str, Any] = {
        "SKU": sku,
        "Status": "PENDING",
        "Family ID": "",
        "Product ID": "",
        "Name Verified": False,
        "Description Verified": False,
        "Category Verified": False,
        "Brand Verified": False,
        "Codes Verified": False,
        "Supplier Verified": False,
        "Price Verified": False,
        "Weight Verified": False,
        "Active Verified": False,
        "Product Fields Verified": False,
        "Image Requested": bool(picture_path),
        "Image Uploaded": False,
        "Image Verified": False,
        "Image Width": "",
        "Image Height": "",
        "Image Low Quality": False,
        "Review Flags": "",
        "Error": "",
    }

    try:
        # Duplicate gate uses the current 2026-10 product search endpoint.
        dup = requests.get(
            f"{base_host}/api/2026-10/products",
            params={"sku": sku, "page_size": 50},
            headers=headers,
            timeout=timeout,
        )
        dup.raise_for_status()
        body = dup.json()
        raw_data = body.get("data")

        # Accept either a list response or a nested products list defensively.
        if isinstance(raw_data, list):
            candidates = raw_data
        elif isinstance(raw_data, dict):
            nested = raw_data.get("products")
            candidates = nested if isinstance(nested, list) else [raw_data]
        else:
            candidates = []

        matches = [
            item for item in candidates
            if isinstance(item, dict) and _text(item.get("sku")) == sku
        ]
        if matches:
            existing = matches[0]
            result["Status"] = "SKIPPED_EXISTING"
            result["Product ID"] = _text(existing.get("id"))
            result["Family ID"] = _text(existing.get("family_id"))
            return result

        created = requests.post(
            f"{base_host}/api/2026-10/product_families",
            json=payload,
            headers={**headers, "Content-Type": "application/json"},
            timeout=timeout,
        )
        if not created.ok:
            raise RuntimeError(f"Create failed {created.status_code}: {created.text[:1000]}")

        create_body = created.json().get("data") or created.json()
        family_id = _text(
            create_body.get("product_family_id")
            or create_body.get("family_id")
            or create_body.get("id")
        )
        product_ids = create_body.get("product_ids") or []
        product_id = _text(product_ids[0] if product_ids else "")

        if not family_id or not product_id:
            raise RuntimeError(f"Create succeeded but IDs were missing: {create_body}")

        result["Family ID"] = family_id
        result["Product ID"] = product_id

        if picture_path:
            if not os.path.isfile(picture_path):
                raise FileNotFoundError(f"Picture not found: {picture_path}")

            quality = assess_image_quality(picture_path)
            result["Image Width"] = quality.get("width") or ""
            result["Image Height"] = quality.get("height") or ""
            result["Image Low Quality"] = bool(quality.get("low_quality"))
            result["Review Flags"] = ",".join(quality.get("review_flags") or [])

            with open(picture_path, "rb") as fh:
                upload = requests.post(
                    f"{base_host}/api/2026-10/product_families/{family_id}/images",
                    headers=headers,
                    files={"image": (os.path.basename(picture_path), fh)},
                    timeout=timeout,
                )
            if not upload.ok:
                raise RuntimeError(f"Image upload failed {upload.status_code}: {upload.text[:1000]}")
            result["Image Uploaded"] = True

        family_read = requests.get(
            f"{base_host}/api/2026-10/product_families/{family_id}",
            headers=headers,
            timeout=timeout,
        )
        if not family_read.ok:
            raise RuntimeError(
                f"Family verify failed {family_read.status_code}: {family_read.text[:1000]}"
            )

        family_data = family_read.json().get("data") or {}
        expected_name = _text(payload.get("name"))
        expected_description = _text(payload.get("description"))
        expected_category = _text(payload.get("category_id"))
        expected_brand = _text(payload.get("brand_id"))

        result["Name Verified"] = _text(family_data.get("name")) == expected_name
        result["Description Verified"] = (
            not expected_description
            or _text(family_data.get("description")) == expected_description
        )
        result["Category Verified"] = (
            _text(family_data.get("category_id")) == expected_category
        )
        result["Brand Verified"] = (
            _text(family_data.get("brand_id")) == expected_brand
        )

        product_read = requests.get(
            f"{base_host}/api/2026-10/products/{product_id}",
            headers=headers,
            timeout=timeout,
        )
        if not product_read.ok:
            raise RuntimeError(
                f"Product verify failed {product_read.status_code}: {product_read.text[:1000]}"
            )
        product_data = product_read.json().get("data") or {}
        expected_product = products[0]

        expected_codes = {
            (_text(x.get("type")).upper(), _text(x.get("code")))
            for x in (expected_product.get("codes") or [])
            if _text(x.get("code"))
        }
        actual_codes = {
            (_text(x.get("type")).upper(), _text(x.get("code")))
            for x in (product_data.get("codes") or [])
            if isinstance(x, dict) and _text(x.get("code"))
        }
        result["Codes Verified"] = expected_codes == actual_codes

        def decimal_equal(left: Any, right: Any) -> bool:
            if left is None or _text(left) == "":
                return right is None or _text(right) == ""
            try:
                return Decimal(str(left)) == Decimal(str(right))
            except (InvalidOperation, ValueError):
                return _text(left) == _text(right)

        expected_suppliers = expected_product.get("suppliers") or []
        actual_suppliers = product_data.get("suppliers") or []
        if expected_suppliers:
            exp = expected_suppliers[0]
            matching = [
                x for x in actual_suppliers
                if isinstance(x, dict)
                and _text(x.get("supplier_id")) == _text(exp.get("supplier_id"))
                and _text(x.get("code")) == _text(exp.get("code"))
                and decimal_equal(x.get("price"), exp.get("price"))
            ]
            result["Supplier Verified"] = bool(matching)
        else:
            result["Supplier Verified"] = not actual_suppliers

        expected_prices = expected_product.get("prices") or {}
        if expected_prices:
            actual_prices = product_data.get("prices") or {}
            result["Price Verified"] = all(
                decimal_equal(actual_prices.get(key), value)
                for key, value in expected_prices.items()
            )
        else:
            result["Price Verified"] = True

        expected_measurements = expected_product.get("measurements") or {}
        if expected_measurements:
            actual_measurements = product_data.get("measurements") or {}
            expected_weight = expected_measurements.get("weight")
            actual_weight = actual_measurements.get("weight")
            weight_matches = (
                abs(float(actual_weight) - float(expected_weight)) < 1e-9
                if actual_weight is not None and expected_weight is not None
                else actual_weight == expected_weight
            )
            result["Weight Verified"] = (
                weight_matches
                and _text(actual_measurements.get("weight_unit")).upper()
                == _text(expected_measurements.get("weight_unit")).upper()
            )
        else:
            result["Weight Verified"] = True

        expected_active = expected_product.get("active") or {}
        actual_active = product_data.get("active") or {}
        result["Active Verified"] = all(
            bool(actual_active.get(key)) == bool(value)
            for key, value in expected_active.items()
        )

        result["Product Fields Verified"] = all([
            result["Name Verified"],
            result["Description Verified"],
            result["Category Verified"],
            result["Brand Verified"],
            result["Codes Verified"],
            result["Supplier Verified"],
            result["Price Verified"],
            result["Weight Verified"],
            result["Active Verified"],
        ])

        # Standard-product catalog images belong on the family. Product-level
        # images are variant/member images and may not appear in the Retail UI.
        images = family_data.get("images") or []
        result["Image Verified"] = bool(images) if picture_path else False

        result["Status"] = "CREATED"
        verification_failures = [
            label for label, ok in [
                ("name", result["Name Verified"]),
                ("description", result["Description Verified"]),
                ("category", result["Category Verified"]),
                ("brand", result["Brand Verified"]),
                ("codes", result["Codes Verified"]),
                ("supplier", result["Supplier Verified"]),
                ("price", result["Price Verified"]),
                ("weight", result["Weight Verified"]),
                ("active", result["Active Verified"]),
            ]
            if not ok
        ]
        if verification_failures:
            result["Status"] = "ERROR"
            result["Error"] = (
                "Read-back mismatch: " + ", ".join(verification_failures)
            )
        if picture_path and not result["Image Verified"]:
            result["Status"] = "ERROR"
            result["Error"] = (
                (result["Error"] + " ").strip()
                + "Image was not present on read-back."
            ).strip()

    except Exception as exc:
        result["Status"] = "ERROR"
        result["Error"] = str(exc)

    return result


def execute_controlled_batch(
    preview: pd.DataFrame,
    payloads: List[Dict[str, Any]],
    *,
    api_domain: str,
    token: str,
    image_search_roots: Iterable[str] = (),
    max_writes: int = 20,
) -> pd.DataFrame:
    """
    Execute a deliberately small test batch and return one result row per item.

    max_writes is a hard guardrail; raise instead of silently processing more.
    """
    absolute_limit = 20
    if max_writes > absolute_limit:
        raise ValueError(
            f"Configured product-write limit {max_writes} exceeds the absolute controlled-test cap of {absolute_limit}."
        )
    if len(payloads) != len(preview):
        raise ValueError("Preview/payload row counts do not match.")
    if len(payloads) > absolute_limit or len(payloads) > max_writes:
        raise ValueError(
            f"Controlled test contains {len(payloads)} products; "
            f"configured limit is {max_writes} and absolute cap is {absolute_limit}."
        )

    results = []
    for idx, payload in enumerate(payloads):
        row = preview.iloc[idx]
        picture_name = row.get("Picture Source", "")
        picture_path = find_picture_path(picture_name, image_search_roots)
        write_result = execute_controlled_product_write(
            payload,
            api_domain=api_domain,
            token=token,
            picture_path=picture_path,
        )
        write_result["Run ID"] = _text(row.get("Run ID"))
        write_result["Picture Source"] = _text(picture_name)
        write_result["Picture Found"] = bool(picture_path)

        existing_flags = [
            x for x in _text(write_result.get("Review Flags")).split(",") if x
        ]
        preview_flags = [
            x for x in _text(row.get("Review Flags")).split(",") if x
        ]
        for flag in preview_flags:
            if flag not in existing_flags:
                existing_flags.append(flag)

        if not picture_path and "missing_image" not in existing_flags:
            existing_flags.append("missing_image")
        write_result["Review Flags"] = ",".join(existing_flags)
        write_result["Opening Inventory"] = 0
        results.append(write_result)

    return pd.DataFrame(results)


def repair_test_batch_images(
    write_results: pd.DataFrame,
    *,
    api_domain: str,
    token: str,
    image_search_roots: Iterable[str] = (),
    timeout: int = 45,
) -> pd.DataFrame:
    """
    Test-only repair helper for products already created by a controlled run.

    Uses Product ID from Migration Test Results, finds Picture Source recursively,
    uploads the image only when the product currently has no images, then reads
    the product back. It never creates products and never changes inventory.
    """
    import os
    import requests

    domain = _text(api_domain)
    auth_token = _text(token)
    if not domain or not auth_token:
        raise ValueError("Lightspeed API domain/token are required.")

    if domain.startswith("http://") or domain.startswith("https://"):
        base_host = domain.rstrip("/")
    else:
        base_host = f"https://{domain}.retail.lightspeed.app"

    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Accept": "application/json",
        "User-Agent": "HobbyCorner-ProductMigration/1.0",
    }

    repaired = []
    for _, row in write_results.iterrows():
        product_id = _text(row.get("Product ID"))
        sku = _text(row.get("SKU"))
        picture_name = _text(row.get("Picture Source"))
        out = {
            "SKU": sku,
            "Product ID": product_id,
            "Picture Source": picture_name,
            "Picture Found": False,
            "Image Uploaded": False,
            "Image Verified": False,
            "Image Width": "",
            "Image Height": "",
            "Image Low Quality": False,
            "Review Flags": "",
            "Status": "",
            "Error": "",
        }

        if not product_id:
            out["Status"] = "SKIPPED_NO_PRODUCT_ID"
            repaired.append(out)
            continue

        picture_path = find_picture_path(picture_name, image_search_roots)
        out["Picture Found"] = bool(picture_path)
        if not picture_path:
            out["Review Flags"] = "missing_image"
            out["Status"] = "MISSING_IMAGE_FILE"
            repaired.append(out)
            continue

        try:
            quality = assess_image_quality(picture_path)
            out["Image Width"] = quality.get("width") or ""
            out["Image Height"] = quality.get("height") or ""
            out["Image Low Quality"] = bool(quality.get("low_quality"))
            out["Review Flags"] = ",".join(quality.get("review_flags") or [])

            # Retail's visible standard-product image is family-level.
            family_id = _text(row.get("Family ID"))
            if not family_id:
                raise RuntimeError("Missing Family ID for image repair.")

            before = requests.get(
                f"{base_host}/api/2026-10/product_families/{family_id}",
                headers=headers,
                timeout=timeout,
            )
            if not before.ok:
                raise RuntimeError(f"Family read failed {before.status_code}: {before.text[:1000]}")
            before_data = before.json().get("data") or {}
            if before_data.get("images"):
                out["Status"] = "ALREADY_HAS_IMAGE"
                out["Image Verified"] = True
                repaired.append(out)
                continue

            with open(picture_path, "rb") as fh:
                upload = requests.post(
                    f"{base_host}/api/2026-10/product_families/{family_id}/images",
                    headers=headers,
                    files={"image": (os.path.basename(picture_path), fh)},
                    timeout=timeout,
                )
            if not upload.ok:
                raise RuntimeError(f"Image upload failed {upload.status_code}: {upload.text[:1000]}")
            out["Image Uploaded"] = True

            after = requests.get(
                f"{base_host}/api/2026-10/product_families/{family_id}",
                headers=headers,
                timeout=timeout,
            )
            if not after.ok:
                raise RuntimeError(f"Family verify failed {after.status_code}: {after.text[:1000]}")
            after_data = after.json().get("data") or {}
            out["Image Verified"] = bool(after_data.get("images"))
            out["Status"] = "IMAGE_VERIFIED" if out["Image Verified"] else "ERROR"
            if not out["Image Verified"]:
                out["Error"] = "Image was not present on read-back."
        except Exception as exc:
            out["Status"] = "ERROR"
            out["Error"] = str(exc)

        repaired.append(out)

    return pd.DataFrame(repaired)


def set_and_verify_reorder_points(
    product_id: str,
    *,
    api_domain: str,
    token: str,
    outlet_id: str,
    reorder_point: float,
    restock_level: float,
    timeout: int = 45,
) -> Dict[str, Any]:
    """
    Set Lightspeed MIN_MAX reorder configuration for one product/outlet and
    verify that current on-hand inventory did not change.

    RMS proposed_reorder_point maps to reorder_point (minimum).
    RMS proposed_restock_level maps to reorder_target (maximum).
    """
    import requests

    domain = _text(api_domain)
    auth_token = _text(token)
    product_id = _text(product_id)
    outlet_id = _text(outlet_id)

    if not domain or not auth_token or not product_id or not outlet_id:
        raise ValueError("Domain, token, product_id, and outlet_id are required.")

    if domain.startswith("http://") or domain.startswith("https://"):
        base_host = domain.rstrip("/")
    else:
        base_host = f"https://{domain}.retail.lightspeed.app"

    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "HobbyCorner-ProductMigration/1.0",
    }

    def read_inventory() -> Dict[str, Any]:
        res = requests.post(
            f"{base_host}/api/2026-04/inventory",
            headers=headers,
            json={
                "product_id": product_id,
                "variants": False,
                "size": 100,
                "sort_direction": "desc",
            },
            timeout=timeout,
        )
        if not res.ok:
            raise RuntimeError(f"Inventory read failed {res.status_code}: {res.text[:1000]}")
        body = res.json()
        rows = body if isinstance(body, list) else body.get("data", [])
        row = next(
            (
                x for x in rows
                if isinstance(x, dict)
                and _text(x.get("product_id")) == product_id
                and _text(x.get("outlet_id")) == outlet_id
            ),
            None,
        )
        if not row:
            raise RuntimeError(
                f"No inventory record found for product {product_id} at outlet {outlet_id}."
            )
        return row

    before = read_inventory()
    before_qty = before.get("current_inventory_level")

    payload = [{
        "product_id": product_id,
        "outlet_id": outlet_id,
        "method": "MIN_MAX",
        "reorder_point": float(reorder_point),
        "reorder_target": float(restock_level),
    }]

    write = requests.post(
        f"{base_host}/api/2026-04/inventory/reorder_points",
        headers=headers,
        json=payload,
        timeout=timeout,
    )
    if not write.ok:
        raise RuntimeError(f"Reorder update failed {write.status_code}: {write.text[:1000]}")

    after = read_inventory()
    after_qty = after.get("current_inventory_level")

    result = {
        "Product ID": product_id,
        "Outlet ID": outlet_id,
        "Requested Reorder Point": float(reorder_point),
        "Requested Restock Level": float(restock_level),
        "Reorder Method": after.get("reorder_method"),
        "Reorder Point": after.get("reorder_point"),
        "Reorder Target": after.get("reorder_target"),
        "Inventory Before": before_qty,
        "Inventory After": after_qty,
        "Inventory Unchanged": before_qty == after_qty,
        "Verified": (
            before_qty == after_qty
            and after.get("reorder_method") == "MIN_MAX"
            and float(after.get("reorder_point")) == float(reorder_point)
            and float(after.get("reorder_target")) == float(restock_level)
        ),
    }

    if not result["Inventory Unchanged"]:
        raise RuntimeError(
            f"Safety failure: inventory changed from {before_qty} to {after_qty}."
        )

    return result


def apply_controlled_reorder_batch(
    rows: pd.DataFrame,
    *,
    api_domain: str,
    token: str,
    outlet_id: str,
    max_writes: int = 20,
) -> pd.DataFrame:
    """
    Apply proposed reorder/restock values to an existing controlled test batch.

    Expected columns:
      - Product ID
      - SKU
      - Reorder Point Proposed
      - Restock Level Proposed
    """
    absolute_limit = 20
    if max_writes > absolute_limit:
        raise ValueError(
            f"Configured reorder-write limit {max_writes} exceeds the absolute controlled-test cap of {absolute_limit}."
        )
    if len(rows) > absolute_limit or len(rows) > max_writes:
        raise ValueError(
            f"Controlled reorder test contains {len(rows)} rows; "
            f"configured limit is {max_writes} and absolute cap is {absolute_limit}."
        )

    out = []
    for _, row in rows.iterrows():
        sku = _text(row.get("SKU"))
        product_id = _text(row.get("Product ID"))
        rp = pd.to_numeric(row.get("Reorder Point Proposed"), errors="coerce")
        rl = pd.to_numeric(row.get("Restock Level Proposed"), errors="coerce")

        result = {
            "SKU": sku,
            "Product ID": product_id,
            "Status": "",
            "Error": "",
        }

        if not product_id:
            result["Status"] = "SKIPPED_NO_PRODUCT_ID"
            out.append(result)
            continue
        if pd.isna(rp) or pd.isna(rl):
            result["Status"] = "SKIPPED_NO_REORDER_VALUES"
            out.append(result)
            continue

        try:
            verified = set_and_verify_reorder_points(
                product_id,
                api_domain=api_domain,
                token=token,
                outlet_id=outlet_id,
                reorder_point=float(rp),
                restock_level=float(rl),
            )
            result.update(verified)
            result["Status"] = "VERIFIED" if verified.get("Verified") else "ERROR"
            if not verified.get("Verified"):
                result["Error"] = "Reorder values did not match on read-back."
        except Exception as exc:
            result["Status"] = "ERROR"
            result["Error"] = str(exc)

        out.append(result)

    return pd.DataFrame(out)


def cleanup_existing_test_products(
    candidates: pd.DataFrame,
    write_results: pd.DataFrame,
    *,
    api_domain: str,
    token: str,
    image_search_roots: Iterable[str] = (),
    timeout: int = 45,
) -> pd.DataFrame:
    """
    Cleanup products already created by the controlled migration test.

    Source rules:
      - Lightspeed family name / customer-facing item description:
        trusted RMS Web Description first; otherwise RMS Description.
      - Lightspeed long description:
        RMS ExtendedDescription / Ext. Description when available.
      - Existing PictureName image is uploaded if the product has no image.
      - Low-resolution images are still used but receive poor_quality_image.

    This helper never creates products and never writes inventory.
    """
    import os
    import requests

    domain = _text(api_domain)
    auth_token = _text(token)
    if not domain or not auth_token:
        raise ValueError("Lightspeed API domain/token are required.")

    if domain.startswith("http://") or domain.startswith("https://"):
        base_host = domain.rstrip("/")
    else:
        base_host = f"https://{domain}.retail.lightspeed.app"

    headers = {
        "Authorization": f"Bearer {auth_token}",
        "Accept": "application/json",
        "User-Agent": "HobbyCorner-ProductMigration/1.0",
    }

    # Build exact SKU lookup from the enriched RMS candidate export.
    candidate_by_sku: Dict[str, pd.Series] = {}
    for _, source_row in candidates.iterrows():
        source_sku = _text(_first_existing(
            source_row, ["RMS_SKU", "ItemLookupCode", "SKU", "Item Lookup Code"]
        ))
        if source_sku:
            candidate_by_sku[source_sku] = source_row

    output = []

    for _, created_row in write_results.iterrows():
        sku = _text(created_row.get("SKU"))
        product_id = _text(created_row.get("Product ID"))
        family_id = _text(created_row.get("Family ID"))

        result: Dict[str, Any] = {
            "SKU": sku,
            "Product ID": product_id,
            "Family ID": family_id,
            "Status": "",
            "RMS Description": "",
            "RMS Web Description": "",
            "Desired Name": "",
            "Name Source": "",
            "Name Updated": False,
            "Name Verified": False,
            "Long Description Updated": False,
            "Long Description Verified": False,
            "Picture Source": "",
            "Picture Found": False,
            "Image Uploaded": False,
            "Image Verified": False,
            "Image Width": "",
            "Image Height": "",
            "Image Low Quality": False,
            "Review Flags": "",
            "Error": "",
        }

        source_row = candidate_by_sku.get(sku)
        if source_row is None:
            result["Status"] = "SOURCE_NOT_FOUND"
            result["Review Flags"] = "migration_source_missing"
            output.append(result)
            continue

        # Never use cleanup to "repair" an existing Lightspeed product from a
        # source row that the migration analysis considers identity-conflicted.
        from analysis import source_identity_review_flags
        source_identity_flags = source_identity_review_flags(source_row)
        if source_identity_flags:
            result["Status"] = "SOURCE_REVIEW"
            result["Review Flags"] = ",".join(source_identity_flags)
            result["Error"] = (
                "Cleanup blocked because the RMS source row has an identity conflict."
            )
            output.append(result)
            continue

        rms_description = _text(_first_existing(
            source_row, ["RMS_Description", "Description"]
        ))
        web_description = _text(_first_existing(
            source_row, ["Web Description", "RMS_WebDescription", "WebDescription"]
        ))
        desired_name, name_source, web_trusted = _trusted_name_parts(source_row)

        long_description = _text(_first_existing(
            source_row, ["ExtendedDescription", "Ext. Description"]
        ))
        picture_name = _text(_first_existing(
            source_row, ["PictureName", "Picture", "RMS_Picture"]
        ))

        result["RMS Description"] = rms_description
        result["RMS Web Description"] = web_description
        result["Desired Name"] = desired_name
        result["Name Source"] = name_source
        result["Picture Source"] = picture_name

        if not product_id or not family_id:
            # Recover IDs from Lightspeed by exact SKU. This handles earlier test
            # rows where duplicate detection prevented IDs from being written to
            # Migration Test Results (for example BSI-117).
            try:
                search_res = requests.get(
                    f"{base_host}/api/2026-10/products",
                    params={"sku": sku, "page_size": 50},
                    headers=headers,
                    timeout=timeout,
                )
                if search_res.ok:
                    search_body = search_res.json()
                    raw_data = search_body.get("data")
                    if isinstance(raw_data, list):
                        search_candidates = raw_data
                    elif isinstance(raw_data, dict):
                        nested = raw_data.get("products")
                        search_candidates = nested if isinstance(nested, list) else [raw_data]
                    else:
                        search_candidates = []

                    exact = next(
                        (
                            item for item in search_candidates
                            if isinstance(item, dict) and _text(item.get("sku")) == sku
                        ),
                        None,
                    )
                    if exact:
                        product_id = _text(exact.get("id"))
                        family_id = _text(
                            exact.get("family_id")
                            or exact.get("product_family_id")
                        )
                        result["Product ID"] = product_id
                        result["Family ID"] = family_id
            except Exception:
                pass

        if not product_id or not family_id:
            result["Status"] = "MISSING_LIGHTSPEED_IDS"
            result["Review Flags"] = "migration_ids_missing"
            output.append(result)
            continue

        review_flags: List[str] = []
        if web_description and not web_trusted:
            review_flags.append("web_description_untrusted")

        try:
            family_res = requests.get(
                f"{base_host}/api/2026-10/product_families/{family_id}",
                headers=headers,
                timeout=timeout,
            )
            if not family_res.ok:
                raise RuntimeError(
                    f"Family read failed {family_res.status_code}: {family_res.text[:1000]}"
                )
            family_data = family_res.json().get("data") or {}

            patch: Dict[str, Any] = {}
            if desired_name and _text(family_data.get("name")) != desired_name:
                patch["name"] = desired_name
            if long_description and _text(family_data.get("description")) != long_description:
                patch["description"] = long_description

            if patch:
                patch_res = requests.patch(
                    f"{base_host}/api/2026-10/product_families/{family_id}",
                    headers={**headers, "Content-Type": "application/json"},
                    json=patch,
                    timeout=timeout,
                )
                if not patch_res.ok:
                    raise RuntimeError(
                        f"Family patch failed {patch_res.status_code}: {patch_res.text[:1000]}"
                    )
                result["Name Updated"] = "name" in patch
                result["Long Description Updated"] = "description" in patch

            # Verify family text after patch.
            verify_family_res = requests.get(
                f"{base_host}/api/2026-10/product_families/{family_id}",
                headers=headers,
                timeout=timeout,
            )
            if not verify_family_res.ok:
                raise RuntimeError(
                    f"Family verify failed {verify_family_res.status_code}: "
                    f"{verify_family_res.text[:1000]}"
                )
            verify_family = verify_family_res.json().get("data") or {}
            result["Name Verified"] = (
                bool(desired_name) and _text(verify_family.get("name")) == desired_name
            )
            result["Long Description Verified"] = (
                not long_description
                or _text(verify_family.get("description")) == long_description
            )

            # Attach the source image at family level so it is visible in the
            # standard-product catalog/editor UI.
            existing_images = family_data.get("images") or []

            picture_path = find_picture_path(picture_name, image_search_roots)
            result["Picture Found"] = bool(picture_path)

            if picture_name and not picture_path:
                review_flags.append("missing_image")

            if picture_path:
                quality = assess_image_quality(picture_path)
                result["Image Width"] = quality.get("width") or ""
                result["Image Height"] = quality.get("height") or ""
                result["Image Low Quality"] = bool(quality.get("low_quality"))
                review_flags.extend(quality.get("review_flags") or [])

                if not existing_images:
                    with open(picture_path, "rb") as fh:
                        upload = requests.post(
                            f"{base_host}/api/2026-10/product_families/{family_id}/images",
                            headers=headers,
                            files={"image": (os.path.basename(picture_path), fh)},
                            timeout=timeout,
                        )
                    if not upload.ok:
                        raise RuntimeError(
                            f"Image upload failed {upload.status_code}: {upload.text[:1000]}"
                        )
                    result["Image Uploaded"] = True

            verify_image_res = requests.get(
                f"{base_host}/api/2026-10/product_families/{family_id}",
                headers=headers,
                timeout=timeout,
            )
            if not verify_image_res.ok:
                raise RuntimeError(
                    f"Family image verify failed {verify_image_res.status_code}: "
                    f"{verify_image_res.text[:1000]}"
                )
            verify_image_family = verify_image_res.json().get("data") or {}
            result["Image Verified"] = bool(verify_image_family.get("images"))

            if not result["Name Verified"]:
                review_flags.append("name_review")
            if picture_name and not result["Image Verified"]:
                review_flags.append("missing_image")

            result["Review Flags"] = ",".join(sorted(set(review_flags)))
            result["Status"] = (
                "VERIFIED"
                if result["Name Verified"]
                and result["Long Description Verified"]
                and (not picture_name or result["Image Verified"])
                else "REVIEW"
            )

        except Exception as exc:
            result["Status"] = "ERROR"
            result["Error"] = str(exc)
            result["Review Flags"] = ",".join(sorted(set(review_flags)))

        output.append(result)

    return pd.DataFrame(output)
