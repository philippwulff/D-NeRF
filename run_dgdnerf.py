import os
import imageio
import time
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm, trange
import math
import json
import pickle
from collections import defaultdict
import configargparse
import cv2

from run_dgdnerf_helpers import *
from utils import * # load_blender_data, load_deepdeform_data, load_owndataset_data, 

try:
    from apex import amp            
except ImportError:
    pass

import torch.nn as nn

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")           
if device.type == "cuda":
    GPU_INDEX = os.environ.get('CUDA_VISIBLE_DEVICES', "None")
    print(f"[Import] Using CUDA version {torch.version.cuda} on {torch.cuda.get_device_name(device.index).strip()} with global GPU index {GPU_INDEX}")

torch.manual_seed(0)
np.random.seed(0)
DEBUG = True


def batchify(fn, chunk):
    """Constructs a version of 'fn' that applies to smaller batches.
    
    Args:
        fn: network forward method.
        chunk: number of pts sent through network in parallel.
    Returns:
        Function that applies the network 'fn' to chunks of position and time outputs. 
    """
    if chunk is None:
        return fn
    def ret(inputs_pos, inputs_time):
        num_batches = inputs_pos.shape[0]

        out_list = []
        dx_list = []
        for i in range(0, num_batches, chunk):
            # inputs_time[0] and inputs_time[1] are the same
            out, dx = fn(inputs_pos[i:i+chunk], [inputs_time[0][i:i+chunk], inputs_time[1][i:i+chunk]])     
            out_list += [out]
            dx_list += [dx]
        return torch.cat(out_list, 0), torch.cat(dx_list, 0)
    return ret


def run_network(inputs, viewdirs, frame_time, fn, embed_fn, embeddirs_fn, embedtime_fn, netchunk=1024*64,
                embd_time_discr=True, use_latent_codes_as_time=False):
    """Prepares inputs and applies network 'fn'.
    inputs (Tensor): N_rays x N_points_per_ray x 3   
    viewdirs (Tensor): N_rays x 3                
    frame_time (Tensor): (N_rays x 1) if direct time or (N_rays, ray_bending_latent_size) if latent codes for deformation network
    fn (function): network forward function
    ray_bending_latents (Tensor or None): .
    """
    if not use_latent_codes_as_time:
        assert len(torch.unique(frame_time)) == 1, "Only accepts all points from same time"
        cur_time = torch.unique(frame_time)[0]

    # embed position
    inputs_flat = torch.reshape(inputs, [-1, inputs.shape[-1]])             # shape: -1, 3
    embedded = embed_fn(inputs_flat)

    # embed views
    if viewdirs is not None:
        input_dirs = viewdirs[:,None].expand(inputs.shape)
        input_dirs_flat = torch.reshape(input_dirs, [-1, input_dirs.shape[-1]]) # shape: -1, 3 
        embedded_dirs = embeddirs_fn(input_dirs_flat)
        embedded = torch.cat([embedded, embedded_dirs], -1)

    if use_latent_codes_as_time:        # latent codes instead of time
        ray_bending_latents = frame_time[:, None].expand((inputs.shape[0], inputs.shape[1], frame_time.shape[-1]))  
        # N_rays x N_samples_per_ray x latent_size
        ray_bending_latents = torch.reshape(ray_bending_latents, [-1, ray_bending_latents.shape[-1]])  
        # N_rays * N_samples_per_ray x latent_size
        embedded_times = [ray_bending_latents, ray_bending_latents]
    elif embd_time_discr:
        # embed time
        B, N, _ = inputs.shape      
        input_frame_time = frame_time[:, None].expand([B, N, 1])
        input_frame_time_flat = torch.reshape(input_frame_time, [-1, 1])    # shape: -1, 1
        embedded_time = embedtime_fn(input_frame_time_flat)
        embedded_times = [embedded_time, embedded_time]
    else:
        assert NotImplementedError

    outputs_flat, position_delta_flat = batchify(fn, netchunk)(embedded, embedded_times)
    outputs = torch.reshape(outputs_flat, list(inputs.shape[:-1]) + [outputs_flat.shape[-1]])   
    position_delta = torch.reshape(position_delta_flat, list(inputs.shape[:-1]) + [position_delta_flat.shape[-1]])
    # shape: N_rays x N_points_per_ray x net_output_len
    return outputs, position_delta


def batchify_rays(rays_flat, chunk=1024*32, H=None, W=None, ray_bending_latent_codes=None, **kwargs):
    """Render rays in smaller minibatches to avoid OOM errors.

    Args:
        rays_flat: [batch_size, 9] or [batch_size, 12] or [batch_size, 15]. all ray directions from a camera.
        chunk: int. The max number of rays to process in parallel. Defaults to 1024*32.
    Returns:
        all_ret: dict.  
        all_ray_debug: dict for debugging rays
    """
    all_ret = defaultdict(list)
    all_ray_debug = defaultdict(list)
    for i in range(0, rays_flat.shape[0], chunk):
        relevant_rblc = ray_bending_latent_codes[i:i+chunk, :] if ray_bending_latent_codes is not None else None
        ret, ray_debug = render_rays(rays_flat[i:i+chunk], ray_bending_latent_codes=relevant_rblc, **kwargs)
        for k in ret:
            all_ret[k].append(ret[k])
        if ray_debug:
            for k in ray_debug:
                all_ray_debug[k].append(ray_debug[k])

    all_ret = {k : torch.cat(all_ret[k], 0) for k in all_ret}
    if all_ray_debug:
        all_ray_debug = {k : torch.cat(all_ray_debug[k], 0).reshape(H, W, -1).cpu() for k in all_ray_debug}
    return all_ret, all_ray_debug


def render(H, W, focal_x, focal_y, chunk=1024*32, rays=None, c2w=None, ndc=True,
           near=0., far=1., frame_time=None, use_latent_codes_as_time=False,
           c2w_staticcam=None, ray_debug_path="", **kwargs):
    """Calls methods to render the given rays.

    Args:
        H: int. Height of image in pixels.
        W: int. Width of image in pixels.
        focal_x: float. Focal length in X direction.
        focal_y: float. Focal length in Y direction.
        chunk: int. Maximum number of rays to process simultaneously. Used to
            control maximum memory usage. Does not affect final results.
        rays: array of shape [2, batch_size, 3]. Ray origin and direction for
            each example in batch.
        c2w: array of shape [3, 4]. Camera-to-world transformation matrix. 
            Horizontal stack of the rotation matrix an the translation vector.
        ndc: bool. If True, represent ray origin, direction in normalized device coordinates (NDC).
        near: float or array of shape [batch_size]. Nearest distance for a ray.
        far: float or array of shape [batch_size]. Farthest distance for a ray.
        use_viewdirs: bool. If True, use viewing direction of a point in space in model.
        c2w_staticcam: array of shape [3, 4]. If not None, use this transformation matrix for 
            camera while using other c2w argument for viewing directions.

    Returns:
        rgb_map: [batch_size, 3]. Predicted RGB values for rays.
        disp_map: [batch_size]. Disparity map. Inverse of depth.
        acc_map: [batch_size]. Accumulated opacity (alpha) along a ray.
        depth_map: [batch_size]. Predicted depth of pixel.
        extras: dict with everything returned by render_rays().
    """
    rays_depth = None
    if c2w is not None:
        # special case to render full image
        rays_o, rays_d = get_rays(H, W, focal_x, focal_y, c2w)
    elif rays.shape[0] == 2:
        # use provided ray batch
        rays_o, rays_d = rays
    else:
        rays_o, rays_d, rays_depth = rays

    if kwargs["use_viewdirs"]:                            # this is set to True when using full 5D input to the canonical model
        # provide ray directions as input
        viewdirs = rays_d
        if c2w_staticcam is not None:           # the code which sets c2w_staticcam to True is commented out in the official D-NeRF code
            # special case to visualize effect of viewdirs
            rays_o, rays_d = get_rays(H, W, focal_x, focal_y, c2w_staticcam)
        viewdirs = viewdirs / torch.norm(viewdirs, dim=-1, keepdim=True)
        viewdirs = torch.reshape(viewdirs, [-1,3]).float()

    sh = rays_d.shape # [..., 3]
    if ndc:
        # for forward facing scenes
        rays_o, rays_d = ndc_rays(H, W, focal_x, 1., rays_o, rays_d)        # UNEDITED!

    # Create ray batch
    rays_o = torch.reshape(rays_o, [-1,3]).float()
    rays_d = torch.reshape(rays_d, [-1,3]).float()

    near, far = near * torch.ones_like(rays_d[...,:1]), far * torch.ones_like(rays_d[...,:1])   # [batch_size * 1] each
    frame_time = frame_time * torch.ones_like(rays_d[...,:1])                                   # [batch_size * 1]
    rays = torch.cat([rays_o, rays_d, near, far, frame_time], -1)                               # [batch_size * 9]
    if kwargs["use_viewdirs"]:
        rays = torch.cat([rays, viewdirs], -1)          # [batch_size * 12]
    if rays_depth is not None:
        rays_depth = torch.reshape(rays_depth, [-1,3]).float()
        rays = torch.cat([rays, rays_depth], -1)        # [batch_size * 12] or [batch_size * 15]

    # Render and reshape
    all_ret, all_ray_debug = batchify_rays(rays, chunk, H, W, 
                                           ray_bending_latent_codes=frame_time if use_latent_codes_as_time else None, 
                                           **kwargs)       
    for k in all_ret:
        k_sh = list(sh[:-1]) + list(all_ret[k].shape[1:])
        all_ret[k] = torch.reshape(all_ret[k], k_sh)

    # Rest is logging and packaging
    if all_ray_debug:
        all_ray_debug["c2w"] = c2w.cpu()
        all_ray_debug["near"] = near[0, 0].item()
        all_ray_debug["far"] = far[0, 0].item()
        with open(ray_debug_path + "ray_debug.pickle", "wb") as f:
            pickle.dump(all_ray_debug, f, pickle.HIGHEST_PROTOCOL)

    k_extract = ['rgb_map', 'disp_map', 'acc_map', 'depth_map']
    ret_list = [all_ret[k] for k in k_extract]
    ret_dict = {k : all_ret[k] for k in all_ret if k not in k_extract}
    return ret_list + [ret_dict]


