from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class ImportedExhibitor:
    display_name: str
    name: str
    booth: str | None
    reserved_phones: int
    reserved_licenses: int | None


def _parse_exhibitor_booth(value: str) -> tuple[str, str | None, str]:
    raw = (value or "").strip()
    if " / " in raw:
        left, right = raw.rsplit(" / ", 1)
        name = left.strip() or raw
        booth = right.strip() or None
        display = raw
        return name, booth, display
    return raw, None, raw


def parse_totali_phone_rentals_xls(file_path: str) -> list[ImportedExhibitor]:
    df = pd.read_excel(file_path, engine="xlrd")

    required = ["Exhibitor/Booth", "iPhones"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing required column: {col}")

    out: list[ImportedExhibitor] = []
    for _, row in df.iterrows():
        exhibitor_booth = str(row.get("Exhibitor/Booth") or "").strip()
        if not exhibitor_booth:
            continue

        phones = row.get("iPhones")
        try:
            reserved_phones = int(phones) if phones == phones else 0  # NaN check
        except Exception:
            reserved_phones = 0

        licenses = row.get("Licenses")
        reserved_licenses: int | None
        try:
            reserved_licenses = int(licenses) if licenses == licenses else None
        except Exception:
            reserved_licenses = None

        name, booth, display = _parse_exhibitor_booth(exhibitor_booth)
        out.append(
            ImportedExhibitor(
                display_name=display,
                name=name,
                booth=booth,
                reserved_phones=reserved_phones,
                reserved_licenses=reserved_licenses,
            )
        )

    return out
