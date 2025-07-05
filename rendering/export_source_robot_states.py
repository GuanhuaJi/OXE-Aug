'''
python /home/guanhuaji/mirage/robot2robot/rendering/export_source_robot_states.py --robot_dataset autolab_ur5 --partition 0


datasets: 
austin_buds, austin_mutex, austin_sailor, 
autolab_ur5, can, furniture_bench, iamlab_cmu, 
lift, nyu_franka, square, stack, three_piece_assembly, 
taco_play, ucsd_kitchen_rlds, viola, kaist
toto, 

'''

import argparse
import json
import os
import cv2
import socket, pickle, struct
import numpy as np
import matplotlib.pyplot as plt
from scipy.spatial.transform import Rotation
import robosuite as suite
import robosuite.utils.transform_utils as T
import robosuite.utils.camera_utils as camera_utils
from robosuite.utils.camera_utils import CameraMover
import xml.etree.ElementTree as ET
import robosuite.macros as macros
macros.IMAGE_CONVENTION = "opencv"
from scipy.spatial.transform import Rotation as R
from tqdm import tqdm
from config.robot_pose_dict import ROBOT_POSE_DICT
from pathlib import Path
from typing import Tuple
from mujoco import mjtObj
import pynvml

import logging
logger = logging.getLogger(__name__) 

np.set_printoptions(suppress=True, precision=6)

def pick_best_gpu(policy="free-mem"):
    """
    Return the index of the “least busy” NVIDIA GPU and set CUDA_VISIBLE_DEVICES
    so frameworks (PyTorch, TensorFlow, JAX…) will automatically use it.

    policy
    ------
    "free-mem"   – prefer the card with the most free memory
    "low-util"   – prefer the card with the lowest compute utilisation
    "hybrid"     – most free mem, break ties with lowest utilisation
    """
    pynvml.nvmlInit()
    n = pynvml.nvmlDeviceGetCount()

    best_idx, best_score = None, None
    for i in range(n):
        h = pynvml.nvmlDeviceGetHandleByIndex(i)
        mem = pynvml.nvmlDeviceGetMemoryInfo(h)          # bytes
        util = pynvml.nvmlDeviceGetUtilizationRates(h)   # %
        if policy == "free-mem":
            score = mem.free
        elif policy == "low-util":
            score = -util.gpu                            # negative ⇒ lower is better
        else:  # hybrid
            score = (mem.free, -util.gpu)                # tuple is fine for max()

        if best_score is None or score > best_score:
            best_idx, best_score = i, score

    os.environ["CUDA_VISIBLE_DEVICES"] = str(best_idx)   # frameworks see *only* this GPU
    print(f"👉  Selected GPU {best_idx}")
    return best_idx

def quat_dist_rad(q1, q2):
    """
    最小旋转角：两单位四元数内积的 arccos。
    输入 shape=(4,), 顺序 [qw, qx, qy, qz]
    """
    dot = np.abs(np.dot(q1, q2))
    dot = np.clip(dot, -1.0, 1.0)  # 数值安全
    return 2.0 * np.arccos(dot)

def gripper_convert(gripper_state_value, robot_type):
    if robot_type == "autolab_ur5":
        return gripper_state_value == 0
    elif robot_type == "furniture_bench":
        return gripper_state_value > 0.05
    elif robot_type == "viola":
        return gripper_state_value > 0.07 # changed
    elif robot_type == "austin_sailor":
        return gripper_state_value > 0.07 # changed
    elif robot_type == "austin_mutex":
        return gripper_state_value > 0.07 # changed
    elif robot_type == "austin_buds":
        return gripper_state_value > 0.07 # changed
    elif robot_type == "nyu_franka":
        return gripper_state_value >= 0
    elif robot_type == "ucsd_kitchen_rlds":
        return gripper_state_value > 0.5
    elif robot_type == "taco_play":
        return gripper_state_value < 0
    elif robot_type == "iamlab_cmu":
        return gripper_state_value > 0.5
    elif robot_type == "toto":
        return gripper_state_value > 0
    elif robot_type == "can":
        return gripper_state_value
    elif robot_type == "lift":
        return gripper_state_value
    elif robot_type == "square":
        return gripper_state_value
    elif robot_type == "stack":
        return gripper_state_value
    elif robot_type == "three_piece_assembly":
        return gripper_state_value
    elif robot_type == "asu_table_top_rlds":
        return gripper_state_value < 0
    elif robot_type == "utokyo_pick_and_place":
        return gripper_state_value > 0.02
    print("UNKNOWN GRIPPER")
    return None

