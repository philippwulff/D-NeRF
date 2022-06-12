import numpy as np
from matplotlib.patches import FancyArrowPatch
from mpl_toolkits.mplot3d import proj3d
import itertools


class Arrow3D(FancyArrowPatch):
    """https://stackoverflow.com/questions/47617952/drawing-a-righthand-coordinate-system-in-mplot3d"""
    def __init__(self, xs, ys, zs, *args, **kwargs):
        FancyArrowPatch.__init__(self, (0, 0), (0, 0), *args, **kwargs)
        self._verts3d = xs, ys, zs

    def draw(self, renderer):
        xs3d, ys3d, zs3d = self._verts3d
        xs, ys, zs = proj3d.proj_transform(xs3d, ys3d, zs3d, renderer.M)
        self.set_positions((xs[0], ys[0]), (xs[1], ys[1]))
        FancyArrowPatch.draw(self, renderer)


def draw_transformed(c2w, ax, axes_len=1.0, edgecolor=None, **kwargs):
    """Draw the camera coordinate frame. Camera-to-world transformation."""
    # R = torch.Tensor(np.array([[-1,0,0],[0,0,1],[0,1,0]])).T @ c2w[:3, :3]
    R = c2w[:3, :3]
    # new_o = torch.Tensor(np.array([[-1,0,0],[0,0,1],[0,1,0]])).T @ c2w[:3, 3]
    new_o = c2w[:3, 3]
    new_x = R @ np.array([axes_len, 0, 0]) + new_o
    new_y = R @ np.array([0, axes_len, 0]) + new_o
    new_z = R @ np.array([0, 0, axes_len]) + new_o
    arrow_prop_dict = dict(mutation_scale=20, arrowstyle='-|>', shrinkA=0, shrinkB=0, fill=True)
    arrow_prop_dict.update(**kwargs)
    arrx = ax.add_artist(Arrow3D([new_o[0], new_x[0]], [new_o[1], new_x[1]], [new_o[2], new_x[2]], **arrow_prop_dict, facecolor='r', edgecolor=edgecolor if edgecolor else "r"))
    arry = ax.add_artist(Arrow3D([new_o[0], new_y[0]], [new_o[1], new_y[1]], [new_o[2], new_y[2]], **arrow_prop_dict, facecolor='b', edgecolor=edgecolor if edgecolor else "b"))
    arrz = ax.add_artist(Arrow3D([new_o[0], new_z[0]], [new_o[1], new_z[1]], [new_o[2], new_z[2]], **arrow_prop_dict, facecolor='g', edgecolor=edgecolor if edgecolor else "g"))
    return arrx, arry, arrz, new_o


def draw_cam(rays_o, rays_d, ax, focal_dist=1.):
    H, W, _ = rays_d.shape
    ps = []
    # plot camera rays
    for iy, ix in [[0, 0], [H-1, 0], [0, W-1], [H-1, W-1]]:
        # o = torch.Tensor(np.array([[-1,0,0],[0,0,1],[0,1,0]])).T @ rays_o[iy, ix]
        # d = torch.Tensor(np.array([[-1,0,0],[0,0,1],[0,1,0]])).T @ rays_d[iy, ix]
        o = rays_o[iy, ix]
        d = rays_d[iy, ix]
        
        p = o + focal_dist * d
        ax.plot([o[0], p[0]], [o[1], p[1]], [o[2], p[2]], color="black")
        ps.append(p)
    # plot camera frame
    for p1, p2 in list(itertools.permutations(ps, 2)):
        ax.plot([p1[0], p2[0]], [p1[1], p2[1]], [p1[2], p2[2]], color="grey", ls="--")