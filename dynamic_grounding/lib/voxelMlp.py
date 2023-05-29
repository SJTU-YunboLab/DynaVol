import os
import time
import functools
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn import init

from torch_scatter import segment_coo

from torch.utils.cpp_extension import load
parent_dir = os.path.dirname(os.path.abspath(__file__))
render_utils_cuda = load(
        name='render_utils_cuda',
        sources=[
            os.path.join(parent_dir, path)
            for path in ['cuda/render_utils.cpp', 'cuda/render_utils_kernel.cu']],
        verbose=True)

total_variation_cuda = load(
        name='total_variation_cuda',
        sources=[
            os.path.join(parent_dir, path)
            for path in ['cuda/total_variation.cpp', 'cuda/total_variation_kernel.cu']],
        verbose=True)

import lib.networks as networks
from lib_extra.attention import SlotAttention

import pdb

'''Model'''
class VoxelMlp(torch.nn.Module):
    def __init__(self, xyz_min, xyz_max,
                 num_voxels=0, num_voxels_base=0,
                 alpha_init=None,
                 mask_cache_path=None, mask_cache_thres=1e-3,
                 fast_color_thres=0,
                 **kwargs):
        super(VoxelMlp, self).__init__()
        print('Slots Decouple without slots_m.')
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.register_buffer('xyz_min', torch.Tensor(xyz_min))
        self.register_buffer('xyz_max', torch.Tensor(xyz_max))
        self.fast_color_thres = fast_color_thres

        # determine based grid resolution
        self.num_voxels_base = num_voxels_base
        self.voxel_size_base = ((self.xyz_max - self.xyz_min).prod() / self.num_voxels_base).pow(1/3)

        # determine the density bias shift
        self.alpha_init = alpha_init
        self.act_shift = np.log(1/(1-alpha_init) - 1)
        print('voxelMlp: set density bias shift to', self.act_shift)

        # determine init grid resolution
        self._set_grid_resolution(num_voxels)

        # init density voxel grid
        self.density = torch.nn.Parameter(torch.zeros([1, kwargs['max_instances'], *self.world_size]))

        self.decoder = networks.init_net(Decoder(n_freq=kwargs['n_freq'], n_freq_view=kwargs['n_freq_view'], input_dim=kwargs['n_freq']*6+3+kwargs['z_dim'], 
                               input_ch_dim=6*kwargs['n_freq_view']+3, z_dim=kwargs['z_dim'], n_layers=kwargs['n_layers'], out_ch=kwargs['out_ch']))
        
         # local dynamics -- w/ slots
        self._time, self._time_out = self.create_time_net(input_dim=kwargs['n_freq_t']*6+3,
                                                          input_dim_time=kwargs['n_freq_time']*2+1, D=kwargs['timenet_layers'], W=kwargs['timenet_hidden'], skips=kwargs['skips'])
        self.skips = kwargs['skips']
        self.n_freq_t = kwargs['n_freq_t']
        self.n_freq_time = kwargs['n_freq_time']


        # slots initialization
        self.num_slots = kwargs['max_instances']
        self.slots_o = torch.randn(1, self.num_slots, kwargs['z_dim'])

        self.z_dim = kwargs['z_dim']


        if 'last_episode_o' in kwargs.keys():
            self.last_episode_o = kwargs['last_episode_o']
        else:
            self.last_episode_o = torch.zeros_like(self.slots_o)

        if 'curr_episode_o' in kwargs.keys():
            self.curr_episode_o_episode = kwargs['curr_episode_o']
        else:
            self.curr_episode_o_episode = torch.zeros((kwargs['timesteps'], 1, self.num_slots, kwargs['z_dim']))

        self.slot_attention = SlotAttention(
            voxel_dim=kwargs['max_instances'],
            in_dim=kwargs['encoder_dim'],
            slot_dim=kwargs['z_dim'],
            iters=kwargs['num_iterations'],
            hidden_dim=kwargs['hidden'],
            kernel_size=kwargs['kernel_size'],
            stride=kwargs['stride'],
        )


        self.kwargs = kwargs
        self.last_timestep = -1

        self.mask_cache = None
        self.nonempty_mask = None 
        self.mask_cache_path = mask_cache_path
        self.mask_cache_thres = mask_cache_thres

    def create_time_net(self, input_dim, input_dim_time, D, W, skips, memory=[]):
        layers = [nn.Linear(input_dim + input_dim_time, W)]
        for i in range(D - 1):
            if i in memory:
                raise NotImplementedError
            else:
                layer = nn.Linear

            in_channels = W
            if i in skips:
                in_channels += input_dim

            layers += [layer(in_channels, W)]
        return nn.ModuleList(layers), nn.Linear(W, 3)

    def query_time(self, new_pts, t, net, net_final, pdb_flag=0):
        # if pdb_flag == 1:
        #     pdb.set_trace()
        pts_sim = sin_emb(new_pts, n_freq=self.n_freq_t)
        t_sim = sin_emb(t.expand([new_pts.shape[0], 1]), n_freq=self.n_freq_time)
        h = torch.cat([pts_sim, t_sim], dim=-1)
        for i, l in enumerate(net):
            h = net[i](h)
            h = F.relu(h)
            if i in self.skips:
                h = torch.cat([pts_sim, h], -1)

        return net_final(h)

    def _set_grid_resolution(self, num_voxels):
        # Determine grid resolution
        # pdb.set_trace()
        self.num_voxels = num_voxels
        self.voxel_size = ((self.xyz_max - self.xyz_min).prod() / num_voxels).pow(1/3)
        self.world_size = ((self.xyz_max - self.xyz_min) / self.voxel_size).long()
        self.voxel_size_ratio = self.voxel_size / self.voxel_size_base
        # define the xyz positions of grid
        self.world_pos = torch.zeros([1, 3, *self.world_size])
        xcoord = torch.linspace(start=self.xyz_min[0], end=self.xyz_max[0], steps=self.world_size[0])
        ycoord = torch.linspace(start=self.xyz_min[1], end=self.xyz_max[1], steps=self.world_size[1])
        zcoord = torch.linspace(start=self.xyz_min[2], end=self.xyz_max[2], steps=self.world_size[2])
        grid = torch.meshgrid(xcoord, ycoord, zcoord)
        for i in range(3):
            self.world_pos[0, i, :, :, :] = grid[i]    
        print('voxelMlp: voxel_size      ', self.voxel_size)
        print('voxelMlp: world_size      ', self.world_size)
        print('voxelMlp: voxel_size_base ', self.voxel_size_base)
        print('voxelMlp: voxel_size_ratio', self.voxel_size_ratio)

    def get_kwargs(self):
        return {
            'xyz_min': self.xyz_min.cpu().numpy(),
            'xyz_max': self.xyz_max.cpu().numpy(),
            'num_voxels': self.num_voxels,
            'num_voxels_base': self.num_voxels_base,
            'alpha_init': self.alpha_init,
            'act_shift': self.act_shift,
            'voxel_size_ratio': self.voxel_size_ratio,
            'mask_cache_path': self.mask_cache_path,
            'mask_cache_thres': self.mask_cache_thres,
            'fast_color_thres': self.fast_color_thres,
            'n_freq': self.kwargs['n_freq'],
            'n_freq_view': self.kwargs['n_freq_view'],
            'n_freq_dynamics': self.kwargs['n_freq_dynamics'],
            'z_dim': self.kwargs['z_dim'],
            'm_dim': self.kwargs['m_dim'],
            'hidden': self.kwargs['hidden'],
            'n_layers': self.kwargs['n_layers'],
            "out_ch": self.kwargs['out_ch'],
            "max_instances": self.kwargs['max_instances'],
            "dropout": self.kwargs['dropout'],
            "encoder_dim": self.kwargs['encoder_dim'],
            "num_iterations": self.kwargs['num_iterations'],
            "weight_init": self.kwargs['weight_init'],
            "kernel_size": self.kwargs['kernel_size'],
            "stride": self.kwargs['stride'],
            "n_freq_t": self.kwargs['n_freq_t'],
            "n_freq_time": self.kwargs['n_freq_time'],
            "timesteps": self.kwargs['timesteps'],
            "timenet_layers": self.kwargs['timenet_layers'],
            "timenet_hidden": self.kwargs['timenet_hidden'],
            "skips": self.kwargs['skips'],
            "last_episode_o": torch.mean(self.curr_episode_o_episode, dim=0)
        }

    @torch.no_grad()
    def maskout_near_cam_vox(self, cam_o, near):
        self_grid_xyz = torch.stack(torch.meshgrid(
            torch.linspace(self.xyz_min[0], self.xyz_max[0], self.density.shape[2]),
            torch.linspace(self.xyz_min[1], self.xyz_max[1], self.density.shape[3]),
            torch.linspace(self.xyz_min[2], self.xyz_max[2], self.density.shape[4]),
        ), -1)
        nearest_dist = torch.stack([
            (self_grid_xyz.unsqueeze(-2) - co).pow(2).sum(-1).sqrt().amin(-1)
            for co in cam_o.split(100)  # for memory saving
        ]).amin(0)

        nearest_dist = nearest_dist[None, None].expand(-1, self.density.shape[1], -1, -1, -1)
        # self.density[nearest_dist[None,None] <= near] = -100
        self.density[nearest_dist <= near] = -100

    @torch.no_grad()
    def scale_volume_grid(self, num_voxels):
        print('voxelMlp: scale_volume_grid start')
        ori_world_size = self.world_size
        self._set_grid_resolution(num_voxels)
        print('voxelMlp: scale_volume_grid scale world_size from', ori_world_size, 'to', self.world_size)

        self.density = torch.nn.Parameter(
            F.interpolate(self.density.data, size=tuple(self.world_size), mode='trilinear', align_corners=True))

        mask_cache = MaskCache(
                path=self.mask_cache_path,
                mask_cache_thres=self.mask_cache_thres).to(self.xyz_min.device)
        self_grid_xyz = torch.stack(torch.meshgrid(
            torch.linspace(self.xyz_min[0], self.xyz_max[0], self.density.shape[2]),
            torch.linspace(self.xyz_min[1], self.xyz_max[1], self.density.shape[3]),
            torch.linspace(self.xyz_min[2], self.xyz_max[2], self.density.shape[4]),
        ), -1)
        self_alpha = F.max_pool3d(self.activate_density(self.density), kernel_size=3, padding=1, stride=1)[0,0]
        self.mask_cache = MaskCache(
                path=None, mask=mask_cache(self_grid_xyz) & (self_alpha>self.fast_color_thres),
                xyz_min=self.xyz_min, xyz_max=self.xyz_max)

        print('voxelMlp: scale_volume_grid finish')

    def voxel_count_views(self, rays_o_tr, rays_d_tr, imsz, near, far, stepsize, downrate=1, irregular_shape=False):
        print('voxelMlp: voxel_count_views start')
        eps_time = time.time()
        N_samples = int(np.linalg.norm(np.array(self.density.shape[2:])+1) / stepsize) + 1
        rng = torch.arange(N_samples)[None].float()
        count = torch.zeros_like(self.density.detach())
        device = rng.device
        for rays_o_, rays_d_ in zip(rays_o_tr.split(imsz), rays_d_tr.split(imsz)):
            ones = torch.ones_like(self.density).requires_grad_()
            if irregular_shape:
                rays_o_ = rays_o_.split(10000)
                rays_d_ = rays_d_.split(10000)
            else:
                rays_o_ = rays_o_[::downrate, ::downrate].to(device).flatten(0,-2).split(10000)
                rays_d_ = rays_d_[::downrate, ::downrate].to(device).flatten(0,-2).split(10000)

            for rays_o, rays_d in zip(rays_o_, rays_d_):
                vec = torch.where(rays_d==0, torch.full_like(rays_d, 1e-6), rays_d)
                rate_a = (self.xyz_max - rays_o) / vec
                rate_b = (self.xyz_min - rays_o) / vec
                t_min = torch.minimum(rate_a, rate_b).amax(-1).clamp(min=near, max=far)
                t_max = torch.maximum(rate_a, rate_b).amin(-1).clamp(min=near, max=far)
                step = stepsize * self.voxel_size * rng
                interpx = (t_min[...,None] + step/rays_d.norm(dim=-1,keepdim=True))
                rays_pts = rays_o[...,None,:] + rays_d[...,None,:] * interpx[...,None]
                self.grid_sampler(rays_pts, ones).sum().backward()
            with torch.no_grad():
                count += (ones.grad > 1)
        eps_time = time.time() - eps_time
        print('voxelMlp: voxel_count_views finish (eps time:', eps_time, 'sec)')
        return count

    def density_total_variation_add_grad(self, weight, dense_mode):
        weight = weight * self.world_size.max() / 128
        total_variation_cuda.total_variation_add_grad(
            self.density, self.density.grad, weight, weight, weight, dense_mode)

    def activate_density(self, density, interval=None):
        interval = interval if interval is not None else self.voxel_size_ratio
        shape = density.shape
        return Raw2Alpha.apply(density.flatten(), self.act_shift, interval).reshape(shape)

    def activate_density_multiple(self, density, interval=None, dens_noise=0):
        interval = interval if interval is not None else self.voxel_size_ratio
        raw_masks = F.softplus(density + self.act_shift, True)

        raw_sigma = raw_masks + dens_noise * torch.randn_like(raw_masks)

        masks = raw_masks / (raw_masks.sum(dim=-1)[:,None] + 1e-5)  # PxK

        sigma = (raw_sigma * masks).sum(dim=-1)
        alpha = 1 - torch.exp(-sigma * interval)
        return alpha
 
    def grid_sampler(self, xyz, *grids, mode=None, align_corners=True):
        '''Wrapper for the interp operation'''
        mode = 'bilinear'
        shape = xyz.shape[:-1]
        xyz = xyz.reshape(1,1,1,-1,3)
        ind_norm = ((xyz - self.xyz_min) / (self.xyz_max - self.xyz_min)).flip((-1,)) * 2 - 1
        ret_lst = [
            # TODO: use `rearrange' to make it readable
            F.grid_sample(grid, ind_norm, mode=mode, align_corners=align_corners).reshape(grid.shape[1],-1).T.reshape(*shape,grid.shape[1])
            for grid in grids
        ]
        for i in range(len(grids)):
            if ret_lst[i].shape[-1] == 1:
                ret_lst[i] = ret_lst[i].squeeze(-1)
        if len(ret_lst) == 1:
            return ret_lst[0]
        return ret_lst

    def hit_coarse_geo(self, rays_o, rays_d, near, far, stepsize, **render_kwargs):
        '''Check whether the rays hit the solved coarse geometry or not'''
        shape = rays_o.shape[:-1]
        rays_o = rays_o.reshape(-1, 3).contiguous()
        rays_d = rays_d.reshape(-1, 3).contiguous()
        stepdist = stepsize * self.voxel_size
        ray_pts, mask_outbbox, ray_id = render_utils_cuda.sample_pts_on_rays(
                rays_o, rays_d, self.xyz_min, self.xyz_max, near, far, stepdist)[:3]
        mask_inbbox = ~mask_outbbox
        hit = torch.zeros([len(rays_o)], dtype=torch.bool)
        hit[ray_id[mask_inbbox][self.mask_cache(ray_pts[mask_inbbox])]] = 1
        return hit.reshape(shape)

    def sample_ray(self, rays_o, rays_d, near, far, stepsize, is_train=False, **render_kwargs):
        '''Sample query points on rays.
        All the output points are sorted from near to far.
        Input:
            rays_o, rayd_d:   both in [N, 3] indicating ray configurations.
            near, far:        the near and far distance of the rays.
            stepsize:         the number of voxels of each sample step.
        Output:
            ray_pts:          [M, 3] storing all the sampled points.
            ray_id:           [M]    the index of the ray of each point.
            step_id:          [M]    the i'th step on a ray of each point.
        '''
        rays_o = rays_o.contiguous()
        rays_d = rays_d.contiguous()
        stepdist = stepsize * self.voxel_size
        ray_pts, mask_outbbox, ray_id, step_id, N_steps, t_min, t_max = render_utils_cuda.sample_pts_on_rays(
            rays_o, rays_d, self.xyz_min, self.xyz_max, near, far, stepdist)
        mask_inbbox = ~mask_outbbox
        ray_pts = ray_pts[mask_inbbox]
        ray_id = ray_id[mask_inbbox]
        step_id = step_id[mask_inbbox]
        return ray_pts, ray_id, step_id

    def update_density(self, frame_time):
        ray_pts = self.world_pos[0].flatten(start_dim=1).permute(1,0)
        dx = self.query_time(ray_pts, frame_time, self._time, self._time_out)
        density = self.grid_sampler(ray_pts+dx, self.density).permute(1,0).reshape(self.density.shape)
        return density

    def forward(self, rays_o, rays_d, viewdirs, frame_time, time_index, global_step=None, start=False, training_flag=True, first_episode=False, stc_data=False, **render_kwargs):
        '''Volume rendering
        @rays_o:   [N, 3] the starting point of the N shooting rays.
        @rays_d:   [N, 3] the shooting direction of the N rays.
        @viewdirs: [N, 3] viewing direction to compute positional embedding for MLP.
        '''
        assert len(rays_o.shape)==2 and rays_o.shape[-1]==3, 'Only suuport point queries in [N, 3] format'

        ret_dict = {}
        N = len(rays_o)

        # sample points on rays
        ray_pts, ray_id, step_id = self.sample_ray(
            rays_o=rays_o, rays_d=rays_d, is_train=global_step is not None, **render_kwargs)
        interval = render_kwargs['stepsize'] * self.voxel_size_ratio

        
        if self.last_timestep != time_index:
            self.update_flag = 1
            self.last_timestep = time_index
        else:
            self.update_flag = 0


        # pdb.set_trace()
        if training_flag or self.update_flag:
            dx = self.query_time(ray_pts, frame_time, self._time, self._time_out)
            ray_pts_ = ray_pts + dx
            
            dynamics_density = self.update_density(frame_time)
            slots_updated, attn = self.slot_attention(self.slots_o, dynamics_density.detach())
            density = self.grid_sampler(ray_pts_, self.density)
            self.slots = slots_updated.detach()

            if training_flag:
                if start:
                    self.last_episode_o = torch.mean(self.curr_episode_o_episode, dim=0).detach()
                self.curr_episode_o_episode[time_index] = slots_updated
        
        else:
            dx = self.query_time(ray_pts, frame_time, self._time, self._time_out)
            ray_pts_ = ray_pts + dx
            density = self.grid_sampler(ray_pts_, self.density)
            
        # query for alpha w/ post-activation
        if self.density.shape[1] == 1:
            odensity = density[None, :]
        else:
            odensity = density.permute(1,0)

        if training_flag:
            if first_episode:
                mean_slots_o = slots_updated
            else:
                temp_slots_o = torch.cat((self.last_episode_o, slots_updated), dim=0)
                mean_slots_o = torch.mean(temp_slots_o, dim=0, keepdim=True)
        else:
            mean_slots_o = self.last_episode_o

        rgb_all, density_all, multi_rgb, multi_density = self.decoder(ray_pts_, viewdirs, mean_slots_o, odensity, ray_id, self.act_shift)
        
        density = density_all
        rgb = rgb_all

        slots_prob_ori = (multi_density / (torch.sum(multi_density,dim=0,keepdim = True) + 1e-10))   #[7,M]
        slots_prob = slots_prob_ori.permute(1,0) #[M,7]

       
        alpha = 1 - torch.exp(-density * interval)
        if self.fast_color_thres > 0:
            mask = (alpha > self.fast_color_thres)
            ray_id_ = ray_id[mask]
            step_id = step_id[mask]
            density = density[mask]
            alpha = alpha[mask]
            rgb = rgb[mask]
            slots_prob = slots_prob[mask]

        # compute accumulated transmittance
        weights, alphainv_last = Alphas2Weights.apply(alpha, ray_id_, N)
        if self.fast_color_thres > 0:
            mask = (weights > self.fast_color_thres)
            weights = weights[mask]
            alpha = alpha[mask]
            ray_id_ = ray_id_[mask]
            step_id = step_id[mask]
            density = density[mask]
            rgb = rgb[mask]
            slots_prob = slots_prob[mask]

       
        # Ray marching
        rgb_marched = segment_coo(
                src=(weights.unsqueeze(-1) * rgb),
                index=ray_id_,
                out=torch.zeros([N, 3]),
                reduce='sum')

        
        rgb_marched += (alphainv_last.unsqueeze(-1) * render_kwargs['bg'])
        ret_dict.update({
            'alphainv_last': alphainv_last,
            'weights': weights,
            'rgb_marched': rgb_marched,
            'raw_alpha': alpha,
            'raw_rgb': rgb,
            'ray_id': ray_id_,
            'mean_of_slots': self.last_episode_o[0],
        })

        if render_kwargs.get('render_depth', False):
            with torch.no_grad():
                depth = segment_coo(
                        src=(weights * step_id),
                        index=ray_id_,
                        out=torch.zeros([N]),
                        reduce='sum')
            ret_dict.update({'depth': depth})

        if render_kwargs.get('segmentation', True):
            contribution = segment_coo(
                src=(weights.unsqueeze(-1) * slots_prob),
                index=ray_id_,
                out=torch.zeros([N, multi_density.shape[0]]),
                reduce='sum') # [M,slots]
            
            seg_contri = torch.cat([alphainv_last.unsqueeze(-1), contribution], dim=-1) # [N, slots+1]
            segmentation = torch.argmax(seg_contri, dim=-1)
            
            ret_dict.update({'segmentation': segmentation})   #[N]
        
        
        return ret_dict