def render_path(render_poses, render_times, hwff, chunk, render_kwargs, gt_imgs=None, gt_depths=None, savedir=None,
                render_factor=0, save_also_gt=False, i_offset=0, ray_bending_latent_codes=None, expname=None, iteration=None):
    """Render images at the given camera poses and times.
    Args:
        render_poses: array of poses to be rendered.
        render_times: array of corresponding times to be rendered.
        hwff: (height, width, focal length in x, focal length in y)
        chunk: Max num of points per batch to avoid memory issues.
        render_kwargs: dict which contains train/test objects such as the run_network function.
        gt_imgs: list of ground truth images. Defaults to None.
        i_offset: int. . Defaults to 0.
    Returns:
        _type_: _description_
    """

    H, W, focal_x, focal_y = hwff

    if render_factor!=0:
        # Render downsampled for speed
        H = H//render_factor
        W = W//render_factor
        focal_x = focal_x/render_factor
        focal_y = focal_y/render_factor

    if savedir is not None:
        save_dir_estim = os.path.join(savedir, "estim")
        save_dir_gt = os.path.join(savedir, "gt")
        save_dir_estim_depth = os.path.join(savedir, "estim_depth")
        save_dir_gt_depth = os.path.join(savedir, "gt_depth")
        if not os.path.exists(save_dir_estim):
            os.makedirs(save_dir_estim)
        if save_also_gt and not os.path.exists(save_dir_gt):
            os.makedirs(save_dir_gt)
        if not os.path.exists(save_dir_estim_depth):
            os.makedirs(save_dir_estim_depth)
        if save_also_gt and not os.path.exists(save_dir_gt_depth):
            os.makedirs(save_dir_gt_depth)

    rgbs = []
    rgbs_gt = []
    disps = []
    depths = []
    depths_gt = []

    for i, (c2w, frame_time) in enumerate(zip(tqdm(render_poses), render_times)):
        rgb, disp, acc, depth, _ = render(H, W, focal_x, focal_y, chunk=chunk, c2w=c2w[:3,:4], frame_time=frame_time, **render_kwargs)
        
        rgb = torch.clamp(rgb,0,1)
        rgbs.append(rgb.cpu().numpy())
        disps.append(disp.cpu().numpy())
        depths.append(depth.cpu().numpy())

        if savedir is not None:
            rgb8_estim = to8b(rgbs[-1])
            filename = os.path.join(save_dir_estim, '{:03d}.png'.format(i+i_offset))
            imageio.imwrite(filename, rgb8_estim)

            depth_estim = to8d(depths[-1])
            filename = os.path.join(save_dir_estim_depth, '{:03d}.png'.format(i+i_offset))
            imageio.imwrite(filename, depth_estim)

        if save_also_gt:
            rgb_gt = gt_imgs[i]
            rgb_gt = np.clip(rgb_gt,0,1)
            depth_gt = gt_depths[i].squeeze()
            rgbs_gt.append(rgb_gt)
            depths_gt.append(depth_gt)

            rgb8_gt = to8b(rgb_gt)
            filename = os.path.join(save_dir_gt, '{:03d}.png'.format(i+i_offset))
            imageio.imwrite(filename, rgb8_gt)

            depth_gt = to8d(depth_gt)
            filename = os.path.join(save_dir_gt_depth, '{:03d}.png'.format(i+i_offset))
            imageio.imwrite(filename, depth_gt)

    rgbs = np.stack(rgbs)
    depths = np.stack(depths)
    disps = np.stack(disps)

    if save_also_gt:
        rgbs_gt = np.stack(rgbs_gt)
        depth_gt = np.stack(depths_gt)
        files_dir = "./logs/" + expname + "/" + 'renderonly_test_{:06d}'.format(iteration)
        compute_metrics(files_dir, rgbs, rgbs_gt, depths, depths_gt)

    return rgbs, disps, depths


