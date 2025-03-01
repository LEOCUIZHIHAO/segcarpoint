import pathlib
import pickle
import time
from collections import defaultdict
from functools import partial

import cv2
import numpy as np
import numpy_indexed as npi
from skimage import io as imgio

from second.core import box_np_ops
from second.core import preprocess as prep
from second.core.geometry import points_in_convex_polygon_3d_jit
from second.data import kitti_common as kitti
from second.utils import simplevis
from second.utils.timer import simple_timer

import seaborn as sns
import matplotlib.pyplot as plt

import os

def SimpleVoxel(voxels, num_points):
    points_mean = np.sum(voxels[:, :, :], axis=1, keepdims=True) / num_points.reshape(-1,1,1) #zyxr if fixed container add 1e-5
    # points_mean = np.sum(voxels[:, :, :], axis=1, keepdims=True) / (num_points.reshape(-1,1,1))
    return points_mean

def AddDepthFeature(points, num_point_features):
    if points.shape[-1] == num_point_features:
        depth_feature = np.sqrt(np.sum(np.square(points[...,:3]), axis=1))
        points = np.concatenate((points, depth_feature.reshape(-1,1)), axis=-1)
    else:
        assert points.shape[-1] == 3
        depth_feature = np.sqrt(np.sum(np.square(points), axis=1))
        points = np.concatenate((points, depth_feature.reshape(-1,1)), axis=-1)
    return points

def VoxelRandomChoice(voxels, coors, sample_size, num_point_features, max_num_points_per_voexl):
    voxel_num = len(voxels)
    if voxel_num < sample_size:
        gap = sample_size - voxel_num

        # voxel_idx = np.random.choice(voxel_num, size=gap, replace=True)
        # voxels = np.concatenate((voxels, voxels[voxel_idx]), axis=0)
        # coors = np.concatenate((coors, coors[voxel_idx]), axis=0)
        # num_points = np.concatenate((num_points, num_points[voxel_idx]), axis=0)
        voxels_zero = np.zeros(shape=(gap, max_num_points_per_voexl, num_point_features)) #fill zero
        coors_zero = np.zeros(shape=(gap, 3)) #fill zero
        # num_points_zero = np.zeros(shape=(gap,)) #fill zero
        voxels = np.concatenate((voxels, voxels_zero), axis=0)
        coors = np.concatenate((coors, coors_zero), axis=0)
        # num_points = np.concatenate((num_points, num_points_zero), axis=0)

    else:
        choice = np.random.randint(0, voxel_num, sample_size)
        # voxel_idx = np.random.choice(voxel_num, size=sample_size, replace=False)
        voxels = voxels[choice]
        coors = coors[choice]
        # num_points = num_points[choice]

    return voxels, coors #, num_points

def Voxel3DStack2D(voxels, coors, num_points):
    coords_xy = np.delete(coors, obj=0, axis=1) #yx
    _coors, voxels = npi.group_by(coords_xy).mean(voxels) #_coors = yx
    _, num_points = npi.group_by(coords_xy).sum(num_points)
    return voxels, _coors, num_points

def SampleNegAnchors(pos_anchors, neg_anchors, sample_size, num_anchor_per_loc):

    tot_points = sample_size * num_anchor_per_loc

    gap = tot_points - len(pos_anchors)
    #neg_anchors_idx = np.random.choice(len(neg_anchors), size=gap, replace=False)
    neg_anchors_idx = np.random.randint(0, len(neg_anchors), gap)
    neg_anchors = neg_anchors[neg_anchors_idx,:]
    anchors = np.concatenate((pos_anchors, neg_anchors),axis=0)

    return anchors

def SamplePointsKeepALLPositive(points_in_box, points_out_box, sample_size, num_point_features):
    tot_points = len(points_out_box) + len(points_in_box)

    #car point greater than sample points sample
    if len(points_in_box) > sample_size:
        pos_gap = int(sample_size/2)
        neg_gap = sample_size - pos_gap
        points_in_box_idx = np.random.choice(len(points_in_box), size=pos_gap, replace=False)
        points_out_box_idx = np.random.choice(len(points_out_box), size=neg_gap, replace=True)
        points_in_box = points_in_box[points_in_box_idx]
        points_out_box = points_out_box[points_out_box_idx]
    #total points less than sample then random upsampling bg points
    elif tot_points < sample_size:
        gap = sample_size - tot_points
        # fill zero
        # points_out_box = np.zeros(shape=(gap, num_point_features)) #

        # sample bg
        points_out_box_idx = np.random.randint(0, len(points_out_box), gap)
        points_out_box = np.concatenate((points_out_box, points_out_box[points_out_box_idx]),axis=0)

    #total points greater than sample then downsample bg points
    else:
        down_sample = sample_size - len(points_in_box)
        # points_out_box_idx = np.random.choice(len(points_out_box), size=down_sample, replace=False)
        points_out_box_idx = np.random.randint(0, len(points_out_box), down_sample)
        points_out_box= points_out_box[points_out_box_idx]

    return points_in_box, points_out_box

