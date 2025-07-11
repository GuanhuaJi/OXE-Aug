import argparse
import json
import os

import numpy as np
import matplotlib.pyplot as plt
from scipy.spatial.transform import Rotation
import robosuite as suite
import robosuite.utils.transform_utils as T
import robosuite.utils.camera_utils as camera_utils
from robosuite.utils.camera_utils import CameraMover
import xml.etree.ElementTree as ET

import robosuite.macros as macros
# from robosuite.wrappers import DomainRandomizationWrapper
macros.IMAGE_CONVENTION = "opencv"
# macros.USING_INSTANCE_RANDOMIZATION = True

def image_to_pointcloud(depth_map, camera_name, camera_height, camera_width, segmask=None):
    """
    Convert depth image to point cloud
    """
    real_depth_map = camera_utils.get_real_depth_map(env.sim, depth_map)
    # Camera transform matrix to project from camera coordinates to world coordinates.
    extrinsic_matrix = camera_utils.get_camera_extrinsic_matrix(env.sim, camera_name=camera_name)
    intrinsic_matrix = camera_utils.get_camera_intrinsic_matrix(env.sim, camera_name=camera_name, camera_height=camera_height, camera_width=camera_width)

    # Convert depth image to point cloud
    points = [] # 3D points in robot frame of shape […, 3]
    for x in range(camera_width):
        for y in range(camera_height):
            if segmask is not None and segmask[y, x] == 0:
                continue
            coord_cam_frame = np.array([(x-intrinsic_matrix[0, -1])/intrinsic_matrix[0, 0], (y-intrinsic_matrix[1, -1])/intrinsic_matrix[1, 1], 1]) * real_depth_map[y, x]
            coord_world_frame = np.dot(extrinsic_matrix, np.concatenate((coord_cam_frame, [1])))
            points.append(coord_world_frame)

    return points


def sample_half_hemisphere(num_samples):
    radius = np.random.normal(0.85, 0.2, num_samples)
    hemisphere_center = np.array([0, 0, 0])
    theta = np.random.uniform(np.pi/4, np.pi/2.2, num_samples)  # Angle with respect to the z-axis
    phi = np.random.uniform(-np.pi*3.7/4, np.pi*3.7/4, num_samples)  # Azimuthal angle
    positions = np.zeros((num_samples, 3))
    positions[:, 0] = radius * np.sin(theta) * np.cos(phi)  # x-coordinate
    positions[:, 1] = radius * np.sin(theta) * np.sin(phi)  # y-coordinate
    positions[:, 2] = radius * np.cos(theta)  # z-coordinate

    # Calculate orientations (quaternions)
    backward_directions = positions - hemisphere_center
    backward_directions /= np.linalg.norm(backward_directions, axis=1, keepdims=True)
    right_directions = np.cross(np.tile(np.array([0, 0, 1]), (num_samples, 1)), backward_directions)  # Assuming right direction is along the x-axis
    right_directions /= np.linalg.norm(right_directions, axis=1, keepdims=True)
    up_directions = np.cross(backward_directions, right_directions)
    up_directions /= np.linalg.norm(up_directions, axis=1, keepdims=True)

    rotations = np.array([np.column_stack((right, down, forward)) for right, down, forward in zip(right_directions, up_directions, backward_directions)])
    # rotations = np.array([                                                        [[ 0.   ,       0.70614784, -0.70806442     ],
    #                                                         [ 1.    ,      0.      ,    0.                ],
    #                                                         [ 0.     ,    -0.70806442 ,-0.70614784     ]]])

    # Convert rotation matrices to quaternions
    quaternions = []
    for rotation_matrix in rotations:
        rotation = Rotation.from_matrix(rotation_matrix)
        quaternion = rotation.as_quat()
        quaternions.append(quaternion)

    quaternions = np.array(quaternions)


    return positions, quaternions

