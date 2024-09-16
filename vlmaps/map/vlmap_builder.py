import os
from pathlib import Path
from typing import Any, Dict, List, Tuple, Set

from tqdm import tqdm
import cv2
import torchvision.transforms as transforms
import numpy as np
from omegaconf import DictConfig
import torch
import gdown
import open3d as o3d
from cv_bridge import CvBridge
import time
from geometry_msgs.msg import PoseWithCovarianceStamped
import gc

from vlmaps.utils.lseg_utils import get_lseg_feat
from vlmaps.utils.mapping_utils import (
    load_3d_map,
    save_3d_map,
    base_pos2grid_id_3d,
    base_pos2grid_id_3d_torch
)
from vlmaps.lseg.modules.models.lseg_net import LSegEncNet
import vlmaps.utils.traverse_pixels as raycast
from vlmaps.utils.camera_utils import (FeaturedPC, project_depth_features_pc_torch)
from vlmaps.utils.visualize_utils import visualize_rgb_map_3d

#ROS2 stuff
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener
from tf2_ros import TransformException
from rclpy.node import Node
from sensor_msgs.msg import Image
import message_filters
from sensor_msgs.msg import PointCloud2, PointField
from std_msgs.msg import Header
import struct
import ctypes

import math
import copy
import torch

try:
    from open3d.cuda.pybind.geometry import PointCloud
    from open3d.cuda.pybind.utility import Vector3dVector
    from open3d.cuda.pybind.visualization import draw_geometries
except ImportError:
    from open3d.cpu.pybind.geometry import PointCloud
    from open3d.cpu.pybind.utility import Vector3dVector
    from open3d.cpu.pybind.visualization import draw_geometries

from threading import Lock
from multiprocessing import Process, Queue

_DATATYPES = {}
_DATATYPES[PointField.INT8]    = ('b', 1)
_DATATYPES[PointField.UINT8]   = ('B', 1)
_DATATYPES[PointField.INT16]   = ('h', 2)
_DATATYPES[PointField.UINT16]  = ('H', 2)
_DATATYPES[PointField.INT32]   = ('i', 4)
_DATATYPES[PointField.UINT32]  = ('I', 4)
_DATATYPES[PointField.FLOAT32] = ('f', 4)
_DATATYPES[PointField.FLOAT64] = ('d', 8)

#class VispyVisualizer(Process):
#    def __init__(self, queue):
#        super().__init__()
#        self.queue = queue
#        self.run_flag = True
#    
#    def run(self):
#        canvas = scene.SceneCanvas(keys="interactive", show=True)
#        view = canvas.central_widget.add_view()
#        scatterplot = Markers()
#        view.add(scatterplot)
#        view.camera = 'turntable'
#        #view.camera = scene.cameras.PanZoomCamera()
#
#        while self.run_flag:
#            if not self.queue.empty():
#                points, colors = self.queue.get()
#                scatterplot.set_data(points, edge_color=None, face_color=colors, size=0.05)
#            app.process_events()
#            canvas.update()
#            canvas.draw()
#        
#        canvas.close()
#    
#    def close(self) -> None:
#        self.run_flag = False

class NonBlockingVisualizer(Process):

    def __init__(self, pcd: o3d.geometry.PointCloud):
        super().__init__() 
        self.background = [0, 0, 0]
        self.vis = o3d.visualization.Visualizer()
        self.vis.create_window()
        self.opt = self.vis.get_render_option()
        self.opt.background_color = np.asarray(self.background)
        self.pcd = pcd
        self.vis.add_geometry(self.pcd)
        self.mutex_lock = Lock()
    
    def run(self):
        while True:
            self.mutex_lock.acquire()
            #print("[run] updating geometry")
            self.vis.update_geometry(self.pcd)
            self.mutex_lock.release()
            self.vis.poll_events()
            self.vis.update_renderer()
            
    
    def update_pc(self, new_pcd):
        self.mutex_lock.acquire()
        print("[update_pc] updating passing new pcd")
        self.vis.remove_geometry(self.pcd)
        self.pcd = new_pcd
        self.vis.add_geometry(self.pcd)
        self.vis.update_geometry(self.pcd)
        self.mutex_lock.release()


