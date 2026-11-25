"""
FramePack F1 — 이미지 + 프롬프트 → 동영상 생성 스크립트

사용법:
    uv run python -m src.framepack_generate \
        --image path/to/image.jpg \
        --prompt "A person walking slowly" \
        --output output/framepack_test.mp4 \
        --seconds 5

FramePack 레포가 ./FramePack/ 에 클론되어 있어야 합니다.
"""

import os
import sys

# FramePack diffusers_helper 경로 추가
FRAMEPACK_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "FramePack")
if FRAMEPACK_DIR not in sys.path:
    sys.path.insert(0, FRAMEPACK_DIR)

os.environ["HF_HOME"] = os.path.join(FRAMEPACK_DIR, "hf_download")
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"

import argparse
import gc
import numpy as np
import torch
from PIL import Image

from diffusers import AutoencoderKLHunyuanVideo
from transformers import (
    LlamaModel,
    CLIPTextModel,
    LlamaTokenizerFast,
    CLIPTokenizer,
    SiglipImageProcessor,
    SiglipVisionModel,
)
from diffusers_helper.hunyuan import encode_prompt_conds, vae_decode, vae_encode
from diffusers_helper.utils import (
    save_bcthw_as_mp4,
    crop_or_pad_yield_mask,
    soft_append_bcthw,
    resize_and_center_crop,
    generate_timestamp,
)
from diffusers_helper.models.hunyuan_video_packed import HunyuanVideoTransformer3DModelPacked
from diffusers_helper.pipelines.k_diffusion_hunyuan import sample_hunyuan
from diffusers_helper.memory import (
    cpu, gpu,
    DynamicSwapInstaller,
    unload_complete_models,
    load_model_as_complete,
    move_model_to_device_with_memory_preservation,
    offload_model_from_device_for_memory_preservation,
    get_cuda_free_memory_gb,
)
from diffusers_helper.clip_vision import hf_clip_vision_encode
from diffusers_helper.bucket_tools import find_nearest_bucket


def detect_device():
    if torch.cuda.is_available():
        return torch.device("cuda"), get_cuda_free_memory_gb(gpu)
    elif torch.backends.mps.is_available():
        free_gb = torch.mps.recommended_max_memory() / 1024 ** 3
        return torch.device("mps"), free_gb
    else:
        return torch.device("cpu"), 64.0  # CPU는 high_vram 모드로 처리


def load_models(device, high_vram):
    print("모델 로딩 중... (최초 실행 시 HuggingFace에서 다운로드, 약 30GB)")

    text_encoder = LlamaModel.from_pretrained(
        "hunyuanvideo-community/HunyuanVideo", subfolder="text_encoder", torch_dtype=torch.float16
    ).cpu()
    text_encoder_2 = CLIPTextModel.from_pretrained(
        "hunyuanvideo-community/HunyuanVideo", subfolder="text_encoder_2", torch_dtype=torch.float16
    ).cpu()
    tokenizer = LlamaTokenizerFast.from_pretrained(
        "hunyuanvideo-community/HunyuanVideo", subfolder="tokenizer"
    )
    tokenizer_2 = CLIPTokenizer.from_pretrained(
        "hunyuanvideo-community/HunyuanVideo", subfolder="tokenizer_2"
    )
    vae = AutoencoderKLHunyuanVideo.from_pretrained(
        "hunyuanvideo-community/HunyuanVideo", subfolder="vae", torch_dtype=torch.float16
    ).cpu()

    feature_extractor = SiglipImageProcessor.from_pretrained(
        "lllyasviel/flux_redux_bfl", subfolder="feature_extractor"
    )
    image_encoder = SiglipVisionModel.from_pretrained(
        "lllyasviel/flux_redux_bfl", subfolder="image_encoder", torch_dtype=torch.float16
    ).cpu()

    transformer = HunyuanVideoTransformer3DModelPacked.from_pretrained(
        "lllyasviel/FramePack_F1_I2V_HY_20250503", torch_dtype=torch.bfloat16
    ).cpu()

    # eval 모드 설정
    for model in [text_encoder, text_encoder_2, image_encoder, vae, transformer]:
        model.eval()
        model.requires_grad_(False)

    transformer.high_quality_fp32_output_for_inference = False

    vae.to(dtype=torch.float16)
    image_encoder.to(dtype=torch.float16)
    text_encoder.to(dtype=torch.float16)
    text_encoder_2.to(dtype=torch.float16)
    transformer.to(dtype=torch.bfloat16)

    if not high_vram:
        vae.enable_slicing()
        vae.enable_tiling()
        DynamicSwapInstaller.install_model(text_encoder, device=device)
        DynamicSwapInstaller.install_model(transformer, device=device)
    else:
        text_encoder.to(device)
        text_encoder_2.to(device)
        image_encoder.to(device)
        vae.to(device)
        transformer.to(device)

    print("모델 로딩 완료")
    return text_encoder, text_encoder_2, tokenizer, tokenizer_2, vae, feature_extractor, image_encoder, transformer


