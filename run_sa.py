import warnings
warnings.filterwarnings('ignore')

from segment_anything_sa import sam_model_registry
from segment_anything_sa.utils import *

import albumentations as A

import torch
import torch.nn as nn 

import torch.distributed as dist
from torch.utils.data import DataLoader, BatchSampler
from torch.utils.data.distributed import DistributedSampler
from torch.nn.parallel import DistributedDataParallel

import os
import argparse
import wandb
import pickle 
from datetime import datetime
from torchinfo import summary

# if use single-gpu
#import os
#os.environ['CUDA_VISIBLE_DEVICES'] = '0' 

import os
os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'expandable_segments:True'


CHECKPOINT_DIR = 'checkpoints'
os.makedirs(CHECKPOINT_DIR, exist_ok=True)

# vanila sam param list
with open('sam_params.pkl', 'rb') as f:
    SAM_PARAMS = pickle.load(f)

def str2bool(v):
    if isinstance(v, bool):
        return v
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')

def get_args_parser():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--batch_size', type=int, default=1, help='batch size allocated to each GPU')
    parser.add_argument('--port', type=int, default=1234, help='port number for distributed learning')
    parser.add_argument('--dist', type=str2bool, default=True, help='if True, use multi-gpu(distributed) training')
    parser.add_argument('--seed', type=int, default=21, help='random seed')
    parser.add_argument('--model_type', type=str, default='vit_b', help='SAM model type')
    parser.add_argument('--checkpoint', type=str, default='sam_vit_b.pth', help='SAM model checkpoint')
    parser.add_argument('--epoch', type=int, default=10, help='total epoch')
    parser.add_argument('--lr', type=float, default=2e-4, help='initial learning rate')
    parser.add_argument('--project_name', type=str, default='Fine-tuning-SAM', help='WandB project name')
    parser.add_argument('--local_rank', type=int)
    parser.add_argument('--train_image_dir', type=str, default='dataset/train/image', help='train dataset image dir')
    parser.add_argument('--train_mask_dir', type=str, default='dataset/train/mask', help='train dataset mask dir')
    
    return parser