def create_nerf(args, autodecoder_variables=None):
    """Instantiate NeRF's MLP model.
    Args:
        args (dict): Model arguments.
        autodecoder_variables (Tensor): Learnable latent vectors as input for deformation network. 
    Returns:
        render_kwargs_train: dict for training configuration.
        render_kwargs_test: dict for test configuration.
        start (int): training step.
        grad_vars: learnable parameters.
        optimizer: Adam optim.
    """
    grad_vars = []
    if autodecoder_variables is not None:
        grad_vars += autodecoder_variables

    # Positional encoding
    embed_fn, input_ch = get_embedder(args.multires, 3, args.i_embed)                       # Encode the 3D position
    embedtime_fn, input_ch_time = get_embedder(args.multires, 1, args.i_embed)

    input_ch_views = 0
    embeddirs_fn = None
    if args.use_viewdirs:
        embeddirs_fn, input_ch_views = get_embedder(args.multires_views, 3, args.i_embed)   # Also encode the 2D direction 

    # output_ch only changes the net architecture if use_viewdirs is true
    output_ch = 5 if args.N_importance > 0 else 4
    skips = [4]
    model = NeRF.get_by_name(
        args.nerf_type, 
        D=args.netdepth, 
        W=args.netwidth,
        input_ch=input_ch, 
        output_ch=output_ch, 
        skips=skips,
        input_ch_views=input_ch_views, 
        input_ch_time=input_ch_time,                    
        use_viewdirs=args.use_viewdirs, 
        embed_fn=embed_fn,
        zero_canonical=not args.not_zero_canonical,
        use_rigidity_network=args.use_rigidity_network,
        use_latent_codes_as_time=args.use_latent_codes_as_time,
        ray_bending_latent_size=args.ray_bending_latent_size,
    ).to(device)
    grad_vars += list(model.parameters())

    model_fine = None
    if args.use_two_models_for_fine:            # fine network for hierarchical sampling
        model_fine = NeRF.get_by_name(
            args.nerf_type, 
            D=args.netdepth_fine, 
            W=args.netwidth_fine,
            input_ch=input_ch, 
            output_ch=output_ch, 
            skips=skips,
            input_ch_views=input_ch_views, 
            input_ch_time=input_ch_time,
            use_viewdirs=args.use_viewdirs, 
            embed_fn=embed_fn,
            zero_canonical=not args.not_zero_canonical, 
            use_rigidity_network=args.use_rigidty_network,
            use_latent_codes_as_time=args.use_latent_codes_as_time,
            ray_bending_latent_size=args.ray_bending_latent_size,
        ).to(device)
        grad_vars += list(model_fine.parameters())
        
    def network_query_fn(inputs, viewdirs, ts, network_fn): return run_network(inputs, viewdirs, ts, network_fn,
                                                                               embed_fn=embed_fn,
                                                                               embeddirs_fn=embeddirs_fn,
                                                                               embedtime_fn=embedtime_fn,
                                                                               netchunk=args.netchunk,
                                                                               embd_time_discr=args.nerf_type != "temporal",
                                                                               use_latent_codes_as_time=args.use_latent_codes_as_time,)

    # Create optimizer
    # Note: needs to be Adam. otherwise need to check how to avoid wrong DeepSDF-style autodecoder optimization of the per-frame latent codes.
    optimizer = torch.optim.Adam(params=grad_vars, lr=args.lrate, betas=(0.9, 0.999))

    if args.do_half_precision: 
        print("[Config] Run model at half precision")
        if model_fine is not None:
            [model, model_fine], optimizers = amp.initialize([model, model_fine], optimizer, opt_level='O1')
        else:
            model, optimizers = amp.initialize(model, optimizer, opt_level='O1')

    start = 0
    basedir = args.basedir
    expname = args.expname

    ##########################

    # Load checkpoints
    if args.ft_path is not None and args.ft_path!='None':
        ckpts = [args.ft_path]
    else:
        ckpts = [os.path.join(basedir, expname, f) for f in sorted(os.listdir(os.path.join(basedir, expname))) if 'tar' in f]

    if len(ckpts) > 0:
        print("[Info] Found ckpts:\n\t\t" + '\n\t\t'.join(ckpts))
    else:
        print("[Info] Found no ckpts")

    if len(ckpts) > 0 and (not args.no_reload or args.render_only):
        ckpt_path = ckpts[-1]
        print('[Config] Reloading from', ckpt_path)
        ckpt = torch.load(ckpt_path, map_location=device)       # will map storages to the given device

        start = ckpt['global_step']
        optimizer.load_state_dict(ckpt['optimizer_state_dict'])

        # Load model
        model.load_state_dict(ckpt['network_fn_state_dict'])
        if model_fine is not None:
            model_fine.load_state_dict(ckpt['network_fine_state_dict'])
        if args.do_half_precision:
            amp.load_state_dict(ckpt['amp'])
        if autodecoder_variables is not None:
            for latent, saved_latent in zip(autodecoder_variables, ckpt["ray_bending_latent_codes"]):
                latent.data[:] = saved_latent[:].detach().clone()

    ##########################

    render_kwargs_train = {
        'network_query_fn' : network_query_fn,
        'perturb' : args.perturb,                           # set to 0. for no jitter, 1. for jitter
        'N_importance' : args.N_importance,                 # number of additional fine samples per ray
        'network_fine': model_fine,
        'N_samples' : args.N_samples,                       # number of coarse samples per ray
        'network_fn' : model,
        'use_viewdirs' : args.use_viewdirs,
        'white_bkgd' : args.white_bkgd,
        'raw_noise_std' : args.raw_noise_std,
        'use_two_models_for_fine' : args.use_two_models_for_fine,
    }

    # NDC only good for LLFF-style forward facing data
    if args.dataset_type != 'llff' or args.no_ndc:
        render_kwargs_train['ndc'] = False
        render_kwargs_train['lindisp'] = args.lindisp

    render_kwargs_test = {k : render_kwargs_train[k] for k in render_kwargs_train}
    render_kwargs_test['perturb'] = False
    render_kwargs_test['raw_noise_std'] = 0.

    return render_kwargs_train, render_kwargs_test, start, grad_vars, optimizer


def raw2outputs(raw, z_vals, rays_d, raw_noise_std=0, white_bkgd=False, pytest=False):
    """Transforms model's predictions to semantically meaningful values. This is sec. 4.2 in D-NeRF.
    Args:
        raw: [num_rays, num_samples along ray, 4]. Prediction from model.
        z_vals: [num_rays, num_samples along ray]. Integration time.
        rays_d: [num_rays, 3]. Direction of each ray.
    Returns:
        rgb_map: [num_rays, 3]. Estimated RGB color of a ray.
        disp_map: [num_rays]. Disparity map. Inverse of depth map.
        acc_map: [num_rays]. Sum of weights along each ray.
        weights: [num_rays, num_samples]. Weights assigned to each sampled color.
        depth_map: [num_rays]. Estimated distance to object.
    """
    dists = z_vals[...,1:] - z_vals[...,:-1]
    dists = torch.cat([dists, torch.Tensor([1e10]).expand(dists[...,:1].shape)], -1)  # [N_rays, N_samples]

    dists = dists * torch.norm(rays_d[...,None,:], dim=-1)

    rgb = torch.sigmoid(raw[...,:3])  # [N_rays, N_samples, 3]
    noise = 0.
    if raw_noise_std > 0.:
        noise = torch.randn(raw[...,3].shape) * raw_noise_std

        # Overwrite randomly sampled data if pytest
        if pytest:
            np.random.seed(0)
            noise = np.random.rand(*list(raw[...,3].shape)) * raw_noise_std
            noise = torch.Tensor(noise)

    # D-NeRF-Eq. (7)
    raw2alpha = lambda raw, dists, act_fn=F.relu: 1.-torch.exp(-act_fn(raw)*dists)
    alpha = raw2alpha(raw[...,3] + noise, dists)  # [N_rays, N_samples]
    # weights = alpha * tf.math.cumprod(1.-alpha + 1e-10, -1, exclusive=True)
    # Combines D-NeRF-Eq. (8) and alpha
    weights = alpha * torch.cumprod(torch.cat([torch.ones((alpha.shape[0], 1)), 1.-alpha + 1e-10], -1), -1)[:, :-1]
    # D-NeRF-Eq. (6)
    rgb_map = torch.sum(weights[...,None] * rgb, -2)  # [N_rays, 3]

    depth_map = torch.sum(weights * z_vals, -1)
    depth_std_map = ((((z_vals - depth_map.unsqueeze(-1)).pow(2) * weights).sum(-1)) + 1e-6).sqrt()     
    disp_map = 1./torch.max(1e-10 * torch.ones_like(depth_map), depth_map / torch.sum(weights, -1))
    acc_map = torch.sum(weights, -1)

    if white_bkgd:
        rgb_map = rgb_map + (1.-acc_map[...,None])
        # rgb_map = rgb_map + torch.cat([acc_map[..., None] * 0, acc_map[..., None] * 0, (1. - acc_map[..., None])], -1)

    return rgb_map, disp_map, acc_map, weights, depth_map, depth_std_map


def stratified_samples(z_vals, pytest=False):
    # get intervals between samples
    mids = .5 * (z_vals[...,1:] + z_vals[...,:-1])
    upper = torch.cat([mids, z_vals[...,-1:]], -1)
    lower = torch.cat([z_vals[...,:1], mids], -1)
    # stratified samples in those intervals
    t_rand = torch.rand(z_vals.shape)

    # Pytest, overwrite u with numpy's fixed random numbers
    if pytest:
        np.random.seed(0)
        t_rand = np.random.rand(*list(z_vals.shape))
        t_rand = torch.Tensor(t_rand)

    z_vals = lower + (upper - lower) * t_rand
    return z_vals    