@torch.no_grad()
def generate(
    image_path: str,
    prompt: str,
    output_path: str,
    seconds: float = 5.0,
    steps: int = 25,
    seed: int = 31337,
    resolution: int = 416,
    gs: float = 10.0,
    latent_window_size: int = 9,
    mp4_crf: int = 16,
):
    device, free_gb = detect_device()
    high_vram = free_gb > 60
    print(f"디바이스: {device} | 가용 메모리: {free_gb:.1f}GB | high_vram: {high_vram}")

    (
        text_encoder, text_encoder_2, tokenizer, tokenizer_2,
        vae, feature_extractor, image_encoder, transformer
    ) = load_models(device, high_vram)

    transformer_dtype = torch.bfloat16

    # --- 텍스트 인코딩 ---
    print("텍스트 인코딩 중...")
    if not high_vram:
        from diffusers_helper.memory import fake_diffusers_current_device
        fake_diffusers_current_device(text_encoder, device)
        load_model_as_complete(text_encoder_2, target_device=device)

    llama_vec, clip_l_pooler = encode_prompt_conds(
        prompt, text_encoder, text_encoder_2, tokenizer, tokenizer_2
    )
    llama_vec_n, clip_l_pooler_n = torch.zeros_like(llama_vec), torch.zeros_like(clip_l_pooler)
    llama_vec, llama_attention_mask = crop_or_pad_yield_mask(llama_vec, length=512)
    llama_vec_n, llama_attention_mask_n = crop_or_pad_yield_mask(llama_vec_n, length=512)

    # --- 이미지 전처리 ---
    print("이미지 전처리 중...")
    input_image_np = np.array(Image.open(image_path).convert("RGB"))
    H, W, _ = input_image_np.shape
    height, width = find_nearest_bucket(H, W, resolution=resolution)
    input_image_np = resize_and_center_crop(input_image_np, target_width=width, target_height=height)

    input_image_pt = torch.from_numpy(input_image_np).float() / 127.5 - 1
    input_image_pt = input_image_pt.permute(2, 0, 1)[None, :, None]

    # --- VAE 인코딩 ---
    print("VAE 인코딩 중...")
    if not high_vram:
        load_model_as_complete(vae, target_device=device)
    start_latent = vae_encode(input_image_pt, vae)

    # --- CLIP Vision 인코딩 ---
    print("CLIP Vision 인코딩 중...")
    if not high_vram:
        load_model_as_complete(image_encoder, target_device=device)
    image_encoder_output = hf_clip_vision_encode(input_image_np, feature_extractor, image_encoder)
    image_encoder_last_hidden_state = image_encoder_output.last_hidden_state

    # dtype 통일
    llama_vec = llama_vec.to(transformer_dtype)
    llama_vec_n = llama_vec_n.to(transformer_dtype)
    clip_l_pooler = clip_l_pooler.to(transformer_dtype)
    clip_l_pooler_n = clip_l_pooler_n.to(transformer_dtype)
    image_encoder_last_hidden_state = image_encoder_last_hidden_state.to(transformer_dtype)

    # --- 샘플링 루프 ---
    total_latent_sections = int(max(round((seconds * 24) / (latent_window_size * 4)), 1))
    print(f"생성 시작 — {total_latent_sections}개 섹션 × {latent_window_size} window")

    rnd = torch.Generator("cpu").manual_seed(seed)
    history_latents = torch.zeros(
        size=(1, 16, 16 + 2 + 1, height // 8, width // 8), dtype=torch.float32
    ).cpu()
    history_pixels = None
    history_latents = torch.cat([history_latents, start_latent.to(history_latents)], dim=2)
    total_generated_latent_frames = 1

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    job_id = generate_timestamp()

    for section_index in range(total_latent_sections):
        print(f"  섹션 {section_index + 1}/{total_latent_sections} 샘플링 중...")

        if not high_vram:
            unload_complete_models()
            move_model_to_device_with_memory_preservation(
                transformer, target_device=device, preserved_memory_gb=6
            )

        transformer.initialize_teacache(enable_teacache=True, num_steps=steps)

        indices = torch.arange(0, sum([1, 16, 2, 1, latent_window_size])).unsqueeze(0)
        clean_latent_indices_start, clean_latent_4x_indices, clean_latent_2x_indices, clean_latent_1x_indices, latent_indices = indices.split(
            [1, 16, 2, 1, latent_window_size], dim=1
        )
        clean_latent_indices = torch.cat([clean_latent_indices_start, clean_latent_1x_indices], dim=1)

        clean_latents_4x, clean_latents_2x, clean_latents_1x = history_latents[
            :, :, -sum([16, 2, 1]):, :, :
        ].split([16, 2, 1], dim=2)
        clean_latents = torch.cat([start_latent.to(history_latents), clean_latents_1x], dim=2)

        generated_latents = sample_hunyuan(
            transformer=transformer,
            sampler="unipc",
            width=width,
            height=height,
            frames=latent_window_size * 4 - 3,
            real_guidance_scale=1.0,
            distilled_guidance_scale=gs,
            guidance_rescale=0.0,
            num_inference_steps=steps,
            generator=rnd,
            prompt_embeds=llama_vec,
            prompt_embeds_mask=llama_attention_mask,
            prompt_poolers=clip_l_pooler,
            negative_prompt_embeds=llama_vec_n,
            negative_prompt_embeds_mask=llama_attention_mask_n,
            negative_prompt_poolers=clip_l_pooler_n,
            device=device,
            dtype=transformer_dtype,
            image_embeddings=image_encoder_last_hidden_state,
            latent_indices=latent_indices,
            clean_latents=clean_latents,
            clean_latent_indices=clean_latent_indices,
            clean_latents_2x=clean_latents_2x,
            clean_latent_2x_indices=clean_latent_2x_indices,
            clean_latents_4x=clean_latents_4x,
            clean_latent_4x_indices=clean_latent_4x_indices,
        )

        total_generated_latent_frames += int(generated_latents.shape[2])
        history_latents = torch.cat([history_latents, generated_latents.to(history_latents)], dim=2)

        if not high_vram:
            offload_model_from_device_for_memory_preservation(
                transformer, target_device=device, preserved_memory_gb=8
            )
            load_model_as_complete(vae, target_device=device)

        real_history_latents = history_latents[:, :, -total_generated_latent_frames:, :, :]

        if history_pixels is None:
            history_pixels = vae_decode(real_history_latents, vae).cpu()
        else:
            section_latent_frames = latent_window_size * 2
            overlapped_frames = latent_window_size * 4 - 3
            current_pixels = vae_decode(real_history_latents[:, :, -section_latent_frames:], vae).cpu()
            history_pixels = soft_append_bcthw(history_pixels, current_pixels, overlapped_frames)

        if not high_vram:
            unload_complete_models()

        # 섹션마다 중간 결과 저장
        save_bcthw_as_mp4(history_pixels, output_path, fps=24, crf=mp4_crf)
        video_seconds = max(0, (total_generated_latent_frames * 4 - 3) / 24)
        print(f"  → 현재까지 {video_seconds:.2f}초 생성 완료, 저장: {output_path}")

    print(f"\n생성 완료: {output_path}")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="FramePack F1 — 이미지 → 동영상 생성")
    parser.add_argument("--image", required=True, help="입력 이미지 경로")
    parser.add_argument("--prompt", required=True, help="생성 프롬프트")
    parser.add_argument("--output", default="output/framepack_output.mp4", help="출력 MP4 경로")
    parser.add_argument("--seconds", type=float, default=5.0, help="생성할 영상 길이 (초, 기본 5)")
    parser.add_argument("--steps", type=int, default=25, help="샘플링 스텝 수 (기본 25)")
    parser.add_argument("--seed", type=int, default=31337, help="랜덤 시드")
    parser.add_argument("--resolution", type=int, default=416, help="해상도 버킷 기준값 (기본 416)")
    parser.add_argument("--gs", type=float, default=10.0, help="Distilled CFG Scale (기본 10.0)")
    args = parser.parse_args()

    generate(
        image_path=args.image,
        prompt=args.prompt,
        output_path=args.output,
        seconds=args.seconds,
        steps=args.steps,
        seed=args.seed,
        resolution=args.resolution,
        gs=args.gs,
    )


if __name__ == "__main__":
    main()
