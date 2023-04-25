#
#
#      0=================================0
#      |    Kernel Point Convolutions    |
#      0=================================0
#
#
# ----------------------------------------------------------------------------------------------------------------------
#
#      Define network blocks
#
# ----------------------------------------------------------------------------------------------------------------------
#
#      Hugues THOMAS - 06/03/2020
#


import time
import math
from matplotlib.font_manager import weight_dict
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.parameter import Parameter
from torch.nn.init import kaiming_uniform_
from kernels.kernel_points import load_kernels

from utils.ply import write_ply
from utils.TriplaneConv_util import get_scorenet_input, knn, feat_trans_pointnet, ScoreNet, pc_normalize, Mlps

# ----------------------------------------------------------------------------------------------------------------------
#
#           Simple functions
#       \**********************/
#


def gather(x, idx, method=2):
    """
    implementation of a custom gather operation for faster backwards.
    :param x: input with shape [N, D_1, ... D_d]
    :param idx: indexing with shape [n_1, ..., n_m]
    :param method: Choice of the method
    :return: x[idx] with shape [n_1, ..., n_m, D_1, ... D_d]
    """

    if method == 0:
        return x[idx]
    elif method == 1:
        x = x.unsqueeze(1)
        x = x.expand((-1, idx.shape[-1], -1))
        idx = idx.unsqueeze(2)
        idx = idx.expand((-1, -1, x.shape[-1]))
        return x.gather(0, idx)
    elif method == 2:
        for i, ni in enumerate(idx.size()[1:]):
            x = x.unsqueeze(i+1)
            new_s = list(x.size())
            new_s[i+1] = ni
            x = x.expand(new_s)
        n = len(idx.size())
        for i, di in enumerate(x.size()[n:]):
            idx = idx.unsqueeze(i+n)
            new_s = list(idx.size())
            new_s[i+n] = di
            idx = idx.expand(new_s)
        return x.gather(0, idx)
    else:
        raise ValueError('Unkown method')


def radius_gaussian(sq_r, sig, eps=1e-9):
    """
    Compute a radius gaussian (gaussian of distance)
    :param sq_r: input radiuses [dn, ..., d1, d0]
    :param sig: extents of gaussians [d1, d0] or [d0] or float
    :return: gaussian of sq_r [dn, ..., d1, d0]
    """
    return torch.exp(-sq_r / (2 * sig**2 + eps))


def closest_pool(x, inds):
    """
    Pools features from the closest neighbors. WARNING: this function assumes the neighbors are ordered.
    :param x: [n1, d] features matrix
    :param inds: [n2, max_num] Only the first column is used for pooling
    :return: [n2, d] pooled features matrix
    """

    # Add a last row with minimum features for shadow pools
    x = torch.cat((x, torch.zeros_like(x[:1, :])), 0)

    # Get features for each pooling location [n2, d]
    return gather(x, inds[:, 0])


def max_pool(x, inds):
    """
    Pools features with the maximum values.
    :param x: [n1, d] features matrix
    :param inds: [n2, max_num] pooling indices
    :return: [n2, d] pooled features matrix
    """

    # Add a last row with minimum features for shadow pools
    x = torch.cat((x, torch.zeros_like(x[:1, :])), 0)

    # Get all features for each pooling location [n2, max_num, d]
    pool_features = gather(x, inds)

    # Pool the maximum [n2, d]
    max_features, _ = torch.max(pool_features, 1)
    return max_features


def global_average(x, batch_lengths):
    """
    Block performing a global average over batch pooling
    :param x: [N, D] input features
    :param batch_lengths: [B] list of batch lengths
    :return: [B, D] averaged features
    """

    # Loop over the clouds of the batch
    averaged_features = []
    i0 = 0
    for b_i, length in enumerate(batch_lengths):

        # Average features for each batch cloud
        averaged_features.append(torch.mean(x[i0:i0 + length], dim=0))

        # Increment for next cloud
        i0 += length

    # Average features in each batch
    return torch.stack(averaged_features)


# ----------------------------------------------------------------------------------------------------------------------
#
#           TriplaneConv class
#       \******************/
#

