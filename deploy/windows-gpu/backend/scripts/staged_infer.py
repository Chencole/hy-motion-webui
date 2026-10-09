"""Run the official HY-Motion standard model in two separate processes.

    python staged_infer.py encode
    python staged_infer.py generate

Core dependencies: torch, transformers==4.53.3, accelerate, safetensors,
huggingface_hub, PyYAML, numpy, scipy, einops, torchdiffeq==0.2.5, fbxsdkpy.
Text encoding stays on CPU; --device cuda:0 runs motion sampling on NVIDIA GPU.
No Gradio, bitsandbytes, alternate model, IK, or motion scoring is used by this adapter.
All model files must already exist locally. Repository source is not edited.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback


BASE = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT = (
    "A person grips a sword with both hands, raises it overhead, delivers a "
    "downward slash, then follows with a diagonal cut."
)
FEATURE_KEYS = ("text_vec_raw", "text_ctxt_raw", "text_ctxt_raw_length")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("encode", "generate"))
    parser.add_argument("--repo", type=Path, default=BASE / "repo")
    parser.add_argument("--model-path", type=Path, help="Standard config.yml/latest.ckpt directory")
    parser.add_argument("--qwen-path", type=Path, help="Official Qwen3-8B local directory")
    parser.add_argument("--clip-path", type=Path, help="Official CLIP ViT-L/14 local directory")
    parser.add_argument("--cache-path", type=Path, default=BASE / "cache" / "text_features.pt")
    parser.add_argument("--output-dir", type=Path, help="New directory; default outputs/<timestamp>")
    parser.add_argument("--prompt", help="English prompt; generate defaults to the cached prompt")
    parser.add_argument("--duration", type=float, default=4.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--steps", type=int, default=50, help="50 for normal inference; fewer only for smoke tests")
    parser.add_argument("--threads", type=int, default=8, help="CPU worker threads")
    parser.add_argument("--device", choices=("cpu", "cuda:0"), default="cpu",
                        help="Motion device; text encoding always uses CPU")
    args = parser.parse_args()
    args.repo = args.repo.resolve()
    args.model_path = (args.model_path or args.repo / "ckpts/tencent/HY-Motion-1.0").resolve()
    args.qwen_path = (args.qwen_path or args.repo / "ckpts/Qwen3-8B").resolve()
    args.clip_path = (args.clip_path or args.repo / "ckpts/clip-vit-large-patch14").resolve()
    args.cache_path = args.cache_path.resolve()
    if args.output_dir is not None:
        args.output_dir = args.output_dir.resolve()
    if args.steps < 1:
        parser.error("--steps must be positive")
    if args.threads < 1:
        parser.error("--threads must be positive")
    if not (0 < args.duration <= 12):
        parser.error("--duration must be in (0, 12] for the standard 360-frame model")
    if not (args.repo / "hymotion").is_dir():
        parser.error(f"Official repository not found: {args.repo}")
    return args


def motion_device(torch, requested):
    if requested == "cuda:0":
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA requested but unavailable. Install CUDA PyTorch and check the NVIDIA driver; refusing a silent CPU fallback.")
        torch.cuda.set_device(0)
    return torch.device(requested)


def features_on_device(features, torch, device):
    validate_features(features, torch)
    # Preserve integer context lengths; only move the three upstream features.
    return {key: features[key].to(device=device) for key in FEATURE_KEYS}


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def file_info(path):
    return {"path": str(path), "bytes": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}


def tensor_info(tensor):
    return {"shape": list(tensor.shape), "dtype": str(tensor.dtype), "device": str(tensor.device)}


def validate_features(features, torch):
    expected = {"text_vec_raw": (1, 1, 768), "text_ctxt_raw": (1, 128, 4096), "text_ctxt_raw_length": (1,)}
    for key in FEATURE_KEYS:
        value = features.get(key)
        if not isinstance(value, torch.Tensor) or tuple(value.shape) != expected[key]:
            raise ValueError(f"Invalid cached feature shape: {key}, expected {expected[key]}")
        if not torch.isfinite(value).all().item():
            raise ValueError(f"Non-finite cached feature: {key}")
    length = features["text_ctxt_raw_length"]
    if length.dtype != torch.int64 or not (0 < length.item() <= 128):
        raise ValueError("Invalid cached text context length")


def encode(args, metadata):
    import torch
    from hymotion.network.text_encoders import text_encoder as official_text

    for path in (args.qwen_path, args.clip_path):
        if not (path / "config.json").is_file():
            raise FileNotFoundError(f"Local model config missing: {path / 'config.json'}")
    # Override local directory locations only; retain the official classes and weights.
    official_text.LLM_ENCODER_LAYOUT["qwen3"]["module_path"] = str(args.qwen_path)
    official_text.SENTENCE_EMB_LAYOUT["clipl"]["module_path"] = str(args.clip_path)
    prompt = args.prompt if args.prompt is not None else DEFAULT_PROMPT
    if not prompt.strip():
        raise ValueError("Prompt must not be empty")
    metadata.update(prompt=prompt, max_length_llm=128, qwen_path=str(args.qwen_path), clip_path=str(args.clip_path))
    print("Loading official Qwen3-8B BF16 and CLIP on CPU...", flush=True)
    encoder = official_text.HYTextModel(llm_type="qwen3", max_length_llm=128, sentence_emb_type="clipl")
    encoder.eval()
    if next(encoder.llm_text_encoder.parameters()).dtype != torch.bfloat16:
        raise RuntimeError("Official Qwen encoder was not loaded in BF16")
    with torch.inference_mode():
        vec, ctxt, length = encoder.encode([prompt])
    features = {key: value.detach().cpu().contiguous() for key, value in zip(FEATURE_KEYS, (vec, ctxt, length))}
    validate_features(features, torch)
    metadata["features"] = {key: tensor_info(value) for key, value in features.items()}
    metadata["device"] = "cpu"
    metadata["qwen_dtype"] = str(next(encoder.llm_text_encoder.parameters()).dtype)
    metadata["clip_dtype"] = str(next(encoder.sentence_emb_text_encoder.parameters()).dtype)
    metadata["cache_schema"] = 1
    args.cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.cache_path.with_name(args.cache_path.name + ".tmp")
    cache_metadata = {**metadata, "status": "completed", "encoded_utc": datetime.now(timezone.utc).isoformat()}
    torch.save({"schema_version": 1, "prompt": prompt, "features": features, "metadata": cache_metadata}, temporary)
    temporary.replace(args.cache_path)
    metadata["artifacts"] = {"text_feature_cache": str(args.cache_path)}
    print(f"Encoded features saved: {args.cache_path}", flush=True)
    print("Encode stage finished. This process exits before the generate stage is started.", flush=True)


def generate(args, metadata):
    import torch
    import yaml
    from hymotion.utils.loaders import load_object
    from hymotion.utils import visualize_mesh_web as official_visualization

    device = motion_device(torch, args.device)
    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats(device)
        metadata.update(gpu_name=torch.cuda.get_device_name(device), cuda_version=torch.version.cuda)

    config_path = args.model_path / "config.yml"
    checkpoint_path = args.model_path / "latest.ckpt"
    for path in (config_path, checkpoint_path, args.cache_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    architecture = config["network_module_args"]
    if architecture.get("feat_dim") != 1280 or architecture.get("num_layers") != 27:
        raise ValueError("Expected the official standard HY-Motion-1.0 configuration, not Lite")
    cached = torch.load(args.cache_path, map_location="cpu", weights_only=True)
    if cached.get("schema_version") != 1:
        raise ValueError("Unsupported text cache schema; rerun encode")
    prompt = cached["prompt"]
    if args.prompt is not None and args.prompt != prompt:
        raise ValueError("--prompt differs from cached features; rerun encode for this prompt")
    features = cached["features"]
    validate_features(features, torch)
    metadata.update(
        prompt=prompt, duration_seconds=args.duration, seed=args.seed, cfg_scale=5.0,
        steps=args.steps, full_default_steps=(args.steps == 50), model_config=config,
        checkpoint=file_info(checkpoint_path), feature_cache=file_info(args.cache_path),
        encoding_metadata=cached.get("metadata", {}), motion_dtype="torch.float32",
        official_processing={
            "rotation_slerp_smoothing": True, "translation_savgol_smoothing": True,
            "global_ground_alignment": True,
            "note": "These are built into the official decoder. No added IK, scoring, or motion correction.",
        },
    )
    write_json(args.output_dir / "run_metadata.json", metadata)
    print(f"Loading official standard DiT only, FP32 on {device}...", flush=True)
    pipeline = load_object(
        config["train_pipeline"], config["train_pipeline_args"],
        network_module=config["network_module"], network_module_args=architecture,
    )
    pipeline.load_in_demo(str(checkpoint_path), build_text_encoder=False, allow_empty_ckpt=False)
    pipeline.to(device=device, dtype=torch.float32).eval()
    pipeline.requires_grad_(False)
    features = features_on_device(features, torch, device)
    metadata["sampling_features"] = {key: tensor_info(value) for key, value in features.items()}
    metadata["model_device"] = str(next(pipeline.motion_transformer.parameters()).device)
    metadata["device"] = metadata["model_device"]
    if metadata["model_device"] != str(device):
        raise RuntimeError(f"Motion model is on {metadata['model_device']}, requested {device}")
    write_json(args.output_dir / "run_metadata.json", metadata)
    pipeline.validation_steps = args.steps
    if hasattr(pipeline, "text_encoder"):
        raise RuntimeError("Unexpected text encoder in the generate process")
    original_decode = pipeline.decode_motion_from_latent
    raw_latent_path = args.output_dir / "raw_latent.pt"

    def capture_then_decode(latent, should_apply_smooothing=True):
        # Capture the official sample before any official decoding or smoothing.
        raw_latent = latent.detach().cpu().clone()
        torch.save(raw_latent, raw_latent_path)
        metadata["raw_latent"] = tensor_info(raw_latent)
        return original_decode(latent, should_apply_smooothing=should_apply_smooothing)

    pipeline.decode_motion_from_latent = capture_then_decode
    print(f"Generating with real cached text features: {args.steps} steps, seed {args.seed}, CFG 5...", flush=True)
    with torch.inference_mode():
        output = pipeline.generate(
            text=prompt, seed_input=[args.seed], duration_slider=args.duration,
            cfg_scale=5.0, use_special_game_feat=False, hidden_state_dict=features,
        )
    if device.type == "cuda":
        torch.cuda.synchronize(device)
        metadata["cuda_peak_allocated_bytes"] = torch.cuda.max_memory_allocated(device)
        metadata["cuda_peak_reserved_bytes"] = torch.cuda.max_memory_reserved(device)
    torch.save(output, args.output_dir / "official_output.pt")
    metadata["output_tensors"] = {key: tensor_info(value) for key, value in output.items() if isinstance(value, torch.Tensor)}
    metadata["actual_frames"] = int(output["rot6d"].shape[1])
    metadata["actual_duration_seconds"] = metadata["actual_frames"] / pipeline.output_mesh_fps
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    _, base_filename = official_visualization.save_visualization_data(
        output=output, text=prompt, rewritten_text=prompt, timestamp=timestamp,
        output_dir=str(args.output_dir), output_filename="motion",
    )
    # The upstream HTML reader assumes repository-relative paths. Scope its path
    # resolver to this run directory; data conversion and template stay official.
    original_output_resolver = official_visualization.get_output_dir
    try:
        official_visualization.get_output_dir = lambda sub_path="": str(args.output_dir)
        html = official_visualization.generate_static_html_content("output", base_filename)
    finally:
        official_visualization.get_output_dir = original_output_resolver
    (args.output_dir / "motion.html").write_text(html, encoding="utf-8")
    from hymotion.utils.smplh2woodfbx import SMPLH2WoodFBX
    converter = SMPLH2WoodFBX()
    if not converter.convert_npz_to_fbx(
        str(args.output_dir / f"{base_filename}_000.npz"),
        str(args.output_dir / "motion.fbx"), fps=pipeline.output_mesh_fps,
    ):
        raise RuntimeError("Official FBX export failed; NPZ and raw tensor outputs are retained")
    metadata["artifacts"] = {path.name: str(path) for path in args.output_dir.iterdir() if path.is_file()}
    print(f"Motion artifacts saved: {args.output_dir}", flush=True)


def main():
    args = parse_args()
    # Resolve all user paths first, then use the upstream working directory for
    # its stats and wooden mesh assets. Disable accidental network fallbacks.
    os.environ["USE_HF_MODELS"] = "0"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HOME"] = str(BASE / "cache" / "huggingface")
    os.environ["OMP_NUM_THREADS"] = str(args.threads)
    os.environ["MKL_NUM_THREADS"] = str(args.threads)
    os.environ["TEMP"] = str(BASE / "tmp")
    os.environ["TMP"] = str(BASE / "tmp")
    (BASE / "tmp").mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(args.repo))
    os.chdir(args.repo)
    started = time.perf_counter()
    metadata = {
        "stage": args.stage, "status": "running", "device": None, "model_device": None,
        "text_device": "cpu", "requested_motion_device": args.device,
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "python": sys.version, "python_executable": sys.executable, "repository": str(args.repo),
        "cpu_threads": args.threads,
    }
    for package in ("torch", "transformers", "numpy", "scipy", "einops", "safetensors", "torchdiffeq", "accelerate", "PyYAML", "huggingface_hub"):
        try:
            metadata.setdefault("package_versions", {})[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    try:
        metadata["repository_commit"] = subprocess.check_output(
            ["git", "-C", str(args.repo), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        metadata["repository_commit"] = None
    if args.stage == "generate":
        args.output_dir = args.output_dir or BASE / "outputs" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        args.output_dir.mkdir(parents=True, exist_ok=False)
        metadata_path = args.output_dir / "run_metadata.json"
    else:
        metadata_path = args.cache_path.with_suffix(".json")
    write_json(metadata_path, metadata)
    try:
        (encode if args.stage == "encode" else generate)(args, metadata)
        metadata["status"] = "completed"
    except Exception as error:
        metadata.update(status="failed", error_type=type(error).__name__, error=str(error), traceback=traceback.format_exc())
        raise
    finally:
        metadata["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        metadata["finished_utc"] = datetime.now(timezone.utc).isoformat()
        write_json(metadata_path, metadata)


if __name__ == "__main__":
    main()
