"""LightGBM directional model.

Target: will price move up by at least one ATR-scaled threshold within the next
`ML_LOOKAHEAD_BARS` bars, before moving down by the same amount? That is a
triple-barrier label, which matches how the strategy actually exits, instead of
the naive "is the close higher in N bars" label that leaks noise.

Training is walk-forward: the validation split is always later in time than the
training split, so the reported metrics are not inflated by look-ahead.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from app.config import settings
from app.features.build import ML_FEATURES, build_features, feature_matrix

log = logging.getLogger(__name__)

MODEL_DIR = Path(settings.state_dir) / "models"


@dataclass
class TrainResult:
    path: str
    rows: int
    features: list[str]
    metrics: dict


def _model_path(symbol: str, timeframe: str) -> Path:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    return MODEL_DIR / f"{symbol}_{timeframe}_{stamp}.joblib"


def make_labels(
    df: pd.DataFrame, lookahead: int, atr_mult_up: float, atr_mult_down: float
) -> pd.Series:
    """Triple-barrier label: 1 if the up barrier is hit first, else 0.

    Rows where neither barrier is touched inside the horizon are labelled NaN
    and dropped, so the model learns from decisive moves only.
    """
    close = df["close"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    atr = df["atr"].to_numpy()

    n = len(df)
    labels = np.full(n, np.nan)

    for i in range(n - lookahead):
        if not np.isfinite(atr[i]) or atr[i] <= 0:
            continue
        up = close[i] + atr[i] * atr_mult_up
        down = close[i] - atr[i] * atr_mult_down
        window_high = high[i + 1 : i + 1 + lookahead]
        window_low = low[i + 1 : i + 1 + lookahead]

        hit_up = np.argmax(window_high >= up) if (window_high >= up).any() else -1
        hit_down = np.argmax(window_low <= down) if (window_low <= down).any() else -1

        if hit_up == -1 and hit_down == -1:
            continue
        if hit_down == -1:
            labels[i] = 1.0
        elif hit_up == -1:
            labels[i] = 0.0
        else:
            labels[i] = 1.0 if hit_up < hit_down else 0.0

    return pd.Series(labels, index=df.index)


def build_training_frame(candles: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    features = build_features(candles)
    labels = make_labels(
        features,
        lookahead=settings.ml_lookahead_bars,
        atr_mult_up=settings.atr_target_mult,
        atr_mult_down=settings.atr_stop_mult,
    )
    matrix = feature_matrix(features)
    mask = labels.notna() & matrix.notna().all(axis=1)
    return matrix[mask], labels[mask]


def train(symbol: str, timeframe: str, candles: pd.DataFrame) -> TrainResult | None:
    """Fit a model on one symbol's history. Returns None if there is too little data."""
    import lightgbm as lgb
    from sklearn.metrics import accuracy_score, log_loss, roc_auc_score

    X, y = build_training_frame(candles)
    if len(X) < 400:
        log.warning("%s %s: only %s labelled rows, skipping training", symbol, timeframe, len(X))
        return None
    if y.nunique() < 2:
        log.warning("%s %s: labels are single-class, skipping", symbol, timeframe)
        return None

    split = int(len(X) * 0.8)
    # Purge the label horizon between the two halves: the last training rows
    # would otherwise be labelled by bars that belong to the validation set,
    # which leaks the answer and inflates the reported AUC.
    purge = settings.ml_lookahead_bars
    X_train, X_valid = X.iloc[: split - purge], X.iloc[split:]
    y_train, y_valid = y.iloc[: split - purge], y.iloc[split:]
    if len(X_valid) < 60 or y_valid.nunique() < 2:
        log.warning("%s %s: validation split unusable, skipping", symbol, timeframe)
        return None

    model = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=400,
        learning_rate=0.03,
        num_leaves=24,
        max_depth=6,
        min_child_samples=40,
        subsample=0.85,
        subsample_freq=1,
        colsample_bytree=0.8,
        reg_alpha=0.1,
        reg_lambda=0.5,
        random_state=42,
        n_jobs=2,
        verbose=-1,
    )
    model.fit(
        X_train,
        y_train,
        eval_set=[(X_valid, y_valid)],
        eval_metric="auc",
        callbacks=[lgb.early_stopping(40, verbose=False), lgb.log_evaluation(0)],
    )

    proba = model.predict_proba(X_valid)[:, 1]
    metrics = {
        "auc": float(roc_auc_score(y_valid, proba)),
        "accuracy": float(accuracy_score(y_valid, (proba > 0.5).astype(int))),
        "log_loss": float(log_loss(y_valid, proba, labels=[0, 1])),
        "train_rows": int(len(X_train)),
        "valid_rows": int(len(X_valid)),
        "positive_rate": float(y.mean()),
        "trained_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    # A model no better than a coin flip is worse than none - it adds latency
    # and false confidence.
    if metrics["auc"] < 0.52:
        log.warning(
            "%s %s: AUC %.3f is not better than chance, model discarded",
            symbol, timeframe, metrics["auc"],
        )
        return None

    path = _model_path(symbol, timeframe)
    joblib.dump({"model": model, "features": list(X.columns), "metrics": metrics}, path)
    log.info("%s %s: trained model AUC=%.3f rows=%s -> %s",
             symbol, timeframe, metrics["auc"], len(X), path.name)

    _prune_old_models(symbol, timeframe, keep=3)
    return TrainResult(str(path), len(X), list(X.columns), metrics)


def _prune_old_models(symbol: str, timeframe: str, keep: int = 3) -> None:
    files = sorted(
        MODEL_DIR.glob(f"{symbol}_{timeframe}_*.joblib"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


class Predictor:
    """Loads a model once and serves probabilities for the latest candle."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[object, list[str], float]] = {}

    def load(self, key: str, path: str) -> bool:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            log.warning("model file missing: %s", path)
            return False
        cached = self._cache.get(key)
        if cached and cached[2] == mtime:
            return True
        try:
            bundle = joblib.load(path)
        except Exception as exc:  # noqa: BLE001 - corrupt or version-mismatched file
            log.error("could not load model %s: %s", path, exc)
            return False
        self._cache[key] = (bundle["model"], bundle["features"], mtime)
        return True

    def predict(self, key: str, features_row: pd.DataFrame) -> float | None:
        """P(up barrier first) for a single-row feature frame."""
        entry = self._cache.get(key)
        if entry is None:
            return None
        model, columns, _ = entry
        try:
            X = features_row.reindex(columns=columns)
            if X.isna().any(axis=1).iloc[0]:
                return None
            return float(model.predict_proba(X)[:, 1][0])
        except Exception as exc:  # noqa: BLE001 - never let inference break the loop
            log.warning("prediction failed for %s: %s", key, exc)
            return None

    def has(self, key: str) -> bool:
        return key in self._cache