class TriplaneConv(nn.Module):
    def __init__(self, kernel_size, p_dim, in_channels, out_channels, layer_ind, aggregation_mode='sum'):
        super(TriplaneConv, self).__init__()
        self.calc_scores = 'softmax'
        self.padding_with_center = False 
        self.no_mlp = False
        self.LoDs = True
        self.k_cin = False
        self.depth_wise = False
        self.with_trick = False
        self.precompute_indices = False
        self.matMode = [[0,1], [0,2], [1,2]]
        self.vecMode =  [2, 1, 0]
        self.method = 'Triplane'
        self.baseGridSize = 128
        self.n_comp = 8
        if self.LoDs:
            self.neiGridSize = int(self.baseGridSize / (2**layer_ind))
        else:
            self.neiGridSize = self.baseGridSize
        self.m = 8
        self.cin = in_channels
        self.cout = out_channels


        self.nei_plane= self.init_one_svd(self.n_comp, self.neiGridSize, 0.1)

        if self.method=='VM' or self.method=='Triplane':
            if self.with_trick:
                self.mlp_map = Mlps(
                    3*self.n_comp, [self.cin], last_bn_norm=False       
                )
            else:
                self.mlp_map = Mlps(
                    3*self.n_comp, [self.cin*self.cout], last_bn_norm=False       
                )
        self.linear = nn.Linear(self.cin, self.cout)

        self.scorenet = ScoreNet(6, self.m, hidden_unit=[16])

        tensor = nn.init.kaiming_normal_(torch.empty(self.m, self.cin, self.cout), nonlinearity='relu') \
            .permute(1, 0, 2).contiguous().view(self.cin, self.m * self.cout)


        # convolutional weight matrices in Weight Bank:
        self.matrice = nn.Parameter(tensor, requires_grad=True)


    def init_one_svd(self, n_component, neighborGridSize, scale):
        neighbor_plane_coef = []
        for i in range(len(self.vecMode)):
            vec_id = self.vecMode[i]
            mat_id_0, mat_id_1 = self.matMode[i]

            neighbor_plane_coef.append(torch.nn.Parameter(
                scale * torch.randn((1, n_component, neighborGridSize, neighborGridSize))))  #

        return torch.nn.ParameterList(neighbor_plane_coef)

    def bilinear_interpolation(self, res, points):
        """
        Performs bilinear interpolation of points with respect to a grid.

        Parameters:
            points (n, 2): A 2D PyTorch tensor representing the points to interpolate.

        Returns:
            indices (4, n, 2): A 3D PyTorch tensor representing the 2D indices of grid
                for the four nearest points for each input point.
            weights (4, n): A 2D PyTorch tensor representing the weights for each of
                the four points.
        """

        points = points[None]
        _, N, _ = points.shape

        x = points[:, :, 0] * (res - 1)
        y = points[:, :, 1] * (res - 1)

        x1 = torch.floor(torch.clip(x, 0, res - 1 - 1e-5)).int()
        y1 = torch.floor(torch.clip(y, 0, res - 1 - 1e-5)).int()

        x2 = torch.clip(x1 + 1, 0, res - 1).int()
        y2 = torch.clip(y1 + 1, 0, res - 1).int()

        w1 = (x2 - x) * (y2 - y)
        w2 = (x - x1) * (y2 - y)
        w3 = (x2 - x) * (y - y1)
        w4 = (x - x1) * (y - y1)

        id1 = torch.stack((y1, x1), dim=-1)
        id2 = torch.stack((y1, x2), dim=-1)
        id3 = torch.stack((y2, x1), dim=-1)
        id4 = torch.stack((y2, x2), dim=-1)

        indices = torch.stack((id1, id2, id3, id4), dim=1)
        weights = torch.stack((w1, w2, w3, w4), dim=0)

        return indices[0], weights
    
    def tensor_field(self, xyz, nei_plane, mlp_map):
        N = xyz.size(0)*xyz.size(1)
        xyz_nei = xyz.reshape(-1, 3)

        nei_coordinate_plane = torch.stack((xyz_nei[..., self.matMode[0]], xyz_nei[..., self.matMode[1]], xyz_nei[..., self.matMode[2]])).view(3, -1, 1, 2)

        nei_plane_coef_point = []
        for idx_plane in range(len(nei_plane)):
            nei_plane_coef_point.append(F.grid_sample(nei_plane[idx_plane], nei_coordinate_plane[[idx_plane]],
                                                align_corners=True).view(-1, *xyz_nei.shape[:1]))
        if self.no_mlp == True:
            nei_plane_coef_point = torch.sum(torch.stack(nei_plane_coef_point), 0)
        else:
            nei_plane_coef_point= torch.cat(nei_plane_coef_point)
        if self.method=='Triplane':
            if self.no_mlp == True:
                return (nei_plane_coef_point.T).reshape(N, -1)
            return mlp_map(((nei_plane_coef_point.T).reshape(N, -1)).unsqueeze(0), format="BNC").squeeze(0)

    def depth_wise_conv(self, x, neighb_inds, xyz):
        N, k, _ = xyz.shape
        x = x[neighb_inds[:, 0], :] # n, cin
        # output = torch.zeros_like(x)
        # if self.precompute_indices:
        #     for j in range(k):
        #         features = []
        #         for idx_plane in range(len(self.nei_plane)):                    
        #             # Expand bilinear_weights to shape (4, n, m)
        #             bilinear_weights_expanded = bilinear_weights[idx_plane, :, :, j].unsqueeze(-1).expand(-1, -1, self.n_comp) # 4, n, m
                    
        #             # Use advanced indexing to retrieve corresponding elements in self_nei_plane
        #             y_indices = bilinear_indices[idx_plane, :, :, j, 0] # 4, n
        #             x_indices = bilinear_indices[idx_plane, :, :, j, 1] # 4, n
        #             selected_features = self.nei_plane[idx_plane][0, :, y_indices, x_indices] # m, 4, n
        #             features.append(torch.einsum('hnm,mhn->nm', bilinear_weights_expanded, selected_features))
        #         features = torch.cat(features, -1) #n, 3*m
        #         output += self.mlp_map(features.unsqueeze(0), format="BNC").squeeze(0) #n, cin
        # else:
        filter = self.tensor_field(xyz, self.nei_plane, self.mlp_map).view(N, k, -1) # n, k, cin
        x = torch.einsum('nki,ni->ni', filter, x) #n, cin
        x = self.linear(x) # n,cout
        return x
    
    def conv_with_trick(self, x, neighb_inds, xyz):
        """ TriplaneConv layer using Point convolution tricks """
        N, k, _ = xyz.shape
        if self.method=='ScoreNet':
            score = self.scorenet(xyz, calc_scores=self.calc_scores, bias=0)
        else:
            score = self.tensor_field(xyz, self.nei_plane, self.mlp_map).view(N, k, -1) # n, k, m
        """feature transformation:"""
        if self.k_cin: # use n, k, cin as input feature
            x = x[neighb_inds, :].view(-1, self.cin) # n*k, cin
            x = feat_trans_pointnet(point_input=x, kernel=self.matrice, m=self.m).view(N, k, self.m, -1)  # n*, k,  m, cout
            """assemble with scores:"""
            x = torch.einsum('nkmo,nkm->no', x, score) # n, cout
        else:
            x = feat_trans_pointnet(point_input=x, kernel=self.matrice, m=self.m)  # n*, m, cout
            x = x[neighb_inds[:, 0], :].view(N, self.m, -1) # n, m, cout 
            """assemble with scores:"""
            x = torch.einsum('nmo,nkm->no', x, score) # n, cout
        return x
    
    def direct_conv(self, x, neighb_inds, xyz):
        """ TriplaneConv layer using Point convolution tricks """
        N, k, _ = xyz.shape
        x = x[neighb_inds, :] # n, k, cin
        if self.method=='ScoreNet':
            score = self.scorenet(xyz, calc_scores=self.calc_scores, bias=0)
        else:
            kernel = self.tensor_field(xyz, self.nei_plane, self.mlp_map).view(N, k, self.cin, self.cout) # n, k, cin, cout
        """convolution:"""
        x = torch.einsum('nki,nkio->no', x, kernel) # n, cout
        return x
    
    def forward(self, q_pts, s_pts, neighb_inds, neighb_r, x):
        # n = s_pts.shape[0]
        # if self.padding_with_center:
        #     mask = neighb_inds == n
        #     first_elements = neighb_inds[:, 0].unsqueeze(1).expand_as(mask)
        #     neighb_inds = torch.where(mask, first_elements, neighb_inds)
        # else:
        # Add a fake point in the last row for shadow neighbors
        s_pts = torch.cat((s_pts, torch.zeros_like(s_pts[:1, :]) + 1e6), 0)
        # Add a zero feature for shadow neighbors
        x = torch.cat((x, torch.zeros_like(x[:1, :])), 0)
        xyz = s_pts[neighb_inds, :] # get neighbor coordinate: n, k, 3
        xyz = xyz.clone() - q_pts.unsqueeze(1) # Center every neighborhood: n, k, 3
        xyz = xyz / neighb_r #normalize xyz by neigbor radius
        # xyz = torch.sin(xyz*(torch.pi/2))
        if self.depth_wise: 
        # return self.depth_wise_conv(x, neighb_inds, xyz, bilinear_indices[:, :, neighb_inds[:, 0], ...], bilinear_weights[:, :, neighb_inds[:, 0], ...])
            return self.depth_wise_conv(x, neighb_inds, xyz)
        elif self.with_trick:
            return self.conv_with_trick(x, neighb_inds, xyz)
        else:
            return self.direct_conv(x, neighb_inds, xyz)



        
