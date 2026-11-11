#!/usr/bin/env python3

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import cv2
import numpy as np
from utils.prompts import load_prompt
from config import (
    RENDER_OBJECTIVE_MIN_PIXELS, RENDER_OBJECTIVE_MIN_DURATION, RENDER_OBJECTIVE_MAX_DURATION,
    RENDER_OBJECTIVE_MAX_ANALYSIS_FRAMES, RENDER_OBJECTIVE_ANALYSIS_MAX_SIDE,
    RENDER_OBJECTIVE_SPATIAL_MAX_SIDE, RENDER_OBJECTIVE_PASS_SCORE, RENDER_OBJECTIVE_RETRY_SCORE,
    RENDER_OBJECTIVE_MINIMUM_PASS_METRIC, RENDER_OBJECTIVE_MOTION_PASS_SCORE,
    RENDER_OBJECTIVE_MOTION_REJECT_SCORE, RENDER_OBJECTIVE_ABNORMAL_REJECT_RATIO,
    RENDER_OBJECTIVE_WEIGHTS as OBJECTIVE_INTERNAL_WEIGHTS,
)


OBJECTIVE_METRICS = (
    "sharpness",
    "exposure",
    "color_stability",
    "temporal_flicker",
    "motion_smoothness",
    "motion_fidelity",
    "scene_stability",
)

METRIC_LABELS = {
    "sharpness": "Sharpness",
    "exposure": "Exposure",
    "color_stability": "Color stability",
    "temporal_flicker": "Temporal flicker",
    "motion_smoothness": "Motion smoothness",
    "motion_fidelity": "Motion fidelity",
    "scene_stability": "Scene stability",
}

METRIC_FEEDBACK = {key: value for key, value in json.loads(
    load_prompt("render/metric_feedback.json")).items() if key in OBJECTIVE_METRICS}


@dataclass(slots=True)
class EvaluationConfig:

    min_pixels: int = RENDER_OBJECTIVE_MIN_PIXELS
    min_duration: float = RENDER_OBJECTIVE_MIN_DURATION
    max_duration: float = RENDER_OBJECTIVE_MAX_DURATION
    max_analysis_frames: int = RENDER_OBJECTIVE_MAX_ANALYSIS_FRAMES
    analysis_max_side: int = RENDER_OBJECTIVE_ANALYSIS_MAX_SIDE
    spatial_max_side: int = RENDER_OBJECTIVE_SPATIAL_MAX_SIDE
    pass_score: float = RENDER_OBJECTIVE_PASS_SCORE
    retry_score: float = RENDER_OBJECTIVE_RETRY_SCORE
    minimum_pass_metric: float = RENDER_OBJECTIVE_MINIMUM_PASS_METRIC
    motion_pass_score: float = RENDER_OBJECTIVE_MOTION_PASS_SCORE
    motion_reject_score: float = RENDER_OBJECTIVE_MOTION_REJECT_SCORE
    abnormal_reject_ratio: float = RENDER_OBJECTIVE_ABNORMAL_REJECT_RATIO

    def validate(self) -> None:
        if self.min_pixels < 1:
            raise ValueError("min_pixels must be positive")
        if self.min_duration < 0 or self.max_duration <= self.min_duration:
            raise ValueError("duration range is invalid")
        if self.max_analysis_frames < 2:
            raise ValueError("max_analysis_frames must be at least 2")
        if self.analysis_max_side < 32 or self.spatial_max_side < self.analysis_max_side:
            raise ValueError("analysis image-size limits are invalid")
        for name in (
            "pass_score",
            "retry_score",
            "minimum_pass_metric",
            "motion_pass_score",
            "motion_reject_score",
        ):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 100.0:
                raise ValueError(f"{name} must be in [0, 100]")
        if self.retry_score >= self.pass_score:
            raise ValueError("retry_score must be lower than pass_score")
        if self.motion_reject_score >= self.motion_pass_score:
            raise ValueError("motion_reject_score must be lower than motion_pass_score")
        if not 0.0 <= self.abnormal_reject_ratio <= 1.0:
            raise ValueError("abnormal_reject_ratio must be in [0, 1]")


@dataclass(slots=True)
class VideoMetadata:
    path: str
    file_size_bytes: int
    width: int
    height: int
    fps: float
    reported_frames: int
    decoded_frames: int
    duration: float
    codec: str
    decode_ok: bool
    complete_decode: bool


@dataclass(slots=True)
class SampledVideo:
    metadata: VideoMetadata
    color_frames: list[np.ndarray]
    gray_frames: list[np.ndarray]
    sample_indices: list[int]
    sample_times: list[float]