def SamplePoints(points, sample_size, num_point_features):
    points_num = len(points)
    if points_num < sample_size:
        gap = sample_size - points_num
        _gap = np.zeros(shape=(gap, num_point_features)) #fill zero
        points = np.concatenate((points, _gap),axis=0)
    else:
        # points_idx = np.random.randint(0, points_num, sample_size)
        points_idx = np.random.choice(points_num, size=sample_size, replace=False) #faster for large num > 10,000
        points = points[points_idx,:]
    return points

def PointRandomChoice(points, sample_size):
    points_num = len(points)
    if points_num < sample_size:
        gap = sample_size - points_num
        # points_idx = np.random.choice(points_num, size=gap, replace=True)
        points_idx = np.random.randint(0, points_num, gap) #faster for samll num
        points = np.concatenate((points, points[points_idx]), axis=0)
    else:
        points_idx = np.random.choice(points_num, size=sample_size, replace=False) #faster for large num > 10,000
        points = points[points_idx]
    return points

def PointRandomChoiceV2(points, sample_size):
    points_num = len(points)
    if points_num < sample_size:
        gap = sample_size - points_num
        points_idx = np.random.randint(0, points_num, gap) #faster for samll num
        points = np.concatenate((points, points[points_idx]), axis=0)
    else:
        pts_depth = points[:, 0] #xyzr
        pts_near_flag = pts_depth < 32 #x = [0,70.4]
        far_idxs_choice = np.where(pts_near_flag == 0)[0]
        near_idxs = np.where(pts_near_flag == 1)[0]

        near_idxs_choice = np.random.choice(near_idxs, sample_size - len(far_idxs_choice), replace=False)

        if len(far_idxs_choice) > 0:
            points_idx = np.concatenate((near_idxs_choice, far_idxs_choice), axis=0)
        else:
            points_idx = near_idxs_choice
        points = points[points_idx]

    return points

def PrepDataAndLabel(points_in_box, points_out_box):
    points_in_label = np.ones(shape=(len(points_in_box),1), dtype=int)
    points_out_label = np.zeros(shape=(len(points_out_box),1), dtype=int)

    data = np.concatenate((points_in_box,points_out_box),axis=0)
    label = np.concatenate((points_in_label,points_out_label),axis=0)
    return data, label

def FillRegWithNeg(seg_labels, bbox_targets, anchors_labels, seg_keep_points, num_anchor_per_loc):
    bbox_targets_channel = bbox_targets.shape[-1]

    if len(seg_labels) > len(anchors_labels):
        gap = len(seg_labels) - len(anchors_labels) + seg_keep_points # gap + another 12000
        bbox_targets_fill = np.zeros(shape=(gap, bbox_targets_channel)) #fill with 0
        anchors_labels_fill = -1*np.ones(shape=(gap)) #label -1
        bbox_targets = np.concatenate((bbox_targets, bbox_targets_fill), axis=0)
        anchors_labels = np.concatenate((anchors_labels, anchors_labels_fill), axis=0)

    else:
        gap = (seg_keep_points * num_anchor_per_loc) - len(anchors_labels)
        bbox_targets_fill = np.zeros(shape=(gap, bbox_targets_channel)) #fill with 0
        anchors_labels_fill = -1 * np.ones(shape=(gap)) #label -1
        bbox_targets = np.concatenate((bbox_targets, bbox_targets_fill), axis=0)
        anchors_labels = np.concatenate((anchors_labels, anchors_labels_fill), axis=0)

    return bbox_targets, anchors_labels

def merge_second_batch(batch_list):
    example_merged = defaultdict(list)
    for example in batch_list:
        for k, v in example.items():
            example_merged[k].append(v)
    ret = {}
    for key, elems in example_merged.items():
        if key in [
                'voxels', 'num_points', 'num_gt', 'voxel_labels', 'gt_names', 'gt_classes', 'gt_boxes'
        ]:
            ret[key] = np.concatenate(elems, axis=0)
        elif key == 'metadata':
            ret[key] = elems
        elif key == "calib":
            ret[key] = {}
            for elem in elems:
                for k1, v1 in elem.items():
                    if k1 not in ret[key]:
                        ret[key][k1] = [v1]
                    else:
                        ret[key][k1].append(v1)
            for k1, v1 in ret[key].items():
                ret[key][k1] = np.stack(v1, axis=0)
        elif key == 'coordinates':
            coors = []
            for i, coor in enumerate(elems):
                coor_pad = np.pad(
                    coor, ((0, 0), (1, 0)), mode='constant', constant_values=i)
                coors.append(coor_pad)
            ret[key] = np.concatenate(coors, axis=0)
        elif key == 'metrics':
            ret[key] = elems
        else:
            ret[key] = np.stack(elems, axis=0)
    return ret

def merge_second_batch_multigpu(batch_list):
    example_merged = defaultdict(list)
    for example in batch_list:
        for k, v in example.items():
            example_merged[k].append(v)
    ret = {}
    for key, elems in example_merged.items():
        if key == 'metadata':
            ret[key] = elems
        elif key == "calib":
            ret[key] = {}
            for elem in elems:
                for k1, v1 in elem.items():
                    if k1 not in ret[key]:
                        ret[key][k1] = [v1]
                    else:
                        ret[key][k1].append(v1)
            for k1, v1 in ret[key].items():
                ret[key][k1] = np.stack(v1, axis=0)
        elif key == 'coordinates':
            coors = []
            for i, coor in enumerate(elems):
                coor_pad = np.pad(
                    coor, ((0, 0), (1, 0)), mode='constant', constant_values=i)
                coors.append(coor_pad)
            ret[key] = np.stack(coors, axis=0)
        elif key in ['gt_names', 'gt_classes', 'gt_boxes']:
            continue
        else:
            ret[key] = np.stack(elems, axis=0)

    return ret