# ----------------------------------------------------------------------------------------------------------------------
#
#           KPConv class
#       \******************/
#

class KPConv(nn.Module):

    def __init__(self, kernel_size, p_dim, in_channels, out_channels, KP_extent, radius, layer_ind,
                 fixed_kernel_points='center', KP_influence='linear', aggregation_mode='sum',
                 deformable=False, modulated=False):
        """
        Initialize parameters for KPConvDeformable.
        :param kernel_size: Number of kernel points.
        :param p_dim: dimension of the point space.
        :param in_channels: dimension of input features.
        :param out_channels: dimension of output features.
        :param KP_extent: influence radius of each kernel point.
        :param radius: radius used for kernel point init. Even for deformable, use the config.conv_radius
        :param fixed_kernel_points: fix position of certain kernel points ('none', 'center' or 'verticals').
        :param KP_influence: influence function of the kernel points ('constant', 'linear', 'gaussian').
        :param aggregation_mode: choose to sum influences, or only keep the closest ('closest', 'sum').
        :param deformable: choose deformable or not
        :param modulated: choose if kernel weights are modulated in addition to deformed
        """
        super(KPConv, self).__init__()

        # Save parameters
        # self.K = kernel_size
        self.K = 10
        self.p_dim = p_dim
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.radius = radius
        self.KP_extent = KP_extent
        self.fixed_kernel_points = fixed_kernel_points
        self.KP_influence = KP_influence
        self.aggregation_mode = aggregation_mode
        self.deformable = deformable
        self.modulated = modulated

        # Triplanes parameters
        self.conv_method = 'Triplanes' # 'Triplanes' or 'Linear'
        self.generative_kernel_points = True
        self.no_mlp = False
        self.LoDs = True
        self.matMode = [[0,1], [0,2], [1,2]]
        self.vecMode =  [2, 1, 0]
        self.baseGridSize = 128
        self.n_comp = 8
        self.layer_ind = layer_ind
        if self.LoDs:
            self.neiGridSize = int(self.baseGridSize / (2**layer_ind))
        else:
            self.neiGridSize = self.baseGridSize

        self.layer_ind2neighbor = [28, 32, 41, 40, 36]
        self.kernel_points_generator = nn.Linear(self.layer_ind2neighbor[layer_ind]*3, self.K*3)
        self.nei_plane= self.init_one_svd(self.n_comp, self.neiGridSize, 0.1)
        self.mlp_map = Mlps(
            3*self.n_comp, [self.in_channels*self.out_channels], last_bn_norm=False       
        )


        tensor = nn.init.kaiming_normal_(torch.empty(self.n_comp, self.in_channels, self.out_channels), nonlinearity='relu') \
            .permute(1, 0, 2).contiguous().view(self.in_channels, self.n_comp * self.out_channels)


        # convolutional weight matrices in Weight Bank:
        self.matrice = nn.Parameter(tensor, requires_grad=True)
        # Running variable containing deformed KP distance to input points. (used in regularization loss)
        self.min_d2 = None
        self.deformed_KP = None
        self.offset_features = None

        # Initialize weights
        self.weights = Parameter(torch.zeros((self.K, in_channels, out_channels), dtype=torch.float32),
                                 requires_grad=True)

        # Initiate weights for offsets
        if deformable:
            if modulated:
                self.offset_dim = (self.p_dim + 1) * self.K
            else:
                self.offset_dim = self.p_dim * self.K
            self.offset_conv = KPConv(self.K,
                                      self.p_dim,
                                      self.in_channels,
                                      self.offset_dim,
                                      KP_extent,
                                      radius,
                                      layer_ind,
                                      fixed_kernel_points=fixed_kernel_points,
                                      KP_influence=KP_influence,
                                      aggregation_mode=aggregation_mode)
            self.offset_bias = Parameter(torch.zeros(self.offset_dim, dtype=torch.float32), requires_grad=True)

        else:
            self.offset_dim = None
            self.offset_conv = None
            self.offset_bias = None

        # Reset parameters
        self.reset_parameters()

        # Initialize kernel points
        self.kernel_points = self.init_KP()

        return

    def init_one_svd(self, n_component, neighborGridSize, scale):
        neighbor_plane_coef = []
        for i in range(len(self.vecMode)):
            vec_id = self.vecMode[i]
            mat_id_0, mat_id_1 = self.matMode[i]

            neighbor_plane_coef.append(torch.nn.Parameter(
                scale * torch.randn((1, n_component, neighborGridSize, neighborGridSize))))  #

        return torch.nn.ParameterList(neighbor_plane_coef)
    
    def reset_parameters(self):
        kaiming_uniform_(self.weights, a=math.sqrt(5))
        if self.deformable:
            nn.init.zeros_(self.offset_bias)
        return

    def init_KP(self):
        """
        Initialize the kernel point positions in a sphere
        :return: the tensor of kernel points
        """

        # Create one kernel disposition (as numpy array). Choose the KP distance to center thanks to the KP extent
        K_points_numpy = load_kernels(self.radius,
                                      self.K,
                                      dimension=self.p_dim,
                                      fixed=self.fixed_kernel_points)

        return Parameter(torch.tensor(K_points_numpy, dtype=torch.float32),
                         requires_grad=False)

    def tensor_field(self, xyz, nei_plane, mlp_map):
        N = xyz.size(0)*xyz.size(1)
        xyz_nei = xyz.reshape(-1, 3)

        nei_coordinate_plane = torch.stack((xyz_nei[..., self.matMode[0]], xyz_nei[..., self.matMode[1]], xyz_nei[..., self.matMode[2]])).view(3, -1, 1, 2)

        nei_plane_coef_point = []
        for idx_plane in range(len(nei_plane)):
            nei_plane_coef_point.append(F.grid_sample(nei_plane[idx_plane], nei_coordinate_plane[[idx_plane]],
                                                align_corners=True).view(-1, *xyz_nei.shape[:1]))
        if self.no_mlp == True:
            nei_plane_coef_point = torch.sum(torch.stack(nei_plane_coef_point), 0)
            return (nei_plane_coef_point.T).reshape(N, -1)
        else:
            nei_plane_coef_point= torch.cat(nei_plane_coef_point)
            return mlp_map(((nei_plane_coef_point.T).reshape(N, -1)).unsqueeze(0), format="BNC").squeeze(0)

    def forward(self, q_pts, s_pts, neighb_inds, x):

        ###################
        # Offset generation
        ###################
        #set kernel points generator
        # if self.generative_kernel_points:
        #     if self.kernel_points_generator is None:
        #         self.kernel_points_generator = nn.Linear(neighb_inds.shape[1]*3, self.K*3).to(neighb_inds.device)
        if self.deformable:

            # Get offsets with a KPConv that only takes part of the features
            self.offset_features = self.offset_conv(q_pts, s_pts, neighb_inds, x) + self.offset_bias

            if self.modulated:

                # Get offset (in normalized scale) from features
                unscaled_offsets = self.offset_features[:, :self.p_dim * self.K]
                unscaled_offsets = unscaled_offsets.view(-1, self.K, self.p_dim)

                # Get modulations
                modulations = 2 * torch.sigmoid(self.offset_features[:, self.p_dim * self.K:])

            else:

                # Get offset (in normalized scale) from features
                unscaled_offsets = self.offset_features.view(-1, self.K, self.p_dim)

                # No modulations
                modulations = None

            # Rescale offset for this layer
            offsets = unscaled_offsets * self.KP_extent

        else:
            offsets = None
            modulations = None

        ######################
        # Deformed convolution
        ######################

        # Add a fake point in the last row for shadow neighbors
        s_pts = torch.cat((s_pts, torch.zeros_like(s_pts[:1, :]) + 1e6), 0)

        # Get neighbor points [n_points, n_neighbors, dim]
        max = torch.max(neighb_inds)
        shape = s_pts.shape[0]
        if max >= shape:
            print("wrong_dim: neighb_inds=" + str(max.item()) + " shape= " + str(shape))
        neighbors = s_pts[neighb_inds, :]

        # Center every neighborhood
        neighbors = neighbors - q_pts.unsqueeze(1) # N, K, 3
        N = neighbors.shape[0]

        # Apply offsets to kernel points [n_points, n_kpoints, dim]
        if self.deformable:
            self.deformed_KP = offsets + self.kernel_points
            deformed_K_points = self.deformed_KP.unsqueeze(1)
        else:
            deformed_K_points = self.kernel_points

        # Get all difference matrices [n_points, n_neighbors, n_kpoints, dim]
        deformed_K_points = deformed_K_points.unsqueeze(0).unsqueeze(1)  # Adds new dimensions at positions 0 and 1, resulting in shape [1, 1, 15, 3]
        if self.generative_kernel_points:
            # Generate kernel points from neighbors
            padded_neighbors = torch.cat([neighbors, torch.full((N, self.layer_ind2neighbor[(self.layer_ind)] - neighbors.shape[1], 3), -1).to(neighbors.device)], dim=1)
            deformed_K_points = self.kernel_points_generator(padded_neighbors.view(N, -1)).view(N, -1, 3).unsqueeze(1) # N, 1, m, 3
        neighbors.unsqueeze_(2)
        differences = neighbors - deformed_K_points

        # Get the square distances [n_points, n_neighbors, n_kpoints]
        sq_distances = torch.sum(differences ** 2, dim=3)

        # Optimization by ignoring points outside a deformed KP range
        if self.deformable:

            # Save distances for loss
            self.min_d2, _ = torch.min(sq_distances, dim=1)

            # Boolean of the neighbors in range of a kernel point [n_points, n_neighbors]
            in_range = torch.any(sq_distances < self.KP_extent ** 2, dim=2).type(torch.int32)

            # New value of max neighbors
            new_max_neighb = torch.max(torch.sum(in_range, dim=1))

            # For each row of neighbors, indices of the ones that are in range [n_points, new_max_neighb]
            neighb_row_bool, neighb_row_inds = torch.topk(in_range, new_max_neighb.item(), dim=1)

            # Gather new neighbor indices [n_points, new_max_neighb]
            new_neighb_inds = neighb_inds.gather(1, neighb_row_inds, sparse_grad=False)

            # Gather new distances to KP [n_points, new_max_neighb, n_kpoints]
            neighb_row_inds.unsqueeze_(2)
            neighb_row_inds = neighb_row_inds.expand(-1, -1, self.K)
            sq_distances = sq_distances.gather(1, neighb_row_inds, sparse_grad=False)

            # New shadow neighbors have to point to the last shadow point
            new_neighb_inds *= neighb_row_bool
            new_neighb_inds -= (neighb_row_bool.type(torch.int64) - 1) * int(s_pts.shape[0] - 1)
        else:
            new_neighb_inds = neighb_inds

        # Get Kernel point influences [n_points, n_kpoints, n_neighbors]
        if self.KP_influence == 'constant':
            # Every point get an influence of 1.
            all_weights = torch.ones_like(sq_distances)
            all_weights = torch.transpose(all_weights, 1, 2)

        elif self.KP_influence == 'linear':
            # Influence decrease linearly with the distance, and get to zero when d = KP_extent.
            all_weights = torch.clamp(1 - torch.sqrt(sq_distances) / self.KP_extent, min=0.0)
            all_weights = torch.transpose(all_weights, 1, 2)

        elif self.KP_influence == 'gaussian':
            # Influence in gaussian of the distance.
            sigma = self.KP_extent * 0.3
            all_weights = radius_gaussian(sq_distances, sigma)
            all_weights = torch.transpose(all_weights, 1, 2)
        else:
            raise ValueError('Unknown influence function type (config.KP_influence)')

        # In case of closest mode, only the closest KP can influence each point
        if self.aggregation_mode == 'closest':
            neighbors_1nn = torch.argmin(sq_distances, dim=2)
            all_weights *= torch.transpose(nn.functional.one_hot(neighbors_1nn, self.K), 1, 2)

        elif self.aggregation_mode != 'sum':
            raise ValueError("Unknown convolution mode. Should be 'closest' or 'sum'")

        # Add a zero feature for shadow neighbors
        x = torch.cat((x, torch.zeros_like(x[:1, :])), 0)

        # Get the features of each neighborhood [n_points, n_neighbors, in_fdim]
        neighb_x = gather(x, new_neighb_inds)

        # Apply distance weights [n_points, n_kpoints, in_fdim]
        weighted_features = torch.matmul(all_weights, neighb_x)

        # Apply modulations
        if self.deformable and self.modulated:
            weighted_features *= modulations.unsqueeze(2)

        if self.conv_method == 'Triplanes':
            relative_coord = deformed_K_points.squeeze(1) - q_pts.unsqueeze(1) # N, M, 3 
            N = relative_coord.shape[0] # N
            kernel = self.tensor_field(relative_coord, self.nei_plane, self.mlp_map).view(N, -1, self.in_channels, self.out_channels) # n, m, cin, cout
            kernel_outputs = torch.einsum('nmi,nmio->nmo', weighted_features, kernel)
            return torch.sum(kernel_outputs, dim=1)

        else:
            # Apply network weights [n_kpoints, n_points, out_fdim]
            weighted_features = weighted_features.permute((1, 0, 2))
            kernel_outputs = torch.matmul(weighted_features, self.weights)
            # Convolution sum [n_points, out_fdim]
            return torch.sum(kernel_outputs, dim=0)

    def __repr__(self):
        return 'KPConv(radius: {:.2f}, in_feat: {:d}, out_feat: {:d})'.format(self.radius,
                                                                              self.in_channels,
                                                                              self.out_channels)

