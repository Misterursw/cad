#!/bin/bash

BATCH_SIZE=1
PORT=1234
DIST=True
SEED=21
MODEL_TYPE=vit_b
CHECKPOINT=/root/autodl-tmp/checkpoints/sam_vit_b_01ec64.pth
EPOCH=20
LR=2e-4
PROJECT_NAME=Conv_Adapter_Experiments
TRAIN_IMAGE_DIR=/root/autodl-tmp/isic2018/train/images
TRAIN_MASK_DIR=/root/autodl-tmp/isic2018/train/masks

python3 run_sa.py \
    --batch_size $BATCH_SIZE \
    --port $PORT \
    --dist $DIST \
    --seed $SEED \
    --model_type $MODEL_TYPE \
    --checkpoint $CHECKPOINT \
    --epoch $EPOCH \
    --lr $LR \
    --project_name $PROJECT_NAME \
    --train_image_dir $TRAIN_IMAGE_DIR \
    --train_mask_dir $TRAIN_MASK_DIR \

python3 run_cad.py \
    --batch_size $BATCH_SIZE \
    --port $PORT \
    --dist $DIST \
    --seed $SEED \
    --model_type $MODEL_TYPE \
    --checkpoint $CHECKPOINT \
    --epoch $EPOCH \
    --lr $LR \
    --project_name $PROJECT_NAME \
    --train_image_dir $TRAIN_IMAGE_DIR \
    --train_mask_dir $TRAIN_MASK_DIR \