def compute_pose_error(current_pose, target_pose,
                       pos_w=1.0, ori_w=0.1):
    """
    current_pose / target_pose: shape=(7,)
        [x, y, z, qw, qx, qy, qz]
    返回一个标量误差，越小越好
    """
    # 位置误差
    p_cur, p_tgt = current_pose[:3], target_pose[:3]
    pos_err = np.linalg.norm(p_cur - p_tgt)

    # 姿态误差（弧度）
    q_cur, q_tgt = current_pose[3:], target_pose[3:]
    ori_err = quat_dist_rad(q_cur, q_tgt)

    return pos_w * pos_err + ori_w * ori_err 

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
        # for _ in range(50):
        #     self.env.sim.forward()
        #     self.env.sim.step()
        #     self.env._update_observables()
    
    def set_camera_pose(self, pos, quat, offset=np.array([0, 0, 0])):
        # Robot base world coord: -0.6 0.0 0.912
        self.camera_mover.set_camera_pose(pos=pos + offset, quat=quat)
        target_pose = np.concatenate((pos + offset, quat))
        current_pose = self.get_camera_pose_world_frame()
        error = compute_pose_error(current_pose, target_pose)

        cid = self.env.sim.model.camera_name2id("agentview")
        R   = self.env.sim.data.cam_xmat[cid].reshape(3, 3)

        forward_world = -R[:, 2]   # –Z column
        up_world      =  R[:, 1]   # +Y column

        # for _ in range(50):
        #     self.camera_mover.set_camera_pose(pos=pos + offset, quat=quat)
        #     self.env.sim.forward()
        #     self.env.sim.step()
        #     self.env._update_observables()
            
    
    def get_camera_pose_world_frame(self):
        camera_pos, camera_quat = self.camera_mover.get_camera_pose()
        # world_camera_pose = T.make_pose(camera_pos, T.quat2mat(camera_quat))
        # print("Camera pose in the world frame:", camera_pos, camera_quat)
        return np.concatenate((camera_pos, camera_quat))
        

def fast_step(env, action):
    """
    Minimal physics step that **完全绕开渲染与观测**，
    兼容 robosuite 1.0 ~ 1.6 及任何自定义 MujocoEnv。
    返回 (reward, done, info)；不生成 obs。
    """
    # 1. 计算“一个控制周期需要多少 sim 子步”
    substeps = int(env.control_timestep / env.model_timestep)

    # 2. 先把动作写入电机
    policy_step = True
    for _ in range(substeps):
        # 和 robosuite.step() 保持同样的前/后处理
        if hasattr(env, "_pre_action"):
            env._pre_action(action, policy_step=policy_step)
        # 纯物理推进：旧版只有 sim.step()
        if hasattr(env.sim, "step"):
            env.sim.step()
        else:                           # 极早期 robosuite
            env.sim.forward()
        policy_step = False

    # 3. 时间推进
    if hasattr(env, "cur_time"):       # robosuite >=0.4
        env.cur_time += env.control_timestep
    if hasattr(env, "timestep"):       # robosuite <=0.3
        env.timestep += 1
    return 0.0, False, {}

def _robot_geom_ids(env):
    sim   = env.sim
    robot = env.robots[0]

    # ---- 1. arm geoms ----
    model = getattr(robot, "robot_model", robot)
    arm_names = (getattr(model, "geom_names", []) or
                 getattr(model, "visual_geoms", []) +
                 getattr(model, "contact_geoms", []))

    # ---- 2. gripper geoms ----
    grip = getattr(robot, "gripper", None)
    grip_names = []
    if grip is not None:
        grip_names = getattr(grip, "visual_geoms", []) + getattr(grip, "contact_geoms", [])

    # ---- 3. union + id mapping ----
    names = set(arm_names) | set(grip_names)
    ids = {sim.model.geom_name2id(n) for n in names if n in sim.model.geom_names}
    return ids