# ----------------------------------------------------------------------------------------------------------------------
#
#           Complex blocks
#       \********************/
#

def block_decider(block_name,
                  radius,
                  in_dim,
                  out_dim,
                  layer_ind,
                  config):

    if block_name == 'unary':
        return UnaryBlock(in_dim, out_dim, config.use_batch_norm, config.batch_norm_momentum)

    elif block_name in ['simple',
                        'simple_deformable',
                        'simple_invariant',
                        'simple_equivariant',
                        'simple_strided',
                        'simple_deformable_strided',
                        'simple_invariant_strided',
                        'simple_equivariant_strided']:
        return SimpleBlock(block_name, in_dim, out_dim, radius, layer_ind, config)

    elif block_name in ['resnetb',
                        'resnetb_invariant',
                        'resnetb_equivariant',
                        'resnetb_deformable',
                        'resnetb_strided',
                        'resnetb_deformable_strided',
                        'resnetb_equivariant_strided',
                        'resnetb_invariant_strided']:
        return ResnetBottleneckBlock(block_name, in_dim, out_dim, radius, layer_ind, config)

    elif block_name == 'max_pool' or block_name == 'max_pool_wide':
        return MaxPoolBlock(layer_ind)

    elif block_name == 'global_average':
        return GlobalAverageBlock()

    elif block_name == 'nearest_upsample':
        return NearestUpsampleBlock(layer_ind)

    else:
        raise ValueError('Unknown block name in the architecture definition : ' + block_name)