def quaternion_matrix(quaternion):  #Copied from https://github.com/ros/geometry/blob/noetic-devel/tf/src/tf/transformations.py#L1515
    """Return homogeneous rotation matrix from quaternion.

    >>> R = quaternion_matrix([0.06146124, 0, 0, 0.99810947])
    >>> numpy.allclose(R, rotation_matrix(0.123, (1, 0, 0)))
    True

    """
    # epsilon for testing whether a number is close to zero
    _EPS = np.finfo(float).eps * 4.0

    q = np.array(quaternion[:4], dtype=np.float64, copy=True)
    nq = np.dot(q, q)
    if nq < _EPS:
        return np.identity(4)
    q *= math.sqrt(2.0 / nq)
    q = np.outer(q, q)
    return np.array((
        (1.0-q[1, 1]-q[2, 2],     q[0, 1]-q[2, 3],     q[0, 2]+q[1, 3], 0.0),
        (    q[0, 1]+q[2, 3], 1.0-q[0, 0]-q[2, 2],     q[1, 2]-q[0, 3], 0.0),
        (    q[0, 2]-q[1, 3],     q[1, 2]+q[0, 3], 1.0-q[0, 0]-q[1, 1], 0.0),
        (                0.0,                 0.0,                 0.0, 1.0)
        ), dtype=np.float64)