def render_rays(ray_batch,
                network_fn,
                network_query_fn,
                N_samples,
                retraw=False,
                lindisp=False,
                perturb=0.,
                N_importance=0,
                network_fine=None,
                white_bkgd=False,
                raw_noise_std=0.,
                verbose=False,
                pytest=False,
                z_vals=None,
                ray_bending_latent_codes=None,
                use_two_models_for_fine=False,
                use_viewdirs=False,
                use_depth_guided_sampling=False,
                debug_rays=False):
    """Performs volumetric rendering, i.e. computes a RGB images and depth map by querying the model in spatial
    locations along the given rays and computing the volume rendering integral.

    Args:
      ray_batch: array of shape [batch_size, ...]. All information necessary
        for sampling along a ray, including: ray origin, ray direction, min
        dist, max dist, and unit-magnitude viewing direction.
      network_fn: function. Model for predicting RGB and density at each point
        in space.
      network_query_fn: function used for passing queries to network_fn.
      N_samples: int. Number of different times to sample along each ray.
      retraw: bool. If True, include model's raw, unprocessed predictions.
      lindisp: bool. If True, sample linearly in inverse depth rather than in depth.
      perturb: float, 0 or 1. If non-zero, each ray is sampled at stratified
        random points in time.
      N_importance: int. Number of additional times to sample along each ray.
        These samples are only passed to network_fine.
      network_fine: "fine" network with same spec as network_fn.
      white_bkgd: bool. If True, assume a white background.
      raw_noise_std: not needed for us.
      verbose: bool. If True, print more debugging info.
      z_vals: [num_rays, num_samples along ray]. Integration time.
    Returns:
        ret (dict): Dictionary with these keys:
            rgb_map: [num_rays, 3]. Estimated RGB color of a ray. Comes from fine model.
            disp_map: [num_rays]. Disparity map. 1 / depth.
            acc_map: [num_rays]. Accumulated opacity along each ray. Comes from fine model.
            depth_map: [num_rays]
            raw: [num_rays, num_samples, 5]. Raw predictions from model.
            z_vals: [num_rays, N_samples] Sample locations along the rays.
            position_delta: TODO
            rgb0: See rgb_map. Output for coarse model.
            disp0: See disp_map. Output for coarse model.
            acc0: See acc_map. Output for coarse model.
            depth0: See depth_map. Output for coarse model.
            z_std: [num_rays]. Standard deviation of distances along ray for each sample.
            position_delta_0: TODO
            z_std: Standard deviation of the predicted depth.
    """

    N_rays = ray_batch.shape[0]
    rays_o, rays_d = ray_batch[:,0:3], ray_batch[:,3:6]                     # [N_rays, 3] each
    bounds = torch.reshape(ray_batch[...,6:9], [-1,1,3])
    near, far, frame_time = bounds[...,0], bounds[...,1], bounds[...,2]     # [N_rays,1] each
    # viewdirs = ray_batch[:,-3:] if ray_batch.shape[-1] > 9 else None
    z_samples, z_coarse, z_fine = None, None, None
    rgb_map_0, disp_map_0, acc_map_0, depth_map_0, depth_std_map_0, position_delta_0 = None, None, None, None, None, None
    viewdirs = None
    depth_range = None

    if use_viewdirs:
        viewdirs = ray_batch[:, 9:12] 
        if ray_batch.shape[-1] > 12:
            depth_range = ray_batch[:, 12:15]
    else:
        if ray_batch.shape[-1] > 9:
            depth_range = ray_batch[:, 9:12]

    if ray_bending_latent_codes is not None:
        frame_time = ray_bending_latent_codes

    if z_vals is None:      
        # create coarse integration locations along the rays
        t_vals = torch.linspace(0., 1., steps=N_samples)
        if not lindisp:
            z_coarse = near * (1.-t_vals) + far * (t_vals)
        else:
            z_coarse = 1./(1./near * (1.-t_vals) + 1./far * (t_vals))
        z_coarse = z_coarse.expand([N_rays, N_samples])

        # compute a lower bound for the sampling standard deviation as the maximal distance between samples
        lower_bound = z_coarse[0, -1] - z_coarse[0, -2] 

        # Add stratified perturbations 
        z_coarse = stratified_samples(z_coarse, pytest=pytest) if perturb > 0. else z_coarse     
        pts = rays_o[...,None,:] + rays_d[...,None,:] * z_coarse[...,:,None]      # [N_rays, N_samples, 3]
        
        if N_importance > 0:   # If fine samples wanted         
            if use_two_models_for_fine:     
                # Forward pass for coarse samples with a separate coarse network
                raw, position_delta_0 = network_query_fn(pts, viewdirs, frame_time, network_fn)
                rgb_map_0, disp_map_0, acc_map_0, weights, depth_map_0, depth_std_map_0 = raw2outputs(raw, z_coarse, rays_d, raw_noise_std, white_bkgd, pytest=pytest)
            else:
                # Forward pass for coarse samples with the same network
                with torch.no_grad():
                    raw, _ = network_query_fn(pts, viewdirs, frame_time, network_fn)
                    _, _, _, weights, _, _ = raw2outputs(raw, z_coarse, rays_d, raw_noise_std, white_bkgd, pytest=pytest)

            if use_depth_guided_sampling:
                # Get fine samples from depth guided sampling
                if depth_range is not None:
                    # Train time: use precomputed samples along the whole ray and additionally sample around the depth
                    valid_depth = depth_range[:,0] >= near[0, 0]
                    invalid_depth = valid_depth.logical_not()
                    z_fine = torch.zeros((N_rays, N_importance))
                    # sample around the predicted depth from the first half of samples, if the input depth is invalid
                    z_fine[invalid_depth] = compute_samples_around_depth(raw.detach()[invalid_depth], z_coarse[invalid_depth], rays_d[invalid_depth], 
                                                                           N_importance, perturb, lower_bound, near[0, 0], far[0, 0], device=device)
                    # sample with in 3 sigma of the input depth, if it is valid
                    z_fine[valid_depth] = sample_3sigma(depth_range[valid_depth, 1], depth_range[valid_depth, 2], 
                                                        N_importance, perturb == 0., near[0, 0], far[0, 0], device=device)
                else:
                    # Test time: use precomputed samples along the whole ray and additionally sample around the predicted depth from the first half of samples
                    z_fine = compute_samples_around_depth(raw, z_coarse, rays_d, N_importance, 
                                                          perturb, lower_bound, near[0, 0], far[0, 0], device=device)
                # Combine coarse and fine integration locations
                z_vals = torch.cat((z_coarse, z_fine), -1)
                z_vals, indices = z_vals.sort()
            else:       
                # Get fine samples from hierarchical sampling
                z_vals_mid = .5 * (z_coarse[...,1:] + z_coarse[...,:-1])
                z_fine = sample_pdf(z_vals_mid, weights[...,1:-1], N_importance, det=(perturb==0.), pytest=pytest)
                z_fine = z_fine.detach()
                z_vals, _ = torch.sort(torch.cat([z_coarse, z_fine], -1), -1)
        else:
            z_vals = z_coarse

    pts = rays_o[...,None,:] + rays_d[...,None,:] * z_vals[...,:,None] # [N_rays, N_samples + N_importance, 3]
    run_fn = network_fn if network_fine is None else network_fine
    raw, position_delta = network_query_fn(pts, viewdirs, frame_time, run_fn)
    rgb_map, disp_map, acc_map, weights, depth_map, depth_std_map = raw2outputs(raw, z_vals, rays_d, raw_noise_std, white_bkgd, pytest=pytest)

    # Rest is logging and returning
    ray_debug = None
    if debug_rays:
        ray_debug = {
            "rays_o": rays_o,
            "rays_d": rays_d,
            "rgb_map": rgb_map,
            "depth_map": depth_map,
        }
        if z_coarse is not None:
            ray_debug["z_coarse"] = z_coarse
        if z_fine is not None:
            ray_debug["z_fine"] = z_fine
        else:
            ray_debug["z_vals"] = z_vals

    ret = {
        'rgb_map': rgb_map,
        'disp_map': disp_map,
        'acc_map': acc_map,
        'depth_map': depth_map,
        'depth_std_map': depth_std_map,
        'z_vals': z_vals,
        'position_delta': position_delta
    }
    if retraw:
        ret['raw'] = raw
    if N_importance > 0:
        if rgb_map_0 is not None:
            ret['rgb0'] = rgb_map_0
        if disp_map_0 is not None:
            ret['disp0'] = disp_map_0
        if acc_map_0 is not None:
            ret['acc0'] = acc_map_0
        if depth_map_0 is not None:
            ret['depth0'] = depth_map_0
        if depth_std_map_0 is not None:
            ret['depthstd0'] = depth_std_map_0
        if position_delta_0 is not None:
            ret['position_delta_0'] = position_delta_0
        if z_samples is not None:
            ret['z_std'] = torch.std(z_samples, dim=-1, unbiased=False)  # [N_rays]

    for k in ret:
        if (torch.isnan(ret[k]).any() or torch.isinf(ret[k]).any()) and DEBUG:
            print(f"! [Numerical Error] {k} contains nan or inf.")

    return ret, ray_debug