class BatchNormBlock(nn.Module):

    def __init__(self, in_dim, use_bn, bn_momentum):
        """
        Initialize a batch normalization block. If network does not use batch normalization, replace with biases.
        :param in_dim: dimension input features
        :param use_bn: boolean indicating if we use Batch Norm
        :param bn_momentum: Batch norm momentum
        """
        super(BatchNormBlock, self).__init__()
        self.bn_momentum = bn_momentum
        self.use_bn = use_bn
        self.in_dim = in_dim
        if self.use_bn:
            self.batch_norm = nn.BatchNorm1d(in_dim, momentum=bn_momentum)
            #self.batch_norm = nn.InstanceNorm1d(in_dim, momentum=bn_momentum)
        else:
            self.bias = Parameter(torch.zeros(in_dim, dtype=torch.float32), requires_grad=True)
        return

    def reset_parameters(self):
        nn.init.zeros_(self.bias)

    def forward(self, x):
        if self.use_bn:

            x = x.unsqueeze(2)
            x = x.transpose(0, 2)
            x = self.batch_norm(x)
            x = x.transpose(0, 2)
            return x.squeeze()
        else:
            return x + self.bias

    def __repr__(self):
        return 'BatchNormBlock(in_feat: {:d}, momentum: {:.3f}, only_bias: {:s})'.format(self.in_dim,
                                                                                         self.bn_momentum,
                                                                                         str(not self.use_bn))