#### ROS2 wrapper
class VLMapBuilderROS(Node):
    def __init__(
        self,
        map_config: DictConfig
    ):
        super().__init__('VLMap_builder_node')
        self.map_config = map_config
        self.amcl_cov = np.full([36], 0.2)  #init with an high covariance value for each element
        # tf buffer init
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        # subscribers init with callback
        img_topic = self.map_config.img_topic_name      #"/cer/realsense_repeater/color_image"
        depth_topic = self.map_config.depth_topic_name         #"/cer/realsense_repeater/depth_image"
        self.img_sub = message_filters.Subscriber(self, Image, img_topic)
        self.depth_sub = message_filters.Subscriber(self, Image, depth_topic)
        self.tss = message_filters.ApproximateTimeSynchronizer([self.img_sub, self.depth_sub], 1, slop=0.3)        
        self.tss.registerCallback(self.sensors_callback)
        self.amcl_sub = self.create_subscription(
            PoseWithCovarianceStamped,
            "/amcl_pose",
            self.amcl_callback,
            10
        )
        ## First part of create_mobile_base_map for init stuff
        # access config info
        maximum_height = self.map_config.maximum_height
        self.cs = self.map_config.cell_size
        self.gs = self.map_config.grid_size

        self.map_save_dir = self.map_config.map_save_dir #"/home/ergocub"
        os.makedirs(self.map_save_dir, exist_ok=True)
        self.map_save_path = self.map_save_dir + "/" + self.map_config.map_name  #"vlmaps.h5df"

        # init lseg model
        self.lseg_model, self.lseg_transform, self.crop_size, self.base_size, self.norm_mean, self.norm_std = self._init_lseg()

        # init the map
        (
            self.vh,
            self.grid_feat,
            self.grid_pos,
            self.weight,
            self.occupied_ids,
            self.grid_rgb,
            self.mapped_iter_set,
            self.max_id,
            self.loaded_map
        ) = self._init_map(maximum_height, self.cs, self.gs, self.map_save_path)

        self.cv_bridge = CvBridge()

        #### Iteration counter
        self.frame_i = 0
        # load camera calib matrix in config
        self.calib_mat = np.array(self.map_config.cam_calib_mat).reshape((3, 3))
        #self.cv_map = np.zeros((self.gs, self.gs, 3), dtype=np.uint8)
        #self.height_map = -100 * np.ones((self.gs, self.gs), dtype=np.float32)

        ### Make more explicit the calib intrinsics:
        self.focal_lenght_x = self.calib_mat[0,0]       #fx
        self.focal_lenght_y = self.calib_mat[1,1]       #fy
        self.principal_point_x = self.calib_mat[0,2]    #cx or ppx
        self.principal_point_y = self.calib_mat[1,2]    #cy or ppy

        self.target_frame = self.map_config.target_frame     #"map"
        self.source_frame = self.map_config.source_frame     #"depth"
        self.amcl_cov_threshold = self.map_config.amcl_cov_threshold
        if self.amcl_cov_threshold < 0:
            self.amcl_cov_threshold = 0.01

        # TODO: add to config file
        self.use_raycast = self.map_config.use_raycasting
        self.raycasting_algorythm = self.map_config.raycasting_algorythm    #"distance_based"
        self.voxel_offset = self.map_config.voxel_offset
        self.raycast_distance_threshold = self.map_config.raycast_distance_threshold
        # Visualization
        self.pointcloud_pub = self.create_publisher(PointCloud2, "vlmap",10)

        #self.vis_initialized = False
        #self.queue = Queue()


    #def non_blocking_visualize_rgb_map_3d(self, pc: np.ndarray, rgb: np.ndarray, voxel_size=1.0):
    #    grid_rgb = rgb / 255.0
    #    pcd = o3d.geometry.PointCloud()
    #    pcd.points = o3d.utility.Vector3dVector(pc)
    #    pcd.colors = o3d.utility.Vector3dVector(grid_rgb)
    #    # visualize the point cloud
    #    if not self.vis_initialized:
    #        self.visualizer = NonBlockingVisualizer(pcd)
    #        self.vis_initialized = True
    #        self.visualizer.start()
    #    else:
    #        self.visualizer.update_pc(pcd)

    def _get_struct_fmt(is_bigendian, fields, field_names=None):
        fmt = '>' if is_bigendian else '<'

        offset = 0
        for field in (f for f in sorted(fields, key=lambda f: f.offset) if field_names is None or f.name in field_names):
            if offset < field.offset:
                fmt += 'x' * (field.offset - offset)
                offset = field.offset
            if field.datatype not in _DATATYPES:
                print('Skipping unknown PointField datatype [%d]' % field.datatype)
            else:
                datatype_fmt, datatype_length = _DATATYPES[field.datatype]
                fmt    += field.count * datatype_fmt
                offset += field.count * datatype_length

        return fmt

    #
    def create_cloud(self, header, fields, points):
        """
        Create a L{sensor_msgs.msg.PointCloud2} message.

        @param header: The point cloud header.
        @type  header: L{std_msgs.msg.Header}
        @param fields: The point cloud fields.
        @type  fields: iterable of L{sensor_msgs.msg.PointField}
        @param points: The point cloud points.
        @type  points: list of iterables, i.e. one iterable for each point, with the
                       elements of each iterable being the values of the fields for 
                       that point (in the same order as the fields parameter)
        @return: The point cloud.
        @rtype:  L{sensor_msgs.msg.PointCloud2}
        """

        cloud_struct = struct.Struct(self._get_struct_fmt(False, fields))

        buff = ctypes.create_string_buffer(cloud_struct.size * len(points))

        point_step, pack_into = cloud_struct.size, cloud_struct.pack_into
        offset = 0
        for p in points:
            pack_into(buff, offset, *p)
            offset += point_step

        return PointCloud2(header=header,
                           height=1,
                           width=len(points),
                           is_dense=False,
                           is_bigendian=False,
                           fields=fields,
                           point_step=cloud_struct.size,
                           row_step=cloud_struct.size * len(points),
                           data=buff.raw)

    def xyzrgb_array_to_pointcloud2(self, points, colors, stamp=None, frame_id=None, seq=None):
        '''
        Create a sensor_msgs.PointCloud2 from an array
        of points.
        '''

        #points_ren = []
        #lim = points.shape[0]
        #for k in range(lim):
        #    x = points[k,0]
        #    y = points[k,1]
        #    z = points[k,2]
        #    r = int(colors[k,0]/255)
        #    g = int(colors[k,1]/255)
        #    b = int(colors[k,2]/255)
        #    a = int(255)
        #    rgb = struct.unpack('I', struct.pack('BBBB', b, g, r, a))[0]
        #    pt = [x, y, z, rgb]
        #    points_ren.append(pt)

        #fields = [PointField(), PointField(), PointField(), PointField()]
        #fields[0].name = "x"
        #fields[0].offset = 0
        #fields[0].datatype = 7
        #fields[0].count = 1
        #fields[1].name = "y"
        #fields[1].offset = 4
        #fields[1].datatype = 7
        #fields[1].count = 1
        #fields[2].name = "z"
        #fields[2].offset = 8
        #fields[2].datatype = 7
        #fields[2].count = 1
        #fields[3].name = "rgb"
        #fields[3].offset = 16
        #fields[3].datatype = 7
        #fields[3].count = 1

        header = Header()
        header.frame_id = "map"
        header.stamp = self.get_clock().now().to_msg()

        ros_dtype = PointField.FLOAT32
        dtype = np.float32
        itemsize = np.dtype(dtype).itemsize
        fields = [PointField(name=n, offset=i*itemsize, datatype=ros_dtype, count=1) for i, n in enumerate('xyzrgb')]
        nbytes = 6
        xyzrgb = np.array(np.hstack([points, colors/255]), dtype=np.float32)
        #xyzrgb = np.array(points_ren, dtype=np.float32)
        msg = PointCloud2(header=header, 
                          height = 1, 
                          width= points.shape[0], 
                          fields=fields, 
                          is_dense= False, 
                          is_bigedian=False, 
                          point_step=(itemsize * nbytes), 
                          row_step = (itemsize * nbytes * points.shape[0]), 
                          data=xyzrgb.tobytes())
    
        #msg.header = header
        #msg.fields = fields
        #msg.height = 1
        #msg.width = len(points) #480*640
        #msg.is_dense = False
        #msg.is_bigendian = False
        #
        #msg.point_step = 24
        #msg.row_step = msg.point_step * len(points)
        #msg.data = xyzrgb.tostring()

        #msg = self.create_cloud(header, fields, points_ren)
        return msg

    def sensors_callback(self, img_msg, depth_msg):
        """
        build the 3D map centering at the first base frame
        """

        loop_timer = time.time()
        #### first do a TF check between the camera and map frame
        try:
            transform = self.tf_buffer.lookup_transform(
                    self.target_frame,
                    depth_msg.header.frame_id,
                    depth_msg.header.stamp
                    )
        except TransformException as ex:
                self.get_logger().info(
                        f'Could not transform {depth_msg.header.frame_id} to {self.target_frame}: {ex}')
                return
        # Check covariance: TODO is it enough to check only two values?
        if not (abs(self.amcl_cov.max()) < self.amcl_cov_threshold) and (abs(self.amcl_cov.min()) < self.amcl_cov_threshold):
            self.get_logger().info(f'Covariance too big: skipping callback untill amcl converges')
            return

        ## Convert tf2 transform to np array components
        transform_pose_np = np.array([transform.transform.translation.x, transform.transform.translation.y, transform.transform.translation.z])
        transform_quat_np = np.array([transform.transform.rotation.x, transform.transform.rotation.y,
                                        transform.transform.rotation.z, transform.transform.rotation.w])
        ## Let's get an SE(4) matrix form
        transform_np = quaternion_matrix(transform_quat_np)
        transform_np[0:3, -1] = transform_pose_np
        #### Convert Inputs formats:
        ## Convert the rgb format from ros to OpenCv
        rgb = self.cv_bridge.imgmsg_to_cv2(img_msg) # TODO check image color encoding
        ## Convert depth from ros2 to OpenCv
        depth = self.cv_bridge.imgmsg_to_cv2(depth_msg, "passthrough")  # TODO check image color encoding
        depth = depth.astype(np.float16)

        #### Segment image and extract features
        # get pixel-aligned LSeg features
        start = time.time()
        pix_feats = get_lseg_feat(
            self.lseg_model, rgb, ["other", "screen", "table", "closet", "chair", "shelf", "door", "wall", "ceiling", "floor", "human"], self.lseg_transform, self.device, self.crop_size, self.base_size, self.norm_mean, self.norm_std, vis=False
        )
        time_diff = time.time() - start
        self.get_logger().info(f"lseg features extracted in: {time_diff}")

        #### Formatted PC with aligned features to pixel
        start = time.time()
        #featured_pc = self.project_depth_features_pc(depth, pix_feats, rgb, downsample_factor=20)
        camera_pointcloud_xyz, features_per_point, color_per_point = project_depth_features_pc_torch(depth, pix_feats, rgb, self.calib_mat, max_depth=8.0, downsampling_factor = 20)
        featured_pc = FeaturedPC()
        featured_pc.points_xyz = copy.deepcopy(camera_pointcloud_xyz)
        featured_pc.embeddings = copy.deepcopy(features_per_point)
        featured_pc.rgb = copy.deepcopy(color_per_point)
        self.get_logger().info(f"Time for executing project_depth_features_pc: {time.time() - start}")

        #### Transform PC into map frame
        start = time.time()
        pcd_feat = o3d.geometry.PointCloud()
        pcd_feat.points = o3d.utility.Vector3dVector(featured_pc.points_xyz)
        pcd_global = pcd_feat.transform(transform_np)
        featured_pc.points_xyz = np.asarray(pcd_global.points)
        time_diff = time.time() - start
        self.get_logger().info(f"Time for transforming PC in map frame: {time_diff}")
        #o3d.visualization.draw_geometries_with_vertex_selection([pcd_global])


        #### Raycast TODO: move it in a separate thread
        if ((self.frame_i != 0) or (self.loaded_map == True)) and self.use_raycast:

            #map_to_cam_tf = np.linalg.inv(transform_np)
            #cam_pose = map_to_cam_tf[0:3,-1]
            cam_pose = [transform.transform.translation.x, transform.transform.translation.y, transform.transform.translation.z]
            voxels_to_clear = self.raycasting(cam_pose, featured_pc.points_xyz)
            self.remove_map_voxels(voxels_to_clear)


        #### Map update TODO: separate it in another thread
        start = time.time()
        for (point, feature, rgb) in zip(featured_pc.points_xyz, featured_pc.embeddings, featured_pc.rgb):
            
            row, col, height = base_pos2grid_id_3d(self.gs, self.cs, point[0], point[1], point[2])
            if self._out_of_range(row, col, height, self.gs, self.vh):
                #self.get_logger().info(f"out of range with p0 {point[0]} p1 {point[1]} p2 {point[2]}")
                continue

            # when the max_id exceeds the reserved size,
            # double the grid_feat, grid_pos, weight, grid_rgb lengths
            if self.max_id >= self.grid_feat.shape[0]:
                self._reserve_map_space()
            
            # apply the distance weighting according to
            # ConceptFusion https://arxiv.org/pdf/2302.07241.pdf Sec. 4.1, Feature fusion
            radial_dist_sq = np.sum(np.square(point))  
            sigma_sq = 0.6  #TODO parameterize
            alpha = np.exp(-radial_dist_sq / (2 * sigma_sq))

            #feat = pix_feats[0, :, py, px]
            feat = feature
            occupied_id = self.occupied_ids[row, col, height]

            if occupied_id == -1:
                self.occupied_ids[row, col, height] = self.max_id
                self.grid_feat[self.max_id] = feat.flatten() * alpha
                self.grid_rgb[self.max_id] = rgb
                self.weight[self.max_id] += alpha
                self.grid_pos[self.max_id] = [row, col, height]
                self.max_id = self.max_id + 1
            else:
                self.grid_feat[occupied_id] = (
                    self.grid_feat[occupied_id] * self.weight[occupied_id] + feat.flatten() * alpha
                ) / (self.weight[occupied_id] + alpha)
                self.grid_rgb[occupied_id] = (self.grid_rgb[occupied_id] * self.weight[occupied_id] + rgb * alpha) / (
                    self.weight[occupied_id] + alpha    #TODO: check why this can give a value > 255 (BUG)
                )
                self.weight[occupied_id] += alpha
        # Set points with color > 255 to 255, as a sort of saturation (feature fusion bug)
        self.grid_rgb[self.grid_rgb > 255] = 255
        
        self.get_logger().info(f"Time for updating Map: {time.time() - start}")
        self.get_logger().info(f"CALLBACK TIME: {time.time() - loop_timer}")
        # Save map each X callbacks TODO prameterize and do it in a separate thread
        #if self.frame_i % 10 == 0:
        self.get_logger().info(f"Temporarily saving {self.max_id} features at iter {self.frame_i}...")
        time_save = time.time()
        self._save_3d_map(self.grid_feat, self.grid_pos, self.weight, self.grid_rgb, self.occupied_ids, self.mapped_iter_set, self.max_id)
        time_save_diff = time.time() - time_save
        self.get_logger().info(f"Time for Saving Map: {time_save_diff}")
        self.get_logger().info(f"iter {self.frame_i}")
        self.frame_i += 1   # increase counter for map saving purposes

        #remove points that are 0.0
        time_save = time.time()
        mask = (self.grid_pos > 0).all(axis=1)
        color = self.grid_rgb[mask]
        points = self.grid_pos[mask]
        msg = self.xyzrgb_array_to_pointcloud2(points, color)
        self.get_logger().info(f"Time for creating pointcloud2 msg: {time.time() - time_save}")
        self.pointcloud_pub.publish(msg)
        return

    # TODO Let's do this in background on a separate thread, global_pc should not be already added
    def raycasting(self, camera_pose: np.ndarray, camera_cloud: np.ndarray):
        """
        

        :param camera_pose: array of shape (1, 3) with the camera pose expressed in the map frame
        :param camera_cloud: array of shape (N, 3) with the points extracted from the camera depth, expressed in the map frame
        :return: List of lists of voxels to remove from the map
        """
        start = time.time()
        # Pass to tensors
        camera_pose_voxel = base_pos2grid_id_3d(self.gs, self.cs, camera_pose[0], camera_pose[1], camera_pose[2])
        camera_pose_voxel_troch = torch.tensor(camera_pose_voxel, device='cuda', dtype=torch.float32)
        cam_pointcloud_torch = torch.tensor(camera_cloud, device="cuda", dtype=torch.float32)
        cam_voxels_torch = base_pos2grid_id_3d_torch(self.gs, self.cs, cam_pointcloud_torch)
        # Filter points of the camera out of range
        mask = ((cam_voxels_torch > (torch.zeros_like(cam_voxels_torch))) & (cam_voxels_torch < torch.tensor([self.gs, self.gs, self.vh], device="cuda"))).all(dim=1)
        cam_voxels_torch =  cam_voxels_torch[mask]

        if self.raycasting_algorythm == "voxel_traversal":
            voxels_to_clear = (raycast.traverse_pixels_torch(camera_pose_voxel_troch, cam_voxels_torch)).to(torch.int)
            voxels_to_clear = voxels_to_clear.cpu().numpy()
            # Find unique voxels:
            voxels_to_clear = np.unique(voxels_to_clear, axis=0)
            #if len(voxels_to_clear) != 0:
            #    pcd_clear = o3d.geometry.PointCloud()
            #    pcd_clear.points = o3d.utility.Vector3dVector(voxels_to_clear)
            #    colors = np.array([[1, 0, 0] for _ in pcd_clear.points])  # RGB color for red
            #    pcd_clear.colors = o3d.utility.Vector3dVector(colors)
            #    pcd_cloud = o3d.geometry.PointCloud()
            #    pcd_cloud.points = o3d.utility.Vector3dVector(cam_voxels_torch.cpu().numpy())
            #    colors = np.array([[0, 1, 0] for _ in pcd_cloud.points])  # RGB color for green
            #    pcd_cloud.colors = o3d.utility.Vector3dVector(colors)
            #    o3d.visualization.draw_geometries_with_vertex_selection([pcd_clear + pcd_cloud])
        elif self.raycasting_algorythm == "distance_based":
            # Filter points on the map only in the bounding box of the camera pontcloud
            voxels_to_clear_list = []
            #
            camera_batch_sz = 2**10
            for i in range(0, cam_voxels_torch.shape[0], camera_batch_sz):
                pc_end = min(i + camera_batch_sz, cam_voxels_torch.shape[0])
                min_bounds = torch.min(cam_voxels_torch[i:pc_end], dim=0)[0]  # Minimum x, y, z of the camera pointcloud
                max_bounds = torch.max(cam_voxels_torch[i:pc_end], dim=0)[0]  # Maximum x, y, z of the camera pointcloud
                map_points = torch.tensor(self.grid_pos, device='cuda', dtype=torch.float32)
                map_mask = torch.all((map_points >= min_bounds) & (map_points <= max_bounds), dim=1)
                map_points = map_points[map_mask]
                self.get_logger().info(f"map points shape: {map_points.shape}")
                voxels_to_clear = (raycast.raycast_map_torch_efficient(camera_pose_voxel_troch, 
                                                                       cam_voxels_torch[i:pc_end], 
                                                                       map_points, 
                                                                       batch_size= 2**11, 
                                                                       distance_threshold=self.raycast_distance_threshold, 
                                                                       offset=self.voxel_offset)).to(torch.int)
                voxels_to_clear = voxels_to_clear.cpu().numpy()
                voxels_to_clear_list.extend(voxels_to_clear)
            self.get_logger().info(f"Time for raycasting: {time.time() - start}")
            return np.array(voxels_to_clear_list)

        else:   # distance based approach
            # Filter points on the map only in the bounding box of the camera pontcloud
            min_bounds = torch.min(cam_voxels_torch, dim=0)[0]  # Minimum x, y, z of the camera pointcloud
            max_bounds = torch.max(cam_voxels_torch, dim=0)[0]  # Maximum x, y, z of the camera pointcloud
            map_points = torch.tensor(self.grid_pos, device='cuda', dtype=torch.float32)
            map_mask = torch.all((map_points >= min_bounds) & (map_points <= max_bounds), dim=1)
            map_points = map_points[map_mask]
            #self.get_logger().info(f"map points shape: {map_points.shape}")

            voxels_to_clear = (raycast.raycast_map_torch_efficient(camera_pose_voxel_troch, 
                                                                   cam_voxels_torch, 
                                                                   map_points
                                                                   )).to(torch.int)
            voxels_to_clear = voxels_to_clear.cpu().numpy()
        
        self.get_logger().info(f"Time for raycasting: {time.time() - start}")
        return voxels_to_clear


    def remove_map_voxels(self, voxels_to_clear):
        """
        :param voxels_to_clear: array of shape (N, 3) with the voxels in the global grid map frame to be removed from self.occupied_ids
        :return: True or False upon completion
        """
        # TODO add try catch
        if voxels_to_clear.size != 0:
                time_start = time.time()
                for voxel in voxels_to_clear:
                    # Check if in range
                    if self._out_of_range(voxel[0], voxel[1], voxel[2], self.gs, self.vh):
                        continue
                    # Check if voxel is already mapped:
                    if self.occupied_ids[voxel[0], voxel[1], voxel[2]] > 0:
                        self.grid_feat[self.occupied_ids[voxel[0], voxel[1], voxel[2]]] = np.zeros_like(self.grid_feat[0], dtype=np.float32)    # TODO parameterize also the type?
                        self.grid_pos[self.occupied_ids[voxel[0], voxel[1], voxel[2]]] = np.zeros_like(self.grid_pos[0], dtype=np.int32)
                        self.grid_rgb[self.occupied_ids[voxel[0], voxel[1], voxel[2]]] = np.zeros_like(self.grid_rgb[0], dtype=np.uint8)
                        self.occupied_ids[voxel[0], voxel[1], voxel[2]] = -1
                self.get_logger().info(f"Time for REMOVING INDICES from map: {time.time() - time_start}")
        #if voxels_to_clear.size != 0:
        #        time_map_raycast = time.time()
        #        for voxel in voxels_to_clear:
        #            self.grid_feat[self.occupied_ids[voxel[0], voxel[1], voxel[2]]] = np.zeros_like(self.grid_feat[0], dtype=np.float32)    # TODO parameterize also the type?
        #            self.grid_pos[self.occupied_ids[voxel[0], voxel[1], voxel[2]]] = np.zeros_like(self.grid_pos[0], dtype=np.int32)
        #            self.grid_rgb[self.occupied_ids[voxel[0], voxel[1], voxel[2]]] = np.zeros_like(self.grid_rgb[0], dtype=np.uint8)
        #            self.occupied_ids[voxel[0], voxel[1], voxel[2]] = -1
        #        self.get_logger().info(f"Time for REMOVING INDICES from map: {time.time() - time_map_raycast}")
        return True

    # Simply store the covariance values, will be analyze
    def amcl_callback(self, msg):
        self.amcl_cov = msg.pose.covariance

    def _init_map(self, maximum_height: float, cs: float, gs: int, map_path: Path) -> Tuple:
        """
        initialize a voxel grid of size (gs, gs, vh), vh = camera_height / cs, each voxel is of
        size cs
        """
        # init the map related variables
        vh = int(maximum_height / cs)
        grid_feat = np.zeros((gs * gs, self.clip_feat_dim), dtype=np.float32)
        grid_pos = np.zeros((gs * gs, 3), dtype=np.int32)
        occupied_ids = -1 * np.ones((gs, gs, vh), dtype=np.int32)
        weight = np.zeros((gs * gs), dtype=np.float32)
        grid_rgb = np.zeros((gs * gs, 3), dtype=np.uint8)
        mapped_iter_set = set()
        mapped_iter_list = list(mapped_iter_set)
        max_id = 0
        loaded_map = False

        # check if there is already saved map TODO print warn msg
        if os.path.exists(map_path):
            (
                mapped_iter_list,
                grid_feat,
                grid_pos,
                weight,
                occupied_ids,
                grid_rgb,
            ) = load_3d_map(self.map_save_path)
            mapped_iter_set = set(mapped_iter_list)
            max_id = grid_feat.shape[0] - 1
            loaded_map = True

        return vh, grid_feat, grid_pos, weight, occupied_ids, grid_rgb, mapped_iter_set, max_id, loaded_map

    def _init_lseg(self):
        crop_size = 480  # 480
        base_size = 640  # 520
        if torch.cuda.is_available():
            self.device = "cuda"
        elif torch.backends.mps.is_available():
            self.device = "mps"
        else:
            self.device = "cpu"
        lseg_model = LSegEncNet("", arch_option=0, block_depth=0, activation="lrelu", crop_size=crop_size)
        model_state_dict = lseg_model.state_dict()
        checkpoint_dir = Path(__file__).resolve().parents[1] / "lseg" / "checkpoints"
        checkpoint_path = checkpoint_dir / "demo_e200.ckpt"
        os.makedirs(checkpoint_dir, exist_ok=True)
        print(f"checkpoint path is : {checkpoint_path}")

        if not checkpoint_path.exists():
            print("Downloading LSeg checkpoint...")
            # the checkpoint is from official LSeg github repo
            # https://github.com/isl-org/lang-seg
            checkpoint_url = "https://drive.google.com/u/0/uc?id=1ayk6NXURI_vIPlym16f_RG3ffxBWHxvb"
            gdown.download(checkpoint_url, output=str(checkpoint_path))

        pretrained_state_dict = torch.load(checkpoint_path, map_location=self.device)
        pretrained_state_dict = {k.lstrip("net."): v for k, v in pretrained_state_dict["state_dict"].items()}
        model_state_dict.update(pretrained_state_dict)
        lseg_model.load_state_dict(pretrained_state_dict)

        lseg_model.eval()
        lseg_model = lseg_model.to(self.device)

        norm_mean = [0.5, 0.5, 0.5]
        norm_std = [0.5, 0.5, 0.5]
        lseg_transform = transforms.Compose(
            [
                transforms.ToTensor(),
                transforms.Normalize([0.5, 0.5, 0.5], [0.5, 0.5, 0.5]),
            ]
        )
        self.clip_feat_dim = lseg_model.out_c
        return lseg_model, lseg_transform, crop_size, base_size, norm_mean, norm_std

    def _out_of_range(self, row: int, col: int, height: int, gs: int, vh: int) -> bool:
        return col >= gs or row >= gs or height >= vh or col < 0 or row < 0 or height < 0

    def _reserve_map_space(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        self.grid_feat = np.concatenate(
            [
                self.grid_feat,
                np.zeros((self.grid_feat.shape[0], self.grid_feat.shape[1]), dtype=np.float32),
            ],
            axis=0,
        )
        self.grid_pos = np.concatenate(
            [
                self.grid_pos,
                np.zeros((self.grid_pos.shape[0], self.grid_pos.shape[1]), dtype=np.int32),
            ],
            axis=0,
        )
        self.weight = np.concatenate([self.weight, np.zeros((self.weight.shape[0]), dtype=np.int32)], axis=0)
        self.grid_rgb = np.concatenate(
            [
                self.grid_rgb,
                np.zeros((self.grid_rgb.shape[0], self.grid_rgb.shape[1]), dtype=np.float32),
            ],
            axis=0,
        )

    def _save_3d_map(
        self,
        grid_feat: np.ndarray,
        grid_pos: np.ndarray,
        weight: np.ndarray,
        grid_rgb: np.ndarray,
        occupied_ids: Set,
        mapped_iter_set: Set,
        max_id: int,
    ) -> None:
        grid_feat = grid_feat[:max_id]
        grid_pos = grid_pos[:max_id]
        weight = weight[:max_id]
        grid_rgb = grid_rgb[:max_id]
        save_3d_map(self.map_save_path, grid_feat, grid_pos, weight, occupied_ids, list(mapped_iter_set), grid_rgb)

