#!/usr/bin/env bash
# 单张训练图总像素上限（宽×高）；仅 max_total_pixels 模式使用，自行改成例如 $((2048*2048))
MAX_IMAGE_TOTAL_PIXELS=$((1024 * 1536))

# architecture：约 4000 张、~3MP 超清壁纸。decode 后先按 max_image_total_pixels 缩到约 3MP，再训时 crop 到 resolution。
# 等效 batch≈8（1×8 accum）时：max_train_steps=4000 → 约 3.2e4 次样本、每张图平均被抽 ~8 次；欠拟合可略增步数，过拟合/糊可减步数或略降 LR。
# python prepare_metadata.py --data-dir datasets/architecture_nofog_washed
# 
# python train_text_to_image_lora_sdxl.py \
#   --pretrained_model_name_or_path="/media/yuchuxi/Data/Servers/ComfyUI/models/checkpoints/illustriousXL_v01.safetensors" \
#   --train_data_dir="datasets/architecture_nofog_washed" \
#   --image_column="image" \
#   --caption_column="text" \
#   --output_dir="output/architecture_lora.$(date +%m%d%H)" \
#   --mixed_precision="fp16" \
#   --resolution=1536 \
#   --train_batch_size=1 \
#   --gradient_accumulation_steps=8 \
#   --learning_rate=1e-4 \
#   --rank=64 \
#   --lora_alpha=64 \
#   --unet_target_modules="\
# to_k,to_q,to_v,to_out.0,\
# conv_in,conv_out,\
# conv1,conv2,conv_shortcut,\
# downsamplers.0.conv,upsamplers.0.conv" \
#   --lr_scheduler="cosine" \
#   --lr_warmup_steps=100 \
#   --max_train_steps=1000 \
#   --checkpointing_steps=100 \
#   --validation_prompt="3girls, animal, animal ears, anklet, architecture, arms up, bamboo broom, bare legs, black hair, broom, cherry blossoms, cleaning, closed eyes, crossed legs, dango, day, detached sleeves, earrings, east asian architecture, flower, food, fox, fox ears, genshin impact, hair ornament, hakama, hakama skirt, interior, japanese clothes, jewelry, kitsune, long hair, looking at another, miko, multiple girls, obi, open mouth, petals, pink hair, purple flower, railing, red skirt, sash, scenery, sitting, skirt, stretching, sunlight, tacshojosora, tree, vision (genshin impact), wagashi, wide sleeves, wooden ceiling, wooden floor, wooden railing, yae miko, yawning" \
#   --num_validation_images=1 \
#   --validation_epochs=1 \
#   --seed=114514 \
#   --shuffle_tags \
#   --cache_latents_to_disk \
#   --latents_cache_dir="./latents_cache" \
#   --report_to="wandb" \
#   --text_encoder_target_modules="q_proj,v_proj" \
#   --train_text_encoder \
#   --text_encoder_lr=1e-5 \
#   --image_preprocess_mode=max_total_pixels \
#   --max_image_total_pixels=$((1536 * 1536)) \
#   --snr_gamma=5.0 \
#   --noise_offset=0.1 \
#   --multires_noise_iterations=8 \
#   --multires_noise_discount=0.3 \
#   --gradient_checkpointing \
#   --precompute_vae_latents \
#   #--priority_tags "sketch, low quality, nachoneko, gawr gura, nekoha shizuku, nachoneko, araka luto" \


python prepare_metadata.py --data-dir datasets/rossi_wash_tag_only --config '[{"subdir":"", "repeat":1},{"subdir":"../rossi_washed", "repeat":2}]'