class UnaryBlock(nn.Module):

    def __init__(self, in_dim, out_dim, use_bn, bn_momentum, no_relu=False):
        """
        Initialize a standard unary block with its ReLU and BatchNorm.
        :param in_dim: dimension input features
        :param out_dim: dimension input features
        :param use_bn: boolean indicating if we use Batch Norm
        :param bn_momentum: Batch norm momentum
        """

        super(UnaryBlock, self).__init__()
        self.bn_momentum = bn_momentum
        self.use_bn = use_bn
        self.no_relu = no_relu
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.mlp = nn.Linear(in_dim, out_dim, bias=False)
        self.batch_norm = BatchNormBlock(out_dim, self.use_bn, self.bn_momentum)
        if not no_relu:
            self.leaky_relu = nn.LeakyReLU(0.1)
        return

    def forward(self, x, batch=None):
        x = self.mlp(x)
        x = self.batch_norm(x)
        if not self.no_relu:
            x = self.leaky_relu(x)
        return x

    def __repr__(self):
        return 'UnaryBlock(in_feat: {:d}, out_feat: {:d}, BN: {:s}, ReLU: {:s})'.format(self.in_dim,
                                                                                        self.out_dim,
                                                                                        str(self.use_bn),
                                                                                        str(not self.no_relu))