''' Module for the searched coarse geometry
It supports query for the known free space and unknown space.
'''
class MaskCache(nn.Module):
    def __init__(self, path=None, mask_cache_thres=None, mask=None, xyz_min=None, xyz_max=None):
        super().__init__()
        
        if path is not None:
            st = torch.load(path)
            self.mask_cache_thres = mask_cache_thres
            density = F.max_pool3d(st['model_state_dict']['density'], kernel_size=3, padding=1, stride=1)
            # alpha = 1 - torch.exp(-F.softplus(density + st['model_kwargs']['act_shift']) * st['model_kwargs']['voxel_size_ratio'])
            if density.shape[1] != 1:
                raw_masks = F.softplus(density + st['model_kwargs']['act_shift'])
                masks = raw_masks / (raw_masks.sum(dim=1)[:,None] + 1e-5)
                sigma = (raw_masks * masks).sum(dim=1)
                alpha = 1.-torch.exp(-sigma*st['model_kwargs']['voxel_size_ratio'])[:, None]

            else:
                alpha = 1 - torch.exp(-F.softplus(density + st['model_kwargs']['act_shift']) * st['model_kwargs']['voxel_size_ratio'])
            mask = (alpha >= self.mask_cache_thres).squeeze(0).squeeze(0)
            xyz_min = torch.Tensor(st['model_kwargs']['xyz_min'])
            xyz_max = torch.Tensor(st['model_kwargs']['xyz_max'])
        else:
            mask = mask.bool()
            xyz_min = torch.Tensor(xyz_min)
            xyz_max = torch.Tensor(xyz_max)

        self.register_buffer('mask', mask)
        xyz_len = xyz_max - xyz_min
        self.register_buffer('xyz2ijk_scale', (torch.Tensor(list(mask.shape)) - 1) / xyz_len)
        self.register_buffer('xyz2ijk_shift', -xyz_min * self.xyz2ijk_scale)

    @torch.no_grad()
    def forward(self, xyz):
        '''Skip know freespace
        @xyz:   [..., 3] the xyz in global coordinate.
        '''
        shape = xyz.shape[:-1]
        xyz = xyz.reshape(-1, 3)
        mask = render_utils_cuda.maskcache_lookup(self.mask, xyz, self.xyz2ijk_scale, self.xyz2ijk_shift)
        mask = mask.reshape(shape)
        return mask