def _clip_score(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return float(np.clip(value, 0.0, 100.0))


def _fourcc_text(value: float) -> str:
    integer = int(value)
    chars = [chr((integer >> (8 * i)) & 0xFF) for i in range(4)]
    return "".join(char for char in chars if char.isprintable()).strip() or "unknown"


def _resize_for_analysis(frame: np.ndarray, max_side: int) -> np.ndarray:
    height, width = frame.shape[:2]
    scale = min(1.0, max_side / max(height, width))
    if scale >= 1.0:
        return frame.copy()
    return cv2.resize(
        frame,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


def _count_decodable_frames(path: Path) -> int:

    capture = cv2.VideoCapture(str(path))
    count = 0
    while capture.isOpened():
        ok, _ = capture.read()
        if not ok:
            break
        count += 1
    capture.release()
    return count


def sample_video(path: Path, config: EvaluationConfig) -> SampledVideo:

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Video file not found: {path}")
    file_size = path.stat().st_size
    if file_size <= 0:
        raise ValueError(f"Video file is empty: {path}")

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ValueError(f"Cannot open video: {path}")

    width = int(round(capture.get(cv2.CAP_PROP_FRAME_WIDTH)))
    height = int(round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    reported_frames = int(round(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
    codec = _fourcc_text(capture.get(cv2.CAP_PROP_FOURCC))
    capture.release()

    sampling_frame_count = reported_frames
    if sampling_frame_count <= 0:
        sampling_frame_count = _count_decodable_frames(path)

    if sampling_frame_count > 0:
        target_count = min(config.max_analysis_frames, sampling_frame_count)
        target_list = np.linspace(0, sampling_frame_count - 1, target_count, dtype=int).tolist()
        targets = set(target_list)
    else:
        targets = set()

    capture = cv2.VideoCapture(str(path))
    color_frames: list[np.ndarray] = []
    gray_frames: list[np.ndarray] = []
    sample_indices: list[int] = []
    decoded_frames = 0
    index = 0
    while capture.isOpened():
        ok, frame = capture.read()
        if not ok:
            break
        decoded_frames += 1
        if index in targets:
            color = _resize_for_analysis(frame, config.spatial_max_side)
            gray_source = _resize_for_analysis(frame, config.analysis_max_side)
            color_frames.append(color)
            gray_frames.append(cv2.cvtColor(gray_source, cv2.COLOR_BGR2GRAY))
            sample_indices.append(index)
        index += 1
    capture.release()

    valid_fps = fps if math.isfinite(fps) and fps > 1e-6 else 0.0
    duration = decoded_frames / valid_fps if valid_fps else 0.0
    complete_decode = (
        reported_frames <= 0
        or decoded_frames >= max(1, math.floor(reported_frames * 0.98))
    )
    metadata = VideoMetadata(
        path=str(path.resolve()),
        file_size_bytes=file_size,
        width=width,
        height=height,
        fps=valid_fps,
        reported_frames=reported_frames,
        decoded_frames=decoded_frames,
        duration=duration,
        codec=codec,
        decode_ok=decoded_frames > 0,
        complete_decode=complete_decode,
    )
    sample_times = [frame_index / valid_fps if valid_fps else 0.0 for frame_index in sample_indices]
    return SampledVideo(metadata, color_frames, gray_frames, sample_indices, sample_times)


def hard_checks(
    metadata: VideoMetadata,
    config: EvaluationConfig,
    *,
    enforce_output_constraints: bool,
) -> dict[str, Any]:
    pixels = metadata.width * metadata.height
    checks: dict[str, bool] = {
        "decode_ok": metadata.decode_ok,
        "complete_decode": metadata.complete_decode,
        "fps_ok": metadata.fps > 0.0,
        "dimensions_ok": metadata.width > 0 and metadata.height > 0,
        "enough_frames_ok": metadata.decoded_frames >= 2,
        "duration_positive_ok": metadata.duration > 0.0,
    }
    if enforce_output_constraints:
        checks["pixel_requirement_ok"] = pixels >= config.min_pixels
        checks["duration_requirement_ok"] = (
            config.min_duration <= metadata.duration <= config.max_duration
        )
    failures = [name for name, passed in checks.items() if not passed]
    return {
        **checks,
        "pixel_count": pixels,
        "required_min_pixels": config.min_pixels if enforce_output_constraints else None,
        "required_duration_range": (
            [config.min_duration, config.max_duration] if enforce_output_constraints else None
        ),
        "passed": not failures,
        "failures": failures,
    }


def combined_hard_checks(
    white: VideoMetadata,
    generated: VideoMetadata,
    config: EvaluationConfig,
) -> dict[str, Any]:
    reference = hard_checks(white, config, enforce_output_constraints=False)
    candidate = hard_checks(generated, config, enforce_output_constraints=True)
    failures = [f"reference:{name}" for name in reference["failures"]]
    failures.extend(f"generated:{name}" for name in candidate["failures"])
    return {
        "reference": reference,
        "generated": candidate,
        "passed": reference["passed"] and candidate["passed"],
        "failures": failures,
    }


def _entropy(gray: np.ndarray) -> float:
    histogram = cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel()
    probabilities = histogram / max(float(histogram.sum()), 1.0)
    probabilities = probabilities[probabilities > 0]
    return float(-(probabilities * np.log2(probabilities)).sum())


def spatial_metrics(video: SampledVideo) -> tuple[dict[str, float], dict[str, Any]]:
    sharpness_values: list[float] = []
    underexposed: list[float] = []
    overexposed: list[float] = []
    entropies: list[float] = []
    histograms: list[np.ndarray] = []

    for frame in video.color_frames:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sharpness_values.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))
        underexposed.append(float(np.mean(gray < 10)))
        overexposed.append(float(np.mean(gray > 245)))
        entropies.append(_entropy(gray))
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        histogram = cv2.calcHist([hsv], [0, 1], None, [32, 16], [0, 180, 0, 256])
        histograms.append(cv2.normalize(histogram, histogram).flatten())

    similarities = [
        max(
            0.0,
            float(cv2.compareHist(histograms[index - 1], histograms[index], cv2.HISTCMP_CORREL)),
        )
        for index in range(1, len(histograms))
    ]
    sharpness_raw = float(np.median(sharpness_values)) if sharpness_values else 0.0
    exposure_bad_ratio = (
        float(np.mean(np.asarray(underexposed) + np.asarray(overexposed)))
        if underexposed
        else 1.0
    )
    color_mean = float(np.mean(similarities)) if similarities else (1.0 if histograms else 0.0)
    color_p10 = float(np.percentile(similarities, 10)) if similarities else color_mean
    color_stability = 0.60 * color_mean + 0.40 * color_p10

    scores = {
        "sharpness": _clip_score(100.0 * (1.0 - math.exp(-sharpness_raw / 120.0))),
        "exposure": _clip_score(100.0 * (1.0 - min(1.0, exposure_bad_ratio * 3.0))),
        "color_stability": _clip_score(100.0 * color_stability),
    }
    raw: dict[str, Any] = {
        "sample_count": len(video.color_frames),
        "median_laplacian_variance": sharpness_raw,
        "mean_underexposed_ratio": float(np.mean(underexposed)) if underexposed else 1.0,
        "mean_overexposed_ratio": float(np.mean(overexposed)) if overexposed else 1.0,
        "median_entropy": float(np.median(entropies)) if entropies else 0.0,
        "mean_adjacent_color_histogram_correlation": color_mean,
        "p10_adjacent_color_histogram_correlation": color_p10,
    }
    return scores, raw


def _border_mask(shape: tuple[int, int], fraction: float = 0.18) -> np.ndarray:
    height, width = shape
    border_y = max(1, round(height * fraction))
    border_x = max(1, round(width * fraction))
    mask = np.zeros((height, width), dtype=np.uint8)
    mask[:border_y, :] = 255
    mask[-border_y:, :] = 255
    mask[:, :border_x] = 255
    mask[:, -border_x:] = 255
    return mask


def _flow_summary(previous: np.ndarray, current: np.ndarray) -> tuple[float, list[float]]:
    flow = cv2.calcOpticalFlowFarneback(
        previous,
        current,
        None,
        pyr_scale=0.5,
        levels=3,
        winsize=15,
        iterations=3,
        poly_n=5,
        poly_sigma=1.2,
        flags=0,
    )
    magnitude, _ = cv2.cartToPolar(flow[..., 0], flow[..., 1])
    cutoff = max(float(np.percentile(magnitude, 75)), 1e-4)
    moving = magnitude >= cutoff
    diagonal = max(math.hypot(*previous.shape), 1.0)
    if np.any(moving):
        motion = float(np.mean(magnitude[moving]) / diagonal)
        vector = [
            float(np.mean(flow[..., 0][moving]) / diagonal),
            float(np.mean(flow[..., 1][moving]) / diagonal),
        ]
    else:
        motion = 0.0
        vector = [0.0, 0.0]
    return motion, vector


def temporal_metrics(video: SampledVideo) -> tuple[dict[str, float], dict[str, Any]]:
    if len(video.gray_frames) < 2:
        return (
            {"temporal_flicker": 0.0, "motion_smoothness": 0.0, "scene_stability": 0.0},
            {
                "reason": "fewer than two sampled frames",
                "luma_curve": [],
                "motion_curve": [],
                "direction_curve": [],
                "freeze_ratio": 1.0,
                "black_frame_ratio": 1.0,
                "abnormal_frame_ratio": 1.0,
            },
        )

    luma = np.asarray([float(frame.mean()) for frame in video.gray_frames], dtype=np.float64)
    luma_deltas = np.abs(np.diff(luma))
    flicker_p95 = float(np.percentile(luma_deltas, 95))

    motion_curve: list[float] = []
    direction_curve: list[list[float]] = []
    mean_abs_diffs: list[float] = []
    scene_similarities: list[float] = []
    previous = video.gray_frames[0]
    border = _border_mask(previous.shape)
    previous_edges = cv2.Canny(previous, 60, 140)
    previous_hist = cv2.calcHist([previous], [0], border, [32], [0, 256])
    previous_hist = cv2.normalize(previous_hist, previous_hist).flatten()

    for current in video.gray_frames[1:]:
        motion, direction = _flow_summary(previous, current)
        motion_curve.append(motion)
        direction_curve.append(direction)
        mean_abs_diffs.append(float(np.mean(cv2.absdiff(previous, current))))

        current_edges = cv2.Canny(current, 60, 140)
        edge_a = previous_edges[border > 0].astype(np.float32)
        edge_b = current_edges[border > 0].astype(np.float32)
        edge_similarity = float(
            np.dot(edge_a, edge_b) / (np.linalg.norm(edge_a) * np.linalg.norm(edge_b) + 1e-6)
        )
        current_hist = cv2.calcHist([current], [0], border, [32], [0, 256])
        current_hist = cv2.normalize(current_hist, current_hist).flatten()
        histogram_similarity = max(
            0.0,
            float(cv2.compareHist(previous_hist, current_hist, cv2.HISTCMP_CORREL)),
        )
        scene_similarities.append(0.45 * edge_similarity + 0.55 * histogram_similarity)
        previous = current
        previous_edges = current_edges
        previous_hist = current_hist

    motion_array = np.asarray(motion_curve, dtype=np.float64)
    if motion_array.size < 2 or float(np.percentile(motion_array, 90)) < 1e-6:
        normalized_motion_jump = 0.0
        smoothness_score = 100.0
    else:
        jump = float(np.percentile(np.abs(np.diff(motion_array)), 90))
        scale = float(np.percentile(motion_array, 90)) + 1e-8
        normalized_motion_jump = jump / scale
        smoothness_score = _clip_score(100.0 * math.exp(-1.6 * normalized_motion_jump))

    freeze_ratio = float(np.mean(np.asarray(mean_abs_diffs) < 0.45)) if mean_abs_diffs else 1.0
    black_frame_ratio = float(np.mean([float(frame.mean()) < 5.0 for frame in video.gray_frames]))
    abnormal_frame_ratio = float(np.mean([_entropy(frame) < 0.5 for frame in video.gray_frames]))
    abnormal_penalty = max(0.0, 1.0 - 2.0 * max(black_frame_ratio, abnormal_frame_ratio))
    flicker_score = _clip_score(100.0 * math.exp(-flicker_p95 / 18.0) * abnormal_penalty)
    scene_mean = float(np.mean(scene_similarities)) if scene_similarities else 0.0
    scene_p10 = float(np.percentile(scene_similarities, 10)) if scene_similarities else 0.0
    scene_score = _clip_score(100.0 * (0.65 * scene_mean + 0.35 * scene_p10) * abnormal_penalty)

    scores = {
        "temporal_flicker": flicker_score,
        "motion_smoothness": smoothness_score,
        "scene_stability": scene_score,
    }
    raw: dict[str, Any] = {
        "sample_count": len(video.gray_frames),
        "sample_times": video.sample_times,
        "luma_curve": luma.tolist(),
        "luma_delta_p95": flicker_p95,
        "normalized_motion_jump_p90": normalized_motion_jump,
        "freeze_ratio": freeze_ratio,
        "black_frame_ratio": black_frame_ratio,
        "abnormal_frame_ratio": abnormal_frame_ratio,
        "mean_scene_similarity": scene_mean,
        "p10_scene_similarity": scene_p10,
        "motion_curve": motion_curve,
        "direction_curve": direction_curve,
    }
    return scores, raw


def _resample(values: Iterable[float], length: int = 64) -> np.ndarray:
    array = np.asarray(list(values), dtype=np.float64)
    if array.size == 0:
        return np.zeros(length, dtype=np.float64)
    if array.size == 1:
        return np.full(length, array[0], dtype=np.float64)
    old_axis = np.linspace(0.0, 1.0, array.size)
    new_axis = np.linspace(0.0, 1.0, length)
    return np.interp(new_axis, old_axis, array)


def _resample_vectors(values: Iterable[Iterable[float]], length: int = 64) -> np.ndarray:
    array = np.asarray(list(values), dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != 2 or array.shape[0] == 0:
        return np.zeros((length, 2), dtype=np.float64)
    return np.column_stack((_resample(array[:, 0], length), _resample(array[:, 1], length)))


def _motion_interval(curve: np.ndarray) -> tuple[float, float, np.ndarray]:
    if curve.size == 0 or float(np.max(curve)) < 1e-8:
        return 0.0, 0.0, np.zeros_like(curve, dtype=bool)
    threshold = max(float(np.percentile(curve, 70)) * 0.20, float(np.max(curve)) * 0.05)
    active = curve > threshold
    indices = np.flatnonzero(active)
    if indices.size == 0:
        return 0.0, 0.0, active
    denominator = max(curve.size - 1, 1)
    return float(indices[0] / denominator), float(indices[-1] / denominator), active


def _direction_similarity(
    white_vectors: np.ndarray,
    generated_vectors: np.ndarray,
    white_motion: np.ndarray,
    generated_motion: np.ndarray,
) -> float:
    white_norm = np.linalg.norm(white_vectors, axis=1)
    generated_norm = np.linalg.norm(generated_vectors, axis=1)
    meaningful = np.logical_or(white_motion > 1e-6, generated_motion > 1e-6)
    if not np.any(meaningful):
        return 1.0
    both = meaningful & (white_norm > 1e-9) & (generated_norm > 1e-9)
    similarities = np.zeros(white_motion.shape, dtype=np.float64)
    if np.any(both):
        cosine = np.sum(white_vectors[both] * generated_vectors[both], axis=1) / (
            white_norm[both] * generated_norm[both] + 1e-12
        )
        similarities[both] = np.clip(cosine, 0.0, 1.0)
    weights = np.maximum(white_motion, generated_motion)
    return float(
        np.sum(similarities[meaningful] * weights[meaningful])
        / (np.sum(weights[meaningful]) + 1e-12)
    )


def motion_fidelity(
    white_temporal_raw: dict[str, Any],
    generated_temporal_raw: dict[str, Any],
    *,
    white_duration: float,
    generated_duration: float,
) -> tuple[float, dict[str, float]]:

    white_source = white_temporal_raw.get("motion_curve", [])
    generated_source = generated_temporal_raw.get("motion_curve", [])
    if not white_source or not generated_source:
        return 0.0, {
            "curve_correlation": 0.0,
            "magnitude_similarity": 0.0,
            "direction_similarity": 0.0,
            "active_interval_overlap": 0.0,
            "timing_similarity": 0.0,
            "duration_similarity": 0.0,
            "white_motion_start": 0.0,
            "white_motion_end": 0.0,
            "generated_motion_start": 0.0,
            "generated_motion_end": 0.0,
        }

    white = _resample(white_source)
    generated = _resample(generated_source)
    white_vectors = _resample_vectors(white_temporal_raw.get("direction_curve", []))
    generated_vectors = _resample_vectors(generated_temporal_raw.get("direction_curve", []))
    white_level = float(np.mean(white))
    generated_level = float(np.mean(generated))

    white_static = float(np.max(white)) < 1e-8
    generated_static = float(np.max(generated)) < 1e-8
    if white_static and generated_static:
        correlation = 1.0
        magnitude_similarity = 1.0
    elif white_static or generated_static:
        correlation = 0.0
        magnitude_similarity = 0.0
    elif np.std(white) < 1e-8 or np.std(generated) < 1e-8:
        correlation = 0.0
        magnitude_similarity = min(white_level, generated_level) / (
            max(white_level, generated_level) + 1e-12
        )
    else:
        correlation = max(0.0, float(np.corrcoef(white, generated)[0, 1]))
        magnitude_similarity = min(white_level, generated_level) / (
            max(white_level, generated_level) + 1e-12
        )

    white_start, white_end, white_active = _motion_interval(white)
    generated_start, generated_end, generated_active = _motion_interval(generated)
    union = int(np.logical_or(white_active, generated_active).sum())
    overlap = int(np.logical_and(white_active, generated_active).sum())
    active_overlap = float(overlap / union) if union else 1.0
    timing_error = (abs(white_start - generated_start) + abs(white_end - generated_end)) / 2.0
    timing_similarity = max(0.0, 1.0 - timing_error)
    direction_similarity = _direction_similarity(
        white_vectors,
        generated_vectors,
        white,
        generated,
    )
    if white_duration > 0.0 and generated_duration > 0.0:
        duration_similarity = min(white_duration, generated_duration) / max(
            white_duration,
            generated_duration,
        )
    else:
        duration_similarity = 0.0

    score = _clip_score(
        100.0
        * (
            0.25 * correlation
            + 0.15 * magnitude_similarity
            + 0.35 * direction_similarity
            + 0.10 * active_overlap
            + 0.10 * timing_similarity
            + 0.05 * duration_similarity
        )
    )
    details = {
        "curve_correlation": correlation,
        "magnitude_similarity": magnitude_similarity,
        "direction_similarity": direction_similarity,
        "active_interval_overlap": active_overlap,
        "timing_similarity": timing_similarity,
        "duration_similarity": duration_similarity,
        "white_motion_start": white_start,
        "white_motion_end": white_end,
        "generated_motion_start": generated_start,
        "generated_motion_end": generated_end,
    }
    return score, details


def aggregate_scores(scores: dict[str, float]) -> dict[str, float]:
    spatial_metrics = ("sharpness", "exposure", "color_stability")
    temporal_metrics = ("temporal_flicker", "motion_smoothness", "scene_stability")
    spatial_weight = sum(OBJECTIVE_INTERNAL_WEIGHTS[key] for key in spatial_metrics)
    temporal_weight = sum(OBJECTIVE_INTERNAL_WEIGHTS[key] for key in temporal_metrics)
    spatial = sum(
        OBJECTIVE_INTERNAL_WEIGHTS[key] * scores[key] for key in spatial_metrics
    ) / spatial_weight
    temporal = sum(
        OBJECTIVE_INTERNAL_WEIGHTS[key] * scores[key] for key in temporal_metrics
    ) / temporal_weight
    objective = sum(OBJECTIVE_INTERNAL_WEIGHTS[key] * scores[key] for key in OBJECTIVE_METRICS)
    return {
        "spatial_quality": _clip_score(spatial),
        "temporal_stability": _clip_score(temporal),
        "motion_fidelity": _clip_score(scores["motion_fidelity"]),
        "objective_score": _clip_score(objective),
        "overall_score": _clip_score(objective),
    }


def _metric_evidence(metric: str, score: float, raw: dict[str, Any]) -> str:
    generated_temporal = raw["generated_temporal"]
    spatial = raw["spatial"]
    fidelity = raw["motion_fidelity"]
    details = {
        "sharpness": f"Median Laplacian variance={spatial['median_laplacian_variance']:.3f}",
        "exposure": (
            f"Underexposed ratio={spatial['mean_underexposed_ratio']:.3%}, "
            f"Overexposed ratio={spatial['mean_overexposed_ratio']:.3%}"
        ),
        "color_stability": (
            "Adjacent-frame HSV histogram correlation: "
            f"mean={spatial['mean_adjacent_color_histogram_correlation']:.3f}, "
            f"10th percentile={spatial['p10_adjacent_color_histogram_correlation']:.3f}"
        ),
        "temporal_flicker": f"95th percentile luminance change={generated_temporal['luma_delta_p95']:.3f}",
        "motion_smoothness": (
            f"90th percentile normalized motion jump={generated_temporal['normalized_motion_jump_p90']:.3f}"
        ),
        "motion_fidelity": (
            f"Motion curve correlation={fidelity['curve_correlation']:.3f}, "
            f"Direction similarity={fidelity['direction_similarity']:.3f}, "
            f"Active motion interval overlap={fidelity['active_interval_overlap']:.3f}"
        ),
        "scene_stability": (
            f"Mean scene similarity={generated_temporal['mean_scene_similarity']:.3f}, "
            f"10th percentile={generated_temporal['p10_scene_similarity']:.3f}"
        ),
    }
    return f"{METRIC_LABELS[metric]}={score:.2f}; {details[metric]}"


def build_findings(
    hard: dict[str, Any],
    scores: dict[str, float],
    raw: dict[str, Any],
    config: EvaluationConfig,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for failure in hard["failures"]:
        findings.append(
            {
                "category": "hard_check",
                "severity": "critical",
                "evidence": failure,
                "suggested_prompt": "",
            }
        )

    for metric in OBJECTIVE_METRICS:
        score = scores[metric]
        if score >= config.pass_score:
            continue
        if metric == "motion_fidelity" and score < config.motion_reject_score:
            severity = "critical"
        elif score < config.retry_score:
            severity = "major"
        else:
            severity = "minor"
        findings.append(
            {
                "category": metric,
                "severity": severity,
                "evidence": _metric_evidence(metric, score, raw),
                "suggested_prompt": METRIC_FEEDBACK[metric],
            }
        )

    generated_temporal = raw["generated_temporal"]
    for key, label in (
        ("black_frame_ratio", "Black-frame ratio"),
        ("abnormal_frame_ratio", "Low-information and abnormal-frame ratio"),
    ):
        ratio = float(generated_temporal.get(key, 0.0))
        if ratio > config.abnormal_reject_ratio:
            findings.append(
                {
                    "category": key,
                    "severity": "critical",
                    "evidence": (
                        f"{label}={ratio:.3%} exceeds the configured threshold "
                        f"{config.abnormal_reject_ratio:.3%}"
                    ),
                    "suggested_prompt": json.loads(load_prompt("render/metric_feedback.json"))["abnormal_frames"],
                }
            )
    return findings


def decide(
    hard: dict[str, Any],
    scores: dict[str, float],
    aggregates: dict[str, float],
    raw: dict[str, Any],
    config: EvaluationConfig,
) -> tuple[str, list[str]]:
    if not hard["passed"]:
        return "REJECT", [f"hard_check:{failure}" for failure in hard["failures"]]

    generated_temporal = raw["generated_temporal"]
    abnormal_reasons = [
        key
        for key in ("black_frame_ratio", "abnormal_frame_ratio")
        if float(generated_temporal.get(key, 0.0)) > config.abnormal_reject_ratio
    ]
    if abnormal_reasons:
        return "REJECT", [f"{key}>{config.abnormal_reject_ratio:g}" for key in abnormal_reasons]

    motion_score = scores["motion_fidelity"]
    overall = aggregates["overall_score"]
    if motion_score < config.motion_reject_score:
        return "REJECT", [f"motion_fidelity<{config.motion_reject_score:g}"]
    if overall < config.retry_score:
        return "REJECT", [f"overall_score<{config.retry_score:g}"]

    retry_reasons: list[str] = []
    if overall < config.pass_score:
        retry_reasons.append(f"overall_score<{config.pass_score:g}")
    if motion_score < config.motion_pass_score:
        retry_reasons.append(f"motion_fidelity<{config.motion_pass_score:g}")
    low_metrics = [name for name, score in scores.items() if score < config.minimum_pass_metric]
    if low_metrics:
        retry_reasons.append(
            f"metric_below_{config.minimum_pass_metric:g}:" + ",".join(sorted(low_metrics))
        )
    if retry_reasons:
        return "RETRY", retry_reasons
    return "PASS", ["All objective acceptance conditions passed"]


def build_feedback_prompt(
    task_prompt: str,
    scores: dict[str, float],
    config: EvaluationConfig,
    max_metric_feedback: int = 3,
) -> str:
    lines = [task_prompt.strip(), json.loads(load_prompt("render/metric_feedback.json"))["preserve"]]
    priorities: list[tuple[str, float]] = []
    for key, score in scores.items():
        if key not in METRIC_FEEDBACK:
            continue
        target = config.motion_pass_score if key == "motion_fidelity" else config.pass_score
        if score >= target:
            continue
        normalized_deficit = (target - score) / max(target, 1.0)
        priorities.append((key, OBJECTIVE_INTERNAL_WEIGHTS[key] * normalized_deficit))
    priorities.sort(key=lambda item: item[1], reverse=True)
    selected = [key for key, _ in priorities[:max_metric_feedback]]
    lines.extend(METRIC_FEEDBACK[key] for key in selected)

    deduplicated: list[str] = []
    seen: set[str] = set()
    for line in lines:
        normalized = line.strip().lower()
        if normalized and normalized in seen:
            continue
        if normalized:
            seen.add(normalized)
        deduplicated.append(line)
    return "\n".join(deduplicated).strip() + "\n"


def _markdown_table(scores: dict[str, float]) -> str:
    rows = ["| Metric | Score |", "| --- | ---: |"]
    for key in OBJECTIVE_METRICS:
        rows.append(f"| {METRIC_LABELS[key]} | {scores[key]:.2f} |")
    return "\n".join(rows)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_report(
    output: Path,
    task_prompt: str,
    white: VideoMetadata,
    generated: VideoMetadata,
    hard: dict[str, Any],
    scores: dict[str, float],
    aggregates: dict[str, float],
    decision: str,
    decision_reasons: list[str],
    findings: list[dict[str, str]],
) -> None:
    finding_lines = [
        f"- **{item['severity']} / {item['category']}**: {item['evidence']}"
        for item in findings
    ] or ["- No objective metrics fall below the current thresholds."]
    content = f"""# Robot Video Objective Quality Report

## Conclusion

- Review decision: **{decision}**
- Overall score (seven objective metrics only): **{aggregates['overall_score']:.2f}**
- Spatial quality: **{aggregates['spatial_quality']:.2f}**
- Temporal stability: **{aggregates['temporal_stability']:.2f}**
- Motion fidelity: **{aggregates['motion_fidelity']:.2f}**
- Hard checks: **{'PASS' if hard['passed'] else 'FAIL'}**
- Decision rationale: {', '.join(decision_reasons)}

## Task Prompt

{task_prompt}

## Inputs

- White-model video: `{white.path}`
- Generated video: `{generated.path}`
- White-model metadata: {white.width}x{white.height}, {white.fps:.3f} fps, {white.duration:.3f} s, codec `{white.codec}`
- Generated video metadata: {generated.width}x{generated.height}, {generated.fps:.3f} fps, {generated.duration:.3f} s, codec `{generated.codec}`

## Seven Objective Metrics

{_markdown_table(scores)}

## Objective Findings

{chr(10).join(finding_lines)}

## Notes

This report does not include VLM assessments, semantic judgments of realism, or physical commonsense evaluation. The overall score is intended only for rapid screening and ranking candidate videos; thresholds require further calibration against human-reviewed samples.
"""
    output.write_text(content, encoding="utf-8")


def evaluate(
    white_model_video: Path,
    generated_video: Path,
    task_prompt: str,
    output_dir: Path,
    *,
    config: EvaluationConfig | None = None,
) -> dict[str, Any]:

    config = config or EvaluationConfig()
    config.validate()
    task_prompt = task_prompt.strip()
    if not task_prompt:
        raise ValueError("task_prompt cannot be empty")

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    white = sample_video(Path(white_model_video), config)
    generated = sample_video(Path(generated_video), config)
    hard = combined_hard_checks(white.metadata, generated.metadata, config)

    spatial_scores, spatial_raw = spatial_metrics(generated)
    _, white_temporal_raw = temporal_metrics(white)
    temporal_scores, generated_temporal_raw = temporal_metrics(generated)
    fidelity_score, fidelity_raw = motion_fidelity(
        white_temporal_raw,
        generated_temporal_raw,
        white_duration=white.metadata.duration,
        generated_duration=generated.metadata.duration,
    )
    scores = {**spatial_scores, **temporal_scores, "motion_fidelity": fidelity_score}
    scores = {key: _clip_score(scores[key]) for key in OBJECTIVE_METRICS}
    aggregates = aggregate_scores(scores)
    raw = {
        "spatial": spatial_raw,
        "white_temporal": white_temporal_raw,
        "generated_temporal": generated_temporal_raw,
        "motion_fidelity": fidelity_raw,
    }
    decision, decision_reasons = decide(hard, scores, aggregates, raw, config)
    findings = build_findings(hard, scores, raw, config)
    feedback = build_feedback_prompt(task_prompt, scores, config)

    artifact_paths = {
        "metrics": str((output_dir / "metrics.json").resolve()),
        "review": str((output_dir / "review.json").resolve()),
        "feedback_prompt": str((output_dir / "feedback_prompt.txt").resolve()),
        "report": str((output_dir / "report.md").resolve()),
    }
    metrics = {
        "schema_version": "2.0-objective-only",
        "evaluator_mode": "objective_only",
        "objective_metric_count": len(OBJECTIVE_METRICS),
        "inputs": {
            "white_model_video": str(Path(white_model_video).resolve()),
            "generated_video": str(Path(generated_video).resolve()),
            "task_prompt": task_prompt,
        },
        "config": asdict(config),
        "weights": {
            "objective_internal": OBJECTIVE_INTERNAL_WEIGHTS,
        },
        "metadata": {
            "white_model": asdict(white.metadata),
            "generated": asdict(generated.metadata),
        },
        "hard_check": hard,
        "scores": scores,
        "aggregates": aggregates,
        "raw": raw,
        "decision": decision,
        "decision_reasons": decision_reasons,
        "findings": findings,
        "artifacts": artifact_paths,
    }
    review = {
        "evaluator_mode": "objective_only",
        "decision": decision,
        "decision_reasons": decision_reasons,
        "overall_score": aggregates["overall_score"],
        "scores": scores,
        "findings": findings,
    }

    _write_json(output_dir / "metrics.json", metrics)
    _write_json(output_dir / "review.json", review)
    (output_dir / "feedback_prompt.txt").write_text(feedback, encoding="utf-8")
    write_report(
        output_dir / "report.md",
        task_prompt,
        white.metadata,
        generated.metadata,
        hard,
        scores,
        aggregates,
        decision,
        decision_reasons,
        findings,
    )
    return metrics


def run_objective_evaluation(
    white_model_video: Path,
    generated_video: Path,
    task_prompt: str,
    output_dir: Path,
    *,
    analysis_frames: int = 180,
) -> dict[str, Any]:

    if not white_model_video.is_file():
        raise FileNotFoundError(f"Local white-model video not found: {white_model_video}")
    if not task_prompt.strip():
        raise ValueError("A non-empty prompt is required when objective evaluation is enabled")
    config = EvaluationConfig(max_analysis_frames=max(2, analysis_frames))
    return evaluate(
        white_model_video,
        generated_video,
        task_prompt,
        output_dir,
        config=config,
    )
