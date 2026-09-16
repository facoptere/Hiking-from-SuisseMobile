#!/usr/bin/env python3
"""Analyse de pente / vitesse a partir d'un GPX (Polars)."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET

import polars as pl

ROOT = Path(__file__).resolve().parent
DEFAULT_GPX = ROOT / "AdelbodenRandonnée20260915100502.gpx"
NS = {
    "gpx": "http://www.topografix.com/GPX/1/1",
    "gpxdata": "http://www.cluetrust.com/XML/GPXDATA/1/0",
}


def parse_time(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def _float_or_zero(el) -> float:
    if el is None or el.text is None or el.text.strip() == "":
        return 0.0
    try:
        return float(el.text)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Bloc 1
# Lire les arguments CLI. Imposes : GPX, CSV, fenetres altitude / dpente /
# pentetranchecount, dpentemax, frequence attendue (1 Hz).
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gpx", nargs="?", default=str(DEFAULT_GPX), help="fichier GPX")
    parser.add_argument("--csv", default="", help="CSV des points (defaut: <gpx>.csv)")
    parser.add_argument(
        "--synthesis",
        default="",
        help="CSV de synthese (defaut: <gpx>_synthese.csv)",
    )
    parser.add_argument(
        "--altitude-window",
        type=float,
        default=120.0,
        help="fenetre dh/dp/dl en secondes (defaut: 120 = 2 min)",
    )
    parser.add_argument(
        "--depente-window",
        type=float,
        default=30.0,
        help="fenetre dpente/mmdp/mmspeed en secondes (defaut: 30)",
    )
    parser.add_argument(
        "--dpente-max",
        type=float,
        default=5.0,
        help="seuil |dpente| en points de pente %% (defaut: 5)",
    )
    parser.add_argument(
        "--pentetranche-count-window",
        type=float,
        default=60.0,
        help="duree min d'un segment stable, en secondes (defaut: 60)",
    )
    parser.add_argument(
        "--hz",
        type=float,
        default=1.0,
        help="points attendus par seconde pour dp (defaut: 1)",
    )
    parser.add_argument(
        "--mmdp-min",
        type=float,
        default=0.99,
        help="mmdp minimum pour valid (defaut: 0.99)",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Bloc 2
# DataFrame brut depuis le GPX :
#   elapsed  = t - t(premier trkpt) en secondes
#   distance = gpxdata:distance, 0 si absent
#   altitude = <ele>
#   speed    = gpxdata:speed en m/s, 0 si absent
# ---------------------------------------------------------------------------
def load_gpx(path: Path) -> pl.DataFrame:
    rows: list[dict] = []
    t0: datetime | None = None
    for _event, elem in ET.iterparse(path, events=("end",)):
        tag = elem.tag.split("}", 1)[-1]
        if tag != "trkpt":
            continue
        ele_el = elem.find("gpx:ele", NS)
        time_el = elem.find("gpx:time", NS)
        if time_el is None or time_el.text is None:
            elem.clear()
            continue
        t = parse_time(time_el.text)
        if t0 is None:
            t0 = t
        altitude = _float_or_zero(ele_el)
        distance = _float_or_zero(elem.find("gpx:extensions/gpxdata:distance", NS))
        speed = _float_or_zero(elem.find("gpx:extensions/gpxdata:speed", NS))
        rows.append(
            {
                "elapsed": (t - t0).total_seconds(),
                "distance": distance,
                "altitude": altitude,
                "speed": speed,
                "time": t,
            }
        )
        elem.clear()
    if not rows:
        raise SystemExit(f"aucun trkpt dans {path}")
    return pl.DataFrame(rows).sort("elapsed")


# ---------------------------------------------------------------------------
# Bloc 3
# Regard en arriere de altitudewindows (defaut 2 min), asof backward :
#   dh = altitude(t) - altitude(t - fenetre)
#   dp = (nb de trkpt dans la fenetre) / (fenetre_s * hz)
#   dl = distance(t) - distance(t - fenetre)  (cumul GPX = somme des pas)
# ---------------------------------------------------------------------------
def add_altitude_window(df: pl.DataFrame, window_s: float, hz: float) -> pl.DataFrame:
    expected = window_s * hz
    ref = df.select("elapsed", "altitude", "distance")
    df = df.with_columns((pl.col("elapsed") - window_s).alias("t_alt_past"))
    past = ref.rename(
        {
            "elapsed": "t_alt_past",
            "altitude": "alt_past",
            "distance": "dist_past",
        }
    )
    df = df.sort("t_alt_past").join_asof(
        past.sort("t_alt_past"), on="t_alt_past", strategy="backward"
    )
    counted = (
        df.sort("time")
        .rolling(index_column="time", period=f"{int(round(window_s))}s", closed="both")
        .agg(pl.len().alias("n_alt_win"))
    )
    df = df.join(counted, on="time", how="left")
    return df.with_columns(
        (pl.col("altitude") - pl.col("alt_past")).alias("dh"),
        (pl.col("n_alt_win").cast(pl.Float64) / expected).alias("dp"),
        (pl.col("distance") - pl.col("dist_past")).alias("dl"),
    )


# ---------------------------------------------------------------------------
# Bloc 4
#   pente  = dh/dl  (ratio, pas x100)
#   dpente = |pente(t) - pente(t-depentewindow)| * 100  (points de %, non relatif)
#   mmdp   = rolling mean de dp sur depentewindow
#   mmspeed= rolling mean de speed (m/s) sur depentewindow
# ---------------------------------------------------------------------------
def add_pente_window(df: pl.DataFrame, window_s: float) -> pl.DataFrame:
    period = f"{int(round(window_s))}s"
    df = df.with_columns(
        pl.when(pl.col("dl") > 0)
        .then(pl.col("dh") / pl.col("dl"))
        .otherwise(None)
        .alias("pente")
    )
    ref = df.select("elapsed", "pente")
    df = df.with_columns((pl.col("elapsed") - window_s).alias("t_pente_past"))
    past = ref.rename({"elapsed": "t_pente_past", "pente": "pente_past"})
    df = df.sort("t_pente_past").join_asof(
        past.sort("t_pente_past"), on="t_pente_past", strategy="backward"
    )
    df = df.with_columns(
        pl.when(pl.col("pente").is_not_null() & pl.col("pente_past").is_not_null())
        .then((pl.col("pente") - pl.col("pente_past")).abs() * 100.0)
        .otherwise(None)
        .alias("dpente")
    )
    rolled = (
        df.sort("time")
        .rolling(index_column="time", period=period, closed="both")
        .agg(
            pl.col("dp").mean().alias("mmdp"),
            pl.col("speed").mean().alias("mmspeed"),
        )
    )
    return df.join(rolled, on="time", how="left")


# ---------------------------------------------------------------------------
# Bloc 5
# valid = speed > 0 ET distance > 0 ET mmdp >= 0.99 ET |dpente| < dpentemax
# (dpente en points de pente, non relatif)
# ---------------------------------------------------------------------------
def add_valid(df: pl.DataFrame, dpente_max: float, mmdp_min: float) -> pl.DataFrame:
    return df.with_columns(
        (
            (pl.col("speed") > 0)
            & (pl.col("distance") > 0)
            & (pl.col("mmdp") >= mmdp_min)
            & pl.col("dpente").is_not_null()
            & (pl.col("dpente") < dpente_max)
            & pl.col("pente").is_not_null()
        ).alias("valid")
    )


# ---------------------------------------------------------------------------
# Bloc 6
# pentetranche = pente en % arrondie au multiple de 5 le plus proche
#   (floor(|pct|/5 + 0.5)*5, signe conserve : 2.5% -> 5, -2.5% -> -5)
# pentetranchecount = index 1..n dans une run consecutive
#   (meme pentetranche ET valid=1) ; 0 des que valid=0
# ---------------------------------------------------------------------------
def add_tranches(df: pl.DataFrame) -> pl.DataFrame:
    pct = pl.col("pente") * 100.0
    mag = (pct.abs() / 5.0 + 0.5).floor() * 5.0
    tranche = (
        pl.when(mag == 0)
        .then(pl.lit(0.0))
        .when(pct >= 0)
        .then(mag)
        .otherwise(-mag)
    )
    df = df.sort("elapsed").with_columns(
        pl.when(pl.col("pente").is_not_null()).then(tranche).otherwise(None).alias("pentetranche")
    )
    run_break = (
        (~pl.col("valid").shift(1).fill_null(False))
        | (pl.col("pentetranche") != pl.col("pentetranche").shift(1))
    )
    df = df.with_columns(run_break.cast(pl.UInt32).cum_sum().alias("run_id"))
    return df.with_columns(
        pl.when(pl.col("valid") & pl.col("pentetranche").is_not_null())
        .then(pl.int_range(pl.len()).over("run_id") + 1)
        .otherwise(0)
        .alias("pentetranchecount")
    )


# ---------------------------------------------------------------------------
# Bloc 6b
# Sacrifier les premieres lignes sur max(altitudewindows, depentewindow,
# pentetranchecountwindow) pour que toutes les fenetres soient definies.
# ---------------------------------------------------------------------------
def drop_warmup(df: pl.DataFrame, warmup_s: float) -> pl.DataFrame:
    return df.filter(pl.col("elapsed") >= warmup_s)


# ---------------------------------------------------------------------------
# Bloc 7
# Enregistrer tout le DataFrame en CSV ; separateur ";" ; decimale ",".
# ---------------------------------------------------------------------------
def write_csv(path: Path, df: pl.DataFrame) -> None:
    skip = {"time", "t_alt_past", "t_pente_past", "alt_past", "dist_past", "pente_past", "n_alt_win"}
    cols = [c for c in df.columns if c not in skip]
    df.select(cols).write_csv(
        path,
        separator=";",
        include_bom=True,
        decimal_comma=True,
        float_precision=6,
        null_value="",
    )


# ---------------------------------------------------------------------------
# Bloc 8
# Segments = runs (run_id) de pentetranche identique, valid=1,
# longueur (max pentetranchecount) > pentetranchecountwindow.
# Vitesse d'un segment = Δdistance / Δelapsed (pas mmspeed).
# Synthese par pentetranche : n_seg, dist km, km/h = ΣΔdistance / ΣΔelapsed * 3.6.
# Ecriture CSV (pas stdout) : pentetranche, n_seg, dist, dt, kmh.
# ---------------------------------------------------------------------------
def write_synthesis(df: pl.DataFrame, count_window: float, path: Path) -> pl.DataFrame | None:
    segs = (
        df.filter(pl.col("valid") & pl.col("pentetranche").is_not_null())
        .group_by("run_id", "pentetranche")
        .agg(
            pl.col("pentetranchecount").max().alias("n"),
            (
                pl.col("distance").sort_by("elapsed").last()
                - pl.col("distance").sort_by("elapsed").first()
            ).alias("ddist"),
            (
                pl.col("elapsed").sort_by("elapsed").last()
                - pl.col("elapsed").sort_by("elapsed").first()
            ).alias("dt"),
        )
        .filter((pl.col("n") > count_window) & (pl.col("dt") > 0) & (pl.col("ddist") > 0))
    )
    if segs.is_empty():
        print("aucune run pentetranchecount > {:.0f}".format(count_window))
        return None
    summary = (
        segs.group_by("pentetranche")
        .agg(
            pl.len().alias("n_seg"),
            pl.col("ddist").sum().alias("dist"),
            pl.col("dt").sum().alias("dt"),
        )
        .with_columns((pl.col("dist") / pl.col("dt") * 3.6).alias("kmh"))
        .sort("pentetranche")
    )
    summary.write_csv(
        path,
        separator=";",
        include_bom=True,
        decimal_comma=True,
        float_precision=6,
        null_value="",
    )
    return summary



def _fmt_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}h {minutes:02d}min {secs:02d}s ({total} s)"


def main() -> int:
    args = parse_args()
    gpx_path = Path(args.gpx)
    if not gpx_path.is_file():
        raise SystemExit(f"fichier introuvable: {gpx_path}")

    df = load_gpx(gpx_path)
    coros_duration = float(df["elapsed"].max() or 0.0)
    df = add_altitude_window(df, args.altitude_window, args.hz)
    df = add_pente_window(df, args.depente_window)
    df = add_valid(df, args.dpente_max, args.mmdp_min)
    df = add_tranches(df)
    warmup = max(args.altitude_window, args.depente_window, args.pentetranche_count_window)
    df = drop_warmup(df, warmup)

    csv_path = Path(args.csv) if args.csv else gpx_path.with_suffix(".csv")
    write_csv(csv_path, df)
    synth_path = (
        Path(args.synthesis)
        if args.synthesis
        else gpx_path.with_name(gpx_path.stem + "_synthese.csv")
    )
    synth = write_synthesis(df, args.pentetranche_count_window, synth_path)
    print(f"csv points: {csv_path}")
    print(f"csv synthese: {synth_path}")
    print(f"lignes: {df.height} (warmup {warmup:.0f} s retire)")
    print(f"duree GPX mesure: {_fmt_duration(coros_duration)}")
    if synth is None:
        print("synthese vide")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