def load_states_from_harsha(robot_dataset, episode, robot_name):
    info_path = Path(harsha_dataset_path[robot_dataset]) / str(episode) / f"panda_replay_info_{episode}.npz"
    info = np.load(info_path, allow_pickle=True)
    joint_angles = info["joint_positions"]
    gripper_states = info["gripper_dist"]
    print(gripper_states)
    translation = info["translation"]
    return joint_angles, gripper_states, translation



class RobotCameraWrapper:
    def __init__(self, robotname="Panda", grippername="PandaGripper", robot_dataset=None, camera_height=256, camera_width=256):
        options = {}
        self.env = suite.make(
            **options,
            robots=robotname,
            gripper_types=grippername,
            env_name="Empty",
            has_renderer=True,  # no on-screen renderer
            has_offscreen_renderer=True,  # no off-screen renderer
            ignore_done=True,
            use_camera_obs=True,  # no camera observations
            controller_configs = suite.load_controller_config(default_controller="OSC_POSE"),
            control_freq=20,
            renderer="mujoco",
            camera_names = ["agentview"],  # You can add more camera names if needed
            camera_heights = camera_height,
            camera_widths = camera_width,
            camera_depths = True,
            camera_segmentations = "robot_only",
            hard_reset=False,
        )
        
        self.camera_wrapper = CameraWrapper(self.env)
        self.robot_name = robotname
        self.robot_base_name = f"robot0_base"
        self.base_body_id = self.env.sim.model.body_name2id(self.robot_base_name)
        self.base_position = self.env.sim.model.body_pos[self.base_body_id].copy()

    def get_gripper_width_from_qpos(self):
        sim   = self.env.sim
        robot = self.env.robots[0]
        if hasattr(robot, "_ref_gripper_joint_pos_indexes") and robot._ref_gripper_joint_pos_indexes is not None:
            qpos_idx = robot._ref_gripper_joint_pos_indexes
        else:
            joint_names = robot.gripper.joints
            qpos_idx = [sim.model.get_joint_qpos_addr(name) for name in joint_names]

        finger_qpos = sim.data.qpos[qpos_idx]
        if self.robot_name == "Panda":
            return 2.0 * finger_qpos[0], np.clip(2.0 * finger_qpos[0] / 0.08, 0, 1) # close 0 -> open 0.08
        elif self.robot_name == "UR5e" or self.robot_name == "Kinova3" or self.robot_name == "IIWA":
            return 2.0 * finger_qpos[0], (1 - np.clip(2.0 * finger_qpos[0], 0, 1)) # close 1 -> open 0
        elif self.robot_name == "Sawyer":
            return 2.0 * finger_qpos[0], 1 - np.clip(2.0 * finger_qpos[0] / -0.024, 0, 1) # close -0.024 -> open 0
        elif self.robot_name == "Jaco":
            return 2.0 * finger_qpos[0], np.clip(2.0 * finger_qpos[0] / 2.2, 0, 1) # close 0 -> open 2.2
        

    def compute_eef_pose(self):
        pos = np.array(self.env.sim.data.site_xpos[self.env.sim.model.site_name2id(self.env.robots[0].controller.eef_name)])
        rot = np.array(T.mat2quat(self.env.sim.data.site_xmat[self.env.sim.model.site_name2id(self.env.robots[0].controller.eef_name)].reshape([3, 3])))
        return np.concatenate((pos, rot))
    
    def teleport_to_joint_positions(self, joint_angles):
        joint_names = self.env.robots[0].robot_joints
        for i, joint_name in enumerate(joint_names):
            qpos_addr = self.env.sim.model.get_joint_qpos_addr(joint_name)
            self.env.sim.data.qpos[qpos_addr] = joint_angles[i]
            self.env.sim.data.qvel[qpos_addr] = 0.0
        self.env.sim.forward()

    def drive_robot_to_target_pose(self, target_pose=None, min_threshold=0.003, max_threshold=0.01, num_iter_max=100):
        self.env.robots[0].controller.use_delta = False # change to absolute pose for setting the initial state
        assert len(target_pose) == 7, "Target pose should be 7DOF"
        current_pose = self.compute_eef_pose()
        error = compute_pose_error(current_pose, target_pose)
        num_iters = 0   

        no_improve_steps = 0
        last_error = error 
        while error > min_threshold and num_iters < num_iter_max:
            action = np.zeros(7)
            action[:3] = target_pose[:3]
            action[3:6] = T.quat2axisangle(target_pose[3:])
            _, _, _ = fast_step(self.env, action)
            current_pose = self.compute_eef_pose()
            current_joints = self.env.sim.data.qpos[self.env.robots[0]._ref_joint_pos_indexes].copy()
            self.some_safe_joint_angles = current_joints
            new_error = compute_pose_error(current_pose, target_pose)

            if abs(new_error - error) < 1e-5:
                no_improve_steps += 1
            else:
                no_improve_steps = 0

            error = new_error
            num_iters += 1
        # print("ERROR", error)
        # print("Take {} iterations to drive robot to target pose".format(num_iters))
        current_pose = self.compute_eef_pose()
        self.env.use_camera_obs = True

        if error < max_threshold:
            return True, current_pose, error
        else:
            print("Failed to drive robot to target pose")
            print("SUGGESTION: ", target_pose - current_pose)
            return False, current_pose, error

    def set_robot_joint_positions(self, joint_angles=None):
        if joint_angles is None:
            joint_angles = self.some_safe_joint_angles
        for _ in range(200):
            self.env.robots[0].set_robot_joint_positions(joint_angles)
            self.env.sim.forward()
            self.env.sim.step()
            self.env._update_observables()

    def set_gripper_joint_positions(self, finger_qpos, robot_name):
        if robot_name == "Panda":
            gripper_joint_names = ["gripper0_finger_joint1", "gripper0_finger_joint2"]
        elif robot_name == "IIWA":
            gripper_joint_names = ["gripper0_finger_joint", "gripper0_right_outer_knuckle_joint"]
        elif robot_name == "Sawyer":
            gripper_joint_names = ['gripper0_l_finger_joint', 'gripper0_r_finger_joint']
        elif robot_name == "Jaco":
            gripper_joint_names = ["gripper0_joint_thumb", "gripper0_joint_index", "gripper0_joint_pinky",]
        
        for i, joint_name in enumerate(gripper_joint_names):
            self.env.sim.data.set_joint_qpos(joint_name, finger_qpos[i])
        for _ in range(10):
            self.env.sim.forward()
            self.env.sim.step()
    
    def open_close_gripper(self, gripper_open=True):
        self.env.robots[0].controller.use_delta = True # change to delta pose
        action = np.zeros(7)
        if not gripper_open:
            action[-1] = 1
        else:
            action[-1] = -1            
        fast_step(self.env, action)
    
    def update_camera(self):
        for _ in range(50):
            self.env.sim.forward()
            self.env.sim.step()
            self.env._update_observables()
          
    def get_observation_fast(self, camera="agentview",
                            width=640, height=480,
                            white_background=True):
        sim = self.env.sim
        sim.forward()                                           # 同步姿态

        rgb = sim.render(width=width, height=height,
                        camera_name=camera)[::-1]
        seg = sim.render(width=width, height=height,
                        camera_name=camera,
                        segmentation=True)[::-1]               # (H,W,2)

        objtype_img = seg[..., 0]
        objid_img   = seg[..., 1]

        robot_body_ids = _robot_geom_ids(self.env)
        mask = (np.isin(objid_img, list(robot_body_ids))).astype(np.uint8)
        if white_background:
            rgb_out = rgb.copy()
            rgb_out[mask == 0] = 255
        else:
            rgb_out = (rgb * mask[..., None]).astype(np.uint8)

        return rgb_out, mask



