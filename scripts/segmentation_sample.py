"""
Generate a large batch of image samples from a model and save them as a large
numpy array. This can be used to produce samples for FID evaluation.
"""

import argparse
import os
import nibabel as nib
from visdom import Visdom
viz = Visdom(port=8850)
import sys
import random
sys.path.append(".")
import numpy as np
import time
import torch as th
from PIL import Image
import torch.distributed as dist
from guided_diffusion import dist_util, logger
from guided_diffusion.bratsloader import BRATSDataset
from guided_diffusion.isicloader import ISICDataset
import torchvision.utils as vutils
from guided_diffusion.utils import staple
from guided_diffusion.script_util import (
    NUM_CLASSES,
    model_and_diffusion_defaults,
    create_model_and_diffusion,
    add_dict_to_argparser,
    args_to_dict,
)
import torchvision.transforms as transforms
from torchsummary import summary
seed=10
th.manual_seed(seed)
th.cuda.manual_seed_all(seed)
np.random.seed(seed)
random.seed(seed)

def visualize(img):
    _min = img.min()
    _max = img.max()
    normalized_img = (img - _min)/ (_max - _min)
    return normalized_img


def main():
    args = create_argparser().parse_args()
    dist_util.setup_dist(args)
    logger.configure(dir = args.out_dir)

    logger.log("creating model and diffusion...")

    model, diffusion = create_model_and_diffusion(
        **args_to_dict(args, model_and_diffusion_defaults().keys())
    )

    transform_test = transforms.Compose([
    transforms.Resize((args.image_size,args.image_size)),
    # transforms.RandomCrop((img_size, img_size)),  # padding=10
    # transforms.RandomHorizontalFlip(),
    # transforms.RandomRotation(10, resample=PIL.Image.BILINEAR),
    transforms.ToTensor(),
    ])

    ds = ISICDataset(args, args.data_dir, transform_test, transform_test, mode = 'Test')
    datal = th.utils.data.DataLoader(
        ds,
        batch_size=1,
        shuffle=False)
    data = iter(datal)
    all_images = []

    # summary(model.to(dist_util.dev()), [(4, 256, 256),(1,)])
    # for k,v in th.load("./res-1119/savedmodel015000.pt").items():
    #     print(k,'\n',v.size())

    # for name, param in model.named_parameters():
    #     if param.requires_grad:
    #         print(name, param.data.size())

    model.load_state_dict(
        dist_util.load_state_dict(args.model_path, map_location="cpu")
        # th.load("./res-1119/savedmodel015000.pt")
    )
    model.to(dist_util.dev())
    if args.use_fp16:
        model.convert_to_fp16()
    model.eval()
    while len(all_images) * args.batch_size < args.num_samples:
        b, m, path = next(data)  #should return an image from the dataloader "data"
        c = th.randn_like(b[:, :1, ...])
        img = th.cat((b, c), dim=1)     #add a noise channel$
        # img = b
        slice_ID=path[0].split("_")[-1].split('.')[0]

        # viz.image(visualize(img[0,0,...]), opts=dict(caption="img input0"))
        # viz.image(visualize(img[0, 1, ...]), opts=dict(caption="img input1"))
        # viz.image(visualize(img[0, 2, ...]), opts=dict(caption="img input2"))
        # viz.image(visualize(img[0, 3, ...]), opts=dict(caption="img input3"))
        # viz.image(visualize(img[0, 4, ...]), opts=dict(caption="img input4"))

        logger.log("sampling...")

        start = th.cuda.Event(enable_timing=True)
        end = th.cuda.Event(enable_timing=True)
        enslist = []

        for i in range(args.num_ensemble):  #this is for the generation of an ensemble of 5 masks.
            model_kwargs = {}
            start.record()
            sample_fn = (
                diffusion.p_sample_loop_known if not args.use_ddim else diffusion.ddim_sample_loop_known
            )
            sample, x_noisy, org, cal, cal_out = sample_fn(
                model,
                (args.batch_size, 3, args.image_size, args.image_size), img,
                clip_denoised=args.clip_denoised, 
                model_kwargs=model_kwargs,
            )
            print('cal size', cal.size())
            print('cal_out size',cal_out.size())

            end.record()
            th.cuda.synchronize()
            print('time for 1 sample', start.elapsed_time(end))  #time measurement for the generation of 1 sample

            s = th.tensor(sample)[:,-1,:,:].unsqueeze(1)
            s = th.cat((s,s,s),1)
            # n = th.tensor(x_noisy)[:,:-1,:,:]
            o = th.tensor(org)[:,:-1,:,:]
            o = th.cat((o,o,o),1)
            c = th.tensor(cal)
            co = th.tensor(cal_out)
            c = th.cat((c,c,c),1)
            co = th.cat((co,co,co),1)
            print('sample size is', s.size())
            enslist.append(co)
            # viz.image(visualize(sample[0, 0, ...]), opts=dict(caption="sampled output"))
            # export(s, './results/'+str(slice_ID)+'_output'+str(i)+".jpg")
            # th.save(s, './results/'+str(slice_ID)+'_output'+str(i)) #save the generated mask
            tup = (s,o,c,co)
            # compose = torch.cat((imgs[:row_num,:,:,:],pred_disc[:row_num,:,:,:], pred_cup[:row_num,:,:,:], gt_disc[:row_num,:,:,:], gt_cup[:row_num,:,:,:]),0)
            compose = th.cat(tup,0)
            vutils.save_image(compose, fp = args.out_dir +str(slice_ID)+'_output'+str(i)+".jpg", nrow = 1, padding = 10)
        ensres = staple(th.stack(enslist,dim=0))
        vutils.save_image(ensres, fp = args.out_dir +str(slice_ID)+'_output'+'_ens'+".jpg", nrow = 1, padding = 10)

def create_argparser():
    defaults = dict(
        data_dir="./data/testing",
        clip_denoised=True,
        num_samples=1,
        batch_size=1,
        use_ddim=False,
        model_path="",
        num_ensemble=5,      #number of samples in the ensemble
        gpu_dev = "0",
        out_dir='./res-ind-ens-1123/'
    )
    defaults.update(model_and_diffusion_defaults())
    parser = argparse.ArgumentParser()
    add_dict_to_argparser(parser, defaults)
    return parser


if __name__ == "__main__":

    main()