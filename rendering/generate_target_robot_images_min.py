#!/usr/bin/env python3
import argparse, random, numpy as np
from pathlib import Path
from envs import TargetEnvWrapper
from core import pick_best_gpu, locked_json
from config.dataset_poses_dict import ROBOT_CAMERA_POSES_DICT
import json
import imageio.v3 as iio

'''
python /home/guanhuaji/mirage/robot2robot/rendering/generate_target_robot_images_min.py --robot_dataset austin_buds --target_robot Sawyer --partition 0 --load_displacement
'''

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--robot_dataset", required=True)
    p.add_argument("--target_robot",  nargs="+", required=True)
    p.add_argument("--partition",     type=int, default=0)
    p.add_argument("--unlimited",     type=str, default="False")
    p.add_argument("--load_displacement", action="store_true")
    # … 其余 CLI 选项（seed、verbose 等） …
    return p.parse_args()

def main():
    args = parse_args()
    pick_best_gpu()

    user_meta = ROBOT_CAMERA_POSES_DICT[args.robot_dataset]
    meta_path = Path(user_meta["replay_path"]) / "dataset_metadata.json"
    with meta_path.open("r", encoding="utf-8") as f:
        dataset_meta = json.load(f)
    H, W = dataset_meta["image_height"], dataset_meta["image_width"]
    out_root = Path(user_meta["replay_path"])

    # episode 分区逻辑
    NUM_PARTS = 20
    num_ep    = dataset_meta["num_episodes"]
    episodes  = range(num_ep * args.partition // NUM_PARTS,
                      num_ep * (args.partition + 1) // NUM_PARTS)

    for robot in args.target_robot:
        if robot == "Sawyer":
            gripper = "RethinkGripper"
        elif robot == "Jaco":
            gripper = "JacoThreeFingerGripper"
        elif robot == "IIWA" or robot == "UR5e" or robot == "Kinova3":
            gripper = "Robotiq85Gripper"
        elif robot == "Panda":
            gripper = "PandaGripper"
        wrapper = TargetEnvWrapper(robot, gripper, args.robot_dataset,
                                   camera_height=H, camera_width=W)

        wl_path = out_root / robot / "whitelist.json"
        for ep in episodes:
            with locked_json(wl_path) as wl:
                robot_list = wl.get(robot, [])
                if robot_list and ep in robot_list:
                    continue

            wrapper.generate_image(
                save_paired_images_folder_path=out_root,
                source_robot_states_path=out_root,
                robot_dataset=args.robot_dataset,
                episode=ep,
                unlimited=args.unlimited,
                load_displacement=args.load_displacement,
            )
    print("✓ done")

if __name__ == "__main__":
    main()