python train_text_to_image_lora_sdxl.py \
  --pretrained_model_name_or_path="/media/yuchuxi/Data/Servers/ComfyUI/models/checkpoints/illustriousXL_v01.safetensors" \
  --train_data_dir="datasets/rossi_wash_tag_only" \
  --image_column="image" \
  --caption_column="text" \
  --output_dir="output/rossi_wash_tag_only_lora.$(date +%m%d%H)" \
  --mixed_precision="fp16" \
  --resolution=1024 \
  --train_batch_size=1 \
  --gradient_accumulation_steps=4 \
  --learning_rate=5e-4 \
  --rank=32 \
  --lora_alpha=32 \
  --unet_target_modules="to_k,to_q,to_v,to_out.0" \
  --lr_scheduler="cosine" \
  --lr_warmup_steps=50 \
  --max_train_steps=1000 \
  --checkpointing_steps=100 \
  --validation_prompt="rossi (arknights), 1girl, animal ear fluff, animal ears, apple, blonde hair, dress, food, fruit, holding, holding food, holding fruit, leaf, loong (loong blo), one eye closed, plant, red scarf, scarf, solo, tagme, white dress, wolf ears, wolf girl, yellow eyes" \
  --num_validation_images=1 \
  --validation_epochs=1 \
  --seed=114514 \
  --trigger_word="rossi (arknights)" \
  --shuffle_tags \
  --cache_latents_to_disk \
  --latents_cache_dir="./latents_cache" \
  --report_to="wandb" \
  --text_encoder_target_modules="q_proj,v_proj" \
  --train_text_encoder \
  --text_encoder_lr=1e-5 \
  --image_preprocess_mode=max_total_pixels \
  --max_image_total_pixels="${MAX_IMAGE_TOTAL_PIXELS}" \
  --gradient_checkpointing \
  --priority_tags "high contrast" \

exit 0

python prepare_metadata.py --data-dir datasets/guo582_washed
  
python train_text_to_image_lora_sdxl.py \
  --pretrained_model_name_or_path="/media/yuchuxi/Data/Servers/ComfyUI/models/checkpoints/illustriousXL_v01.safetensors" \
  --train_data_dir="datasets/guo582_washed" \
  --image_column="image" \
  --caption_column="text" \
  --output_dir="output/guo582_washed_lora.$(date +%m%d%H)" \
  --mixed_precision="fp16" \
  --resolution=1024 \
  --train_batch_size=1 \
  --gradient_accumulation_steps=4 \
  --learning_rate=2e-4 \
  --rank=24 \
  --use_dora \
  --lora_alpha=24 \
  --unet_target_modules="\
to_k,to_q,to_v,to_out.0,\
conv_in,conv_out,\
conv1,conv2,conv_shortcut,\
downsamplers.0.conv,upsamplers.0.conv" \
  --lr_scheduler="cosine" \
  --lr_warmup_steps=50 \
  --max_train_steps=2000 \
  --checkpointing_steps=100 \
  --validation_prompt="guo582, masterpiece (quality), 1girl, solo, fox ears, white hair, blue eyes, looking back, cat pose, sitting" \
  --num_validation_images=1 \
  --validation_epochs=1 \
  --seed=114514 \
  --trigger_word="guo582" \
  --shuffle_tags \
  --cache_latents_to_disk \
  --latents_cache_dir="./latents_cache" \
  --report_to="wandb" \
  --text_encoder_target_modules="q_proj,v_proj" \
  --train_text_encoder \
  --text_encoder_lr=1e-5 \
  --gradient_checkpointing \
  --priority_tags "sketch, close-up, low quality, bad hands, backlight, shadow, bad anatomy, dark" \

exit



python prepare_metadata.py --data-dir datasets/amashiro_natsuki

python train_text_to_image_lora_sdxl.py \
  --pretrained_model_name_or_path="/media/yuchuxi/Data/Servers/ComfyUI/models/checkpoints/illustriousXL_v01.safetensors" \
  --train_data_dir="datasets/amashiro_natsuki" \
  --image_column="image" \
  --caption_column="text" \
  --output_dir="output/amashiro_natsuki_lora.$(date +%m%d%H)" \
  --mixed_precision="fp16" \
  --resolution=1024 \
  --train_batch_size=1 \
  --gradient_accumulation_steps=4 \
  --learning_rate=2e-4 \
  --rank=24 \
  --use_dora \
  --lora_alpha=24 \
  --unet_target_modules="\
to_k,to_q,to_v,to_out.0,\
conv_in,conv_out,\
conv1,conv2,conv_shortcut,\
downsamplers.0.conv,upsamplers.0.conv" \
  --lr_scheduler="cosine" \
  --lr_warmup_steps=50 \
  --max_train_steps=2000 \
  --checkpointing_steps=100 \
  --validation_prompt="amashiro natsuki, masterpiece (quality), 1girl, solo, fox ears, white hair, blue eyes, looking back, cat pose, sitting" \
  --num_validation_images=1 \
  --validation_epochs=1 \
  --seed=114514 \
  --trigger_word="amashiro natsuki" \
  --shuffle_tags \
  --cache_latents_to_disk \
  --latents_cache_dir="./latents_cache" \
  --report_to="wandb" \
  --text_encoder_target_modules="q_proj,v_proj" \
  --train_text_encoder \
  --text_encoder_lr=1e-5 \
  --image_preprocess_mode=max_total_pixels \
  --max_image_total_pixels="${MAX_IMAGE_TOTAL_PIXELS}" \
  --gradient_checkpointing \
  --priority_tags "sketch, low quality, nachoneko, gawr gura, nekoha shizuku, nachoneko, araka luto" \