def config_parser():

    parser = configargparse.ArgumentParser()
    parser.add_argument('--config', is_config_file=True, 
                        help='config file path')
    parser.add_argument("--expname", type=str, 
                        help='experiment name')
    parser.add_argument("--basedir", type=str, default='./logs/', 
                        help='where to store ckpts and logs')
    parser.add_argument("--datadir", type=str, default='./data/llff/fern', 
                        help='input data directory')

    # network architecture options
    parser.add_argument("--nerf_type", type=str, default="original",
                        help='nerf network type. Options: original / temporal')
    parser.add_argument("--netdepth", type=int, default=8, 
                        help='layers in network')
    parser.add_argument("--netwidth", type=int, default=256, 
                        help='channels per layer')
    parser.add_argument("--netdepth_fine", type=int, default=8, 
                        help='layers in fine network')
    parser.add_argument("--netwidth_fine", type=int, default=256, 
                        help='channels per layer in fine network')
    parser.add_argument("--use_rigidity_network", action='store_true', 
                        help='if set to true use rigidity network')
    parser.add_argument("--use_latent_codes_as_time", action="store_true",
                        help="learnable latent codes instead of time input to deformation network")
    parser.add_argument("--ray_bending_latent_size", type=int, default=32,
                        help="size of per-frame autodecoding latent vector used for deformation network")

    # rendering options
    parser.add_argument("--N_samples", type=int, default=64, 
                        help='number of coarse samples per ray')
    parser.add_argument("--not_zero_canonical", action='store_true',
                        help='if set zero time is not the canonic space')
    parser.add_argument("--N_importance", type=int, default=0,
                        help='number of additional fine samples per ray')
    parser.add_argument("--perturb", type=float, default=1.,
                        help='set to 0. for no jitter, 1. for jitter')
    parser.add_argument("--use_viewdirs", action='store_true', 
                        help='use full 5D input instead of 3D')
    parser.add_argument("--i_embed", type=int, default=0, 
                        help='set 0 for default positional encoding, -1 for none')
    parser.add_argument("--multires", type=int, default=10, 
                        help='log2 of max freq for positional encoding (3D location)')
    parser.add_argument("--multires_views", type=int, default=4, 
                        help='log2 of max freq for positional encoding (2D direction)')
    parser.add_argument("--raw_noise_std", type=float, default=0., 
                        help='std dev of noise added to regularize sigma_a output, 1e0 recommended')
    parser.add_argument("--use_two_models_for_fine", action='store_true',
                        help='use two models for fine results')
    parser.add_argument("--use_depth_guided_sampling", action='store_true',
                        help='use depth guided sampling instead of hierarchical sampling')

    parser.add_argument("--render_only", action='store_true', 
                        help='do not optimize, reload weights and render out render_poses path')
    parser.add_argument("--render_test", action='store_true', 
                        help='render the test set instead of render_poses path')
    parser.add_argument("--render_factor", type=int, default=0, 
                        help='downsampling factor to speed up rendering, set 4 or 8 for fast preview')
    parser.add_argument("--render_pose_type", type=str, default="spherical",
                        help='render pose trajectory. Options depend on data loader implementation: spherical / spiral / static / original_trajectory / stat_dyn_stat / dynamic') # For explanations look in the docstrings in load_owndataset.py
    parser.add_argument("--slowmo", action='store_true', 
                        help='slow-motion effect in rendering video')

    # training options
    parser.add_argument("--N_iter", type=int, default=500000,
                        help='num training iterations')
    parser.add_argument("--N_rand", type=int, default=32*32*4, 
                        help='batch size (number of random rays per gradient step)')
    parser.add_argument("--do_half_precision", action='store_true',
                        help='do half precision training and inference')
    parser.add_argument("--lrate", type=float, default=5e-4, 
                        help='learning rate')
    parser.add_argument("--lrate_decay", type=int, default=250, 
                        help='exponential learning rate decay (in 1000 steps)')
    parser.add_argument("--chunk", type=int, default=1024*32, 
                        help='number of rays processed in parallel, decrease if running out of memory')
    parser.add_argument("--netchunk", type=int, default=1024*64, 
                        help='number of pts sent through network in parallel, decrease if running out of memory')
    parser.add_argument("--no_batching", action='store_true', 
                        help='only take random rays from 1 image at a time')
    parser.add_argument("--no_reload", action='store_true', 
                        help='do not reload weights from saved ckpt')
    parser.add_argument("--ft_path", type=str, default=None,        
                        help='specific weights npy file to reload for coarse network')
    parser.add_argument("--precrop_iters", type=int, default=0,
                        help='number of steps to train on central crops')
    parser.add_argument("--precrop_iters_time", type=int, default=0,
                        help='number of steps to train on central time')
    parser.add_argument("--precrop_frac", type=float,
                        default=.5, help='fraction of img taken for central crops')
    parser.add_argument("--add_tv_loss", action='store_true',
                        help='evaluate tv loss')
    parser.add_argument("--tv_loss_weight", type=float,
                        default=1.e-4, help='weight of tv loss')
    parser.add_argument("--depth_loss_type", default="mse",
                        help="use depth maps for supervision with MSE or Gaussian-Neg-Log-Likelihood. Options: mse / gnll")
    parser.add_argument("--depth_loss_weight", type=float,
                        default=0.01, help='weight of depth loss')

    # dataset options
    parser.add_argument("--dataset_type", type=str, default='llff', 
                        help='options: llff / blender / deepvoxels / deepdeform / owndataset')
    parser.add_argument("--testskip", type=int, default=2,
                        help='will load 1/N images from test/val sets, useful for large datasets like deepvoxels')

    ## deepvoxels flags
    parser.add_argument("--shape", type=str, default='greek', 
                        help='options : armchair / cube / greek / vase')

    ## blender flags
    parser.add_argument("--white_bkgd", action='store_true', 
                        help='set to render synthetic data on a white bkgd (always use for dvoxels)')
    parser.add_argument("--half_res", action='store_true', 
                        help='load blender synthetic data at 400x400 instead of 800x800')

    ## llff flags
    parser.add_argument("--factor", type=int, default=8, 
                        help='downsample factor for LLFF images')
    parser.add_argument("--no_ndc", action='store_true', 
                        help='do not use normalized device coordinates (set for non-forward facing scenes)')
    parser.add_argument("--lindisp", action='store_true', 
                        help='sampling linearly in disparity rather than depth')
    parser.add_argument("--spherify", action='store_true', 
                        help='set for spherical 360 scenes')
    parser.add_argument("--llffhold", type=int, default=8, 
                        help='will take every 1/N images as LLFF test set, paper uses 8')

    # logging/saving options
    parser.add_argument("--i_print",   type=int, default=1000,
                        help='frequency of console printout and metric loggin')
    parser.add_argument("--i_img",     type=int, default=10000,
                        help='frequency of tensorboard image logging')
    parser.add_argument("--i_weights", type=int, default=25000,
                        help='frequency of weight ckpt saving')
    parser.add_argument("--i_testset", type=int, default=200000,
                        help='frequency of testset saving')
    parser.add_argument("--i_video",   type=int, default=200000,
                        help='frequency of render_poses video saving')

    return parser