''' Misc
'''
class Raw2Alpha(torch.autograd.Function):
    @staticmethod
    def forward(ctx, density, shift, interval):
        '''
        alpha = 1 - exp(-softplus(density + shift) * interval)
              = 1 - exp(-log(1 + exp(density + shift)) * interval)
              = 1 - exp(log(1 + exp(density + shift)) ^ (-interval))
              = 1 - (1 + exp(density + shift)) ^ (-interval)
        '''
        exp, alpha = render_utils_cuda.raw2alpha(density, shift, interval);
        if density.requires_grad:
            ctx.save_for_backward(exp)
            ctx.interval = interval
        return alpha

    @staticmethod
    @torch.autograd.function.once_differentiable
    def backward(ctx, grad_back):
        '''
        alpha' = interval * ((1 + exp(density + shift)) ^ (-interval-1)) * exp(density + shift)'
               = interval * ((1 + exp(density + shift)) ^ (-interval-1)) * exp(density + shift)
        '''
        exp = ctx.saved_tensors[0]
        interval = ctx.interval
        return render_utils_cuda.raw2alpha_backward(exp, grad_back.contiguous(), interval), None, None

class Alphas2Weights(torch.autograd.Function):
    @staticmethod
    def forward(ctx, alpha, ray_id, N):
        weights, T, alphainv_last, i_start, i_end = render_utils_cuda.alpha2weight(alpha, ray_id, N)
        if alpha.requires_grad:
            ctx.save_for_backward(alpha, weights, T, alphainv_last, i_start, i_end)
            ctx.n_rays = N
        return weights, alphainv_last

    @staticmethod
    @torch.autograd.function.once_differentiable
    def backward(ctx, grad_weights, grad_last):
        alpha, weights, T, alphainv_last, i_start, i_end = ctx.saved_tensors
        grad = render_utils_cuda.alpha2weight_backward(
                alpha, weights, T, alphainv_last,
                i_start, i_end, ctx.n_rays, grad_weights, grad_last)
        return grad, None, None




