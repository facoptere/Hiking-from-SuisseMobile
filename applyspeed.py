#!/usr/bin/env python3
"""Applique une synthese CSV de vitesses a un GPX hiking (Polars)."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

import polars as pl

ROOT = Path(__file__).resolve().parent
NS = {
    "gpx": "http://www.topografix.com/GPX/1/1",
    "gpxdata": "http://www.cluetrust.com/XML/GPXDATA/1/0",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "gpx",
        nargs="?",
        default=str(ROOT / "hiking/1-19/1.14r Adelboden - Kandersteg.gpx"),
        help="GPX hiking a dater",
    )
    parser.add_argument(
        "--synthesis",
        default=str(ROOT / "AdelbodenRandonnée20260915100502_synthese.csv"),
        help="CSV de synthese (pentetranche; kmh)",
    )
    parser.add_argument(
        "--pente-window",
        type=int,
        default=10,
        help="fenetre pente instantanee en nombre de points (defaut: 10)",
    )
    parser.add_argument(
        "--duration",
        default="+45%",
        help="facteur sur la duree de chaque point, ex. +45%% (defaut: +45%%)",
    )
    parser.add_argument(
        "--start",
        default="",
        help="datetime UTC de depart (ISO, defaut: maintenant)",
    )
    parser.add_argument(
        "--output",
        default="",
        help="GPX de sortie (defaut: <stem>_timed.gpx a cote de la source)",
    )
    return parser.parse_args()


def _float_or_zero(el) -> float:
    if el is None or el.text is None or el.text.strip() == "":
        return 0.0
    try:
        return float(el.text)
    except ValueError:
        return 0.0


def _parse_duration_factor(value: str) -> float:
    text = value.strip().replace(",", ".")
    if text.endswith("%"):
        pct = float(text[:-1].replace("+", ""))
        if text.startswith("-") and pct > 0:
            pct = -pct
        return 1.0 + pct / 100.0
    return float(text)


def _parse_start(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc).replace(microsecond=0)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    t = datetime.fromisoformat(text)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t


def _fmt_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}h {minutes:02d}min {secs:02d}s ({total} s)"


def unique_timed_path(src: Path) -> Path:
    candidate = src.with_name(f"{src.stem}_timed{src.suffix}")
    if not candidate.exists():
        return candidate
    i = 2
    while True:
        candidate = src.with_name(f"{src.stem}_timed-{i}{src.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def load_synthesis(path: Path) -> pl.DataFrame:
    df = pl.read_csv(
        path,
        separator=";",
        decimal_comma=True,
        encoding="utf-8-sig",
    )
    need = {"pentetranche", "kmh"}
    missing = need - set(df.columns)
    if missing:
        raise SystemExit(f"synthese incomplete, colonnes manquantes: {sorted(missing)}")
    return df.filter(pl.col("pentetranche").is_not_null() & pl.col("kmh").is_not_null())


def load_hiking_gpx(path: Path) -> tuple[pl.DataFrame, dict]:
    meta = {"name": "", "link": "", "type": "hiking"}
    rows: list[dict] = []
    for _event, elem in ET.iterparse(path, events=("end",)):
        tag = elem.tag.split("}", 1)[-1]
        if tag == "name" and not meta["name"] and elem.text:
            meta["name"] = elem.text
        elif tag == "link" and not meta["link"]:
            meta["link"] = elem.attrib.get("href", "")
        elif tag == "type" and elem.text:
            meta["type"] = elem.text
        elif tag == "trkpt":
            ele_el = elem.find("gpx:ele", NS)
            rows.append(
                {
                    "lat": float(elem.attrib["lat"]),
                    "lon": float(elem.attrib["lon"]),
                    "ele": _float_or_zero(ele_el),
                }
            )
            elem.clear()
    if not rows:
        raise SystemExit(f"aucun trkpt dans {path}")
    return pl.DataFrame(rows), meta


def interpolate_kmh(df: pl.DataFrame, summary: pl.DataFrame) -> pl.DataFrame:
    synth = summary.select("pentetranche", "kmh").sort("pentetranche")
    lo = synth.rename({"pentetranche": "pente_pct", "kmh": "kmh_lo"}).with_columns(
        pl.col("pente_pct").alias("t_lo")
    )
    hi = summary.select(
        pl.col("pentetranche").alias("pente_pct"),
        pl.col("kmh").alias("kmh_hi"),
        pl.col("pentetranche").alias("t_hi"),
    ).sort("pente_pct")
    df = df.with_row_index("_idx").with_columns((pl.col("pente") * 100.0).alias("pente_pct"))
    df = df.sort("pente_pct").join_asof(lo.sort("pente_pct"), on="pente_pct", strategy="backward")
    df = df.sort("pente_pct").join_asof(hi, on="pente_pct", strategy="forward")
    df = df.sort("_idx")
    return df.with_columns(
        pl.when(pl.col("pente").is_null())
        .then(None)
        .when(pl.col("t_lo").is_null())
        .then(pl.col("kmh_hi"))
        .when(pl.col("t_hi").is_null())
        .then(pl.col("kmh_lo"))
        .when(pl.col("t_hi") == pl.col("t_lo"))
        .then(pl.col("kmh_lo"))
        .otherwise(
            pl.col("kmh_lo")
            + (pl.col("kmh_hi") - pl.col("kmh_lo"))
            * (pl.col("pente_pct") - pl.col("t_lo"))
            / (pl.col("t_hi") - pl.col("t_lo"))
        )
        .alias("kmh")
    )


def apply_hiking_times(
    path: Path,
    summary: pl.DataFrame,
    pente_window: int,
    start: datetime,
    duration_factor: float,
    output: Path | None = None,
) -> tuple[Path, float]:
    df, meta = load_hiking_gpx(path)
    lat1 = pl.col("lat").radians()
    lat2 = pl.col("lat").shift(1).radians()
    dlat = (pl.col("lat") - pl.col("lat").shift(1)).radians()
    dlon = (pl.col("lon") - pl.col("lon").shift(1)).radians()
    a = (dlat / 2).sin() ** 2 + lat1.cos() * lat2.cos() * (dlon / 2).sin() ** 2
    step = (2 * 6371000.0 * a.sqrt().arcsin()).fill_null(0.0)
    n = max(int(pente_window), 1)
    df = df.with_columns(
        step.alias("step_m"),
        step.cum_sum().alias("gpxdata_distance"),
    )
    df = df.with_columns(
        pl.when(pl.col("gpxdata_distance") - pl.col("gpxdata_distance").shift(n - 1) > 0)
        .then(
            (pl.col("ele") - pl.col("ele").shift(n - 1))
            / (pl.col("gpxdata_distance") - pl.col("gpxdata_distance").shift(n - 1))
        )
        .otherwise(None)
        .alias("pente")
    )
    df = df.with_columns(pl.col("pente").fill_null(strategy="backward").fill_null(strategy="forward"))
    df = interpolate_kmh(df, summary)
    df = df.with_columns(
        pl.col("kmh").fill_null(strategy="backward").fill_null(strategy="forward")
    )
    df = df.sort("_idx") if "_idx" in df.columns else df
    df = df.with_columns(
        pl.when(pl.col("kmh") > 0)
        .then(pl.col("step_m") / (pl.col("kmh") / 3.6) * duration_factor)
        .otherwise(0.0)
        .alias("dt"),
    )
    df = df.with_columns(
        pl.when(pl.col("dt") > 0)
        .then(pl.col("step_m") / pl.col("dt"))
        .otherwise(0.0)
        .alias("gpxdata_speed")
    )
    df = df.with_columns(pl.col("dt").cum_sum().alias("elapsed"))
    out = output if output is not None else unique_timed_path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_hiking_gpx(out, df, meta, start)
    return out, float(df["elapsed"].max() or 0.0)


def kilometer_waypoints(df: pl.DataFrame) -> pl.DataFrame:
    total = float(df["gpxdata_distance"].max() or 0.0)
    n_km = int(total // 1000)
    if n_km < 1:
        return df.head(0)
    targets = pl.DataFrame({"km": list(range(1, n_km + 1))}).with_columns(
        (pl.col("km") * 1000.0).alias("target")
    )
    pts = df.select("lat", "lon", "ele", "elapsed", "gpxdata_distance")
    return (
        targets.join(pts, how="cross")
        .with_columns((pl.col("gpxdata_distance") - pl.col("target")).abs().alias("err"))
        .sort(["err", "km"])
        .unique(subset=["km"], keep="first")
        .sort("km")
    )


def write_hiking_gpx(path: Path, df: pl.DataFrame, meta: dict, start: datetime) -> None:
    gpx_ns = "http://www.topografix.com/GPX/1/1"
    data_ns = "http://www.cluetrust.com/XML/GPXDATA/1/0"
    ET.register_namespace("", gpx_ns)
    ET.register_namespace("gpxdata", data_ns)
    root = ET.Element(
        f"{{{gpx_ns}}}gpx",
        {"version": "1.1", "creator": "crawler"},
    )
    wpts = kilometer_waypoints(df)
    for row in wpts.iter_rows(named=True):
        wpt = ET.SubElement(
            root,
            f"{{{gpx_ns}}}wpt",
            {"lat": f"{row['lat']:.7f}", "lon": f"{row['lon']:.7f}"},
        )
        ET.SubElement(wpt, f"{{{gpx_ns}}}ele").text = f"{row['ele']:.1f}"
        t = start + timedelta(seconds=round(float(row["elapsed"])))
        ET.SubElement(wpt, f"{{{gpx_ns}}}time").text = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        ET.SubElement(wpt, f"{{{gpx_ns}}}name").text = f"{int(row['km'])} km"
    trk = ET.SubElement(root, f"{{{gpx_ns}}}trk")
    if meta.get("name"):
        ET.SubElement(trk, f"{{{gpx_ns}}}name").text = meta["name"]
    if meta.get("link"):
        ET.SubElement(trk, f"{{{gpx_ns}}}link", {"href": meta["link"]})
    ET.SubElement(trk, f"{{{gpx_ns}}}type").text = meta.get("type") or "hiking"
    seg = ET.SubElement(trk, f"{{{gpx_ns}}}trkseg")
    for row in df.iter_rows(named=True):
        pt = ET.SubElement(
            seg,
            f"{{{gpx_ns}}}trkpt",
            {"lat": f"{row['lat']:.7f}", "lon": f"{row['lon']:.7f}"},
        )
        ET.SubElement(pt, f"{{{gpx_ns}}}ele").text = f"{row['ele']:.1f}"
        t = start + timedelta(seconds=round(float(row["elapsed"])))
        ET.SubElement(pt, f"{{{gpx_ns}}}time").text = t.strftime("%Y-%m-%dT%H:%M:%SZ")
        ext = ET.SubElement(pt, f"{{{gpx_ns}}}extensions")
        dist = ET.SubElement(ext, f"{{{data_ns}}}distance")
        dist.text = f"{row['gpxdata_distance']:.2f}"
        spd = ET.SubElement(ext, f"{{{data_ns}}}speed")
        spd.text = f"{row['gpxdata_speed']:.3f}"
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def main() -> int:
    args = parse_args()
    gpx_path = Path(args.gpx)
    synth_path = Path(args.synthesis)
    if not gpx_path.is_file():
        raise SystemExit(f"gpx introuvable: {gpx_path}")
    if not synth_path.is_file():
        raise SystemExit(f"synthese introuvable: {synth_path}")
    summary = load_synthesis(synth_path)
    if summary.is_empty():
        raise SystemExit("synthese vide")
    out, duration = apply_hiking_times(
        gpx_path,
        summary,
        pente_window=args.pente_window,
        start=_parse_start(args.start),
        duration_factor=_parse_duration_factor(args.duration),
        output=Path(args.output) if args.output else None,
    )
    print(f"synthese: {synth_path}")
    print(f"gpx hiking: {out}")
    print(f"duree GPX simule: {_fmt_duration(duration)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
