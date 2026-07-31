"""Verified deep line-art extraction for technical drawings.

The backend is an ensemble of DexiNed (BIPED v2) and TEED.  Both upstream
projects are MIT licensed by Xavier Soria Poma.  Packaged TorchScript models
are derived from their official checkpoints and verified by SHA-256 before
loading.  Region matting is deliberately not used as a fallback here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import re
import subprocess
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional


LINE_ART_MODES = {"stroke_centerlines", "stroke_edges", "all_visible_edges"}
MODEL_SPECS = {
    "dexined": {
        "filename": "dexined_biped_v2.pt",
        "sha256": "096C2230D3060104BAE083A4B0C882EBC6301D730C401C7E8556A6D8F944C65B",
        "mean_bgr": [103.939, 116.779, 123.68],
        "multiple": 16,
        "source": "xavysp/DexiNed official BIPED v2 checkpoint",
    },
    "teed": {
        "filename": "teed_biped_5.pt",
        "sha256": "3175EC531B2238EAAACBCFAC17111666F2B8C885BD04F785DE36F8C776C80C3A",
        "mean_bgr": [104.007, 116.669, 122.679],
        "multiple": 8,
        "source": "xavysp/TEED official BIPED checkpoint 5",
    },
}


@dataclass
class LineArtResult:
    paths: list
    reference: Any
    confidence: float
    diagnostics: list
    quality: Dict[str, Any]


_MODEL_LOCK = threading.Lock()
_MODEL_BUNDLE = None


def _model_directory() -> Path:
    return Path(__file__).resolve().parents[1] / "models" / "lineart"


def _lightweight_cuda_probe() -> Dict[str, Any]:
    """Check the installed Torch build and NVIDIA driver without importing Torch."""
    spec = importlib.util.find_spec("torch")
    if spec is None:
        return {
            "torch": False, "cuda": False, "device": None,
            "torch_cuda_build": None,
            "probe": "torch-package-metadata+nvidia-smi",
        }

    torch_cuda_build = None
    try:
        locations = list(spec.submodule_search_locations or [])
        version_file = Path(locations[0]) / "version.py"
        match = re.search(
            r"^cuda(?:\s*:[^=]+)?\s*=\s*['\"]([^'\"]*)['\"]",
            version_file.read_text(encoding="utf-8"), re.MULTILINE)
        if match and match.group(1).strip().lower() not in {"", "none"}:
            torch_cuda_build = match.group(1).strip()
    except Exception:
        pass

    device = None
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=3, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode == 0:
            device = next((line.strip() for line in completed.stdout.splitlines()
                           if line.strip()), None)
    except (OSError, subprocess.SubprocessError):
        pass

    return {
        "torch": True,
        "cuda": bool(torch_cuda_build and device),
        "device": device,
        "torch_cuda_build": torch_cuda_build,
        "probe": "torch-package-metadata+nvidia-smi",
    }


@lru_cache(maxsize=8)
def _verified_file(path_text: str, size: int, modified_ns: int,
                   expected_hash: str) -> bool:
    path = Path(path_text)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper() == expected_hash


def capability_report() -> Dict[str, Any]:
    """Report readiness without importing Torch or warming CUDA kernels."""
    cuda_probe = _lightweight_cuda_probe()
    torch_installed = bool(cuda_probe["torch"])
    cuda = bool(cuda_probe["cuda"])
    device = cuda_probe["device"]
    files = {}
    directory = _model_directory()
    for name, spec in MODEL_SPECS.items():
        path = directory / spec["filename"]
        exists = path.is_file()
        verified = False
        if exists:
            stat = path.stat()
            verified = _verified_file(
                str(path), stat.st_size, stat.st_mtime_ns, spec["sha256"])
        files[name] = {
            "path": str(path), "exists": exists,
            "sha256_verified": verified, "source": spec["source"],
        }
    ready = torch_installed and cuda and all(
        item["sha256_verified"] for item in files.values())
    return {
        "available": ready,
        "torch": torch_installed,
        "cuda": cuda,
        "device": device,
        "torch_cuda_build": cuda_probe["torch_cuda_build"],
        "readiness_probe": cuda_probe["probe"],
        "models": files,
        "supported_modes": sorted(LINE_ART_MODES),
        "backends": ["dexined_biped_v2", "teed_biped_5"],
    }


def _load_models():
    global _MODEL_BUNDLE
    with _MODEL_LOCK:
        if _MODEL_BUNDLE is not None:
            return _MODEL_BUNDLE
        report = capability_report()
        if not report["available"]:
            raise RuntimeError(
                "Line-art ensemble is unavailable; CUDA and both verified "
                "DexiNed/TEED model files are required")
        import torch

        models = {}
        for name, spec in MODEL_SPECS.items():
            models[name] = torch.jit.load(
                str(_model_directory() / spec["filename"]),
                map_location="cuda").eval()
        _MODEL_BUNDLE = {"torch": torch, "models": models}
        return _MODEL_BUNDLE


def _roi_mask(shape, trace):
    import numpy as np

    height, width = shape
    result = np.ones(shape, dtype=bool)
    roi = trace.get("roi_px")
    if roi is None:
        return result
    if len(roi) != 4:
        raise ValueError("trace.roi_px must be [x0,y0,x1,y1]")
    x0, y0, x1, y1 = [int(round(value)) for value in roi]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(width, x1), min(height, y1)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("trace.roi_px is outside the image")
    result[:] = False
    result[y0:y1, x0:x1] = True
    return result


def _infer(rgb_bgr, model_name, bundle):
    import cv2
    import numpy as np

    torch = bundle["torch"]
    spec = MODEL_SPECS[model_name]
    height, width = rgb_bgr.shape[:2]
    multiple = int(spec["multiple"])
    padded_height = ((height + multiple - 1) // multiple) * multiple
    padded_width = ((width + multiple - 1) // multiple) * multiple
    padded = cv2.copyMakeBorder(
        rgb_bgr, 0, padded_height - height, 0, padded_width - width,
        cv2.BORDER_REFLECT_101)
    values = (padded.astype(np.float32) -
              np.asarray(spec["mean_bgr"], dtype=np.float32))
    tensor = torch.from_numpy(values.transpose(2, 0, 1)[None]).to("cuda")
    with torch.inference_mode():
        probability = torch.sigmoid(
            bundle["models"][model_name](tensor)[-1])
    return probability[0, 0, :height, :width].float().cpu().numpy()


def _model_consensus(rgb_bgr, trace, bundle):
    import numpy as np
    from scipy import ndimage
    from skimage.morphology import skeletonize

    threshold = float(trace.get("line_probability_threshold", 0.5))
    dexi_threshold = float(trace.get("dexined_threshold", threshold))
    teed_threshold = float(trace.get("teed_threshold", threshold))
    if not (0.05 <= dexi_threshold <= 0.95 and
            0.05 <= teed_threshold <= 0.95):
        raise ValueError("Line-art probability thresholds must be within [0.05, 0.95]")
    radius = float(trace.get("consensus_radius_px", 2.0))
    if not 0.5 <= radius <= 8.0:
        raise ValueError("trace.consensus_radius_px must be within [0.5, 8]")
    roi = _roi_mask(rgb_bgr.shape[:2], trace)
    dexi_probability = _infer(rgb_bgr, "dexined", bundle)
    teed_probability = _infer(rgb_bgr, "teed", bundle)
    dexi_band = (dexi_probability >= dexi_threshold) & roi
    teed_band = (teed_probability >= teed_threshold) & roi
    dexi_skeleton = skeletonize(dexi_band)
    teed_skeleton = skeletonize(teed_band)
    distance_to_dexi = ndimage.distance_transform_edt(~dexi_skeleton)
    distance_to_teed = ndimage.distance_transform_edt(~teed_skeleton)
    dexi_support = float((distance_to_teed[dexi_skeleton] <= radius).mean())
    teed_support = float((distance_to_dexi[teed_skeleton] <= radius).mean())
    confirmed_band = dexi_band & (ndimage.distance_transform_edt(~teed_band) <= radius)
    consensus = skeletonize(confirmed_band)
    if not consensus.any():
        raise RuntimeError("DexiNed and TEED found no mutually supported edges")
    distances = np.concatenate([
        distance_to_teed[dexi_skeleton], distance_to_dexi[teed_skeleton]])
    return consensus, {
        "dexined_threshold": dexi_threshold,
        "teed_threshold": teed_threshold,
        "consensus_radius_px": radius,
        "dexined_support": dexi_support,
        "teed_support": teed_support,
        "balanced_support": min(dexi_support, teed_support),
        "distance_median_px": float(np.median(distances)),
        "distance_p95_px": float(np.percentile(distances, 95)),
        "edge_pixels": int(consensus.sum()),
    }


def _perturbations(rgb_bgr, names):
    import cv2
    import numpy as np

    result = {}
    height, width = rgb_bgr.shape[:2]
    for name in names:
        if name == "jpeg_q90":
            encoded = cv2.imencode(
                ".jpg", rgb_bgr, [cv2.IMWRITE_JPEG_QUALITY, 90])[1]
            result[name] = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        elif name == "resample_75":
            down = cv2.resize(
                rgb_bgr, (round(width * 0.75), round(height * 0.75)),
                interpolation=cv2.INTER_AREA)
            result[name] = cv2.resize(
                down, (width, height), interpolation=cv2.INTER_LANCZOS4)
        elif name == "gamma_108":
            gamma = np.power(rgb_bgr.astype(np.float32) / 255.0, 1.08)
            result[name] = np.clip(
                np.round(gamma * 255.0), 0, 255).astype(np.uint8)
        else:
            raise ValueError(f"Unknown stability perturbation '{name}'")
    return result


def _skeleton_metrics(reference, candidate, radius):
    import numpy as np
    from scipy import ndimage

    distance_to_reference = ndimage.distance_transform_edt(~reference)
    distance_to_candidate = ndimage.distance_transform_edt(~candidate)
    reference_distances = distance_to_candidate[reference]
    candidate_distances = distance_to_reference[candidate]
    all_distances = np.concatenate([reference_distances, candidate_distances])
    reference_support = float((reference_distances <= radius).mean())
    candidate_support = float((candidate_distances <= radius).mean())
    return {
        "reference_support": reference_support,
        "candidate_support": candidate_support,
        "balanced_support": min(reference_support, candidate_support),
        "mean_px": float(all_distances.mean()),
        "p95_px": float(np.percentile(all_distances, 95)),
        "max_px": float(all_distances.max()),
    }


def _ink_mask(rgb_bgr, trace):
    import cv2
    import numpy as np

    gray = cv2.cvtColor(rgb_bgr, cv2.COLOR_BGR2GRAY)
    configured = trace.get("stroke_threshold", "auto")
    if configured == "auto":
        otsu, _ = cv2.threshold(
            gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
        threshold = min(float(otsu), 160.0)
    else:
        threshold = float(configured)
    if not 0.0 <= threshold <= 254.0:
        raise ValueError("trace.stroke_threshold must be auto or within [0, 254]")
    mask = (gray <= threshold) & _roi_mask(gray.shape, trace)
    kernel = np.ones((3, 3), dtype=np.uint8)
    mask = cv2.morphologyEx(
        mask.astype(np.uint8), cv2.MORPH_CLOSE, kernel) > 0
    return mask, threshold


def _path_length(points):
    import numpy as np

    points = np.asarray(points, dtype=float)
    return float(np.linalg.norm(np.diff(points, axis=0), axis=1).sum())


def _graph_paths(skeleton, min_length_px, max_paths):
    import numpy as np

    offsets = [(-1, -1), (-1, 0), (-1, 1), (0, -1),
               (0, 1), (1, -1), (1, 0), (1, 1)]

    def neighbors(point, coordinates):
        row, column = point
        result = []
        for dr, dc in offsets:
            candidate = (row + dr, column + dc)
            if candidate not in coordinates:
                continue
            # An 8-connected digital curve contains tiny triangular cycles at
            # ordinary 45-degree turns.  The diagonal is redundant whenever
            # an orthogonal step already connects the same two pixels; keeping
            # it would manufacture a branch node and several sub-pixel paths.
            if dr and dc and (
                    (row + dr, column) in coordinates or
                    (row, column + dc) in coordinates):
                continue
            result.append(candidate)
        return result

    coordinates = {tuple(item) for item in np.argwhere(skeleton)}

    # Repeatedly remove only short endpoint-to-junction spurs.  Merely hiding
    # short output paths leaves their junction behind and fragments the real
    # curve into hundreds of entities.  This topology-first pruning lets the
    # surviving degree-two chain merge again without touching closed details.
    pruned_spurs = 0
    pruned_spur_length = 0.0
    for _ in range(16):
        adjacency = {point: neighbors(point, coordinates)
                     for point in coordinates}
        endpoints = [point for point, adjacent in adjacency.items()
                     if len(adjacent) == 1]
        remove = set()
        for endpoint in endpoints:
            path = [endpoint]
            previous = None
            current = endpoint
            while True:
                options = [item for item in adjacency[current]
                           if item != previous]
                if not options:
                    break
                following = options[0]
                path.append(following)
                previous, current = current, following
                if len(adjacency[current]) != 2:
                    break
            xy = [[float(column), float(row)] for row, column in path]
            length = _path_length(xy)
            if (length < min_length_px and len(path) > 1 and
                    len(adjacency[path[-1]]) >= 3):
                remove.update(path[:-1])
                pruned_spurs += 1
                pruned_spur_length += length
        if not remove:
            break
        coordinates.difference_update(remove)

    adjacency = {point: neighbors(point, coordinates) for point in coordinates}
    nodes = {point for point, adjacent in adjacency.items()
             if len(adjacent) != 2}
    visited = set()
    raw_paths = []

    def edge_key(left, right):
        return tuple(sorted((left, right)))

    def trace(start, first):
        path = [start, first]
        previous, current = start, first
        visited.add(edge_key(previous, current))
        while current not in nodes:
            options = [item for item in adjacency[current]
                       if item != previous and edge_key(current, item) not in visited]
            if not options:
                break
            following = options[0]
            visited.add(edge_key(current, following))
            path.append(following)
            previous, current = current, following
            if current == start:
                break
        return path

    for node in sorted(nodes):
        for adjacent in adjacency[node]:
            if edge_key(node, adjacent) not in visited:
                raw_paths.append(trace(node, adjacent))
    for point in sorted(coordinates):
        for adjacent in adjacency[point]:
            if edge_key(point, adjacent) not in visited:
                raw_paths.append(trace(point, adjacent))

    paths = []
    discarded = []
    for raw in raw_paths:
        points = [[float(column), float(row)] for row, column in raw]
        closed = len(points) > 2 and math.dist(points[0], points[-1]) <= 1.5
        length = _path_length(points)
        record = {"points": points, "closed": closed,
                  "length_px": length}
        if length >= min_length_px:
            paths.append(record)
        else:
            discarded.append(record)
    paths.sort(key=lambda item: item["length_px"], reverse=True)
    if len(paths) > max_paths:
        raise RuntimeError(
            f"Line-art graph needs {len(paths)} paths; trace.max_paths={max_paths}")
    return paths, {
        "skeleton_pixels": len(coordinates),
        "graph_nodes": len(nodes),
        "raw_paths": len(raw_paths),
        "selected_paths": len(paths),
        "discarded_short_paths": len(discarded),
        "discarded_short_length_px": float(sum(
            item["length_px"] for item in discarded)),
        "pruned_spurs": pruned_spurs,
        "pruned_spur_length_px": pruned_spur_length,
    }


def _stroke_edge_paths(mask, edge_side, min_length_px, max_paths):
    import cv2

    contours, hierarchy = cv2.findContours(
        mask.astype("uint8"), cv2.RETR_TREE, cv2.CHAIN_APPROX_NONE)
    hierarchy = hierarchy[0] if hierarchy is not None else []
    paths = []
    discarded = 0
    for index, contour in enumerate(contours):
        parent = int(hierarchy[index][3]) if len(hierarchy) else -1
        role = "outer_edge" if parent < 0 else "inner_edge"
        if edge_side != "both" and role != edge_side:
            continue
        points = contour[:, 0, :].astype(float).tolist()
        if points:
            points.append(points[0])
        length = _path_length(points)
        if length < min_length_px:
            discarded += 1
            continue
        paths.append({"points": points, "closed": True,
                      "role": role, "length_px": length})
    paths.sort(key=lambda item: item["length_px"], reverse=True)
    if len(paths) > max_paths:
        raise RuntimeError(
            f"Stroke edges need {len(paths)} paths; trace.max_paths={max_paths}")
    return paths, {"selected_paths": len(paths),
                   "discarded_short_paths": discarded}


def _gradient_support(rgb_bgr, paths):
    import cv2
    import numpy as np

    gray = cv2.cvtColor(rgb_bgr, cv2.COLOR_BGR2GRAY)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    gradient = np.hypot(gx, gy)
    values = []
    height, width = gray.shape
    for path in paths:
        points = np.round(path["points"]).astype(int)
        columns = np.clip(points[:, 0], 0, width - 1)
        rows = np.clip(points[:, 1], 0, height - 1)
        values.extend(gradient[rows, columns].tolist())
    values = np.asarray(values, dtype=float)
    return {
        "sample_count": int(values.size),
        "p05": float(np.percentile(values, 5)) if values.size else 0.0,
        "median": float(np.median(values)) if values.size else 0.0,
    }


def _paths_to_raster(paths, shape):
    import cv2
    import numpy as np

    raster = np.zeros(shape, dtype=np.uint8)
    for path in paths:
        points = np.round(path["points"]).astype(np.int32)
        cv2.polylines(raster, [points], bool(path.get("closed")), 1, 1,
                      lineType=cv2.LINE_8)
    return raster > 0


def _filter_paths_by_stability(paths, perturbation_skeletons, radius,
                               minimum_support):
    import numpy as np
    from scipy import ndimage

    fields = [ndimage.distance_transform_edt(~item)
              for item in perturbation_skeletons]
    selected = []
    rejected = []
    for path in paths:
        points = np.round(path["points"]).astype(int)
        ratios = []
        for field in fields:
            rows = np.clip(points[:, 1], 0, field.shape[0] - 1)
            columns = np.clip(points[:, 0], 0, field.shape[1] - 1)
            ratios.append(float((field[rows, columns] <= radius).mean()))
        record = dict(path)
        record["stability_support"] = ratios
        record["stability_min_support"] = min(ratios) if ratios else 1.0
        if record["stability_min_support"] >= minimum_support:
            selected.append(record)
        else:
            rejected.append(record)
    return selected, rejected


def vectorize_line_art(rgb_bgr, trace: Optional[Dict[str, Any]] = None,
                       validation: Optional[Dict[str, Any]] = None,
                       model_config: Optional[Dict[str, Any]] = None
                       ) -> LineArtResult:
    """Extract a verified open/closed path graph from technical line art."""
    import numpy as np
    from skimage.morphology import skeletonize

    trace = dict(trace or {})
    validation = dict(validation or {})
    model_config = dict(model_config or {})
    if model_config:
        raise ValueError(
            "Line-art model overrides are not supported; packaged models are "
            "checksum-pinned for reproducibility")
    mode = trace.get("mode")
    if mode not in LINE_ART_MODES:
        raise ValueError(f"Unknown line-art mode '{mode}'")
    bundle = _load_models()
    consensus, model_report = _model_consensus(rgb_bgr, trace, bundle)
    min_length = float(trace.get("min_branch_length_px", 8.0))
    max_paths = int(trace.get("max_paths", 512))
    if min_length < 1.0:
        raise ValueError("trace.min_branch_length_px must be >= 1")
    if not 1 <= max_paths <= 4096:
        raise ValueError("trace.max_paths must be within [1, 4096]")

    ink_threshold = None
    graph_report = {}
    topology_source = None
    if mode == "all_visible_edges":
        # The two networks are the independent evidence gate.  For technical
        # drawings, the skeleton of the dark-stroke mask is the cleaner graph:
        # skeletonizing a wide neural probability ridge creates artificial
        # side branches at ordinary antialiased corners.
        topology_source = trace.get("edge_topology_source", "ink_centerline")
        if topology_source not in {"ink_centerline", "ensemble_ridge"}:
            raise ValueError(
                "trace.edge_topology_source must be ink_centerline or ensemble_ridge")
        if topology_source == "ink_centerline":
            ink, ink_threshold = _ink_mask(rgb_bgr, trace)
            reference = skeletonize(ink)
        else:
            reference = consensus
        paths, graph_report = _graph_paths(reference, min_length, max_paths)
        for path in paths:
            path["role"] = "visible_edge"
    elif mode == "stroke_centerlines":
        ink, ink_threshold = _ink_mask(rgb_bgr, trace)
        reference = skeletonize(ink)
        paths, graph_report = _graph_paths(reference, min_length, max_paths)
        for path in paths:
            path["role"] = "stroke_centerline"
    else:
        edge_side = trace.get("stroke_edge_side")
        if edge_side not in {"outer_edge", "inner_edge", "both"}:
            raise ValueError(
                "stroke_edges requires trace.stroke_edge_side="
                "outer_edge, inner_edge, or both")
        ink, ink_threshold = _ink_mask(rgb_bgr, trace)
        paths, graph_report = _stroke_edge_paths(
            ink, edge_side, min_length, max_paths)
        reference = _paths_to_raster(paths, ink.shape)

    if not paths:
        raise RuntimeError("Line-art extraction produced no paths above the minimum length")
    model_rejected_paths = []
    if mode in {"all_visible_edges", "stroke_centerlines"}:
        path_consensus_radius = float(
            trace.get("path_consensus_radius_px", 3.0))
        if not 0.5 <= path_consensus_radius <= 16.0:
            raise ValueError(
                "trace.path_consensus_radius_px must be within [0.5, 16]")
        minimum_model_path_support = float(
            validation.get("min_path_model_support", 0.70))
        paths, model_rejected_paths = _filter_paths_by_stability(
            paths, [consensus], path_consensus_radius,
            minimum_model_path_support)
        if not paths:
            raise RuntimeError(
                "Every line-art path failed independent model support")
    perturbation_names = validation.get(
        "stability_perturbations", ["jpeg_q90", "resample_75", "gamma_108"])
    stability_radius = float(validation.get("line_stability_radius_px", 2.0))
    minimum_path_support = float(
        validation.get("min_path_stability_support", 0.80))
    perturbation_skeletons = []
    stability = {}
    for name, perturbed in _perturbations(rgb_bgr, perturbation_names).items():
        perturbed_consensus, _ = _model_consensus(perturbed, trace, bundle)
        if mode == "all_visible_edges" and topology_source == "ensemble_ridge":
            perturbed_reference = perturbed_consensus
        elif mode in {"all_visible_edges", "stroke_centerlines"}:
            perturbed_ink, _ = _ink_mask(perturbed, trace)
            perturbed_reference = skeletonize(perturbed_ink)
        else:
            perturbed_ink, _ = _ink_mask(perturbed, trace)
            perturbed_paths, _ = _stroke_edge_paths(
                perturbed_ink, trace["stroke_edge_side"],
                min_length, max_paths)
            perturbed_reference = _paths_to_raster(
                perturbed_paths, perturbed_ink.shape)
        perturbation_skeletons.append(perturbed_reference)
        stability[name] = _skeleton_metrics(
            reference, perturbed_reference, stability_radius)
    paths, rejected_paths = _filter_paths_by_stability(
        paths, perturbation_skeletons, stability_radius, minimum_path_support)
    if not paths:
        raise RuntimeError("Every detected line-art path failed perturbation stability")
    selected_length = sum(item["length_px"] for item in paths)
    rejected_paths = model_rejected_paths + rejected_paths
    rejected_length = sum(item["length_px"] for item in rejected_paths)
    discarded_short_length = float(
        graph_report.get("discarded_short_length_px", 0.0))
    rejected_total_length = rejected_length + discarded_short_length
    rejected_ratio = rejected_total_length / max(
        selected_length + rejected_total_length, 1e-9)
    selected_reference = _paths_to_raster(paths, reference.shape)
    gradient = _gradient_support(rgb_bgr, paths)
    thresholds = {
        "min_model_consensus": float(
            validation.get("min_model_consensus", 0.90)),
        "max_model_p95_px": float(
            validation.get("max_model_consensus_p95_px", 3.0)),
        "min_path_stability_support": minimum_path_support,
        "max_rejected_length_ratio": float(
            validation.get("max_rejected_length_ratio", 0.10)),
        "min_gradient_median": float(
            validation.get("min_line_gradient_median", 40.0)),
    }
    checks = {
        "independent_model_consensus": (
            model_report["balanced_support"] >= thresholds["min_model_consensus"]),
        "model_spatial_agreement": (
            model_report["distance_p95_px"] <= thresholds["max_model_p95_px"]),
        "perturbation_stability": all(
            item["balanced_support"] >= minimum_path_support
            for item in stability.values()),
        "rejected_length": rejected_ratio <= thresholds["max_rejected_length_ratio"],
        "source_gradient": gradient["median"] >= thresholds["min_gradient_median"],
    }
    passed = all(checks.values())
    confidence = float(min(
        model_report["balanced_support"],
        min(item["balanced_support"] for item in stability.values()),
        max(0.0, 1.0 - rejected_ratio)))
    quality = {
        "backend": "dexined_teed_ensemble",
        "trace_mode": mode,
        "applied_trace": {
            "mode": mode,
            "line_probability_threshold": float(
                trace.get("line_probability_threshold", 0.5)),
            "dexined_threshold": model_report["dexined_threshold"],
            "teed_threshold": model_report["teed_threshold"],
            "consensus_radius_px": model_report["consensus_radius_px"],
            "path_consensus_radius_px": float(
                trace.get("path_consensus_radius_px", 3.0)),
            "min_branch_length_px": min_length,
            "max_paths": max_paths,
            "stroke_threshold": ink_threshold,
            "stroke_edge_side": trace.get("stroke_edge_side"),
            "edge_topology_source": topology_source,
        },
        "model_consensus": model_report,
        "graph": graph_report,
        "gradient_support": gradient,
        "stability": stability,
        "rejected_paths": [{
            "role": item.get("role"),
            "length_px": item["length_px"],
            "stability_min_support": item["stability_min_support"],
        } for item in rejected_paths],
        "rejected_length_ratio": rejected_ratio,
        "thresholds": thresholds,
        "checks": checks,
        "pass": passed,
    }
    diagnostics = [{
        "name": "dexined_teed_consensus",
        "score": confidence,
        "paths": len(paths),
        "rejected_paths": len(rejected_paths),
    }]
    return LineArtResult(
        paths=paths, reference=selected_reference.astype(np.uint8) * 255,
        confidence=confidence, diagnostics=diagnostics, quality=quality)