class SourceEnvWrapper:
    def __init__(self, source_name, source_gripper, robot_dataset, camera_height=256, camera_width=256, verbose=False):
        self.source_env = RobotCameraWrapper(robotname=source_name, grippername=source_gripper, robot_dataset=robot_dataset, camera_height=camera_height, camera_width=camera_width)
        self.source_name = source_name
        self.fixed_cam_positions = None
        self.fixed_cam_quaternions = None
        self.verbose = verbose

    def _load_dataset_info(self, dataset_name):
        from config.dataset_poses_dict import ROBOT_CAMERA_POSES_DICT
        info = ROBOT_CAMERA_POSES_DICT[dataset_name]
        return info
    
    def _load_dataset_files(self, info, dataset_name):
        # 可能包含 joint_angles, ee_states, gripper_states
        joint_angles = None
        ee_states = None
        gripper_states = None
        if "robot_joint_angles_path" in info:
            joint_angles_path = info["robot_joint_angles_path"]
            joint_angles = np.loadtxt(joint_angles_path)
            if dataset_name == "toto":
                joint_angles[:, 5] += 3.14159 / 2
                joint_angles[:, 6] += 3.14159 / 4
            if dataset_name == "autolab_ur5":
                joint_angles[:, 5] += 3.14159 / 2
        if "robot_ee_states_path" in info:
            ee_states_path = info["robot_ee_states_path"]
            ee_states = np.loadtxt(ee_states_path)
        gripper_states_path = info["gripper_states_path"]
        gripper_states = np.loadtxt(gripper_states_path)
        return joint_angles, ee_states, gripper_states

    
    
    def get_source_robot_states(self, save_source_robot_states_path="paired_images", reference_joint_angles_path=None, reference_ee_states_path=None, reference_gripper_states_path=None, robot_dataset=None, episode=0):
        info = self._load_dataset_info(robot_dataset)
        if robot_dataset == "ucsd_kitchen_rlds" or robot_dataset == "utokyo_pick_and_place":
            joint_angles, gripper_states, translation = load_states_from_harsha(robot_dataset, episode, self.source_env.robot_name)
        else:
            joint_angles = np.loadtxt(os.path.join("/home/guanhuaji/mirage/robot2robot/rendering/datasets/states", robot_dataset, f"episode_{episode}", "joint_states.txt"))
            gripper_states = np.loadtxt(os.path.join("/home/guanhuaji/mirage/robot2robot/rendering/datasets/states", robot_dataset, f"episode_{episode}", "gripper_states.txt"))
        if robot_dataset == "toto":
            joint_angles[:, 5] += 3.14159 / 2
            joint_angles[:, 6] += 3.14159 / 4
        elif robot_dataset == "autolab_ur5":
            joint_angles[:, 5] += 3.14159 / 2
        elif robot_dataset == "asu_table_top_rlds":
            joint_angles[:, 1] -= np.pi / 2
            joint_angles[:, 2] *= -1
            joint_angles[:, 3] -= np.pi / 2
            joint_angles[:, 5] -= np.pi / 2
        elif robot_dataset == "viola":
            tol = 1e-8
            for i, row in enumerate(joint_angles):
                if not np.all(np.isclose(row, 0.0, atol=tol)):
                    if i > 0:
                        joint_angles[:i] = row
                        print(f"WARNING: first {i} rows were all zeros; "
                            f"copied row {i} into them.")
                    break
            else:
                print("WARNING: all joint_angles rows are zeros; nothing replaced.")
        if robot_dataset == "can":
            camera_reference_pose = np.array([0.9, 0.1, 1.75, 0.271, 0.271, 0.653, 0.653])
            cam_id = self.source_env.camera_wrapper.env.sim.model.camera_name2id("agentview")
            fov = self.source_env.camera_wrapper.env.sim.model.cam_fovy[cam_id]
        elif robot_dataset == "lift":
            camera_reference_pose = np.array([0.45, 0, 1.35, 0.271, 0.271, 0.653, 0.653])
            cam_id = self.source_env.camera_wrapper.env.sim.model.camera_name2id("agentview")
            fov = self.source_env.camera_wrapper.env.sim.model.cam_fovy[cam_id]
        elif robot_dataset == "square":
            camera_reference_pose = np.array([0.45, 0, 1.35, 0.271, 0.271, 0.653, 0.653])
            cam_id = self.source_env.camera_wrapper.env.sim.model.camera_name2id("agentview")
            fov = self.source_env.camera_wrapper.env.sim.model.cam_fovy[cam_id]
        elif robot_dataset == "stack":
            camera_reference_pose = np.array([0.45, 0, 1.35, 0.271, 0.271, 0.653, 0.653])
            cam_id = self.source_env.camera_wrapper.env.sim.model.camera_name2id("agentview")
            fov = self.source_env.camera_wrapper.env.sim.model.cam_fovy[cam_id]
        elif robot_dataset == "three_piece_assembly":
            camera_reference_pose = np.array([0.713078462147161, 2.062036796036723e-08, 1.5194726087166726, 0.293668270111084, 0.2936684489250183, 0.6432408690452576, 0.6432409286499023])
            cam_id = self.source_env.camera_wrapper.env.sim.model.camera_name2id("agentview")
            fov = self.source_env.camera_wrapper.env.sim.model.cam_fovy[cam_id]

        else:
            for viewpoint in info["viewpoints"]:
                if episode in viewpoint["episodes"]:
                    camera_reference_position = viewpoint["camera_position"] + np.array([-0.6, 0.0, 0.912]) 
                    roll_deg = viewpoint["roll"]
                    pitch_deg = viewpoint["pitch"]
                    yaw_deg = viewpoint["yaw"]
                    fov = viewpoint["camera_fov"]
                    r = R.from_euler('xyz', [roll_deg, pitch_deg, yaw_deg], degrees=True)
                    camera_reference_quaternion = r.as_quat()
                    camera_reference_pose = np.concatenate((camera_reference_position, camera_reference_quaternion))
                    break
        target_pose_list = []
        gripper_list = []
        num_frames = joint_angles.shape[0]

        for pose_index in tqdm(range(num_frames), desc=f'{self.source_name} Pose States Calculation'):    
            source_reached = False
            attempt_counter = 0
            while source_reached == False:
                attempt_counter += 1
                if attempt_counter > 10:
                    break
                if robot_dataset == "kaist":
                    gripper_open = False
                elif robot_dataset in ["can", "lift", "square", "stack", "three_piece_assembly"]:
                    gripper_open = (gripper_states[pose_index][0] - gripper_states[pose_index][1]) > 0.06
                else:
                    gripper_open = gripper_convert(gripper_states[pose_index], robot_dataset)
                for i in range(5):
                    joint_angle = joint_angles[pose_index]
                    self.source_env.teleport_to_joint_positions(joint_angle)
                    target_pose = self.source_env.compute_eef_pose()

            gripper_list.append(gripper_open)
            target_pose_list.append(target_pose)       
        target_pose_array = np.vstack(target_pose_list)
        if robot_dataset == "ucsd_kitchen_rlds":
            target_pose_array[:, :3] -= translation
        gripper_array = np.vstack(gripper_list)
        eef_npy_path = os.path.join(
            save_source_robot_states_path, f"{self.source_name}_eef_states_{episode}.npy"
        )
        GREEN = "\033[92m"
        RESET = "\033[0m"
        np.save(eef_npy_path, target_pose_array)
        print(f"{GREEN}✔ End effector saved under {eef_npy_path}{RESET}")
        npz_path = os.path.join(save_source_robot_states_path, f"{episode}.npz")
        #np.savez(npz_path, pos=target_pose_array, grip=gripper_array)
        
        #if no element of gripper_states is > 0.09, then divide each element by 0.08
        if self.source_name == "Panda":
            if np.all(gripper_array <= 0.09):
                gripper_states = np.clip(gripper_array / 0.08, 0, 1)
            else:
                gripper_states = np.clip(gripper_array, 0, 1)
        elif self.source_name == "UR5e":
            if np.all(gripper_array <= 0.06) and np.all(gripper_array >= -0.06):
                gripper_states = np.clip((gripper_array + 0.05) / 0.1, 0, 1)
            else:
                gripper_states = np.clip(gripper_array, 0, 1)

        np.savez(npz_path, pos=target_pose_array, grip=gripper_states)
        print(f"{GREEN}✔ States saved under {npz_path}{RESET}")

        