def sample_robot_ee_pose():
    # the position should be in the follow range:
    # x: -0.3 ~ 0.3
    # y: -0.3 ~ 0.3
    # z: 0.5 ~ 1.5
    # np.random.seed(0)
    pos = np.random.uniform(-0.25, 0.25, 3)
    pos[2] = np.random.uniform(0.6, 1.3)
    # quat = np.random.uniform(-1, 1, 4)
    # quat /= np.linalg.norm(quat)
    
    def sample_rotation_matrix():
        # Sample theta (zenith angle) from a normal distribution centered around pi
        theta = np.random.normal(loc=np.pi, scale=np.pi/3.5)
        print("theta: ", theta)
        # Sample phi (azimuthal angle) uniformly between 0 and 2*pi
        phi = np.random.uniform(0, 2*np.pi)
        
        # Convert spherical coordinates to Cartesian coordinates
        z_axis = np.array([np.sin(theta) * np.cos(phi),
                        np.sin(theta) * np.sin(phi),
                        np.cos(theta)])
        
        # Sample a random vector for the rightward direction (perpendicular to z-axis)
        rightward = np.random.uniform(-1, 1, size=3)
        rightward -= np.dot(rightward, z_axis) * z_axis
        rightward /= np.linalg.norm(rightward)
        
        # Compute the inward direction (perpendicular to both z and rightward axes)
        inward = np.cross(rightward, z_axis)
        
        # Construct the rotation matrix
        R = np.column_stack((inward, rightward, z_axis))
    
        return R

    quat = T.mat2quat(sample_rotation_matrix())
    return np.concatenate((pos, quat))

def compute_eef_pose(env):
    pos = np.array(env.sim.data.site_xpos[env.sim.model.site_name2id(env.robots[0].controller.eef_name)])
    rot = np.array(T.mat2quat(env.sim.data.site_xmat[env.sim.model.site_name2id(env.robots[0].controller.eef_name)].reshape([3, 3])))
    return np.concatenate((pos, rot))

def compute_pose_error(current_pose, target_pose):
    # quarternions are equivalent up to sign
    error = min(np.linalg.norm(current_pose - target_pose), np.linalg.norm(current_pose - np.concatenate((target_pose[:3], -target_pose[3:]))))
    return error
            
def drive_robot_to_target_pose(env, target_pose=None, tracking_error_threshold=0.003, num_iter_max=100):
    # breakpoint()
    # reset robot joint positions so the robot is hopefully not in a weird pose
    set_robot_joint_positions(env)
    env.robots[0].controller.use_delta = False # change to absolute pose for setting the initial state
    
    assert len(target_pose) == 7, "Target pose should be 7DOF"
    current_pose = compute_eef_pose(env)
    error = compute_pose_error(current_pose, target_pose)
    num_iters = 0    
    while error > tracking_error_threshold and num_iters < num_iter_max:
        action = np.zeros(7)
        action[:3] = target_pose[:3]
        action[3:6] = T.quat2axisangle(target_pose[3:])
        
        obs, _, _, _ = env.step(action)

        current_pose = compute_eef_pose(env)
        error = compute_pose_error(current_pose, target_pose)
        num_iters += 1

    print("Take {} iterations to drive robot to target pose".format(num_iters))
    try:
        assert error < tracking_error_threshold, "Starting states are not the same\n"
        return True
    except:
        print("Starting states are not the same"
                "Source: ", current_pose,
                "Target: ", target_pose)
        return False

def set_robot_joint_positions(env, joint_angles=[9.44962915e-03,  2.04028892e-01,  2.27688289e-02, -2.64987059e+00, 1.89505922e-03,  2.91240765e+00,  7.87470020e-01]):
    for _ in range(50):
        env.robots[0].set_robot_joint_positions(joint_angles)
        env.sim.forward()
        env.sim.step()
        env._update_observables()
        


