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
    cost = _decimal_string(_first_existing(row, [
        "LastCost", "Cost", "RMS_Cost", "supply_price", "Supply Price"
    ]))
    description = _text(_first_existing(row, [
        "ExtendedDescription", "Ext. Description", "Web Description"
    ]))

    codes = [{"type": "CUSTOM", "code": sku}]
    seen_codes = {sku}
    if upc and upc not in seen_codes:
        codes.append({"type": "UPC", "code": upc})
        seen_codes.add(upc)
    for alias in aliases:
        if alias in seen_codes:
            continue
        # Preserve alternate RMS aliases. Numeric GTIN-like values are barcodes;
        # other aliases remain CUSTOM codes.
        alias_type = "UPC" if alias.isdigit() and len(alias) in {8, 12, 13, 14} else "CUSTOM"
        codes.append({"type": alias_type, "code": alias})
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
        # Hobby Corner currently uses tax-inclusive retail pricing in the source
        # migration data; this can be flipped centrally if store config requires.
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

    Exact filename wins. A case-insensitive filename match is allowed so RMS
    names like .JPG still resolve on case-sensitive Colab mounts.
    """
    import os

    name = _text(picture_name)
    if not name:
        return None

    wanted = name.casefold()

    for root in search_roots:
        root = _text(root)
        if not root or not os.path.isdir(root):
            continue

        exact = os.path.join(root, name)
        if os.path.isfile(exact):
            return exact

        # Images are commonly organized into nested Drive folders such as
        # Lightspeed Ready/category/vendor. Walk recursively so PictureName
        # does not depend on a specific folder layout.
        try:
            for current_root, _, files in os.walk(root):
                for filename in files:
                    if filename.casefold() == wanted:
                        candidate = os.path.join(current_root, filename)
                        if os.path.isfile(candidate):
                            return candidate
        except OSError:
            continue

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
        "Description Verified": False,
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
        # Duplicate gate uses the mature 2.0 product search endpoint.
        dup = requests.get(
            f"{base_host}/api/2.0/products",
            params={"sku": sku, "page_size": 50},
            headers=headers,
            timeout=timeout,
        )
        dup.raise_for_status()
        body = dup.json()
        raw_data = body.get("data")

        # Lightspeed's 2.0 response can be either a list of products or an
        # object containing a products list depending on the query/result.
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
                    f"{base_host}/api/2026-10/products/{product_id}/images",
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
        expected_description = _text(payload.get("description"))
        actual_description = _text(family_data.get("description"))
        result["Description Verified"] = (
            not expected_description or actual_description == expected_description
        )

        verified_products = family_data.get("products") or []
        verified_product = next(
            (p for p in verified_products if _text(p.get("id")) == product_id),
            {},
        )
        images = verified_product.get("images") or []
        result["Image Verified"] = bool(images) if picture_path else False

        result["Status"] = "CREATED"
        if expected_description and not result["Description Verified"]:
            result["Status"] = "ERROR"
            result["Error"] = "Description did not match on read-back."
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
    if len(payloads) != len(preview):
        raise ValueError("Preview/payload row counts do not match.")
    if len(payloads) > max_writes:
        raise ValueError(
            f"Controlled test contains {len(payloads)} products; hard limit is {max_writes}."
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
        if not picture_path:
            existing_flags = [x for x in _text(write_result.get("Review Flags")).split(",") if x]
            if "missing_image" not in existing_flags:
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

            before = requests.get(
                f"{base_host}/api/2026-10/products/{product_id}",
                headers=headers,
                timeout=timeout,
            )
            if not before.ok:
                raise RuntimeError(f"Product read failed {before.status_code}: {before.text[:1000]}")
            before_data = before.json().get("data") or {}
            if before_data.get("images"):
                out["Status"] = "ALREADY_HAS_IMAGE"
                out["Image Verified"] = True
                repaired.append(out)
                continue

            with open(picture_path, "rb") as fh:
                upload = requests.post(
                    f"{base_host}/api/2026-10/products/{product_id}/images",
                    headers=headers,
                    files={"image": (os.path.basename(picture_path), fh)},
                    timeout=timeout,
                )
            if not upload.ok:
                raise RuntimeError(f"Image upload failed {upload.status_code}: {upload.text[:1000]}")
            out["Image Uploaded"] = True

            after = requests.get(
                f"{base_host}/api/2026-10/products/{product_id}",
                headers=headers,
                timeout=timeout,
            )
            if not after.ok:
                raise RuntimeError(f"Product verify failed {after.status_code}: {after.text[:1000]}")
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