class SimpleBlock(nn.Module):

    def __init__(self, block_name, in_dim, out_dim, radius, layer_ind, config):
        """
        Initialize a simple convolution block with its ReLU and BatchNorm.
        :param in_dim: dimension input features
        :param out_dim: dimension input features
        :param radius: current radius of convolution
        :param config: parameters
        """
        super(SimpleBlock, self).__init__()

        # get KP_extent from current radius
        current_extent = radius * config.KP_extent / config.conv_radius

        # Get other parameters
        self.bn_momentum = config.batch_norm_momentum
        self.use_bn = config.use_batch_norm
        self.layer_ind = layer_ind
        self.block_name = block_name
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.method = config.method

        if config.method == 'KPConv':
            # Define the KPConv class
            self.KPConv = KPConv(config.num_kernel_points,
                                config.in_points_dim,
                                in_dim,
                                out_dim // 2,
                                current_extent,
                                radius,
                                layer_ind,
                                fixed_kernel_points=config.fixed_kernel_points,
                                KP_influence=config.KP_influence,
                                aggregation_mode=config.aggregation_mode,
                                deformable='deform' in block_name,
                                modulated=config.modulated)
        else:
            self.TriplaneConv = TriplaneConv(config.num_kernel_points,
                                config.in_points_dim,
                                in_dim,
                                out_dim // 2,
                                layer_ind)

        # Other opperations
        self.batch_norm = BatchNormBlock(out_dim // 2, self.use_bn, self.bn_momentum)
        self.leaky_relu = nn.LeakyReLU(0.1)

        return

    def forward(self, x, batch):

        if 'strided' in self.block_name:
            q_pts = batch.points[self.layer_ind + 1]
            s_pts = batch.points[self.layer_ind]
            neighb_inds = batch.pools[self.layer_ind]
            neighb_r = batch.neighbor_r[self.layer_ind]
            # bilinear_indices = batch.bilinear_indices[self.layer_ind]
            # bilinear_weights = batch.bilinear_weights[self.layer_ind]
        else:
            q_pts = batch.points[self.layer_ind]
            s_pts = batch.points[self.layer_ind]
            neighb_inds = batch.neighbors[self.layer_ind]
            # bilinear_indices = batch.bilinear_indices[self.layer_ind]
            # bilinear_weights = batch.bilinear_weights[self.layer_ind]
            neighb_r = batch.neighbor_r[self.layer_ind]
            # max = torch.max(neighb_inds)
            # shape = s_pts.shape[0]
            # if max > shape:
            #     print("wrong_dim: neighb_inds=" + str(max.item()) + " shape= " + str(shape))

        if self.method=='KPConv':
            x = self.KPConv(q_pts, s_pts, neighb_inds, x)
        else:
            x = self.TriplaneConv(q_pts, s_pts, neighb_inds, neighb_r, x)
        return self.leaky_relu(self.batch_norm(x))


class ResnetBottleneckBlock(nn.Module):

    def __init__(self, block_name, in_dim, out_dim, radius, layer_ind, config):
        """
        Initialize a resnet bottleneck block.
        :param in_dim: dimension input features
        :param out_dim: dimension input features
        :param radius: current radius of convolution
        :param config: parameters
        """
        super(ResnetBottleneckBlock, self).__init__()

        # get KP_extent from current radius
        current_extent = radius * config.KP_extent / config.conv_radius

        # Get other parameters
        self.bn_momentum = config.batch_norm_momentum
        self.use_bn = config.use_batch_norm
        self.block_name = block_name
        self.layer_ind = layer_ind
        self.in_dim = in_dim
        self.out_dim = out_dim
        self.method = config.method

        # First downscaling mlp
        if in_dim != out_dim // 4:
            self.unary1 = UnaryBlock(in_dim, out_dim // 4, self.use_bn, self.bn_momentum)
        else:
            self.unary1 = nn.Identity()

        # Conv block
        if self.method == 'KPConv':
            self.KPConv = KPConv(config.num_kernel_points,
                                config.in_points_dim,
                                out_dim // 4,
                                out_dim // 4,
                                current_extent,
                                radius,
                                layer_ind,
                                fixed_kernel_points=config.fixed_kernel_points,
                                KP_influence=config.KP_influence,
                                aggregation_mode=config.aggregation_mode,
                                deformable='deform' in block_name,
                                modulated=config.modulated)
        else:
            self.TriplaneConv = TriplaneConv(config.num_kernel_points,
                                config.in_points_dim,
                                out_dim // 4,
                                out_dim // 4,
                                layer_ind)
        self.batch_norm_conv = BatchNormBlock(out_dim // 4, self.use_bn, self.bn_momentum)

        # Second upscaling mlp
        self.unary2 = UnaryBlock(out_dim // 4, out_dim, self.use_bn, self.bn_momentum, no_relu=True)

        # Shortcut optional mpl
        if in_dim != out_dim:
            self.unary_shortcut = UnaryBlock(in_dim, out_dim, self.use_bn, self.bn_momentum, no_relu=True)
        else:
            self.unary_shortcut = nn.Identity()

        # Other operations
        self.leaky_relu = nn.LeakyReLU(0.1)

        return

    def forward(self, features, batch):

        if 'strided' in self.block_name:
            q_pts = batch.points[self.layer_ind + 1]
            s_pts = batch.points[self.layer_ind]
            neighb_inds = batch.pools[self.layer_ind]
            neighb_r = batch.neighbor_r[self.layer_ind]            
            # bilinear_indices = batch.bilinear_indices[self.layer_ind]
            # bilinear_weights = batch.bilinear_weights[self.layer_ind]
        else:
            q_pts = batch.points[self.layer_ind]
            s_pts = batch.points[self.layer_ind]
            neighb_inds = batch.neighbors[self.layer_ind]
            neighb_r = batch.neighbor_r[self.layer_ind]
            # bilinear_indices = batch.bilinear_indices[self.layer_ind]
            # bilinear_weights = batch.bilinear_weights[self.layer_ind]

        # First downscaling mlp
        x = self.unary1(features)

        # Convolution
        if self.method=='KPConv':
            x = self.KPConv(q_pts, s_pts, neighb_inds, x)
        else:
            x = self.TriplaneConv(q_pts, s_pts, neighb_inds, neighb_r, x)
        x = self.leaky_relu(self.batch_norm_conv(x))

        # Second upscaling mlp
        x = self.unary2(x)

        # Shortcut
        if 'strided' in self.block_name:
            shortcut = max_pool(features, neighb_inds)
        else:
            shortcut = features
        shortcut = self.unary_shortcut(shortcut)

        return self.leaky_relu(x + shortcut)


class GlobalAverageBlock(nn.Module):

    def __init__(self):
        """
        Initialize a global average block with its ReLU and BatchNorm.
        """
        super(GlobalAverageBlock, self).__init__()
        return

    def forward(self, x, batch):
        return global_average(x, batch.lengths[-1])


class NearestUpsampleBlock(nn.Module):

    def __init__(self, layer_ind):
        """
        Initialize a nearest upsampling block with its ReLU and BatchNorm.
        """
        super(NearestUpsampleBlock, self).__init__()
        self.layer_ind = layer_ind
        return

    def forward(self, x, batch):
        return closest_pool(x, batch.upsamples[self.layer_ind - 1])

    def __repr__(self):
        return 'NearestUpsampleBlock(layer: {:d} -> {:d})'.format(self.layer_ind,
                                                                  self.layer_ind - 1)


class MaxPoolBlock(nn.Module):

    def __init__(self, layer_ind):
        """
        Initialize a max pooling block with its ReLU and BatchNorm.
        """
        super(MaxPoolBlock, self).__init__()
        self.layer_ind = layer_ind
        return

    def forward(self, x, batch):
        return max_pool(x, batch.pools[self.layer_ind + 1])