from config.dataset_pair_location import harsha_dataset_path
from config.dataset_poses_dict import ROBOT_CAMERA_POSES_DICT

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=0, help="(optional) set seed")
    parser.add_argument("--source_robot", type=str, help="PandaGripper or Robotiq85Gripper")
    parser.add_argument("--source_gripper", type=str, help="PandaGripper or Robotiq85Gripper")
    parser.add_argument("--save_paired_images_folder_path", type=str, default="paired_images", help="(optional) folder path to save the paired images")
    parser.add_argument("--robot_dataset", type=str, help="(optional) to match the robot poses from a dataset, provide the dataset name")
    parser.add_argument("--reference_joint_angles_path", type=str, help="(optional) to match the robot poses from a dataset, provide the path to the joint angles file (np.savetxt)")
    parser.add_argument("--reference_ee_states_path", type=str, help="(optional) to match the robot poses from a dataset, provide the path to the ee state file (np.savetxt)")
    parser.add_argument("--reference_gripper_states_path", type=str, help="(optional) to match the gripper's open/close status")
    parser.add_argument("--verbose", action='store_true', help="If set, prints extra debug/warning information")
    parser.add_argument("--partition", type=int, default=0, help="(optional) camera height")
    args = parser.parse_args()

    if args.source_gripper is not None:
        source_gripper = args.source_gripper
    elif args.robot_dataset == "autolab_ur5" or args.robot_dataset == "asu_table_top_rlds":
        source_name = "UR5e"
        source_gripper = "Robotiq85Gripper"
    else:
        source_name = "Panda"
        source_gripper = "PandaGripper"

    save_source_robot_states_path = (Path(ROBOT_CAMERA_POSES_DICT[args.robot_dataset]['replay_path']) / "source_robot_states")
    save_source_robot_states_path.mkdir(parents=True, exist_ok=True)
    
    if args.robot_dataset is not None:
        robot_dataset_info = ROBOT_CAMERA_POSES_DICT[args.robot_dataset]
        camera_height = robot_dataset_info["camera_heights"]
        camera_width = robot_dataset_info["camera_widths"]
    else:
        camera_height = 256
        camera_width = 256
    
    NUM_PARTITIONS = 5
    num_episode = ROBOT_CAMERA_POSES_DICT[args.robot_dataset]['num_episodes']
    episodes = range(num_episode * args.partition // NUM_PARTITIONS, num_episode * (args.partition + 1) // NUM_PARTITIONS)

    '''
    conda activate mirage
    python /home/guanhuaji/mirage/robot2robot/rendering/export_source_robot_states.py --robot_dataset kaist
    python /home/guanhuaji/mirage/robot2robot/rendering/export_source_robot_states.py --robot_dataset ucsd_kitchen_rlds
    python /home/guanhuaji/mirage/robot2robot/rendering/export_source_robot_states.py --robot_dataset utokyo_pick_and_place
    '''


    for episode in episodes:
        source_env = SourceEnvWrapper(source_name, source_gripper, args.robot_dataset, camera_height, camera_width, verbose=args.verbose)
        source_env.get_source_robot_states(
            save_source_robot_states_path=save_source_robot_states_path, 
            reference_joint_angles_path=args.reference_joint_angles_path, 
            reference_ee_states_path=args.reference_ee_states_path, 
            reference_gripper_states_path=args.reference_gripper_states_path,
            robot_dataset=args.robot_dataset,
            episode=episode
        )

        source_env.source_env.env.close_renderer()