class Decoder(nn.Module):
    def __init__(self, n_freq=5, n_freq_view=3, input_dim=33+64, input_ch_dim=21, z_dim=64, n_layers=3, out_ch=3):
        """
        freq: raised frequency
        input_dim: pos emb dim + voxel grid dim
        z_dim: network latent dim
        n_layers: #layers before/after skip connection.
        """
        super().__init__()
        self.n_freq = n_freq
        self.n_freq_view = n_freq_view
        self.out_ch = out_ch
        before_skip = [nn.Linear(input_dim, z_dim), nn.ReLU(True)]
        after_skip = [nn.Linear(z_dim+input_dim, z_dim), nn.ReLU(True)]
        for i in range(n_layers-1):
            before_skip.append(nn.Linear(z_dim, z_dim))
            before_skip.append(nn.ReLU(True))
            after_skip.append(nn.Linear(z_dim, z_dim))
            after_skip.append(nn.ReLU(True))
        self.before = nn.Sequential(*before_skip)
        self.after = nn.Sequential(*after_skip)
        self.after_latent = nn.Linear(z_dim, z_dim) # feature_linear
        # self.after_shape = nn.Linear(z_dim, self.out_ch - 3) # alpha_linear

        self.views_linears = nn.Sequential(nn.Linear(input_ch_dim + z_dim, z_dim//2),
                                           nn.ReLU(True))
        self.color = nn.Sequential(nn.Linear(z_dim//2, z_dim//4), # rgb_linear
                                     nn.ReLU(True),
                                     nn.Linear(z_dim//4, 3))


    def forward(self, sampling_coor, sampling_view, slots, raw_density, ray_id, act_shift, dens_noise=0.):
        """
        1. pos emb by Fourier
        2. for each instances, decode all points from coord and voxel grid corresponding probability
        input:
            sampling_coor: Px3, P = #points, typically P = NxD
            sampling_view: Nx3
            slots: KxC'(64)
            O: KxPxC, K: #max_instances, C: #feat_dim=1
            dens_noise: Noise added to density
        """
        K = raw_density.shape[0]
        P = sampling_coor.shape[0]

        sampling_coor_ = sin_emb(sampling_coor, n_freq=self.n_freq)
        query_ex = sampling_coor_.expand(K, sampling_coor_.shape[0], sampling_coor_.shape[1]).flatten(end_dim=1) # ((K)*P)*33
        
        sampling_view_ = sin_emb(sampling_view, n_freq=self.n_freq_view)[ray_id,:] # P*21
        query_view = sampling_view_.expand(K, sampling_view_.shape[0], sampling_view_.shape[1]).flatten(end_dim=1)
        
   
        slots_ex = slots.permute(1,0,2).expand(-1, P, -1).flatten(end_dim=1)  # ((K-1)xP)xC
        input = torch.cat([query_ex, slots_ex], dim=1)  # ((K)xP)x(34+C)
        
        # input = query_ex

        tmp = self.before(input)
        tmp = self.after(torch.cat([input, tmp], dim=1))  # ((K)xP)x64
        latent = self.after_latent(tmp)  # ((K)xP)x64
        
        # raw_shape = self.after_shape(tmp).view([K, P]).contiguous()  # ((K)xP)x1 -> (K)xP, density
        h = torch.cat([latent, query_view], -1)
        h = self.views_linears(h)
        raw_rgb = self.color(h).view([K, P, 3]).contiguous()  # ((K)xP)x3 -> (K)xPx3

        raws = torch.cat([raw_rgb, raw_density[..., None]], dim=-1)  # (K)xPx4
        
        raw_masks = F.softplus(raws[:, :, -1:] + act_shift, True)
        raw_sigma = raw_masks + dens_noise * torch.randn_like(raw_masks)

        raw_rgb = (raws[:, :, :3].tanh() + 1) / 2

        if K == 1:
            return raw_rgb[0], raw_sigma.squeeze(-1)[0], raw_rgb, raw_sigma.squeeze(-1)
        
        # else
        masks = raw_masks / (raw_masks.sum(dim=0) + 1e-5)  # KxPx1

        raw_rgb_all = (raw_rgb * masks).sum(dim=0)
        raw_sigma_all = (raw_sigma * masks).sum(dim=0)

        return raw_rgb_all, raw_sigma_all.squeeze(-1), raw_rgb, raw_sigma.squeeze(-1)



class Dynamics_v2(nn.Module):
    def __init__(self, n_freq=3, input_dim=1+21+64, hidden=128, out_dim=4):
        super().__init__()
        self.n_freq = n_freq
        self.network = nn.Sequential(nn.Linear(input_dim, hidden),
                                     nn.ReLU(True),
                                     nn.Linear(hidden, hidden),
                                     nn.ReLU(True),
                                     nn.Linear(hidden, out_dim))
        
    def forward(self, x, coord, slots):
        assert len(coord.shape) == 5
        assert len(x.shape) == 5
        assert len(slots.shape) == 3
        # pdb.set_trace()
        N, C, X, Y, Z = x.shape

        x_ = x.permute(0, 2, 3, 4, 1).flatten()[:, None] # [X*Y*Z*M, 1]
        
        coord_ = coord.permute(0, 2, 3, 4, 1).flatten(end_dim=-2)
        query = sin_emb(coord_, n_freq=self.n_freq)[:, None, :].expand(-1, C, -1).flatten(end_dim=-2)
        
        slots_ = slots.expand(X*Y*Z, -1, -1).flatten(end_dim=-2) # [X*Y*Z, M, Z_dim]

        input = torch.cat([x_, slots_, query], dim=-1)
        output = self.network(input).reshape(N, X, Y, Z, C).permute(0, 4, 1, 2, 3)
        return output



def sin_emb(x, n_freq=5, keep_ori=True):

    """
    create sin embedding for 3d coordinates
    input:
        x: Px3
        n_freq: number of raised frequency
    """
    embedded = []
    if keep_ori:
        embedded.append(x)
    emb_fns = [torch.sin, torch.cos]
    freqs = 2. ** torch.linspace(0., n_freq - 1, steps=n_freq)
    for freq in freqs:
        for emb_fn in emb_fns:
            embedded.append(emb_fn(freq * x))
    embedded_ = torch.cat(embedded, dim=1)
    return embedded_



''' Ray and batch
'''
def get_rays(H, W, K, c2w, inverse_y, flip_x, flip_y, mode='center'):
    i, j = torch.meshgrid(
        torch.linspace(0, W-1, W, device=c2w.device),
        torch.linspace(0, H-1, H, device=c2w.device))  # pytorch's meshgrid has indexing='ij'
    i = i.t().float()
    j = j.t().float()
    if mode == 'lefttop':
        pass
    elif mode == 'center':
        i, j = i+0.5, j+0.5
    elif mode == 'random':
        i = i+torch.rand_like(i)
        j = j+torch.rand_like(j)
    else:
        raise NotImplementedError

    if flip_x:
        i = i.flip((1,))
    if flip_y:
        j = j.flip((0,))
    if inverse_y:
        dirs = torch.stack([(i-K[0][2])/K[0][0], (j-K[1][2])/K[1][1], torch.ones_like(i)], -1)
    else:
        dirs = torch.stack([(i-K[0][2])/K[0][0], -(j-K[1][2])/K[1][1], -torch.ones_like(i)], -1)
    # Rotate ray directions from camera frame to the world frame
    rays_d = torch.sum(dirs[..., np.newaxis, :] * c2w[:3,:3], -1)  # dot product, equals to: [c2w.dot(dir) for dir in dirs]
    # Translate camera frame's origin to the world frame. It is the origin of all rays.
    rays_o = c2w[:3,3].expand(rays_d.shape)
    return rays_o, rays_d


def get_rays_np(H, W, K, c2w):
    i, j = np.meshgrid(np.arange(W, dtype=np.float32), np.arange(H, dtype=np.float32), indexing='xy')
    dirs = np.stack([(i-K[0][2])/K[0][0], -(j-K[1][2])/K[1][1], -np.ones_like(i)], -1)
    # Rotate ray directions from camera frame to the world frame
    rays_d = np.sum(dirs[..., np.newaxis, :] * c2w[:3,:3], -1)  # dot product, equals to: [c2w.dot(dir) for dir in dirs]
    # Translate camera frame's origin to the world frame. It is the origin of all rays.
    rays_o = np.broadcast_to(c2w[:3,3], np.shape(rays_d))
    return rays_o, rays_d


def ndc_rays(H, W, focal, near, rays_o, rays_d):
    # Shift ray origins to near plane
    t = -(near + rays_o[...,2]) / rays_d[...,2]
    rays_o = rays_o + t[...,None] * rays_d

    # Projection
    o0 = -1./(W/(2.*focal)) * rays_o[...,0] / rays_o[...,2]
    o1 = -1./(H/(2.*focal)) * rays_o[...,1] / rays_o[...,2]
    o2 = 1. + 2. * near / rays_o[...,2]

    d0 = -1./(W/(2.*focal)) * (rays_d[...,0]/rays_d[...,2] - rays_o[...,0]/rays_o[...,2])
    d1 = -1./(H/(2.*focal)) * (rays_d[...,1]/rays_d[...,2] - rays_o[...,1]/rays_o[...,2])
    d2 = -2. * near / rays_o[...,2]

    rays_o = torch.stack([o0,o1,o2], -1)
    rays_d = torch.stack([d0,d1,d2], -1)

    return rays_o, rays_d


def get_rays_of_a_view(H, W, K, c2w, ndc, inverse_y, flip_x, flip_y, mode='center'):
    rays_o, rays_d = get_rays(H, W, K, c2w, inverse_y=inverse_y, flip_x=flip_x, flip_y=flip_y, mode=mode)
    viewdirs = rays_d / rays_d.norm(dim=-1, keepdim=True)
    if ndc:
        rays_o, rays_d = ndc_rays(H, W, K[0][0], 1., rays_o, rays_d)
    return rays_o, rays_d, viewdirs


@torch.no_grad()
def get_training_rays(rgb_tr, train_poses, HW, Ks, ndc, inverse_y, flip_x, flip_y):
    print('get_training_rays: start')
    assert len(np.unique(HW, axis=0)) == 1
    assert len(np.unique(Ks.reshape(len(Ks),-1), axis=0)) == 1
    assert len(rgb_tr) == len(train_poses) and len(rgb_tr) == len(Ks) and len(rgb_tr) == len(HW)
    H, W = HW[0]
    K = Ks[0]
    eps_time = time.time()
    rays_o_tr = torch.zeros([len(rgb_tr), H, W, 3], device=rgb_tr.device)
    rays_d_tr = torch.zeros([len(rgb_tr), H, W, 3], device=rgb_tr.device)
    viewdirs_tr = torch.zeros([len(rgb_tr), H, W, 3], device=rgb_tr.device)
    imsz = [1] * len(rgb_tr)
    for i, c2w in enumerate(train_poses):
        rays_o, rays_d, viewdirs = get_rays_of_a_view(
                H=H, W=W, K=K, c2w=c2w, ndc=ndc, inverse_y=inverse_y, flip_x=flip_x, flip_y=flip_y)
        rays_o_tr[i].copy_(rays_o.to(rgb_tr.device))
        rays_d_tr[i].copy_(rays_d.to(rgb_tr.device))
        viewdirs_tr[i].copy_(viewdirs.to(rgb_tr.device))
        del rays_o, rays_d, viewdirs
    eps_time = time.time() - eps_time
    print('get_training_rays: finish (eps time:', eps_time, 'sec)')
    return rgb_tr, rays_o_tr, rays_d_tr, viewdirs_tr, imsz

@torch.no_grad()
def get_random_rays(random_poses, HW, Ks, ndc, inverse_y, flip_x, flip_y):
    print('get_random_rays: start')
    assert len(np.unique(HW, axis=0)) == 1
    assert len(np.unique(Ks.reshape(len(Ks),-1), axis=0)) == 1
    assert len(random_poses) == len(Ks) and len(random_poses) == len(HW)
    H, W = HW[0]
    H = int(H)
    W = int(W)
    K = Ks[0]
    eps_time = time.time()
    rays_o_tr = torch.zeros([len(random_poses), H, W, 3], device=random_poses.device)
    rays_d_tr = torch.zeros([len(random_poses), H, W, 3], device=random_poses.device)
    viewdirs_tr = torch.zeros([len(random_poses), H, W, 3], device=random_poses.device)
    imsz = [1] * len(random_poses)
    for i, c2w in enumerate(random_poses):
        rays_o, rays_d, viewdirs = get_rays_of_a_view(
                H=H, W=W, K=K, c2w=c2w, ndc=ndc, inverse_y=inverse_y, flip_x=flip_x, flip_y=flip_y)
        rays_o_tr[i].copy_(rays_o.to(random_poses.device))
        rays_d_tr[i].copy_(rays_d.to(random_poses.device))
        viewdirs_tr[i].copy_(viewdirs.to(random_poses.device))
        del rays_o, rays_d, viewdirs
    eps_time = time.time() - eps_time
    print('get_random_rays: finish (eps time:', eps_time, 'sec)')
    return rays_o_tr, rays_d_tr, viewdirs_tr, imsz    


@torch.no_grad()
def get_training_rays_flatten(rgb_tr_ori, train_poses, HW, Ks, ndc, inverse_y, flip_x, flip_y):
    print('get_training_rays_flatten: start')
    assert len(rgb_tr_ori) == len(train_poses) and len(rgb_tr_ori) == len(Ks) and len(rgb_tr_ori) == len(HW)
    eps_time = time.time()
    DEVICE = rgb_tr_ori[0].device
    N = sum(im.shape[0] * im.shape[1] for im in rgb_tr_ori)
    rgb_tr = torch.zeros([N,3], device=DEVICE)
    rays_o_tr = torch.zeros_like(rgb_tr)
    rays_d_tr = torch.zeros_like(rgb_tr)
    viewdirs_tr = torch.zeros_like(rgb_tr)
    imsz = []
    top = 0
    for c2w, img, (H, W), K in zip(train_poses, rgb_tr_ori, HW, Ks):
        assert img.shape[:2] == (H, W)
        rays_o, rays_d, viewdirs = get_rays_of_a_view(
                H=H, W=W, K=K, c2w=c2w, ndc=ndc,
                inverse_y=inverse_y, flip_x=flip_x, flip_y=flip_y)
        n = H * W
        rgb_tr[top:top+n].copy_(img.flatten(0,1))
        rays_o_tr[top:top+n].copy_(rays_o.flatten(0,1).to(DEVICE))
        rays_d_tr[top:top+n].copy_(rays_d.flatten(0,1).to(DEVICE))
        viewdirs_tr[top:top+n].copy_(viewdirs.flatten(0,1).to(DEVICE))
        imsz.append(n)
        top += n

    assert top == N
    eps_time = time.time() - eps_time
    print('get_training_rays_flatten: finish (eps time:', eps_time, 'sec)')
    return rgb_tr, rays_o_tr, rays_d_tr, viewdirs_tr, imsz


@torch.no_grad()
def get_training_rays_in_maskcache_sampling(rgb_tr_ori, train_poses, HW, Ks, ndc, inverse_y, flip_x, flip_y, model, render_kwargs):
    print('get_training_rays_in_maskcache_sampling: start')
    assert len(rgb_tr_ori) == len(train_poses) and len(rgb_tr_ori) == len(Ks) and len(rgb_tr_ori) == len(HW)
    CHUNK = 64
    DEVICE = rgb_tr_ori[0].device
    eps_time = time.time()
    N = sum(im.shape[0] * im.shape[1] for im in rgb_tr_ori)
    rgb_tr = torch.zeros([N,3], device=DEVICE)
    rays_o_tr = torch.zeros_like(rgb_tr)
    rays_d_tr = torch.zeros_like(rgb_tr)
    viewdirs_tr = torch.zeros_like(rgb_tr)
    imsz = []
    top = 0
    for c2w, img, (H, W), K in zip(train_poses, rgb_tr_ori, HW, Ks):
        assert img.shape[:2] == (H, W)
        rays_o, rays_d, viewdirs = get_rays_of_a_view(
                H=H, W=W, K=K, c2w=c2w, ndc=ndc,
                inverse_y=inverse_y, flip_x=flip_x, flip_y=flip_y)
        mask = torch.empty(img.shape[:2], device=DEVICE, dtype=torch.bool)
        for i in range(0, img.shape[0], CHUNK):
            mask[i:i+CHUNK] = model.hit_coarse_geo(
                    rays_o=rays_o[i:i+CHUNK], rays_d=rays_d[i:i+CHUNK], **render_kwargs).to(DEVICE)
        n = mask.sum()
        rgb_tr[top:top+n].copy_(img[mask])
        rays_o_tr[top:top+n].copy_(rays_o[mask].to(DEVICE))
        rays_d_tr[top:top+n].copy_(rays_d[mask].to(DEVICE))
        viewdirs_tr[top:top+n].copy_(viewdirs[mask].to(DEVICE))
        imsz.append(n)
        top += n

    print('get_training_rays_in_maskcache_sampling: ratio', top / N)
    rgb_tr = rgb_tr[:top]
    rays_o_tr = rays_o_tr[:top]
    rays_d_tr = rays_d_tr[:top]
    viewdirs_tr = viewdirs_tr[:top]
    eps_time = time.time() - eps_time
    print('get_training_rays_in_maskcache_sampling: finish (eps time:', eps_time, 'sec)')
    return rgb_tr, rays_o_tr, rays_d_tr, viewdirs_tr, imsz


def batch_indices_generator(N, BS):
    # torch.randperm on cuda produce incorrect results in my machine
    idx, top = torch.LongTensor(np.random.permutation(N)), 0
    while True:
        if top + BS > N:
            idx, top = torch.LongTensor(np.random.permutation(N)), 0
        yield idx[top:top+BS]
        top += BS

