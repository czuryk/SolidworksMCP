"""High-confidence semantic segmentation and boundary matting backend.

SAM 2.1 supplies the semantic object prior. ViTMatte refines that prior at the
original image resolution. Acceptance is based on independent image evidence,
prompt consensus, and repeatability under small codec/resampling changes.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


SAM_MODEL_ID = "facebook/sam2.1-hiera-large"
MATTE_MODEL_ID = "hustvl/vitmatte-base-composition-1k"
SUPPORTED_REGION_MODES = {
    "outer_silhouette",
    "silhouette_with_holes",
    "all_region_boundaries",
    "guided_components",
}
LINE_ART_MODES = {"stroke_centerlines", "stroke_edges", "all_visible_edges"}


@dataclass
class DeepVectorResult:
    mask: Any
    topology_field: Any
    topology_level: float
    confidence: float
    diagnostics: list
    quality: Dict[str, Any]


_MODEL_LOCK = threading.Lock()
_MODEL_BUNDLE = None


def _hf_repo_cached(repo_id: str) -> bool:
    """Check the local Hugging Face snapshot index without a recursive scan."""
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
        repo_root = Path(HF_HUB_CACHE) / (
            "models--" + repo_id.replace("/", "--"))
        snapshots = repo_root / "snapshots"
        if not snapshots.is_dir():
            return False

        refs = repo_root / "refs"
        if refs.is_dir():
            for ref in refs.iterdir():
                if not ref.is_file():
                    continue
                revision = ref.read_text(encoding="utf-8").strip()
                if revision and (snapshots / revision).is_dir():
                    return True
        return any(item.is_dir() for item in snapshots.iterdir())
    except (OSError, ImportError):
        return False


def capability_report() -> Dict[str, Any]:
    """Return readiness without importing Torch or scanning the full HF cache."""
    import importlib.util

    packages = {
        "torch": importlib.util.find_spec("torch") is not None,
        "transformers": importlib.util.find_spec("transformers") is not None,
    }
    try:
        from .lineart_vectorization import (
            capability_report as lineart_report,
            _lightweight_cuda_probe,
        )
        cuda_probe = _lightweight_cuda_probe()
        lineart = lineart_report()
    except Exception as exc:
        cuda_probe = {
            "cuda": False, "device": None, "torch_cuda_build": None,
            "probe": "unavailable",
        }
        lineart = {"available": False, "error": str(exc)}
    cuda = bool(cuda_probe["cuda"])
    device_name = cuda_probe["device"]
    models = {
        SAM_MODEL_ID: _hf_repo_cached(SAM_MODEL_ID),
        MATTE_MODEL_ID: _hf_repo_cached(MATTE_MODEL_ID),
    }
    return {
        "available": all(packages.values()) and cuda and all(models.values()),
        "packages": packages,
        "cuda": cuda,
        "device": device_name,
        "torch_cuda_build": cuda_probe.get("torch_cuda_build"),
        "readiness_probe": cuda_probe.get("probe"),
        "models_cached": models,
        "supported_modes": sorted(SUPPORTED_REGION_MODES),
        "line_art_modes": sorted(LINE_ART_MODES),
        "line_art_backend_ready": bool(lineart.get("available")),
        "line_art": lineart,
    }


def _load_models(sam_model_id: str, matte_model_id: str):
    global _MODEL_BUNDLE
    key = (sam_model_id, matte_model_id)
    with _MODEL_LOCK:
        if _MODEL_BUNDLE is not None and _MODEL_BUNDLE[0] == key:
            return _MODEL_BUNDLE[1]
        import torch
        from transformers import (
            Sam2Model,
            Sam2Processor,
            VitMatteForImageMatting,
            VitMatteImageProcessor,
        )

        if not torch.cuda.is_available():
            raise RuntimeError(
                "The deep vectorization backend requires a CUDA-capable GPU")
        try:
            sam_processor = Sam2Processor.from_pretrained(
                sam_model_id, local_files_only=True)
            sam_model = Sam2Model.from_pretrained(
                sam_model_id, local_files_only=True,
                dtype=torch.bfloat16).to("cuda").eval()
            matte_processor = VitMatteImageProcessor.from_pretrained(
                matte_model_id, local_files_only=True)
            matte_model = VitMatteForImageMatting.from_pretrained(
                matte_model_id, local_files_only=True,
                dtype=torch.bfloat16).to("cuda").eval()
        except Exception as exc:
            raise RuntimeError(
                "Deep vector models are not available in the local cache. "
                f"Required: {sam_model_id}, {matte_model_id}. Details: {exc}") from exc
        bundle = {
            "torch": torch,
            "sam_processor": sam_processor,
            "sam_model": sam_model,
            "matte_processor": matte_processor,
            "matte_model": matte_model,
        }
        _MODEL_BUNDLE = (key, bundle)
        return bundle


def _largest_component(mask, keep_all: bool = False):
    import cv2
    import numpy as np

    mask = mask.astype(bool)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8), connectivity=8)
    if count <= 1:
        raise RuntimeError("No foreground component found")
    if keep_all:
        return mask
    height, width = mask.shape
    candidates = []
    for label in range(1, count):
        x, y, w, h, area = stats[label]
        border = x == 0 or y == 0 or x + w == width or y + h == height
        candidates.append((not border, int(area), label))
    _, _, best = max(candidates)
    return labels == best


def _select_components(mask, trace, mode):
    """Apply the explicit component policy and return an auditable report."""
    import cv2
    import numpy as np

    policy = trace.get("component_policy", "largest_prompted")
    allowed = {"largest_prompted", "largest", "all_above_min_area",
               "prompted_only"}
    if policy not in allowed:
        raise ValueError(
            "trace.component_policy must be largest_prompted, largest, "
            "all_above_min_area, or prompted_only")
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8), connectivity=8)
    min_area = max(1, int(trace.get("min_area_px", 100)))
    components = []
    eligible = []
    for label in range(1, count):
        x, y, width, height, area = [int(value) for value in stats[label]]
        record = {
            "label": label, "area_px": area,
            "bbox_px": [x, y, x + width, y + height],
            "above_min_area": area >= min_area,
        }
        components.append(record)
        if area >= min_area:
            eligible.append(label)
    if not eligible:
        raise RuntimeError(
            f"No foreground component meets trace.min_area_px={min_area}")

    prompted = set()
    height, width = mask.shape
    for point in trace.get("positive_points_px") or []:
        px, py = [int(round(value)) for value in point]
        if 0 <= px < width and 0 <= py < height:
            label = int(labels[py, px])
            if label in eligible:
                prompted.add(label)
    if mode == "all_region_boundaries" or policy == "all_above_min_area":
        selected = set(eligible)
        reason = "all components above min_area_px"
    elif mode == "guided_components" or policy == "prompted_only":
        if not prompted:
            raise RuntimeError(
                "guided_components requires a positive point inside every "
                "requested foreground component")
        selected = prompted
        reason = "components containing positive prompt points"
    elif policy == "largest_prompted" and prompted:
        selected = {max(prompted, key=lambda item: int(stats[item, cv2.CC_STAT_AREA]))}
        reason = "largest component containing a positive prompt point"
    else:
        selected = {max(eligible, key=lambda item: int(stats[item, cv2.CC_STAT_AREA]))}
        reason = "largest component above min_area_px"
    selected_mask = np.isin(labels, list(selected))
    for record in components:
        record["selected"] = record["label"] in selected
    report = {
        "policy": policy, "reason": reason, "min_area_px": min_area,
        "found": len(components), "eligible": len(eligible),
        "selected": len(selected), "discarded": len(components) - len(selected),
        "components": components,
    }
    return selected_mask, report


def _roi_mask(shape, trace):
    import numpy as np

    height, width = shape
    mask = np.ones(shape, dtype=bool)
    roi = trace.get("roi_px")
    if roi is None:
        return mask
    if len(roi) != 4:
        raise ValueError("trace.roi_px must be [x0,y0,x1,y1]")
    x0, y0, x1, y1 = [int(round(value)) for value in roi]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(width, x1), min(height, y1)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("trace.roi_px is outside the image")
    mask[:] = False
    mask[y0:y1, x0:x1] = True
    return mask


def _coarse_prompt(rgb, trace):
    import cv2
    import numpy as np
    from scipy import ndimage

    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    border = np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]], axis=0)
    background = np.median(border, axis=0)
    distance = np.linalg.norm(lab - background, axis=2)
    configured = trace.get("background_threshold", "auto")
    threshold = (max(6.0, float(np.percentile(distance, 42.0)))
                 if configured == "auto" else float(configured))
    raw = np.logical_and(distance > threshold, _roi_mask(distance.shape, trace))
    raw = ndimage.binary_closing(raw, iterations=2)
    raw = ndimage.binary_fill_holes(raw)
    keep_all = trace.get("mode") == "all_region_boundaries"
    coarse = _largest_component(raw, keep_all=keep_all)
    ys, xs = np.nonzero(coarse)
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    diagonal = float(np.hypot(x1 - x0 + 1, y1 - y0 + 1))
    margin = max(3, int(round(diagonal * 0.006)))
    height, width = coarse.shape
    box = trace.get("box_px") or [
        float(max(0, x0 - margin)), float(max(0, y0 - margin)),
        float(min(width - 1, x1 + margin)),
        float(min(height - 1, y1 + margin)),
    ]
    positives = trace.get("positive_points_px") or []
    if not positives:
        interior = cv2.distanceTransform(
            coarse.astype(np.uint8), cv2.DIST_L2, 5)
        py, px = np.unravel_index(int(np.argmax(interior)), interior.shape)
        positives = [[float(px), float(py)]]
    negatives = trace.get("negative_points_px") or []
    return list(map(float, box)), positives, negatives, coarse, threshold


def _select_sam_mask(masks, scores, coarse):
    import numpy as np

    diagnostics = []
    ranked = []
    for index, candidate in enumerate(masks):
        candidate = candidate.astype(bool)
        intersection = np.logical_and(candidate, coarse).sum()
        union = np.logical_or(candidate, coarse).sum()
        overlap = float(intersection / max(1, union))
        area_ratio = float(candidate.sum() / max(1, coarse.sum()))
        model_score = float(scores[index])
        area_penalty = abs(math.log(max(area_ratio, 1e-6)))
        selection_score = model_score + 0.35 * overlap - 0.12 * area_penalty
        diagnostics.append({
            "index": index,
            "model_score": model_score,
            "coarse_iou": overlap,
            "area_ratio": area_ratio,
            "selection_score": selection_score,
        })
        ranked.append((selection_score, candidate, model_score))
    if not ranked:
        raise RuntimeError("SAM did not return a mask")
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1], ranked[0][2], diagnostics


def _run_sam(rgb, bundle, trace, prompt_mode="combined"):
    import cv2
    import numpy as np
    from PIL import Image

    if (trace.get("mode") == "all_region_boundaries" and
            not trace.get("_single_component")):
        _, _, _, coarse, background_threshold = _coarse_prompt(rgb, trace)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(
            coarse.astype(np.uint8), connectivity=8)
        min_area = max(1, int(trace.get("min_area_px", 100)))
        component_labels = [
            label for label in range(1, count)
            if int(stats[label, cv2.CC_STAT_AREA]) >= min_area]
        max_components = max(1, int(trace.get("max_components", 16)))
        if len(component_labels) > max_components:
            raise RuntimeError(
                f"all_region_boundaries found {len(component_labels)} components; "
                f"trace.max_components={max_components}")
        union = np.zeros(coarse.shape, dtype=bool)
        component_reports = []
        scores = []
        height, width = coarse.shape
        for label in component_labels:
            x, y, box_width, box_height, _ = [
                int(value) for value in stats[label]]
            component = labels == label
            interior = cv2.distanceTransform(
                component.astype(np.uint8), cv2.DIST_L2, 5)
            py, px = np.unravel_index(int(np.argmax(interior)), interior.shape)
            margin = max(3, int(round(np.hypot(box_width, box_height) * 0.01)))
            subtrace = dict(trace)
            subtrace.update({
                "mode": "outer_silhouette", "_single_component": True,
                "roi_px": [max(0, x - margin), max(0, y - margin),
                           min(width, x + box_width + margin),
                           min(height, y + box_height + margin)],
                "box_px": [float(max(0, x - margin)),
                           float(max(0, y - margin)),
                           float(min(width - 1, x + box_width + margin)),
                           float(min(height - 1, y + box_height + margin))],
                "positive_points_px": [[float(px), float(py)]],
            })
            selected, info = _run_sam(
                rgb, bundle, subtrace, prompt_mode=prompt_mode)
            union |= selected
            scores.append(float(info["selected_model_score"]))
            component_reports.append(info)
        if not component_reports:
            raise RuntimeError(
                "all_region_boundaries found no components above min_area_px")
        return union, {
            "box_px": trace.get("box_px"),
            "positive_points_px": trace.get("positive_points_px") or [],
            "negative_points_px": trace.get("negative_points_px") or [],
            "prompt_mode": prompt_mode,
            "background_threshold": background_threshold,
            "coarse_area_px": int(coarse.sum()),
            "sam_area_px": int(union.sum()),
            "selected_model_score": min(scores),
            "component_runs": component_reports,
            "candidates": [],
        }

    torch = bundle["torch"]
    model = bundle["sam_model"]
    processor = bundle["sam_processor"]
    box, positives, negatives, coarse, background_threshold = _coarse_prompt(
        rgb, trace)
    points = [list(map(float, point)) for point in positives + negatives]
    labels = [1] * len(positives) + [0] * len(negatives)
    prompt = {"images": Image.fromarray(rgb), "return_tensors": "pt"}
    if prompt_mode in {"combined", "point"}:
        prompt.update(input_points=[[points]], input_labels=[[labels]])
    if prompt_mode in {"combined", "box"}:
        prompt.update(input_boxes=[[box]])
    inputs = processor(**prompt).to(model.device)
    with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
        output = model(**inputs, multimask_output=True)
    masks = processor.post_process_masks(
        output.pred_masks.cpu(), inputs["original_sizes"])[0][0].numpy()
    scores = output.iou_scores.detach().float().cpu().numpy()[0, 0]
    selected, model_score, candidates = _select_sam_mask(masks, scores, coarse)
    keep_all = trace.get("mode") == "all_region_boundaries"
    selected = _largest_component(selected, keep_all=keep_all)
    return selected, {
        "box_px": box,
        "positive_points_px": positives,
        "negative_points_px": negatives,
        "prompt_mode": prompt_mode,
        "background_threshold": background_threshold,
        "coarse_area_px": int(coarse.sum()),
        "sam_area_px": int(selected.sum()),
        "selected_model_score": model_score,
        "candidates": candidates,
    }


def _run_matte(rgb, mask, bundle, trace):
    import cv2
    import numpy as np
    from PIL import Image

    torch = bundle["torch"]
    model = bundle["matte_model"]
    processor = bundle["matte_processor"]
    diagonal = float(np.hypot(*mask.shape))
    radius = trace.get("trimap_radius_px")
    if radius is None:
        radius = max(5, int(round(diagonal * 0.006)))
    radius = max(2, int(round(float(radius))))
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    sure_foreground = cv2.erode(mask.astype(np.uint8), kernel) > 0
    possible_foreground = cv2.dilate(mask.astype(np.uint8), kernel) > 0
    trimap = np.zeros(mask.shape, dtype=np.uint8)
    trimap[possible_foreground] = 128
    trimap[sure_foreground] = 255
    inputs = processor(
        images=Image.fromarray(rgb), trimaps=Image.fromarray(trimap),
        return_tensors="pt").to(model.device)
    with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
        alpha = model(**inputs).alphas
    alpha = alpha[0, 0, :rgb.shape[0], :rgb.shape[1]].float().cpu().numpy()
    alpha = np.clip(alpha, 0.0, 1.0)
    alpha[sure_foreground] = 1.0
    alpha[np.logical_not(possible_foreground)] = 0.0
    return alpha, trimap, radius


def _boundary(mask):
    import cv2
    import numpy as np

    eroded = cv2.erode(mask.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    return np.logical_and(mask, np.logical_not(eroded))


def _contour_points(mask, spacing=2.0):
    import cv2
    import numpy as np

    contours, _ = cv2.findContours(
        mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        raise RuntimeError("Mask has no external contour")
    contour = max(contours, key=cv2.contourArea)[:, 0, :].astype(np.float32)
    closed = np.vstack([contour, contour[0]])
    lengths = np.linalg.norm(np.diff(closed, axis=0), axis=1)
    cumulative = np.concatenate([[0.0], np.cumsum(lengths)])
    targets = np.arange(0.0, cumulative[-1], spacing)
    return np.column_stack([
        np.interp(targets, cumulative, closed[:, 0]),
        np.interp(targets, cumulative, closed[:, 1]),
    ])


def _sample_field(field, points):
    import cv2
    import numpy as np

    x = points[..., 0].astype(np.float32)
    y = points[..., 1].astype(np.float32)
    sampled = cv2.remap(
        field.astype(np.float32), x, y, cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE)
    return sampled.reshape(points.shape[:-1])


def _ink_support(rgb, mask):
    import cv2
    import numpy as np

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    points = _contour_points(mask)
    tangent = np.roll(points, -2, axis=0) - np.roll(points, 2, axis=0)
    tangent /= np.maximum(np.linalg.norm(tangent, axis=1, keepdims=True), 1e-6)
    normals = np.column_stack([-tangent[:, 1], tangent[:, 0]])
    plus = _sample_field(mask.astype(np.float32), points + normals * 3.0)
    minus = _sample_field(mask.astype(np.float32), points - normals * 3.0)
    inward = normals.copy()
    inward[minus > plus] *= -1.0
    offsets = np.arange(-8.0, 9.0, dtype=np.float32)
    samples = points[:, None, :] + inward[:, None, :] * offsets[None, :, None]
    profiles = _sample_field(gray, samples)
    outside = np.median(profiles[:, 1:6], axis=1)
    inside_dark = np.min(profiles[:, 9:15], axis=1)
    contrast = outside - inside_dark
    dark_entry = profiles[:, :-2] - profiles[:, 2:]
    best = np.argmax(dark_entry, axis=1) + 1
    best_offsets = offsets[best]
    best_drop = np.max(dark_entry, axis=1)
    supported = np.logical_and.reduce([
        outside >= 175.0, inside_dark <= 150.0,
        contrast >= 35.0, best_drop >= 20.0,
    ])
    return {
        "sample_count": int(len(points)),
        "support_ratio": float(np.mean(supported)),
        "contrast_p05": float(np.percentile(contrast, 5)),
        "contrast_median": float(np.median(contrast)),
        "gradient_drop_p05": float(np.percentile(best_drop, 5)),
        "gradient_offset_abs_median_px": float(np.median(np.abs(best_offsets))),
        "gradient_offset_abs_p95_px": float(np.percentile(np.abs(best_offsets), 95)),
        "gradient_offset_signed_median_px": float(np.median(best_offsets)),
    }


def _boundary_metrics(left, right):
    import numpy as np
    from scipy import ndimage

    left_edge = _boundary(left)
    right_edge = _boundary(right)
    left_field = ndimage.distance_transform_edt(np.logical_not(left_edge))
    right_field = ndimage.distance_transform_edt(np.logical_not(right_edge))
    distances = np.concatenate([left_field[right_edge], right_field[left_edge]])
    intersection = np.logical_and(left, right).sum()
    union = np.logical_or(left, right).sum()
    return {
        "iou": float(intersection / max(1, union)),
        "mean_px": float(np.mean(distances)),
        "p95_px": float(np.percentile(distances, 95)),
        "p99_px": float(np.percentile(distances, 99)),
        "max_px": float(np.max(distances)),
        "outlier_ratio_gt2px": float(np.mean(distances > 2.0)),
    }


def _perturbations(rgb, names):
    import cv2
    import numpy as np

    height, width = rgb.shape[:2]
    result = {}
    for name in names:
        if name == "jpeg_q90":
            encoded = cv2.imencode(
                ".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
                [cv2.IMWRITE_JPEG_QUALITY, 90])[1]
            result[name] = cv2.cvtColor(
                cv2.imdecode(encoded, cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)
        elif name == "resample_75":
            down = cv2.resize(
                rgb, (round(width * 0.75), round(height * 0.75)),
                interpolation=cv2.INTER_LANCZOS4)
            result[name] = cv2.resize(
                down, (width, height), interpolation=cv2.INTER_LANCZOS4)
        elif name == "gamma_108":
            gamma = np.power(rgb.astype(np.float32) / 255.0, 1.08) * 255.0
            result[name] = np.clip(np.round(gamma), 0, 255).astype(np.uint8)
        else:
            raise ValueError(f"Unknown stability perturbation '{name}'")
    return result


def _quality_gate(sam_info, ink, prompt_consensus, stability, validation):
    thresholds = {
        "min_sam_score": float(validation.get("min_sam_score", 0.90)),
        "min_boundary_support": float(
            validation.get("min_boundary_support", 0.98)),
        "max_gradient_offset_px": float(
            validation.get("max_gradient_offset_px", 1.5)),
        "min_prompt_consensus_iou": float(
            validation.get("min_prompt_consensus_iou", 0.99)),
        "max_prompt_consensus_p95_px": float(
            validation.get("max_prompt_consensus_p95_px", 1.5)),
        "min_perturbation_iou": float(
            validation.get("min_perturbation_iou", 0.995)),
        "max_perturbation_p95_px": float(
            validation.get("max_perturbation_p95_px", 1.5)),
        "max_perturbation_outlier_ratio": float(
            validation.get("max_perturbation_outlier_ratio", 0.01)),
    }
    checks = {
        "sam_score": sam_info["selected_model_score"] >= thresholds["min_sam_score"],
        "boundary_support": ink["support_ratio"] >= thresholds["min_boundary_support"],
        "gradient_alignment": ink["gradient_offset_abs_p95_px"] <=
                              thresholds["max_gradient_offset_px"],
        "prompt_consensus_iou": prompt_consensus["iou"] >=
                                thresholds["min_prompt_consensus_iou"],
        "prompt_consensus_p95": prompt_consensus["p95_px"] <=
                                thresholds["max_prompt_consensus_p95_px"],
        "perturbation_iou": all(
            item["iou"] >= thresholds["min_perturbation_iou"]
            for item in stability.values()),
        "perturbation_p95": all(
            item["p95_px"] <= thresholds["max_perturbation_p95_px"]
            for item in stability.values()),
        "perturbation_outliers": all(
            item["outlier_ratio_gt2px"] <=
            thresholds["max_perturbation_outlier_ratio"]
            for item in stability.values()),
    }
    return all(checks.values()), thresholds, checks


def vectorize_region(rgb_bgr, alpha_channel, trace: Optional[Dict[str, Any]] = None,
                     validation: Optional[Dict[str, Any]] = None,
                     model_config: Optional[Dict[str, Any]] = None) -> DeepVectorResult:
    """Produce a high-confidence region matte and its verification report."""
    import cv2
    import numpy as np
    from scipy import ndimage

    trace = dict(trace or {})
    validation = dict(validation or {})
    model_config = dict(model_config or {})
    mode = trace.get("mode", "outer_silhouette")
    if mode in LINE_ART_MODES:
        raise NotImplementedError(
            f"Trace mode '{mode}' requires the line-art graph backend; the "
            "region-matting backend may not substitute for it")
    if mode not in SUPPORTED_REGION_MODES:
        raise ValueError(f"Unknown trace mode '{mode}'")
    edge_semantics = trace.get("edge_semantics", "physical_outer_ink_edge")
    if edge_semantics not in {"physical_outer_ink_edge", "matte_alpha"}:
        raise ValueError(
            "Region matting supports edge_semantics physical_outer_ink_edge "
            "or matte_alpha")

    rgb = cv2.cvtColor(rgb_bgr, cv2.COLOR_BGR2RGB)
    bundle = _load_models(
        model_config.get("sam_model_id", SAM_MODEL_ID),
        model_config.get("matte_model_id", MATTE_MODEL_ID))
    sam_mask, sam_info = _run_sam(rgb, bundle, trace, prompt_mode="combined")
    alpha, trimap, trimap_radius = _run_matte(rgb, sam_mask, bundle, trace)
    alpha_threshold = float(trace.get("alpha_threshold", 0.5))
    if not 0.05 <= alpha_threshold <= 0.95:
        raise ValueError("trace.alpha_threshold must be within [0.05, 0.95]")
    binary = alpha >= alpha_threshold
    if mode == "outer_silhouette":
        binary = ndimage.binary_fill_holes(binary)
        alpha = np.maximum(alpha, binary.astype(np.float32))
    binary, component_report = _select_components(binary, trace, mode)
    selection_support = ndimage.binary_dilation(
        binary, iterations=max(1, trimap_radius + 2))
    alpha = np.where(selection_support, alpha, 0.0)

    box_mask, box_info = _run_sam(rgb, bundle, trace, prompt_mode="box")
    prompt_consensus = _boundary_metrics(sam_mask, box_mask)
    prompt_consensus["box_model_score"] = box_info["selected_model_score"]
    ink = _ink_support(rgb, binary)
    perturbation_names = validation.get(
        "stability_perturbations", ["jpeg_q90", "resample_75", "gamma_108"])
    stability = {}
    for name, perturbed in _perturbations(rgb, perturbation_names).items():
        perturbed_sam, _ = _run_sam(
            perturbed, bundle, trace, prompt_mode="combined")
        perturbed_alpha, _, _ = _run_matte(
            perturbed, perturbed_sam, bundle, trace)
        perturbed_binary = perturbed_alpha >= alpha_threshold
        if mode == "outer_silhouette":
            perturbed_binary = ndimage.binary_fill_holes(perturbed_binary)
        perturbed_binary, _ = _select_components(
            perturbed_binary, trace, mode)
        stability[name] = _boundary_metrics(binary, perturbed_binary)
        stability[name]["ink_support"] = _ink_support(
            perturbed, perturbed_binary)

    passed, thresholds, checks = _quality_gate(
        sam_info, ink, prompt_consensus, stability, validation)
    quality_values = [
        min(1.0, sam_info["selected_model_score"] /
            max(thresholds["min_sam_score"], 1e-9)),
        min(1.0, ink["support_ratio"] /
            max(thresholds["min_boundary_support"], 1e-9)),
        min(1.0, prompt_consensus["iou"] /
            max(thresholds["min_prompt_consensus_iou"], 1e-9)),
    ]
    confidence = float(min(quality_values)) if passed else float(
        min(quality_values) * 0.75)
    quality = {
        "backend": "sam2_vitmatte",
        "trace_mode": mode,
        "edge_semantics": edge_semantics,
        "models": {
            "sam": model_config.get("sam_model_id", SAM_MODEL_ID),
            "matte": model_config.get("matte_model_id", MATTE_MODEL_ID),
        },
        "alpha_threshold": alpha_threshold,
        "topology_level": alpha_threshold,
        "trimap_radius_px": trimap_radius,
        "alpha_transition_px": int(np.logical_and(alpha > 0.02, alpha < 0.98).sum()),
        "components": component_report,
        "ink_support": ink,
        "prompt_consensus": prompt_consensus,
        "stability": stability,
        "thresholds": thresholds,
        "checks": checks,
        "pass": passed,
        "trimap": trimap,
        "alpha": alpha,
    }
    diagnostics = [{
        "name": "sam2_vitmatte",
        "score": confidence,
        "area_px": int(binary.sum()),
        "sam": sam_info,
    }]
    return DeepVectorResult(
        mask=binary.astype(np.uint8) * 255,
        topology_field=np.round(alpha * 255).astype(np.uint8),
        topology_level=alpha_threshold,
        confidence=confidence,
        diagnostics=diagnostics,
        quality=quality)
