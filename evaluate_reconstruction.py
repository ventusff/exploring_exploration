#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import os
import sys
import json
import h5py
import torch
import logging
import numpy as np
import torch.nn as nn

from exploring_exploration.arguments import get_args
from exploring_exploration.envs import (
    make_vec_envs_avd,
    make_vec_envs_habitat,
)
from exploring_exploration.models import RGBEncoder, MapRGBEncoder, Policy
from exploring_exploration.utils.reconstruction_eval import evaluate_reconstruction
from exploring_exploration.models.reconstruction import (
    FeatureReconstructionModule,
    FeatureNetwork,
    PoseEncoder,
)
from exploring_exploration.utils.reconstruction import rec_loss_fn_classify

args = get_args()

torch.manual_seed(args.seed)
if args.cuda:
    torch.cuda.manual_seed(args.seed)

try:
    os.makedirs(args.log_dir)
except OSError:
    pass

eval_log_dir = os.path.join(args.log_dir, "monitor")

try:
    os.makedirs(eval_log_dir)
except OSError:
    pass


def main():
    torch.set_num_threads(1)
    device = torch.device("cuda:0" if args.cuda else "cpu")
    ndevices = torch.cuda.device_count()
    # Setup loggers
    logging.basicConfig(filename=f"{args.log_dir}/eval_log.txt", level=logging.DEBUG)
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
    logging.getLogger().setLevel(logging.INFO)

    args.feat_shape_sim = (512,)
    args.odometer_shape = (4,)  # (delta_y, delta_x, delta_head, delta_elev)
    args.requires_policy = args.actor_type not in [
        "random",
        "oracle",
        "forward",
        "forward-plus",
        "frontier",
    ]
    if "habitat" in args.env_name:
        if "CUDA_VISIBLE_DEVICES" in os.environ:
            devices = [
                int(dev) for dev in os.environ["CUDA_VISIBLE_DEVICES"].split(",")
            ]
            # Devices need to be indexed between 0 to N-1
            devices = [dev for dev in range(len(devices))]
        else:
            devices = None
        eval_envs = make_vec_envs_habitat(
            args.habitat_config_file, device, devices, seed=args.seed
        )
        if args.actor_type == "frontier":
            large_map_range = 100.0
            H = eval_envs.observation_space.spaces["highres_coarse_occupancy"].shape[1]
            args.occ_map_scale = 0.1 * (2 * large_map_range + 1) / H
    else:
        eval_envs = make_vec_envs_avd(
            args.env_name,
            args.seed + args.num_processes,
            args.num_processes,
            eval_log_dir,
            device,
            True,
            split=args.eval_split,
            nRef=args.num_pose_refs,
            set_return_topdown_map=True,
        )
        if args.actor_type == "frontier":
            large_map_range = 100.0
            H = eval_envs.observation_space.spaces["highres_coarse_occupancy"].shape[0]
            args.occ_map_scale = 50.0 * (2 * large_map_range + 1) / H
    args.obs_shape = eval_envs.observation_space.spaces["im"].shape

    # =================== Load clusters =================
    clusters_h5 = h5py.File(args.clusters_path, "r")
    cluster_centroids = torch.Tensor(np.array(clusters_h5["cluster_centroids"])).to(
        device
    )
    args.nclusters = cluster_centroids.shape[0]
    clusters2images = {}
    for i in range(args.nclusters):
        cluster_images = np.array(
            clusters_h5[f"cluster_{i}/images"]
        )  # (K, C, H, W) torch Tensor
        cluster_images = np.ascontiguousarray(cluster_images.transpose(0, 2, 3, 1))
        cluster_images = (cluster_images * 255.0).astype(np.uint8)
        clusters2images[i] = cluster_images  # (K, H, W, C)
    clusters_h5.close()

    # =================== Create models ====================
    decoder = FeatureReconstructionModule(
        args.nclusters, args.nclusters, nlayers=args.n_transformer_layers,
    )
    feature_network = FeatureNetwork()
    feature_network = nn.DataParallel(feature_network, dim=0)
    pose_encoder = PoseEncoder()
    if args.use_multi_gpu:
        decoder = nn.DataParallel(decoder, dim=1)
        pose_encoder = nn.DataParallel(pose_encoder, dim=0)
    if args.requires_policy:
        encoder = RGBEncoder() if args.encoder_type == "rgb" else MapRGBEncoder()
        action_config = (
            {
                "nactions": eval_envs.action_space.n,
                "embedding_size": args.action_embedding_size,
            }
            if args.use_action_embedding
            else None
        )
        collision_config = (
            {"collision_dim": 2, "embedding_size": args.collision_embedding_size}
            if args.use_collision_embedding
            else None
        )
        actor_critic = Policy(
            eval_envs.action_space,
            base_kwargs={
                "feat_dim": args.feat_shape_sim[0],
                "recurrent": True,
                "hidden_size": args.feat_shape_sim[0],
                "action_config": action_config,
                "collision_config": collision_config,
            },
        )

    # =================== Load models ====================
    decoder_state, pose_encoder_state = torch.load(args.load_path_rec)[:2]
    decoder.load_state_dict(decoder_state)
    pose_encoder.load_state_dict(pose_encoder_state)
    decoder.to(device)
    feature_network.to(device)
    decoder.eval()
    feature_network.eval()
    pose_encoder.eval()
    pose_encoder.to(device)
    if args.requires_policy:
        encoder_state, actor_critic_state = torch.load(args.load_path)[:2]
        encoder.load_state_dict(encoder_state)
        actor_critic.load_state_dict(actor_critic_state)
        actor_critic.to(device)
        encoder.to(device)
        actor_critic.eval()
        encoder.eval()

    eval_config = {}
    eval_config["num_steps"] = args.num_steps
    eval_config["num_processes"] = args.num_processes
    eval_config["feat_shape_sim"] = args.feat_shape_sim
    eval_config["odometer_shape"] = args.odometer_shape
    eval_config["num_eval_episodes"] = args.eval_episodes
    eval_config["num_pose_refs"] = args.num_pose_refs
    eval_config["env_name"] = args.env_name
    eval_config["actor_type"] = args.actor_type
    eval_config["encoder_type"] = args.encoder_type
    eval_config["use_action_embedding"] = args.use_action_embedding
    eval_config["use_collision_embedding"] = args.use_collision_embedding
    eval_config["cluster_centroids"] = cluster_centroids
    eval_config["clusters2images"] = clusters2images
    eval_config["rec_loss_fn"] = rec_loss_fn_classify
    eval_config["vis_save_dir"] = os.path.join(args.log_dir, "visualizations")
    eval_config["forward_action_id"] = 2 if "avd" in args.env_name else 0
    eval_config["turn_action_id"] = 0 if "avd" in args.env_name else 1
    if args.actor_type == "frontier":
        eval_config["occ_map_scale"] = args.occ_map_scale
        eval_config["frontier_dilate_occ"] = args.frontier_dilate_occ
        eval_config["max_time_per_target"] = args.max_time_per_target

    models = {}
    models["decoder"] = decoder
    models["pose_encoder"] = pose_encoder
    models["feature_network"] = feature_network
    if args.requires_policy:
        models["actor_critic"] = actor_critic
        models["encoder"] = encoder

    metrics, per_episode_metrics = evaluate_reconstruction(
        models,
        eval_envs,
        eval_config,
        device,
        multi_step=True,
        interval_steps=args.interval_steps,
        visualize_policy=args.visualize_policy,
    )

    json.dump(
        per_episode_metrics, open(os.path.join(args.log_dir, "statistics.json"), "w")
    )


if __name__ == "__main__":
    main()