### Fine-tuning SAM ###
def main(rank, opts) -> str:
    """
    Model fine-tuning
    
    Returns:
        str: Save path of model checkpoint 
    """
    seed.seed_everything(opts.seed)
    
    set_dist.init_distributed_training(rank, opts)
    local_gpu_id = opts.gpu
    
    ### checkpoint & WandB set ### 
    run_time = datetime.now()
    run_time = run_time.strftime("%b%d_%H%M%S")
    file_name = run_time + '_sa.pth'
    save_path = os.path.join(CHECKPOINT_DIR, file_name)
    
    if opts.rank == 0:
        wandb.init(project=opts.project_name)
        wandb.run.name = run_time 

    ### dataset & dataloader ### 
    # data augmentation for train image & mask 
    transform = A.Compose([
        A.Resize(512, 512),
        A.OneOf([
            A.HorizontalFlip(p=1),
            A.VerticalFlip(p=1),
            A.RandomRotate90(p=1),
            A.ShiftScaleRotate(p=1)
        ], p=0.5)
    ])
    
    train_set = dataset.make_dataset(
        image_dir=opts.train_image_dir,
        mask_dir=opts.train_mask_dir,
        transform=transform
    )
    
    if opts.dist:
        train_sampler = DistributedSampler(dataset=train_set, shuffle=True, seed=opts.seed)
        batch_sampler_train = BatchSampler(train_sampler, opts.batch_size, drop_last=True)
        train_loader = DataLoader(train_set, batch_sampler=batch_sampler_train, num_workers=opts.num_workers)
    
    if not opts.dist:
        train_loader = DataLoader(
            train_set, 
            batch_size=opts.batch_size, 
            shuffle=True, 
            num_workers=opts.num_workers
        )

    ### SAM config ### 
    sam_checkpoint = opts.checkpoint
    model_type = opts.model_type
    
    # adapter config 
    adapter_config = {
        'task_specific_tune_out': 192, # = 768 / 4, r = 4 in EVP 
        'task_specific_adapter_hidden_dim': 32, 
        'task_specific_adapter_act_layer': nn.GELU,
        'task_specific_adapter_skip_connection': False, 
    }

    sam = sam_model_registry[model_type](adapter_config=adapter_config, checkpoint=sam_checkpoint)
    sam.cuda(local_gpu_id)
    
    # set trainable parameters
    for n, p in sam.named_parameters():
        # non-trainable params (image encoder & prompt encoder)
        if n in SAM_PARAMS: 
            p.requires_grad = False
        # trainable params (sam adapter)
        else:
            p.requires_grad = True

    # fine-tuning mask decoder         
    for _, p in sam.mask_decoder.named_parameters():
        p.requires_grad = True
    
    # print model info 
    print()
    print('=== MODEL INFO ===')
    summary(sam)
    print()

    if not opts.dist:
        model = sam
    
    if opts.dist:
        # if multi-gpu training, use SyncBatchNorm
        sam = DistributedDataParallel(module=sam, device_ids=[local_gpu_id])    
        model = sam.module

        
    ### training config ###  
    bceloss = nn.BCELoss().to(local_gpu_id)
    iouloss = iou_loss_torch.IoULoss().to(local_gpu_id)

    EPOCHS = opts.epoch
    lr = opts.lr
    optimizer = torch.optim.AdamW(
        model.parameters(), 
        lr=lr
    )
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, 
        T_max=len(train_loader), 
        eta_min=0,
        last_epoch=-1
    )

    if opts.rank == 0:
        wandb.watch(
            models=model,
            criterion=(bceloss, iouloss),
            log='all',
            log_freq=10
        )
    
        wandb.run.summary['optimizer'] = type(optimizer).__name__
        wandb.run.summary['scheduler'] = type(scheduler).__name__
        wandb.run.summary['initial lr'] = lr
        wandb.run.summary['total epoch'] = EPOCHS
    
    for epoch in range(EPOCHS):
        
        if opts.dist:
            train_sampler.set_epoch(epoch)
        
        ### model train ###
        train_bce_loss, train_iou_loss, train_dice, train_iou = trainer.model_train(
            model=model,
            data_loader=train_loader,
            criterion=[bceloss, iouloss],
            optimizer=optimizer,
            device=local_gpu_id,
            scheduler=scheduler
        )
        
        if opts.dist:
            dist.barrier()
            
            dist.all_reduce(train_bce_loss, op=dist.ReduceOp.SUM)            
            train_bce_loss = train_bce_loss.item() / dist.get_world_size()
            
            dist.all_reduce(train_iou_loss, op=dist.ReduceOp.SUM)            
            train_iou_loss = train_iou_loss.item() / dist.get_world_size()
            
            dist.all_reduce(train_dice, op=dist.ReduceOp.SUM)
            train_dice = train_dice.item() / dist.get_world_size()
            
            dist.all_reduce(train_iou, op=dist.ReduceOp.SUM)
            train_iou = train_iou.item() / dist.get_world_size()
            
        if not opts.dist:
            train_bce_loss = train_bce_loss.item()
            train_iou_loss = train_iou_loss.item()
            train_dice = train_dice.item()
            train_iou = train_iou.item()
        
        if opts.rank == 0:
            
            wandb.log(
                {
                    'Train BCE Loss': train_bce_loss,
                    'Train IoU Loss': train_iou_loss,
                    'Train Dice Metric': train_dice,
                    'Train IoU Metric': train_iou
                }, step=epoch+1
            )
            
            ### print current loss / metric ###
            print(f'epoch {epoch+1:02d}, bce_loss: {train_bce_loss:.5f}, iou_loss: {train_iou_loss:.5f}, dice: {train_dice:.5f}, iou: {train_iou:.5f}')

    ### save model ### 
    if opts.rank == 0:
        torch.save(model.state_dict(), save_path)
    
    print(f'Model checkpoint saved at: {save_path} \n') 
    
    return

if __name__ == '__main__': 

    wandb.login()
    
    parser = argparse.ArgumentParser('CAD-Comparison', parents=[get_args_parser()])
    opts = parser.parse_args() 
    
    if opts.dist:
        opts.ngpus_per_node = torch.cuda.device_count()
        opts.gpu_ids = list(range(opts.ngpus_per_node))
        #opts.num_workers = opts.ngpus_per_node * 4
        opts.num_workers = 0
        
        torch.multiprocessing.spawn(
            main,
            args=(opts,),
            nprocs=opts.ngpus_per_node,
            join=True
        )
        
    if not opts.dist:
        opts.ngpus_per_node = 1
        opts.gpu_ids = [0]
        opts.num_workers = 0
    
        main(rank=0, opts=opts)
    
    print('=== DONE === \n')    