class CameraWrapper:
    def __init__(self, env, camera_name="agentview"):
        self.env = env
        # Create the camera mover
        self.camera_mover = CameraMover(
            env=env,
            camera=camera_name,
        )
        self.cam_tree = ET.Element("camera", attrib={"name": camera_name})
        CAMERA_NAME = self.cam_tree.get("name") # Make sure we're using the camera that we're modifying
        self.camera_id = env.sim.model.camera_name2id(CAMERA_NAME)
        self.env.viewer.set_camera(camera_id=self.camera_id)
        
        
        # Define initial file camera pose
        initial_file_camera_pos, initial_file_camera_quat = self.camera_mover.get_camera_pose()
        initial_file_camera_pose = T.make_pose(initial_file_camera_pos, T.quat2mat(initial_file_camera_quat))

        # remember difference between camera pose in initial tag and absolute camera pose in world
        # usually we just operate in the wolrd frame, so we don't need to worry about the difference
        # but if we ever want to know the camera pose in the file frame, we can use this
        initial_world_camera_pos, initial_world_camera_quat = self.camera_mover.get_camera_pose()
        initial_world_camera_pose = T.make_pose(initial_world_camera_pos, T.quat2mat(initial_world_camera_quat))
        self.world_in_file = initial_file_camera_pose.dot(T.pose_inv(initial_world_camera_pose))
        
    
    def set_camera_fov(self, fov=45.0):
        self.env.sim.model.cam_fovy[self.camera_id] = float(fov)
        for _ in range(50):
            self.env.sim.forward()
            self.env.sim.step()
            self.env._update_observables()
    
    def set_camera_pose(self, pos, quat, offset=np.array([-0.6, 0.0, 0.912])):
        # Robot base world coord: -0.6 0.0 0.912
        self.camera_mover.set_camera_pose(pos=pos + offset, quat=quat)
        for _ in range(50):
            self.env.sim.forward()
            self.env.sim.step()
            self.env._update_observables()
    
    def get_camera_pose_world_frame(self):
        camera_pos, camera_quat = self.camera_mover.get_camera_pose()
        world_camera_pose = T.make_pose(camera_pos, T.quat2mat(camera_quat))
        print("Camera pose in the world frame:", camera_pos, camera_quat)
        return world_camera_pose
    
    def get_camera_pose_file_frame(self, world_camera_pose):
        file_camera_pose = self.world_in_file.dot(world_camera_pose)
        camera_pos, camera_quat = T.mat2pose(file_camera_pose)
        camera_quat = T.convert_quat(camera_quat, to="wxyz")

        print("\n\ncurrent camera tag you should copy")
        self.cam_tree.set("pos", "{} {} {}".format(camera_pos[0], camera_pos[1], camera_pos[2]))
        self.cam_tree.set("quat", "{} {} {} {}".format(camera_quat[0], camera_quat[1], camera_quat[2], camera_quat[3]))
        print(ET.tostring(self.cam_tree, encoding="utf8").decode("utf8"))
        
    def perturb_camera(self):
        # self.camera_mover.rotate_camera(point=None, axis=np.random.uniform(-1, 1, 3), angle=10)
        # self.camera_mover.move_camera(direction=np.random.uniform(-1, 1, 3), scale=0.15)
        for _ in range(50):
            self.env.sim.forward()
            self.env.sim.step()
            self.env._update_observables()
        