def train():

    parser = config_parser()
    args = parser.parse_args()

    #################### Load data ####################

    if args.dataset_type == 'blender':
        images, poses, times, render_poses, render_times, hwf, i_split = load_blender_data(args.datadir, args.half_res, args.testskip)
        print('Loaded blender', images.shape, render_poses.shape, hwf, args.datadir)
        i_train, i_val, i_test = i_split

        hwff = hwf + [hwf[2]]       # focal_x = focal_y
        near = 2.
        far = 6.
        depth_maps = None

        # the RGBA-to-RGB conversion depends on the background color (alpha compositioning)
        # see https://stackoverflow.com/questions/2049230/convert-rgba-color-to-rgb
        if args.white_bkgd:
            images = images[...,:3]*images[...,-1:] + (1.-images[...,-1:])  # (img_RGB * img_A) + (1 - img_A) * bkgd_RGB
        else:
            images = images[...,:3]     # approximation as the background RGB values are unknown

        # images = [rgb2hsv(img) for img in images]

    elif args.dataset_type == 'deepdeform':
        images, depth_maps, poses, times, render_poses, render_times, hwff, i_split = load_deepdeform_data(args.datadir, args.half_res, args.testskip, args.render_pose_type)
        print(f"[Info] Loaded DeepDeform:\n\t\timages.shape: {images.shape}\n\t\trender_poses.shape: {render_poses.shape}\n\t\thwff: {hwff}\n\t\targs.datadir: {args.datadir}")
        i_train, i_val, i_test = i_split
        near = 0.1
        far = np.max(depth_maps) + 0.1
     
        print(f"[Info] Setting near plane at distance {near} and far plane at distance {far}.")
        
        # No RGB-to-RGBA conversion needed

    elif args.dataset_type == 'owndataset':
        images, depth_maps, poses, times, render_poses, render_times, hwff, i_split = load_owndataset_data(args.datadir, args.half_res, args.testskip, args.render_pose_type, args.slowmo)
        print(f"[Info] Loaded Own Dataset:\n\t\timages.shape: {images.shape}\n\t\trender_poses.shape: {render_poses.shape}\n\t\thwff: {hwff}\n\t\targs.datadir: {args.datadir}")
        i_train, i_val, i_test = i_split
        near = 0.1
        far = np.max(depth_maps) + 0.1
     
        print(f"[Info] Setting near plane at distance {near} and far plane at distance {far}.")
        
        # savedir = "data/johannes_2"
        # imageio.mimwrite(os.path.join(savedir, 'video_depth_{}.mp4'.format("original")), to8b(depth_maps/np.max(depth_maps)), fps=15, quality=8)
        # imageio.mimwrite(os.path.join(savedir, 'video_{}.mp4'.format("original")), to8b(images), fps=15, quality=8)
        # No RGB-to-RGBA conversion needed

    else:
        print('[WARNING] Unknown dataset type: ', args.dataset_type, '. Exiting')
        return

    min_time, max_time = times[i_train[0]], times[i_train[-1]]
    if args.dataset_type == "blender":
        assert min_time == 0., "time must start at 0"
        assert max_time == 1., "max time must be 1"
    elif args.dataset_type == "deepdeform":             # There cannot be a t=0 img in every split in DeepDeform
        assert min_time >= 0., "time must be >= 0"
        assert max_time <= 1., "max time must be <= 1"

    if args.depth_loss_type not in ["mse", "gnll"]:
        print(f"[WARNING] Invalid depth loss type: {args.depth_loss_type}. Exiting.")
        return       
    comp_depth = True if args.depth_loss_weight > 0 or args.use_depth_guided_sampling else False
    if depth_maps is None and comp_depth:
        print("[WARNING] No depth maps loaded. Cannot apply depth loss. Exiting.")
        return

    print(f"[Config] Experiment name: {args.expname}")

    #################### Set up training ####################

    # Cast intrinsics to right types
    H, W, focal_x, focal_y = hwff
    H, W = int(H), int(W)
    hwff = [H, W, focal_x, focal_y]

    if args.render_test:
        render_poses = np.array(poses[i_test])
        render_times = np.array(times[i_test])

    # Create log dir and copy the config file
    basedir = args.basedir
    expname = args.expname
    os.makedirs(os.path.join(basedir, expname), exist_ok=True)
    with open(os.path.join(basedir, expname, 'args.txt'), 'w') as file:
        vars_dict = vars(args)
        vars_dict["CUDA_VISIBLE_DEVICES"] = GPU_INDEX
        vars_dict["hwff"] = hwff
        vars_dict["near_and_far"] = [near, far]
        for arg in sorted(vars_dict):
            attr = getattr(args, arg)
            file.write('{} = {}\n'.format(arg, attr))
    if args.config is not None:
        config_text = open(args.config, 'r').read()
        with open(os.path.join(basedir, expname, 'config.txt'), 'w') as file:
            file.write(config_text)

    ray_bending_latents_list = []
    if args.use_latent_codes_as_time:
        # create autodecoder variables as pytorch tensors
        ray_bending_latents_list = [
            torch.zeros(args.ray_bending_latent_size) for _ in range(len(times))   
        ]
        for latent in ray_bending_latents_list:
            latent.requires_grad = True

        # select nearest timesteps from latents list
        render_times = [ray_bending_latents_list[int(t*(len(ray_bending_latents_list)-1))].clone().detach() for t in render_times]   

    # Create nerf model
    render_kwargs_train, render_kwargs_test, start, grad_vars, optimizer = create_nerf(args, autodecoder_variables=ray_bending_latents_list)
    global_step = start

    bds_dict = {
        'near' : near,
        'far' : far,
        "use_depth_guided_sampling": args.use_depth_guided_sampling,
        "use_latent_codes_as_time": args.use_latent_codes_as_time,
    }
    render_kwargs_train.update(bds_dict)
    render_kwargs_test.update(bds_dict)

    # Move testing data to GPU
    render_poses = torch.Tensor(render_poses).to(device)
    if args.use_latent_codes_as_time:
        render_times = [t.to(device) for t in render_times]
    else:
        render_times = torch.Tensor(render_times).to(device)

    # Short circuit if only rendering out from trained model
    if args.render_only:
        print('[Config] RENDER ONLY')
        with torch.no_grad():
            if args.render_test:
                # render_test switches to test poses
                images = images[i_test]
                gt_depths = depth_maps[i_test]
                save_also_gt = True
            else:
                # Default is smoother render_poses path
                images = None
                gt_depths = None
                save_also_gt = False

            testsavedir = os.path.join(basedir, expname, 'renderonly_{}_{:06d}'.format('test' if args.render_test else 'path', start))
            os.makedirs(testsavedir, exist_ok=True)
            print('[Info] Test poses shape:', render_poses.shape)

            rgbs, _, depths = render_path(render_poses, render_times, hwff, args.chunk, render_kwargs_test, gt_imgs=images,
                                  gt_depths=gt_depths, savedir=testsavedir, render_factor=args.render_factor, save_also_gt=save_also_gt, expname=args.expname, iteration=start)
            print('[Info] Saving rendering to:', testsavedir)
            imageio.mimwrite(os.path.join(testsavedir, 'video_{}.mp4'.format(args.render_pose_type)), to8b(rgbs), fps=15, quality=8)
            imageio.mimwrite(os.path.join(testsavedir, 'video_depth_{}.mp4'.format(args.render_pose_type)), to8b(depths/np.max(depths)), fps=15, quality=8)
            return

    # Prepare raybatch tensor if batching random rays
    N_rand = args.N_rand
    use_batching = not args.no_batching
    if use_batching:
        # For random ray batching
        print('get rays')
        rays = np.stack([get_rays_np(H, W, focal_x, focal_y, p) for p in poses[:,:3,:4]], 0) # [N, ro+rd, H, W, 3]
        print('done, concats')
        rays_rgb = np.concatenate([rays, images[:,None]], 1) # [N, ro+rd+rgb, H, W, 3]
        rays_rgb = np.transpose(rays_rgb, [0,2,3,1,4]) # [N, H, W, ro+rd+rgb, 3]
        rays_rgb = np.stack([rays_rgb[i] for i in i_train], 0) # train images only
        rays_rgb = np.reshape(rays_rgb, [-1,3,3]) # [(N-1)*H*W, ro+rd+rgb, 3]
        rays_rgb = rays_rgb.astype(np.float32)
        print('shuffle rays')
        np.random.shuffle(rays_rgb)

        print('done')
        i_batch = 0

    # Move training data to GPU
    images = torch.Tensor(images).to(device)
    if depth_maps is not None:
        depth_maps = torch.Tensor(depth_maps).to(device)
    poses = torch.Tensor(poses).to(device)
    times = torch.Tensor(times).to(device)
    if use_batching:
        rays_rgb = torch.Tensor(rays_rgb).to(device)
    ray_bending_latents_list = [l_vec.to(device) for l_vec in ray_bending_latents_list]

    #################### Training loop ####################

    N_iters = args.N_iter + 1
    print('[Info] Beginning training')

    # Summary writers
    writer = SummaryWriter(os.path.join(basedir, 'summaries', expname))
    
    start = start + 1
    for i in trange(start, N_iters):
        time0 = time.time()

        # Sample random ray batch
        # target_depth_s m??sste dann hier auch bestimmt werden
        if use_batching:
            raise NotImplementedError("Time not implemented")

            # Random over all images
            batch = rays_rgb[i_batch:i_batch+N_rand] # [B, 2+1, 3*?]
            batch = torch.transpose(batch, 0, 1)
            batch_rays, target_s = batch[:2], batch[2]

            i_batch += N_rand
            if i_batch >= rays_rgb.shape[0]:
                print("Shuffle data after an epoch!")
                rand_idx = torch.randperm(rays_rgb.shape[0])
                rays_rgb = rays_rgb[rand_idx]
                i_batch = 0

        else:
            # Random from one image
            if i >= args.precrop_iters_time:
                img_i = np.random.choice(i_train)
            else:
                skip_factor = i / float(args.precrop_iters_time) * len(i_train)
                max_sample = max(int(skip_factor), 3)
                img_i = np.random.choice(i_train[:max_sample])

            target = images[img_i]
            if comp_depth:
                target_depth = depth_maps[img_i]
            pose = poses[img_i, :3, :4]
            if args.use_latent_codes_as_time:
                frame_time = ray_bending_latents_list[img_i]
            else:
                frame_time = times[img_i]
             
            if N_rand is not None:
                rays_o, rays_d = get_rays(H, W, focal_x, focal_y, torch.Tensor(pose))  # (H, W, 3), (H, W, 3)

                if i < args.precrop_iters:
                    dH = int(H//2 * args.precrop_frac)
                    dW = int(W//2 * args.precrop_frac)
                    coords = torch.stack(torch.meshgrid(
                        torch.linspace(H//2 - dH, H//2 + dH - 1, 2*dH),
                        torch.linspace(W//2 - dW, W//2 + dW - 1, 2*dW), indexing="ij"
                    ), -1)
                    if i == start:
                        print(f"[Config] Center cropping of size {2*dH} x {2*dW} is enabled until iter {args.precrop_iters}")                
                else:
                    coords = torch.stack(torch.meshgrid(torch.linspace(
                        0, H-1, H), torch.linspace(0, W-1, W), indexing="ij"), -1)  # (H, W, 2)

                coords = torch.reshape(coords, [-1,2])  # (H * W, 2)
                select_inds = np.random.choice(coords.shape[0], size=[N_rand], replace=False)  # (N_rand,)
                select_coords = coords[select_inds].long()  # (N_rand, 2)
                rays_o = rays_o[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                rays_d = rays_d[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)
                target_s = target[select_coords[:, 0], select_coords[:, 1]]  # (N_rand, 3)

                batch_rays = torch.stack([rays_o, rays_d], 0) # (2, N_rand, 3)
                if comp_depth:
                    target_depth_s = target_depth[select_coords[:, 0], select_coords[:, 1]] # (N_rand, 1) 
                    # IMPORTANT: The input depth std is hardcoded here!!!
                    target_stds = torch.full(target_depth.shape, 0.03)       
                    target_stds_s = target_stds[select_coords[:, 0], select_coords[:, 1]]
                    if args.use_depth_guided_sampling:
                        depth_range = comp_depth_sampling(target_depth_s, target_stds_s)
                        batch_rays = torch.stack([rays_o, rays_d, depth_range], 0) # (3, N_rand, 3))

        ####################  Core optimization  ####################
        rgb, disp, acc, depth, extras = render(H, W, focal_x, focal_y, chunk=args.chunk, rays=batch_rays, frame_time=frame_time,
                                                verbose=i < 10, retraw=True, **render_kwargs_train)
        rgb.to(device)
        disp.to(device)
        acc.to(device)
        depth.to(device)

        if args.add_tv_loss:
            frame_time_prev = times[img_i - 1] if img_i > 0 else None
            frame_time_next = times[img_i + 1] if img_i < times.shape[0] - 1 else None

            if frame_time_prev is not None and frame_time_next is not None:
                if np.random.rand() > .5:
                    frame_time_prev = None
                else:
                    frame_time_next = None

            if frame_time_prev is not None:
                rand_time_prev = frame_time_prev + (frame_time - frame_time_prev) * torch.rand(1)[0]
                _, _, _, _, extras_prev = render(H, W, focal_x, focal_y, chunk=args.chunk, rays=batch_rays, frame_time=rand_time_prev,
                                                verbose=i < 10, retraw=True, z_vals=extras['z_vals'].detach(),
                                                **render_kwargs_train)

            if frame_time_next is not None:
                rand_time_next = frame_time + (frame_time_next - frame_time) * torch.rand(1)[0]
                _, _, _, _, extras_next = render(H, W, focal_x, focal_y, chunk=args.chunk, rays=batch_rays, frame_time=rand_time_next,
                                                verbose=i < 10, retraw=True, z_vals=extras['z_vals'].detach(),
                                                **render_kwargs_train)

        optimizer.zero_grad() 
        # reset autodecoder gradients to avoid wrong DeepSDF-style optimization. Note: this is only guaranteed to work if the optimizer is Adam
        for latent in ray_bending_latents_list:
            latent.grad = None

        img_loss = img2mse(rgb, target_s)

        tv_loss = 0
        if args.add_tv_loss:
            if frame_time_prev is not None:
                tv_loss += ((extras['position_delta'] - extras_prev['position_delta']).pow(2)).sum()
                if 'position_delta_0' in extras:
                    tv_loss += ((extras['position_delta_0'] - extras_prev['position_delta_0']).pow(2)).sum()
            if frame_time_next is not None:
                tv_loss += ((extras['position_delta'] - extras_next['position_delta']).pow(2)).sum()
                if 'position_delta_0' in extras:
                    tv_loss += ((extras['position_delta_0'] - extras_next['position_delta_0']).pow(2)).sum()
            tv_loss = tv_loss * args.tv_loss_weight

        depth_loss = 0
        if args.depth_loss_weight > 0:
            if args.depth_loss_type == "mse":
                depth_loss = depth2mse(depth, target_depth_s.squeeze())
            elif args.depth_loss_type == "gnll":
                depth_loss = depth2gnll(depth, target_depth_s.squeeze(), extras['depth_std_map'])
            else:
                print("No depth_loss_type specified -- either mse or gnll")
        loss = img_loss + tv_loss + args.depth_loss_weight * depth_loss 

        psnr = mse2psnr(img_loss)

        if 'rgb0' in extras:
            img_loss0 = img2mse(extras['rgb0'], target_s)
            depth_loss0 = 0
            if args.depth_loss_weight > 0:
                depth_loss0 = depth2mse(extras['depth0'], target_depth_s) 
            loss0 = img_loss0 + args.depth_loss_weight * depth_loss0
            loss += loss0
            psnr0 = mse2psnr(img_loss0)

        if args.do_half_precision:
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()

        nn.utils.clip_grad_value_(grad_vars, 0.1)
        optimizer.step()

        ############################ update learning rate ############################
        # NOTE: IMPORTANT!

        decay_rate = 0.1
        decay_steps = args.lrate_decay * 1000
        new_lrate = args.lrate * (decay_rate ** (global_step / decay_steps))
        for param_group in optimizer.param_groups:
            param_group['lr'] = new_lrate

        ############################ LOGGING ################################

        dt = time.time()-time0
        # print(f"Step: {global_step}, Loss: {loss}, Time: {dt}")

        # Save network weights checkpoint
        if i%args.i_weights == 0:     
            all_latents = torch.zeros(0).cpu()
            for l in ray_bending_latents_list:
                all_latents = torch.cat([all_latents, l.cpu().unsqueeze(0)], 0)  

            path = os.path.join(basedir, expname, '{:06d}.tar'.format(i))
            save_dict = {
                'global_step': global_step + 1,     
                'network_fn_state_dict': render_kwargs_train['network_fn'].state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'network_fine_state_dict': render_kwargs_train['network_fine'].state_dict() if render_kwargs_train['network_fine'] is not None else None,
                "ray_bending_latent_codes": all_latents,        # shape: frames x latent_size
            }
            # if render_kwargs_train['network_fine'] is not None:
            #     save_dict['network_fine_state_dict'] = render_kwargs_train['network_fine'].state_dict()

            if args.do_half_precision:
                save_dict['amp'] = amp.state_dict()
            torch.save(save_dict, path)
            print('[Info] Saved checkpoints at:', path)

        # Log training stats
        if i % args.i_print == 0:       
            tqdm_txt = f"[TRAIN] Iter: {i} Loss_fine: {img_loss.item()} PSNR: {psnr.item()}"
            if args.add_tv_loss:
                tqdm_txt += f" TV: {tv_loss.item()}"
            if args.depth_loss_weight > 0:
                tqdm_txt += f" Depth_loss: {depth_loss.item()}"
            tqdm.write(tqdm_txt)

            writer.add_scalar('train_1_loss', loss.item(), i)
            writer.add_scalar('train_2_img_loss', img_loss.item(), i)
            writer.add_scalar('train_3_psnr', psnr.item(), i)
            if args.depth_loss_weight > 0:
                writer.add_scalar('train_4_depth_loss', depth_loss.item(), i) 
            if args.add_tv_loss:
                writer.add_scalar('train_5_tv', tv_loss.item(), i)
            if 'rgb0' in extras:
                writer.add_scalar('train_6_loss0', loss0.item(), i)
                writer.add_scalar('train_img_7_loss0', img_loss0.item(), i)
                writer.add_scalar('train_8_psnr0', psnr0.item(), i)
                if args.depth_loss_weight > 0:
                    writer.add_scalar('train_depth_9_loss0', depth_loss0.item(), i)


        del loss, img_loss, psnr, target_s
        if 'rgb0' in extras:
            del img_loss0, psnr0
        if args.add_tv_loss:
            del tv_loss
        if args.depth_loss_weight > 0:
            del depth_loss, target_depth_s
            if "rgb0" in extras:
                del depth_loss0
        del rgb, disp, acc, extras

        # Validate on a random image from the val set
        if i%args.i_img == 0:       
            torch.cuda.empty_cache()
            
            # === Image from Training Set ===

            img_i = np.random.choice(i_train)

            target = images[img_i]
            pose = poses[img_i, :3,:4]
            if args.use_latent_codes_as_time:
                frame_time = ray_bending_latents_list[img_i]
            else:
                frame_time = times[img_i]

            with torch.no_grad():
                rgb, disp, acc, depth, extras = render(H, W, focal_x, focal_y, chunk=args.chunk, c2w=pose, frame_time=frame_time, debug_rays=False, **render_kwargs_test)

            if depth_maps is not None:
                target_depth = depth_maps[img_i].squeeze()
   
            writer.add_image('train_1_rgb_gt', to8b(target.cpu().numpy()), i, dataformats='HWC')
            writer.add_image('train_2_rgb', to8b(rgb.cpu().numpy()), i, dataformats='HWC')
            writer.add_image('train_3_disp', disp.cpu().numpy(), i, dataformats='HW')
            writer.add_image('train_4_acc', acc.cpu().numpy(), i, dataformats='HW')
            if depth_maps is not None:
                writer.add_image('train_5_depth_gt', to8b(target_depth.cpu().numpy()/np.max(target_depth.cpu().numpy())), i, dataformats='HW')
                writer.add_image('train_6_depth', to8b(depth.cpu().numpy()/np.max(depth.cpu().numpy())), i, dataformats='HW')

            if 'rgb0' in extras:
                writer.add_image('train_7_rgb_rough', to8b(extras['rgb0'].cpu().numpy()), i, dataformats='HWC')
            if 'disp0' in extras:
                writer.add_image('train_8_disp_rough', extras['disp0'].cpu().numpy(), i, dataformats='HW')
            if 'depth0' in extras:
                writer.add_image('train_9_depth_rough', extras['depth0'].cpu().numpy(), i, dataformats='HW')
            if 'z_std' in extras:
                writer.add_image('train_10_acc_rough', extras['z_std'].cpu().numpy(), i, dataformats='HW')

            writer.flush()

            # === Image from Validation Set ===
            img_i = np.random.choice(i_val)
            debug_rays = False
            ray_debug_path = ""
            if i%(5 * args.i_img) == 0 and DEBUG:       # every 5*i_img steps val on the same img
                debug_rays = True
                ray_debug_path = os.path.join(basedir, expname, f"step_{i:06d}_")
                img_i = i_val[-1]           # just alway use the last val frame

            target = images[img_i]
            pose = poses[img_i, :3,:4]
            if args.use_latent_codes_as_time:
                # get ray bending latent code from nearest train image
                frame_time = ray_bending_latents_list[get_nearest_train_index(times[img_i].item(), [times[_] for _ in i_train])]
            else:
                frame_time = times[img_i] 

            with torch.no_grad():
                rgb, disp, acc, depth, extras = render(H, W, focal_x, focal_y, chunk=args.chunk, c2w=pose, frame_time=frame_time, debug_rays=debug_rays,
                                                    ray_debug_path=ray_debug_path, **render_kwargs_test)

            if depth_maps is not None:
                target_depth = depth_maps[img_i].squeeze()

            img_loss = img2mse(rgb, target)
            depth_loss = 0
            if args.depth_loss_weight > 0:
                depth_loss = depth2mse(depth, target_depth)

            loss = img_loss + args.depth_loss_weight * depth_loss
            psnr = mse2psnr(img_loss)

            tqdm_txt = f"[VAL] Iter: {i} Val_loss: {loss.item()} Val_img_loss: {img_loss.item()}"
            if args.depth_loss_weight > 0:
                tqdm_txt += f" Val_depth_loss: {depth_loss.item()}"
            tqdm.write(tqdm_txt)

            writer.add_scalar('val_1_loss', loss.item(), i)
            writer.add_scalar('val_2_img_loss', img_loss.item(), i)
            writer.add_scalar('val_3_psnr', psnr.item(), i)
            if args.depth_loss_weight > 0:
                writer.add_scalar('val_4_depth_loss', depth_loss.item(), i)     
            writer.add_image('val_5_rgb_gt', to8b(target.cpu().numpy()), i, dataformats='HWC')
            writer.add_image('val_6_rgb', to8b(rgb.cpu().numpy()), i, dataformats='HWC')
            writer.add_image('val_7_disp', disp.cpu().numpy(), i, dataformats='HW')
            writer.add_image('val_8_acc', acc.cpu().numpy(), i, dataformats='HW')
            if depth_maps is not None:
                writer.add_image('val_9_depth_gt', to8b(target_depth.cpu().numpy()/np.max(target_depth.cpu().numpy())), i, dataformats='HW')
                writer.add_image('val_10_depth', to8b(depth.cpu().numpy()/np.max(depth.cpu().numpy())), i, dataformats='HW')

            if 'rgb0' in extras:
                writer.add_image('val_11_rgb_rough', to8b(extras['rgb0'].cpu().numpy()), i, dataformats='HWC')
            if 'disp0' in extras:
                writer.add_image('val_12_disp_rough', extras['disp0'].cpu().numpy(), i, dataformats='HW')
            if 'depth0' in extras:
                writer.add_image('val_13_depth_rough', extras['depth0'].cpu().numpy(), i, dataformats='HW')
            if 'z_std' in extras:
                writer.add_image('val_14_acc_rough', extras['z_std'].cpu().numpy(), i, dataformats='HW')

            writer.flush()

            del loss, img_loss, psnr
            if args.depth_loss_weight > 0:
                del depth_loss
            del rgb, disp, acc, extras

        # Render novel view video
        if i%args.i_video==0:       
            
            print("[Info] Rendering video...")
            with torch.no_grad():
                savedir = os.path.join(basedir, expname, 'frames_{}_spiral_{:06d}_time/'.format(expname, i))
                rgbs, disps, depths = render_path(render_poses, render_times, hwff, args.chunk, render_kwargs_test, savedir=savedir)
            print('[Info] Done, saving', rgbs.shape, disps.shape)
            moviebase = os.path.join(basedir, expname, '{}_spiral_{:06d}_'.format(expname, i))
            imageio.mimwrite(moviebase + 'rgb.mp4', to8b(rgbs), fps=15, quality=8)
            imageio.mimwrite(moviebase + 'disp.mp4', to8b(disps / np.max(disps)), fps=15, quality=8)

        # Rerender images from the test set
        if i%args.i_testset==0:
            testsavedir = os.path.join(basedir, expname, 'testset_{:06d}'.format(i))
            print('[Info] Testing poses shape...', poses[i_test].shape)
            with torch.no_grad():
                if args.use_latent_codes_as_time:
                    nearest_train_i = [get_nearest_train_index(times[i_].item(), [times[_] for _ in i_train]) for i_ in i_test]
                    testset_times = [ray_bending_latents_list[_] for _ in nearest_train_i]
                else:
                    testset_times = torch.Tensor(times[i_test]).to(device)
                render_path(torch.Tensor(poses[i_test]).to(device), testset_times,
                            hwff, args.chunk, render_kwargs_test, gt_imgs=images[i_test], gt_depths=depth_maps[i_test], savedir=testsavedir)
            print('[Info] Saved test set')

        global_step += 1


if __name__=='__main__':
    torch.set_default_tensor_type('torch.cuda.FloatTensor')

    train()