def _dict_select(dict_, inds):
    for k, v in dict_.items():
        if isinstance(v, dict):
            _dict_select(v, inds)
        else:
            dict_[k] = v[inds]


def prep_pointcloud(input_dict,
                    root_path,
                    voxel_generator,
                    target_assigner,
                    db_sampler=None,
                    max_voxels=20000,
                    remove_outside_points=False,
                    training=True,
                    create_targets=True,
                    shuffle_points=False,
                    remove_unknown=False,
                    gt_rotation_noise=(-np.pi / 3, np.pi / 3),
                    gt_loc_noise_std=(1.0, 1.0, 1.0),
                    global_rotation_noise=(-np.pi / 4, np.pi / 4),
                    global_scaling_noise=(0.95, 1.05),
                    global_random_rot_range=(0.78, 2.35),
                    global_translate_noise_std=(0, 0, 0),
                    num_point_features=4,
                    anchor_area_threshold=1,
                    gt_points_drop=0.0,
                    gt_drop_max_keep=10,
                    remove_points_after_sample=True,
                    anchor_cache=None,
                    remove_environment=False,
                    random_crop=False,
                    reference_detections=None,
                    out_size_factor=2,
                    use_group_id=False,
                    multi_gpu=False,
                    min_points_in_gt=-1,
                    random_flip_x=True,
                    random_flip_y=True,
                    sample_importance=1.0,
                    out_dtype=np.float32,
                    bcl_keep_voxels=6500, #6000~8000 pillar
                    seg_keep_points=8000,
                    points_per_voxel=200,
                    feature_map_size=[1, 200, 176],
                    num_anchor_per_loc=2,
                    segmentation=False,
                    object_detection=True):
    """convert point cloud to voxels, create targets if ground truths
    exists.

    input_dict format: dataset.get_sensor_data format

    """

    class_names = target_assigner.classes
    points = input_dict["lidar"]["points"]

    if training or segmentation:
        anno_dict = input_dict["lidar"]["annotations"]
        gt_dict = {
            "gt_boxes": anno_dict["boxes"],
            "gt_names": anno_dict["names"],
            "gt_importance": np.ones([anno_dict["boxes"].shape[0]], dtype=anno_dict["boxes"].dtype),
        }

        if "difficulty" not in anno_dict:
            difficulty = np.zeros([anno_dict["boxes"].shape[0]],
                                  dtype=np.int32)
            gt_dict["difficulty"] = difficulty
        else:
            gt_dict["difficulty"] = anno_dict["difficulty"]

        if use_group_id and "group_ids" in anno_dict:
            group_ids = anno_dict["group_ids"]
            gt_dict["group_ids"] = group_ids

    calib = None
    if "calib" in input_dict:
        calib = input_dict["calib"]

    if reference_detections is not None:
        assert calib is not None and "image" in input_dict
        C, R, T = box_np_ops.projection_matrix_to_CRT_kitti(P2)
        frustums = box_np_ops.get_frustum_v2(reference_detections, C)
        frustums -= T
        frustums = np.einsum('ij, akj->aki', np.linalg.inv(R), frustums)
        frustums = box_np_ops.camera_to_lidar(frustums, rect, Trv2c)
        surfaces = box_np_ops.corner_to_surfaces_3d_jit(frustums)
        masks = points_in_convex_polygon_3d_jit(points, surfaces)
        points = points[masks.any(-1)]
    if remove_outside_points:
        assert calib is not None
        image_shape = input_dict["image"]["image_shape"]
        points = box_np_ops.remove_outside_points(
            points, calib["rect"], calib["Trv2c"], calib["P2"], image_shape)
    if remove_environment is True and training:
        selected = kitti.keep_arrays_by_name(gt_names, target_assigner.classes)
        _dict_select(gt_dict, selected)
        masks = box_np_ops.points_in_rbbox(points, gt_dict["gt_boxes"])
        points = points[masks.any(-1)]


    if training:
        boxes_lidar = gt_dict["gt_boxes"]
        selected = kitti.drop_arrays_by_name(gt_dict["gt_names"], ["DontCare"])
        _dict_select(gt_dict, selected)
        if remove_unknown:
            remove_mask = gt_dict["difficulty"] == -1
            """
            gt_boxes_remove = gt_boxes[remove_mask]
            gt_boxes_remove[:, 3:6] += 0.25
            points = prep.remove_points_in_boxes(points, gt_boxes_remove)
            """
            keep_mask = np.logical_not(remove_mask)
            _dict_select(gt_dict, keep_mask)
        gt_dict.pop("difficulty")
        if min_points_in_gt > 0:
            # points_count_rbbox takes 10ms with 10 sweeps nuscenes data
            point_counts = box_np_ops.points_count_rbbox(points, gt_dict["gt_boxes"])
            mask = point_counts >= min_points_in_gt
            _dict_select(gt_dict, mask)
        gt_boxes_mask = np.array(
            [n in class_names for n in gt_dict["gt_names"]], dtype=np.bool_)
        if db_sampler is not None:
            group_ids = None
            if "group_ids" in gt_dict:
                group_ids = gt_dict["group_ids"]

            sampled_dict = db_sampler.sample_all(
                root_path,
                gt_dict["gt_boxes"],
                gt_dict["gt_names"],
                num_point_features,
                random_crop,
                gt_group_ids=group_ids,
                calib=calib)

            if sampled_dict is not None:
                sampled_gt_names = sampled_dict["gt_names"]
                sampled_gt_boxes = sampled_dict["gt_boxes"]
                sampled_points = sampled_dict["points"]
                sampled_gt_masks = sampled_dict["gt_masks"]
                gt_dict["gt_names"] = np.concatenate(
                    [gt_dict["gt_names"], sampled_gt_names], axis=0)
                gt_dict["gt_boxes"] = np.concatenate(
                    [gt_dict["gt_boxes"], sampled_gt_boxes])
                gt_boxes_mask = np.concatenate(
                    [gt_boxes_mask, sampled_gt_masks], axis=0)
                sampled_gt_importance = np.full([sampled_gt_boxes.shape[0]], sample_importance, dtype=sampled_gt_boxes.dtype)
                gt_dict["gt_importance"] = np.concatenate(
                    [gt_dict["gt_importance"], sampled_gt_importance])

                if group_ids is not None:
                    sampled_group_ids = sampled_dict["group_ids"]
                    gt_dict["group_ids"] = np.concatenate(
                        [gt_dict["group_ids"], sampled_group_ids])

                if remove_points_after_sample:
                    masks = box_np_ops.points_in_rbbox(points,
                                                       sampled_gt_boxes)
                    points = points[np.logical_not(masks.any(-1))]

                points = np.concatenate([sampled_points, points], axis=0)

        pc_range = voxel_generator.point_cloud_range
        group_ids = None
        if "group_ids" in gt_dict:
            group_ids = gt_dict["group_ids"]

        prep.noise_per_object_v3_(
            gt_dict["gt_boxes"],
            points,
            gt_boxes_mask,
            rotation_perturb=gt_rotation_noise,
            center_noise_std=gt_loc_noise_std,
            global_random_rot_range=global_random_rot_range,
            group_ids=group_ids,
            num_try=100)

        # should remove unrelated objects after noise per object
        # for k, v in gt_dict.items():
        #     print(k, v.shape)
        _dict_select(gt_dict, gt_boxes_mask)
        gt_classes = np.array(
            [class_names.index(n) + 1 for n in gt_dict["gt_names"]],
            dtype=np.int32)
        gt_dict["gt_classes"] = gt_classes
        gt_dict["gt_boxes"], points = prep.random_flip(gt_dict["gt_boxes"],
                                                   points, 0.5, random_flip_x, random_flip_y)
        gt_dict["gt_boxes"], points = prep.global_rotation_v2(
            gt_dict["gt_boxes"], points, *global_rotation_noise)
        gt_dict["gt_boxes"], points = prep.global_scaling_v2(
            gt_dict["gt_boxes"], points, *global_scaling_noise)
        prep.global_translate_(gt_dict["gt_boxes"], points, global_translate_noise_std)

        bv_range = voxel_generator.point_cloud_range[[0, 1, 3, 4]]
        mask = prep.filter_gt_box_outside_range_by_center(gt_dict["gt_boxes"], bv_range)
        _dict_select(gt_dict, mask)

        # limit rad to [-pi, pi]
        gt_dict["gt_boxes"][:, 6] = box_np_ops.limit_period(
            gt_dict["gt_boxes"][:, 6], offset=0.5, period=2 * np.pi)

    # add depth for point feature and remove intensity
    # points = points[...,:3]
    # points = AddDepthFeature(points, num_point_features)
    # num_point_features = points.shape[-1] #update point shape

    #remove points out of PC rannge
    pc_range = voxel_generator.point_cloud_range # [0, -40, -3, 70.4, 40, 1] xmin,ymin.zmin. xmax. ymax, zmax
    points = box_np_ops.remove_out_pc_range_points(points, pc_range)

    if shuffle_points and not segmentation:
        np.random.shuffle(points) # shuffle is a little slow.

    if not training and segmentation:
        #Keep Car Only
        gt_boxes_mask = np.array(
            [n in class_names for n in gt_dict["gt_names"]], dtype=np.bool_)
        _dict_select(gt_dict, gt_boxes_mask)

        points_in_box, points_out_box = box_np_ops.split_points_in_boxes(points, gt_dict["gt_boxes"]) #xyzr
        points_in_box, points_out_box = SamplePointsKeepALLPositive(points_in_box, points_out_box, seg_keep_points, num_point_features) #fixed points
        data, label = PrepDataAndLabel(points_in_box, points_out_box)

        example = {
        'seg_points': data, #data
        'seg_labels': label, #label
        'gt_boxes' : gt_dict["gt_boxes"],
        'image_idx' : input_dict['metadata']['image_idx'],
        }

        ################# For feature map Focs
        # # # NOTE: For feature map Focs
        point_cloud_range = np.array(voxel_generator.point_cloud_range)
        anchor_strides = (point_cloud_range[3:] - point_cloud_range[:3]) / feature_map_size[::-1]
        anchor_offsets = point_cloud_range[:3] + anchor_strides / 2
        centers = box_np_ops.create_anchors_3d_stride(feature_map_size,
                                            anchor_strides = anchor_strides,
                                            anchor_offsets = anchor_offsets,
                                            rotations=[0])
        centers = centers.squeeze()[...,:3].reshape(-1,3)
        example.update({
            'coords_center': centers, # if anchors free the 0 is the horizontal/vertical anchors
        })
        ##############

        ################ Fcos & points to voxel Test
        # NOTE: For voxel seg net
        # _, coords, coords_center, p2voxel_idx = box_np_ops.points_to_3dvoxel(data,
        #                                         feat_size=[100,80,10],
        #                                         max_voxels=bcl_keep_voxels,
        #                                         num_p_voxel=points_per_voxel)
        # example = {
        # 'seg_points': data, #data
        # 'coords': coords,
        # 'coords_center': coords_center,
        # 'p2voxel_idx': p2voxel_idx,
        # 'gt_boxes' : gt_dict["gt_boxes"],
        # 'image_idx' : input_dict['metadata']['image_idx'],
        # "gt_num" :  len(gt_dict["gt_boxes"]),
        # 'gt_boxes' : gt_dict["gt_boxes"],
        # 'seg_labels': label
        # }
        ################ Fcos & points to voxel

        if anchor_cache is not None:

            example.update({
                "gt_num" :  len(gt_dict["gt_boxes"]), #how many objects in eval GT
                "anchors": anchor_cache["anchors"]
            })

        return example

    ################################Car point segmentation#####################
    if training and segmentation:
        # points_in_box = box_np_ops.points_in_rbbox(points, gt_dict["gt_boxes"]) #xyzr
        # enlarge bouding box
        # enlarge_size = 0.2
        # gt_dict["gt_boxes"][:, 3:6] = gt_dict["gt_boxes"][:, 3:6] + enlarge_size #xyzhwlr
        # masks = box_np_ops.points_in_rbbox(points, gt_dict["gt_boxes"])
        # points = points[np.logical_not(masks.any(-1))]
        # points = np.concatenate((points, points_in_box), axis=0)

        #above and below bouding box should have no points
        # gt_dict["gt_boxes"][:,3] += 2

        #random sample
        # points = SamplePoints(points, seg_keep_points, num_point_features) #Sample zero
        # points = PointRandomChoice(points, seg_keep_points) #Repeat sample
        points = PointRandomChoiceV2(points, seg_keep_points) #Repeat sample according points distance
        points_in_box, points_out_box = box_np_ops.split_points_in_boxes(points, gt_dict["gt_boxes"]) #xyzr
        data, label = PrepDataAndLabel(points_in_box, points_out_box)

        #keep positive sample
        # points_in_box, points_out_box = box_np_ops.split_points_in_boxes(points, gt_dict["gt_boxes"]) #xyzr
        # points_in_box, points_out_box = SamplePointsKeepALLPositive(points_in_box, points_out_box, seg_keep_points, num_point_features) #fixed 18888 points
        # data, label = PrepDataAndLabel(points_in_box, points_out_box)

        """shuffle car seg points"""
        indices = np.arange(data.shape[0])
        np.random.shuffle(indices)
        data = data[indices]
        label = label[indices]

        example = {
        'seg_points': data, #data
        'seg_labels': label, #label
        'gt_boxes': gt_dict["gt_boxes"],
        }

        ################# For feature map Focs
        # # NOTE: For feature map Focs
        point_cloud_range = np.array(voxel_generator.point_cloud_range)
        anchor_strides = (point_cloud_range[3:] - point_cloud_range[:3]) / feature_map_size[::-1]
        anchor_offsets = point_cloud_range[:3] + anchor_strides / 2
        centers = box_np_ops.create_anchors_3d_stride(feature_map_size,
                                            anchor_strides = anchor_strides,
                                            anchor_offsets = anchor_offsets,
                                            rotations=[0])
        centers = centers.squeeze()[...,:3].reshape(-1,3)
        targets_dict = box_np_ops.fcos_box_encoder_v2(centers, gt_dict["gt_boxes"])
        # bbox = box_np_ops.fcos_box_decoder_v2(np.expand_dims(centers, 0),
        #                                 np.expand_dims(targets_dict["bbox_targets"], 0))
        # labels = targets_dict["labels"]
        # with open(os.path.join('./debug_tool',"points.pkl") , 'wb') as f:
        #     pickle.dump(data,f)
        # with open(os.path.join('./debug_tool',"seg_points.pkl") , 'wb') as f:
        #     pickle.dump(centers[labels==1],f)
        # with open(os.path.join('./debug_tool',"pd_boxes.pkl") , 'wb') as f:
        #     pickle.dump(bbox.squeeze()[labels==1],f)
        # with open(os.path.join('./debug_tool',"gt_boxes.pkl") , 'wb') as f:
        #     pickle.dump(gt_dict["gt_boxes"],f)
        # exit()
        example.update({
            'labels': targets_dict['labels'], # if anchors free the 0 is the horizontal/vertical anchors
            # 'seg_labels': targets_dict['labels'], # if anchors free the 0 is the horizontal/vertical anchors
            'reg_targets': targets_dict['bbox_targets'], # target assign get offsite
            'importance': targets_dict['importance'],
            # 'reg_weights': targets_dict['bbox_outside_weights'],
        })
        ##############

        ################ Fcos & points to voxel
        # NOTE: For voxel seg net
        # _, coords, coords_center, p2voxel_idx = box_np_ops.points_to_3dvoxel(data,
        #                                         feat_size=[200,176,10],
        #                                         max_voxels=bcl_keep_voxels,
        #                                         num_p_voxel=points_per_voxel)
        #
        # targets_dict = box_np_ops.fcos_box_encoder_v2(coords_center,
        #                                     gt_dict["gt_boxes"])
        # # Jim added
        # example.update({
        # 'coords': coords,
        # 'p2voxel_idx': p2voxel_idx,
        # 'cls_labels': targets_dict['labels'], # if anchors free the 0 is the horizontal/vertical anchors
        # 'reg_targets': targets_dict['bbox_targets'], # target assign get offsite
        # 'importance': targets_dict['importance'],
        # })
        ################ Fcos & points to voxel

        ################ Fcos & points to voxel
        if anchor_cache is not None:
            anchors = anchor_cache["anchors"]
            anchors_bv = anchor_cache["anchors_bv"]
            anchors_dict = anchor_cache["anchors_dict"]
            matched_thresholds = anchor_cache["matched_thresholds"]
            unmatched_thresholds = anchor_cache["unmatched_thresholds"]

            targets_dict = target_assigner.assign(
                anchors,
                anchors_dict, #this is the key to control the number of anchors (input anchors) ['anchors, unmatch,match']
                gt_dict["gt_boxes"],
                anchors_mask=None,
                gt_classes=gt_dict["gt_classes"],
                gt_names=gt_dict["gt_names"],
                matched_thresholds=matched_thresholds,
                unmatched_thresholds=unmatched_thresholds,
                importance=gt_dict["gt_importance"])

            example.update({
                'labels': targets_dict['labels'], # if anchors free the 0 is the horizontal/vertical anchors
                'reg_targets': targets_dict['bbox_targets'], # target assign get offsite
                #'importance': targets_dict['importance'],
            })

            # boxes_lidar = gt_dict["gt_boxes"]
            # bev_map = simplevis.kitti_vis(points, boxes_lidar, gt_dict["gt_names"])
            # assigned_anchors = anchors[targets_dict['labels'] > 0]
            # ignored_anchors = anchors[targets_dict['labels'] == -1]
            # bev_map = simplevis.draw_box_in_bev(bev_map, [0, -40, -3, 70.4, 40, 1], ignored_anchors, [128, 128, 128], 2)
            # bev_map = simplevis.draw_box_in_bev(bev_map, [0, -40, -3, 70.4, 40, 1], assigned_anchors, [255, 0, 0])
            # cv2.imwrite('./visualization/anchors/anchors_{}.png'.format(input_dict['metadata']['image_idx']),bev_map)

        return example

    #################################voxel_generator############################
    '''
    voxel_size = voxel_generator.voxel_size # [0, -40, -3, 70.4, 40, 1]
    pc_range = voxel_generator.point_cloud_range
    grid_size = voxel_generator.grid_size # [352, 400]
    max_num_points_per_voxel = voxel_generator.max_num_points_per_voxel

    if not multi_gpu:
        res = voxel_generator.generate(
            points, max_voxels)
        voxels = res["voxels"]
        coordinates = res["coordinates"]
        num_points = res["num_points_per_voxel"]
        num_voxels = np.array([voxels.shape[0]], dtype=np.int64)
    else:
        res = voxel_generator.generate_multi_gpu(
            points, max_voxels)
        voxels = res["voxels"]
        coordinates = res["coordinates"]
        num_points = res["num_points_per_voxel"]
        num_voxels = np.array([res["voxel_num"]], dtype=np.int64)

    example = {
        'voxels': voxels,
        #'num_points': num_points,
        'coordinates': coordinates,
        "num_voxels": num_voxels,
    }


    ## WARNING:  For Simplex voxel Testing if bug comment this
    voxels= SimpleVoxel(voxels, num_points) #(V,100,C) -> (B, C, V, N) #For Second, if Pillar comment it
    max_num_points_per_voxel=1 #If SimpleVoxel max_num_points_per_voxel=1

    voxels, coordinates = VoxelRandomChoice(voxels, coordinates,
                                        bcl_keep_voxels, num_point_features,
                                        max_num_points_per_voexl=max_num_points_per_voxel)

    example['voxels']=voxels
    example['coordinates']=coordinates
    '''
    ############################################################################


    # if calib is not None:
    #     example["calib"] = calib

    if anchor_cache is not None:
        anchors = anchor_cache["anchors"]
        anchors_bv = anchor_cache["anchors_bv"]
        anchors_dict = anchor_cache["anchors_dict"]
        matched_thresholds = anchor_cache["matched_thresholds"]
        unmatched_thresholds = anchor_cache["unmatched_thresholds"]

    else:
        # generate anchors from ground truth
        """
        voxels= SimpleVoxel(voxels, num_points) #(V,100,C) -> (B, C, V, N)
        voxels, coordinates, num_points = VoxelRandomChoice(voxels, coordinates, num_points, bcl_keep_voxels)
        example['voxels']=voxels
        example['num_points']=num_points
        example['coordinates']=coordinates
        example['num_voxels']=bcl_keep_voxels

        if training:
            # for anchor free
            gt_boxes_coords = gt_dict["gt_boxes"][:,:3] #original gt xyz
            example['gt_boxes_coords']=gt_boxes_coords #GT save to example
            gt_boxes_coords = np.round(gt_dict["gt_boxes"][:,:3]).astype(int) #round xyz
            gt_boxes_coords = gt_boxes_coords[:,::-1] #zyx reverse
            ret = target_assigner.generate_anchors_from_gt(gt_boxes_coords) #for GT generate anchors
            anchors = ret["anchors"]
            anchors_dict = target_assigner.generate_anchors_dict_from_gt(gt_boxes_coords) #for GT generate anchors

        if not training:
            # for anchor free
            feature_map_size = grid_size[:2] // out_size_factor
            feature_map_size = [*feature_map_size, 1][::-1]
            ret = target_assigner.generate_anchors(feature_map_size)
            anchors_dict = target_assigner.generate_anchors_dict(feature_map_size)
            anchors = ret["anchors"]
        """


        # # generate anchors from anchor free (Voxel-wise)
        # ret = target_assigner.generate_anchors_from_voxels(coordinates) #for coordinates generate anchors
        # anchors_dict = target_assigner.generate_anchors_dict_from_voxels(coordinates) #this is the key to control the number of anchors (input anchors)
        # anchors = ret["anchors"]
        # matched_thresholds = ret["matched_thresholds"]
        # unmatched_thresholds = ret["unmatched_thresholds"]

        # generate anchors from  voxel + anchor free
        """
        gt_boxes_coords = gt_dict["gt_boxes"][:,:3] #original gt xyz
        #gt_boxes_coords = np.round(gt_dict["gt_boxes"][:,:3]).astype(int) #round xyz
        gt_boxes_coords = gt_boxes_coords[:,::-1] #zyx reverse

        #stack ret and ret_gt
        ret = target_assigner.generate_anchors_from_voxels(coordinates) #for coordinates generate anchors
        ret_gt = target_assigner.generate_anchors_from_gt(gt_boxes_coords) #for GT generate anchors
        for k in ret.keys():
            ret[k] = np.concatenate((ret[k], ret_gt[k]))
        anchors = ret["anchors"]

        #stack anchors_dict and anchors_dict_gt
        anchors_dict = target_assigner.generate_anchors_dict_from_voxels(coordinates) #this is the key to control the number of anchors (input anchors) ['anchors, unmatch,match']
        anchors_dict_gt = target_assigner.generate_anchors_dict_from_gt(gt_boxes_coords) #for GT generate anchors

        for order_k in anchors_dict.keys():
            for k in anchors_dict[order_k].keys():
                anchors_dict[order_k][k] = np.concatenate((anchors_dict[order_k][k], anchors_dict_gt[order_k][k]))
        """

        # generate anchors from groundtruth
        """
        if training:
            # generate anchors from car points
            points_in_box = points_in_box[:,:3] #xyz
            points_in_box = points_in_box[:,::-1] #zyx
            ret = target_assigner.generate_anchors_from_gt(points_in_box) #for GT generate anchors
            anchors = ret["anchors"]
            anchors_dict = target_assigner.generate_anchors_dict_from_gt(points_in_box) #for GT generate anchors


            anchors_bv = box_np_ops.rbbox2d_to_near_bbox(
                anchors[:, [0, 1, 3, 4, 6]])
            matched_thresholds = ret["matched_thresholds"]
            unmatched_thresholds = ret["unmatched_thresholds"]
        """

    # Fcos points sampling
    points = SamplePoints(points, bcl_keep_voxels, num_point_features)
    example = {
        'voxels': np.expand_dims(points, 0),
        #'num_points': num_points,
        'coordinates': points,
        # "num_voxels": None,
    }
    if not training:
        anno_dict = input_dict["lidar"]["annotations"]
        gt_dict = {
            "gt_boxes": anno_dict["boxes"],
            "gt_names": anno_dict["names"],
            "gt_importance": np.ones([anno_dict["boxes"].shape[0]], dtype=anno_dict["boxes"].dtype),
        }
    targets_dict = box_np_ops.fcos_box_encoder_v2(points, gt_dict["gt_boxes"])
    # targets_dict = box_np_ops.fcos_box_encoder(points, gt_dict["gt_boxes"])
    example.update({
        'labels': targets_dict['labels'], # if anchors free the 0 is the horizontal/vertical anchors
        'seg_labels': targets_dict['labels'], # if anchors free the 0 is the horizontal/vertical anchors
        'reg_targets': targets_dict['bbox_targets'], # target assign get offsite
        'importance': targets_dict['importance'],
        # 'reg_weights': targets_dict['bbox_outside_weights'],
    })
    # example["anchors"] = anchors
    # anchors_mask = None
    # if anchor_area_threshold >= 0:
    #     # slow with high resolution. recommend disable this forever.
    #     coors = coordinates
    #     dense_voxel_map = box_np_ops.sparse_sum_for_anchors_mask(
    #         coors, tuple(grid_size[::-1][1:]))
    #     dense_voxel_map = dense_voxel_map.cumsum(0)
    #     dense_voxel_map = dense_voxel_map.cumsum(1)
    #     anchors_area = box_np_ops.fused_get_anchors_area(
    #         dense_voxel_map, anchors_bv, voxel_size, pc_range, grid_size)
    #     anchors_mask = anchors_area > anchor_area_threshold
    #     # example['anchors_mask'] = anchors_mask.astype(np.uint8)
    #     example['anchors_mask'] = anchors_mask

    if not training:
        # Use it when debuging eval nms for good
        eval_classes = input_dict["lidar"]["annotations"]["names"]
        eval_gt_dict = {"gt_names" : eval_classes}
        gt_boxes_mask = np.array(
            [n in class_names for n in eval_classes], dtype=np.bool_)
        _dict_select(eval_gt_dict, gt_boxes_mask)
        example["gt_num"]= len(eval_gt_dict["gt_names"]) #how many objects in eval GT

        return example

    example["gt_names"] = gt_dict["gt_names"]
    # voxel_labels = box_np_ops.assign_label_to_voxel(gt_boxes, coordinates,
    #                                                 voxel_size, coors_range)

    """
    # bev anchors without screening
    boxes_lidar = gt_dict["gt_boxes"]
    bev_map = simplevis.kitti_vis(points, boxes_lidar, gt_dict["gt_names"])
    bev_map = simplevis.draw_box_in_bev(bev_map, [0, -40, -3, 70.4, 40, 1], anchors, [255, 0, 0]) #assigned_anchors blue
    cv2.imwrite('anchors/anchors_{}.png'.format(input_dict['metadata']['image_idx']),bev_map)
    # cv2.imshow('anchors', bev_map)
    # cv2.waitKey(0)
    """

    if create_targets:
        # No particular use
        where = None
        # Fcos target generator and encoder
        # targets_dict = target_assigner.assign(
        #     anchors,
        #     anchors_dict, #this is the key to control the number of anchors (input anchors) ['anchors, unmatch,match']
        #     gt_dict["gt_boxes"],
        #     anchors_mask,
        #     gt_classes=gt_dict["gt_classes"],
        #     gt_names=gt_dict["gt_names"],
        #     matched_thresholds=matched_thresholds,
        #     unmatched_thresholds=unmatched_thresholds,
        #     importance=gt_dict["gt_importance"])
        ################################Visualaiziton###########################
        """
        bev anchors with points
        boxes_lidar = gt_dict["gt_boxes"]
        bev_map = simplevis.kitti_vis(points, boxes_lidar, gt_dict["gt_names"])
        assigned_anchors = anchors[targets_dict['labels'] > 0]
        ignored_anchors = anchors[targets_dict['labels'] == -1]
        bev_map = simplevis.draw_box_in_bev(bev_map, [0, -40, -3, 70.4, 40, 1], ignored_anchors, [128, 128, 128], 2) #ignored_anchors gray    #[0, -30, -3, 64, 30, 1] for kitti
        bev_map = simplevis.draw_box_in_bev(bev_map, [0, -40, -3, 70.4, 40, 1], assigned_anchors, [255, 0, 0]) #assigned_anchors blue
        cv2.imwrite('anchors/anchors_{}.png'.format(input_dict['metadata']['image_idx']),bev_map)
        cv2.imshow('anchors', bev_map)
        cv2.waitKey(0)
        """

        """
        # bev boxes_lidar with voxels (put z in to the plane)
        boxes_lidar = gt_dict["gt_boxes"]
        pp_map = np.zeros(grid_size[:2], dtype=np.float32) # (1408, 1600)
        #print(voxels.shape)  #(16162, 5, 4) $ 4=bzyx
        voxels_max = np.max(voxels[:, :, 1], axis=1, keepdims=False)
        voxels_min = np.min(voxels[:, :, 1], axis=1, keepdims=False)
        voxels_height = voxels_max - voxels_min
        voxels_height = np.minimum(voxels_height, 4) #keep every voxels length less than 4

        # sns.distplot(voxels_height)
        # plt.show()
        pp_map[coordinates[:, 2], coordinates[:, 1]] = voxels_height / 4 #coordinates bzyx
        pp_map = (pp_map * 255).astype(np.uint8)
        pp_map = cv2.cvtColor(pp_map, cv2.COLOR_GRAY2RGB)
        pp_map = simplevis.draw_box_in_bev(pp_map, [0, -30, -3, 64, 30, 1], boxes_lidar, [128, 0, 128], 2) # for kitti 0, -30, -3, 64, 30, 1
        cv2.imwrite('bev_pp_map/pp_map{}.png'.format(input_dict['metadata']['image_idx']),pp_map)
        # cv2.imshow('heights', pp_map)
        # cv2.waitKey(0)
        """

        # example.update({
        #     'labels': targets_dict['labels'], # if anchors free the 0 is the horizontal/vertical anchors
        #     'reg_targets': targets_dict['bbox_targets'], # target assign get offsite
        #     'importance': targets_dict['importance'],
        #     # 'reg_weights': targets_dict['bbox_outside_weights'],
        # })

    return example