if __name__ == "__main__":

    """
    Registered environments: Lift, Stack, NutAssembly, NutAssemblySingle, NutAssemblySquare, NutAssemblyRound,
                             PickPlace, PickPlaceSingle, PickPlaceMilk, PickPlaceBread, PickPlaceCereal,
                             PickPlaceCan, Door, Wipe, TwoArmLift, TwoArmPegInHole, TwoArmHandover

    Possible robots: Baxter, IIWA, Jaco, Kinova3, Panda, Sawyer, UR5e
    """

    options = {}

    # print welcome info
    print("Welcome to robosuite v{}!".format(suite.__version__))
    print(suite.__logo__)

    parser = argparse.ArgumentParser()
    parser.add_argument("--renderer", type=str, default="mujoco", help="Valid options include mujoco, and nvisii")

    args = parser.parse_args()
    renderer = args.renderer

    options["env_name"] = "Empty"  # You can choose your desired environment here
    options["robots"] = "Panda"   # You can choose your desired robot here
    
    # Choose controller
    controller_name = "OSC_POSE"
    
    # Load the desired controller
    options["controller_configs"] = suite.load_controller_config(default_controller=controller_name)

    env = suite.make(
        **options,
        has_renderer=False if renderer != "mujoco" else True,  # no on-screen renderer
        has_offscreen_renderer=True,  # no off-screen renderer
        ignore_done=True,
        use_camera_obs=True,  # no camera observations
        control_freq=20,
        renderer=renderer,
        camera_names = ["agentview"],  # You can add more camera names if needed
        camera_heights = 256,
        camera_widths = 256,
        camera_depths = True,
        camera_segmentations = "robot_only",
        hard_reset=False, 
    )
    # env = DomainRandomizationWrapper(env)
    env.reset()
    
    camera_wrapper = CameraWrapper(env)
    
    num_robot_poses = 5 
    # Generate random camera poses
    num_cam_poses_per_robot_pose = 10  # You can adjust the number of camera poses
    positions, quaternions = sample_half_hemisphere(num_cam_poses_per_robot_pose)
    
    
    # for each robot pose
    for k in range(num_robot_poses):
        # sample robot eef pose
        reached = False
        while not reached:
            target_pose = sample_robot_ee_pose()
            reached = drive_robot_to_target_pose(env, target_pose=target_pose)
            ee_pose = compute_eef_pose(env)
        print("Target pose: ", target_pose)
        print("Actual ee pose:", ee_pose)
        
        
        # Capture images from each camera pose
        for i, (pos, quat) in enumerate(zip(positions, quaternions)):
            camera_wrapper.set_camera_pose(pos=positions[i], quat=quaternions[i], offset=ee_pose[:3])
            camera_wrapper.perturb_camera()
            # sample an fov
            fov = np.random.uniform(30, 60)
            camera_wrapper.set_camera_fov(fov=fov)
            print("Camera fov: ", fov, env.sim.model.cam_fovy[camera_wrapper.camera_id])
            
            
            obs = env._get_observations()
            
            for view in ["agentview"]:
                # frontview
                front_rgb_img_raw = obs[f'{view}_image']
                front_seg_img = obs[f'{view}_segmentation_robot_only']
                # # only retain the robot part in the front_rgb_img, set other pixels to black
                # front_rgb_img = (front_rgb_img * front_seg_img).astype(np.uint8)
                # Create a mask where non-robot pixels are set to 0 and robot pixels are set to 1
                # breakpoint()
                mask = (np.repeat(front_seg_img, 3, axis=2)).astype(bool)
                front_rgb_img = np.where(~mask, [255, 255, 255], front_rgb_img_raw) # Set non-robot pixels to white
                front_rgb_img = front_rgb_img.astype(np.uint8) # Convert the resulting image to uint8 type

                # Save or process the captured images
                image_dir = "images"
                if not os.path.exists(image_dir):
                    os.makedirs(image_dir)

                import cv2
                cv2.imwrite(f"output/{options['robots']}_output_{k*num_cam_poses_per_robot_pose+i}_{view}_rgb_256.png", cv2.cvtColor(front_rgb_img, cv2.COLOR_RGB2BGR))
                cv2.imwrite(f"output/{options['robots']}_output_{k*num_cam_poses_per_robot_pose+i}_{view}_rgb_256_raw.png", cv2.cvtColor(front_rgb_img_raw, cv2.COLOR_RGB2BGR))
                # cv2.imwrite(f"output/{options['robots']}_output_{i}_{view}_seg_256.png", front_seg_img * 255)

    env.close_renderer()
    print("Done.")