python prepare_metadata.py --data-dir datasets/zhao_washed_sub

python train_text_to_image_lora_sdxl.py \
  --pretrained_model_name_or_path="/media/yuchuxi/Data/Servers/ComfyUI/models/checkpoints/illustriousXL_v01.safetensors" \
  --train_data_dir="datasets/zhao_washed_sub" \
  --image_column="image" \
  --caption_column="text" \
  --output_dir="output/zhao_washed_lora.$(date +%m%d%H)" \
  --mixed_precision="fp16" \
  --resolution=1024 \
  --train_batch_size=1 \
  --gradient_accumulation_steps=4 \
  --learning_rate=2e-4 \
  --rank=24 \
  --use_dora \
  --lora_alpha=24 \
  --unet_target_modules="\
to_k,to_q,to_v,to_out.0,\
conv_in,conv_out,\
conv1,conv2,conv_shortcut,\
downsamplers.0.conv,upsamplers.0.conv" \
  --lr_scheduler="cosine" \
  --lr_warmup_steps=50 \
  --max_train_steps=2000 \
  --checkpointing_steps=100 \
  --validation_prompt="zhao (zenless zone zero), masterpiece (quality), 1girl, solo, fox ears, white hair, blue eyes, looking back, cat pose, sitting" \
  --num_validation_images=1 \
  --validation_epochs=1 \
  --seed=114514 \
  --trigger_word="zhao (zenless zone zero)" \
  --shuffle_tags \
  --cache_latents_to_disk \
  --latents_cache_dir="./latents_cache" \
  --report_to="wandb" \
  --text_encoder_target_modules="q_proj,v_proj" \
  --train_text_encoder \
  --text_encoder_lr=1e-5 \
  --image_preprocess_mode=max_total_pixels \
  --max_image_total_pixels="${MAX_IMAGE_TOTAL_PIXELS}" \
  --gradient_checkpointing \
  --priority_tags "sketch, close-up, low quality, bad hands, backlight, shadow, bad anatomy, dark, bad feet, western art style" \


python prepare_metadata.py --data-dir datasets/chen_bin_washed

python train_text_to_image_lora_sdxl.py \
  --pretrained_model_name_or_path="/media/yuchuxi/Data/Servers/ComfyUI/models/checkpoints/illustriousXL_v01.safetensors" \
  --train_data_dir="datasets/chen_bin_washed" \
  --image_column="image" \
  --caption_column="text" \
  --output_dir="output/chen_bin_washed_lora.$(date +%m%d%H)" \
  --mixed_precision="fp16" \
  --resolution=1024 \
  --train_batch_size=1 \
  --gradient_accumulation_steps=4 \
  --learning_rate=2e-4 \
  --rank=24 \
  --use_dora \
  --lora_alpha=24 \
  --unet_target_modules="\
to_k,to_q,to_v,to_out.0,\
conv_in,conv_out,\
conv1,conv2,conv_shortcut,\
downsamplers.0.conv,upsamplers.0.conv" \
  --lr_scheduler="cosine" \
  --lr_warmup_steps=50 \
  --max_train_steps=2000 \
  --checkpointing_steps=100 \
  --validation_prompt="chen bin, masterpiece (quality), 1girl, solo, fox ears, white hair, blue eyes, looking back, cat pose, sitting" \
  --num_validation_images=1 \
  --validation_epochs=1 \
  --seed=114514 \
  --trigger_word="chen bin" \
  --shuffle_tags \
  --cache_latents_to_disk \
  --latents_cache_dir="./latents_cache" \
  --report_to="wandb" \
  --text_encoder_target_modules="q_proj,v_proj" \
  --train_text_encoder \
  --text_encoder_lr=1e-5 \
  --image_preprocess_mode=max_total_pixels \
  --max_image_total_pixels="${MAX_IMAGE_TOTAL_PIXELS}" \
  --gradient_checkpointing \
  --priority_tags "sketch, close-up, low quality, bad hands, backlight, shadow, bad anatomy, dark, green theme, pink theme, blue theme, yellow theme, orange theme, purple theme, red theme" \

