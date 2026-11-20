#!/usr/bin/env python3
"""Generate three R08 building QR codes and an A6 SVG sheet.

Example: .venv/bin/python scripts/generate_booth_qr.py --base-url http://127.0.0.1:5181 --output-dir /tmp/baro-qr-three
This is the fixed local demo origin; regenerate with the actual accessible origin before use.
Use the configured origin exactly: localhost and 127.0.0.1 are different browser origins.
"""

import argparse
import csv
import html
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

import qrcode
from qrcode.constants import ERROR_CORRECT_M

ROOT = Path(__file__).resolve().parents[1]
BUILDINGS = ("301", "60", "62-1")
BORDER = 4
QR_MM = 29


def validate_origin(value: str) -> str:
    if any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("주소에 공백 또는 제어 문자를 사용할 수 없습니다.")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError as exc:
        raise ValueError("올바른 포트와 호스트가 필요합니다.") from exc
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
            or url.password is not None or url.path not in {"", "/"} or url.query or url.fragment
            or "?" in value or "#" in value):
        raise ValueError("주소는 사용자 정보·경로·쿼리 없는 HTTP(S) 원점이어야 합니다.")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("올바른 포트가 필요합니다.")
    try:
        host = url.hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("호스트를 해석할 수 없습니다.") from exc
    if ":" in host:
        host = f"[{host}]"
    return urlunsplit((url.scheme, host + (f":{port}" if port is not None else ""), "", "", ""))


def building_names() -> dict[str, str]:
    with (ROOT / "data/campus-profile/buildings.csv").open(encoding="utf-8", newline="") as f:
        rows = {row["building_id"]: row["name"] for row in csv.DictReader(f)
                if row["campus"] == "GWANAK" and row["building_id"] in BUILDINGS}
    if set(rows) != set(BUILDINGS) or any(not name for name in rows.values()):
        raise ValueError("R08 건물 이름을 확인할 수 없습니다.")
    return rows


def qr_matrix(url: str) -> list[list[bool]]:
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=BORDER)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    # Protect the module size on the A6 sheet, including the four-module quiet zone.
    if len(matrix) > 57:
        raise ValueError("인쇄 QR이 너무 촘촘합니다. 더 짧은 원점을 사용해 주세요.")
    return matrix


def qr_svg(matrix: list[list[bool]], x: float = 0, y: float = 0, size: float = 40) -> str:
    n = len(matrix)
    path = "".join(f"M{col} {row}h1v1h-1z" for row, cells in enumerate(matrix)
                   for col, dark in enumerate(cells) if dark)
    return (f'<svg x="{x}" y="{y}" width="{size}" height="{size}" '
            f'viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            f'<rect width="{n}" height="{n}" fill="white"/>'
            f'<path d="{path}" fill="black"/></svg>')


def generate(base_url: str, output_dir: Path) -> None:
    origin = validate_origin(base_url)
    names = building_names()
    matrices = {}
    for building in BUILDINGS:
        code = f"GWANAK_{building}"
        url = f"{origin}/report?{urlencode({'loc': code})}"
        matrices[building] = qr_matrix(url)
    output_dir.mkdir(parents=True, exist_ok=True)
    for building, matrix in matrices.items():
        body = qr_svg(matrix)
        (output_dir / f"GWANAK_{building}.svg").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="40mm" '
            'viewBox="0 0 40 40">' + body + '</svg>\n', encoding="utf-8")
    sheet = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<svg xmlns="http://www.w3.org/2000/svg" width="105mm" height="148mm" viewBox="0 0 105 148">',
             '<rect width="105" height="148" fill="white"/>',
             '<text x="52.5" y="6" text-anchor="middle" font-family="sans-serif" font-size="3.2" font-weight="bold">건물별 시설 신고</text>']
    for index, building in enumerate(BUILDINGS):
        x = 1 + index % 2 * 52
        y = 9 + index // 2 * 46
        title = html.escape(f"{building}동 {names[building]}")
        sheet += [f'<text x="{x + 26}" y="{y + 4}" text-anchor="middle" font-family="sans-serif" font-size="2.9" font-weight="bold">{title}</text>',
                  qr_svg(matrices[building], x + 11.5, y + 5, QR_MM),
                  f'<text x="{x + 26}" y="{y + 38}" text-anchor="middle" font-family="sans-serif" font-size="2.35">찍으면 이 건물로 신고가 시작돼요</text>']
    sheet.append('</svg>\n')
    (output_dir / "booth-a6.svg").write_text("\n".join(sheet), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="배포된 서비스의 HTTP(S) 원점 (경로 제외)")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        generate(args.base_url, args.output_dir)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"QR {len(BUILDINGS)}개와 A6 시트를 생성했습니다: {args.output_dir}")


if __name__ == "__main__":
    main()
