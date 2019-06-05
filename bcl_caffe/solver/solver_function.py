import os
import tempfile
import numpy as np
from pathlib import Path
import time
import torch
import caffe
from caffe.proto import caffe_pb2
from tqdm import tqdm
from visualdl import LogWriter

from google.protobuf import text_format
import second.data.kitti_common as kitti
from second.builder import target_assigner_builder, voxel_builder
from second.pytorch.core import box_torch_ops
from second.data.preprocess import merge_second_batch, merge_second_batch_multigpu
from second.protos import pipeline_pb2
from second.pytorch.builder import box_coder_builder, input_reader_builder
from second.pytorch.models.voxel_encoder import get_paddings_indicator_np #for pillar
from second.utils.log_tool import SimpleModelLog
from tools import some_useful_tools as sut
from second.core import box_np_ops

import cv2
from second.utils import simplevis
import pickle

def get_prototxt(solver_proto, save_path=None):
    if save_path:
        f = open(save_path, mode='w+')
    else:
        f = tempfile.NamedTemporaryFile(mode='w+', delete=False)
    f.write(str(solver_proto))
    f.close()

    return f.name

class TrainSolverWrapper:
    def __init__(self,  train_net,
                        save_path,
                        prefix,
                        pretrained,
                        solver_type='ADAM',
                        weight_decay=0.001,
                        base_lr=0.002,
                        gamma=0.8, #0.1 for lr_policy
                        stepsize=100,
                        test_iter=3769,
                        test_interval=50, #set test_interval to 999999999 if not it will auto run validation
                        max_iter=1e5,
                        iter_size=1,
                        snapshot=1000,
                        display=1,
                        random_seed=0,
                        debug_info=False,
                        create_prototxt=True,
                        log_path=None,
                        keep_topk=8):
        """Initialize the SolverWrapper."""
        self.solver_param = caffe_pb2.SolverParameter()
        self.solver_param.train_net = train_net

        self.solver_param.test_initialization = False

        self.solver_param.base_lr = base_lr
        self.solver_param.lr_policy = 'step'  # "fixed" #exp
        self.solver_param.gamma = gamma
        self.solver_param.stepsize = stepsize

        self.solver_param.display = display
        self.solver_param.max_iter = max_iter
        self.solver_param.iter_size = iter_size
        self.solver_param.snapshot = snapshot
        self.solver_param.snapshot_prefix = os.path.join(save_path, prefix)
        self.solver_param.random_seed = random_seed

        self.solver_param.solver_mode = caffe_pb2.SolverParameter.GPU
        if solver_type is 'SGD':
            self.solver_param.solver_type = caffe_pb2.SolverParameter.SGD
        elif solver_type is 'ADAM':
            self.solver_param.solver_type = caffe_pb2.SolverParameter.ADAM
        self.solver_param.momentum = 0.9
        self.solver_param.momentum2 = 0.999

        self.solver_param.weight_decay = weight_decay
        self.solver_param.debug_info = debug_info

        if create_prototxt:
            solver_prototxt = get_prototxt(self.solver_param, os.path.join(save_path, 'solver.prototxt'))
            print(solver_prototxt)

        self.solver = caffe.get_solver(solver_prototxt)

        self.pretrained = pretrained
        if self.pretrained is not None:
            self.pretrain_path = self.pretrained['pretrain_path']
            self.pretrain_weight_name =self.pretrained['pretrain_weight_name']

        self.keep_topk = keep_topk
        self.test_interval = 1856*2 #1856  #replace self.solver_param.test_interval #9280
        self.save_path = save_path

        self.log_path = log_path
        if self.log_path is not None:
            self.logw = LogWriter(self.log_path, sync_cycle=100)
            with self.logw.mode('train') as logger:
                self.sc_train_reg_loss = logger.scalar("reg_loss")
                self.sc_train_cls_loss = logger.scalar("cls_loss")

    def load_solver(self):
        return self.solver

    def train_model(self):
        if self.pretrained is not None:
            print("\n[info] Load Pretrained Model\n")
            self.load_pretrained_caffe_weight(self.pretrain_path, self.pretrain_weight_name)

        cur_iter = 0
        while cur_iter < self.solver_param.max_iter:
            for i in range(self.test_interval):
                self.solver.step(1)
                if self.log_path is not None:
                    step = self.solver.iter
                    reg_loss = self.solver.net.blobs['reg_loss'].data
                    cls_loss = self.solver.net.blobs['cls_loss'].data

                    self.sc_train_reg_loss.add_record(step, reg_loss)
                    self.sc_train_cls_loss.add_record(step, cls_loss) # for logger

            #always keep top 8 caffemodel
            sut.clear_caffemodel(self.save_path, self.keep_topk)
            cur_iter += self.test_interval

    def load_pretrained_caffe_weight(self, path, weight_name):
        print("### Start loading pretrained caffe weights")
        old_proto_path = os.path.join(path, "train.prototxt")
        old_weight_path = os.path.join(path, weight_name)
        print("### Load old caffe model")
        old_net = caffe.Net(old_proto_path, old_weight_path, caffe.TRAIN)
        print("### Start loading model layers")
        for layer in old_net.params.keys():
            param_length = len(old_net.params[layer])
            print("# Loading layer: " + layer)
            for index in range(param_length):
                try:
                    self.solver.net.params[layer][index].data[...] = old_net.params[layer][index].data[...]
                except Exception as e:
                    print(e)
                    print("!! Cannot load layer: " + layer)
                    continue
        print("### Finish loading pretrained model")

def build_network(model_cfg, measure_time=False):
    voxel_generator = voxel_builder.build(model_cfg.voxel_generator)
    bv_range = voxel_generator.point_cloud_range[[0, 1, 3, 4]]
    box_coder = box_coder_builder.build(model_cfg.box_coder)
    target_assigner_cfg = model_cfg.target_assigner
    target_assigner = target_assigner_builder.build(target_assigner_cfg,
                                                    bv_range, box_coder)
    # box_coder.custom_ndim = target_assigner._anchor_generators[0].custom_ndim

    return voxel_generator, target_assigner

def _worker_init_fn(worker_id):
    time_seed = np.array(time.time(), dtype=np.int32)
    np.random.seed(time_seed + worker_id)
    print(f"WORKER {worker_id} seed:", np.random.get_state()[1][0])

def load_config(model_dir, config_path):
    model_dir = str(Path(model_dir).resolve())
    model_dir = Path(model_dir)
    config_file_bkp = "pipeline.config"
    if isinstance(config_path, str):
        # directly provide a config object. this usually used
        # when you want to train with several different parameters in
        # one script.
        config = pipeline_pb2.TrainEvalPipelineConfig()
        with open(config_path, "r") as f:
            proto_str = f.read()
            text_format.Merge(proto_str, config)
    else:
        config = config_path
        proto_str = text_format.MessageToString(config, indent=2)
    with (model_dir / config_file_bkp).open("w") as f:
        f.write(proto_str)

    input_cfg = config.train_input_reader
    eval_input_cfg = config.eval_input_reader
    model_cfg = config.model.second
    train_cfg = config.train_config


    return (input_cfg, eval_input_cfg, model_cfg, train_cfg)

def log_function(model_dir, config_path):
    model_logging = SimpleModelLog(model_dir)
    model_logging.open()

    config = pipeline_pb2.TrainEvalPipelineConfig()
    with open(config_path, "r") as f:
        proto_str = f.read()
        text_format.Merge(proto_str, config)

    model_logging.log_text(proto_str + "\n", 0, tag="config")
    return model_logging

def load_dataloader(eval_input_cfg, model_cfg, voxel_generator, target_assigner,generate_anchors_cachae):
    eval_dataset = input_reader_builder.build(
        eval_input_cfg,
        model_cfg,
        training=False,
        voxel_generator=voxel_generator,
        target_assigner=target_assigner,
        generate_anchors_cachae=generate_anchors_cachae,
        segmentation_eval=False) #True FOR Pillar, False For BCL

    eval_dataloader = torch.utils.data.DataLoader(
        eval_dataset,
        batch_size=eval_input_cfg.batch_size, # only support multi-gpu train
        shuffle=False,
        num_workers=eval_input_cfg.preprocess.num_workers,
        pin_memory=False,
        collate_fn=merge_second_batch)

    return eval_dataloader, eval_dataset

def segmentation_dataloader(eval_input_cfg, model_cfg, voxel_generator, target_assigner,generate_anchors_cachae, segmentation_eval):
    seg_eval_dataset = input_reader_builder.build(
        eval_input_cfg,
        model_cfg,
        training=False,
        voxel_generator=voxel_generator,
        target_assigner=target_assigner,
        generate_anchors_cachae=generate_anchors_cachae,
        segmentation_eval=segmentation_eval)

    seg_eval_dataloader = torch.utils.data.DataLoader(
        seg_eval_dataset,
        batch_size=eval_input_cfg.batch_size, # only support multi-gpu train
        shuffle=False,
        num_workers=eval_input_cfg.preprocess.num_workers,
        pin_memory=False,
        collate_fn=merge_second_batch)

    return seg_eval_dataloader

def example_convert_to_torch(example, dtype=torch.float32,
                             device=None) -> dict:
    device = device or torch.device("cuda:0")
    example_torch = {}
    float_names = [
        "voxels", "anchors", "reg_targets", "reg_weights", "bev_map", "importance"
    ]
    for k, v in example.items():
        if k in float_names:
            # slow when directly provide fp32 data with dtype=torch.half
            example_torch[k] = torch.tensor(
                v, dtype=torch.float32, device=device).to(dtype)
        elif k in ["coordinates", "labels", "num_points"]:
            example_torch[k] = torch.tensor(
                v, dtype=torch.int32, device=device)
        elif k in ["anchors_mask"]:
            example_torch[k] = torch.tensor(
                v, dtype=torch.uint8, device=device)
        elif k == "calib":
            calib = {}
            for k1, v1 in v.items():
                calib[k1] = torch.tensor(
                    v1, dtype=dtype, device=device).to(dtype)
            example_torch[k] = calib
        elif k == "num_voxels":
            example_torch[k] = torch.tensor(v)
        elif k in ["box_preds", "cls_preds"]:
            example_torch[k] = torch.tensor(
                v, dtype=torch.float32, device=device).to(dtype)
        else:
            example_torch[k] = v
    return example_torch

class SolverWrapperTest:
    def __init__(self,
                        test_net,
                        eval_weights,
                        prefix,
                        pretrained,
                        model_dir=None,
                        config_path=None,):
        """Initialize the SolverWrapper."""
        self.net = caffe.Net(test_net, eval_weights, caffe.TEST)
        self.pretrained = pretrained
        self.model_dir = model_dir
        self.config_path = config_path

    def load_solver(self):
        return self.solver
    def train_model(self):
        caffe.set_mode_gpu()
        caffe.set_device(0)
        self.eval_on_val(self.net)

    ############################################################################
    # For Object detection evaluation
    ############################################################################
    def eval_on_val(self, net):
        _, eval_input_cfg, model_cfg, train_cfg = load_config(self.model_dir, self.config_path)
        voxel_generator, self.target_assigner = build_network(model_cfg)
        dataloader, eval_dataset = load_dataloader(eval_input_cfg, model_cfg, voxel_generator, self.target_assigner, generate_anchors_cachae=False) #True FOR Pillar, False For BCL
        model_logging = log_function(self.model_dir, self.config_path)
        data_iter=iter(dataloader)

        self._box_coder=self.target_assigner.box_coder
        self._num_class=1
        self._encode_background_as_zeros =True
        self._nms_class_agnostic=False
        self._class_name="Car"
        self._use_multi_class_nms=False
        self._nms_pre_max_sizes=[1000]
        self._multiclass_nms=False
        self._use_sigmoid_score=True
        self._num_anchor_per_loc=2
        self._box_code_size = 7

        self._use_rotate_nms=False  #False for pillar, True for second
        self._nms_post_max_sizes=[100] #300 for pillar, 100 for second
        self._nms_score_thresholds=[0.8] # 0.4 in submit, but 0.3 can get better hard performance #pillar use 0.05, second 0.3
        self._nms_iou_thresholds=[0.5] ## NOTE: double check #pillar use 0.5, second use 0.01
        self._post_center_range=list(model_cfg.post_center_limit_range) ## NOTE: double check
        self._use_direction_classifier=False ## NOTE: double check
        cls_thresh = 0.3
        detections = []
        t = time.time()

        for i in tqdm(range(len(data_iter))):
            example = next(data_iter)
            seg_points = example['seg_points']
            net.blobs['top_prev'].reshape(*seg_points.shape)
            net.blobs['top_prev'].data[...] = seg_points
            net.forward()
            #seg_cls_pred output shape (1,1,1,16000)
            seg_cls_pred = net.blobs["output"].data[...].squeeze()
            cls_preds = net.blobs['f_cls_preds'].data[...].reshape(1,16000,-1)
            box_preds = net.blobs['f_box_preds'].data[...].reshape(1,16000,-1)
            # Select car prediction and classification

            car_points = seg_points[:, (seg_cls_pred > cls_thresh)].squeeze()
            cls_preds = cls_preds[:,(seg_cls_pred > cls_thresh)]
            box_preds = box_preds[:,(seg_cls_pred > cls_thresh)]
            car_points = car_points[:,:3][:,::-1]
            ret = self.target_assigner.generate_anchors_from_gt(car_points)
            anchors = ret["anchors"]
            # anchor generated from generator shape (n_anchors, 7)
            # needed to expand dim for prediction
            example["anchors"] = np.expand_dims(anchors, 0)
            preds_dict = {"box_preds":box_preds.reshape(1,-1,7), "cls_preds":cls_preds.reshape(1,-1,1)}

            example = example_convert_to_torch(example, torch.float32)
            preds_dict = example_convert_to_torch(preds_dict, torch.float32)
            detections += self.predict(example, preds_dict)
            ################ visualization #####################
            # image_idx = example['metadata'][0]["image_idx"]
            # boxes_lidar = detections[-1]["box3d_lidar"].cpu().detach().numpy()
            # bev_map = simplevis.kitti_vis(seg_points.squeeze(), boxes_lidar)
            # cv2.imwrite('./visualization/detect_eval_bv/detect_eval_bv{}.png'.format(image_idx), bev_map)
            # print(detections[-1])
        sec_per_ex = len(data_iter) / (time.time() - t)
        global_step = 1 ## TODO:
        model_dir = str(Path(self.model_dir).resolve())
        model_dir = Path(model_dir)
        result_path = model_dir / 'results'
        result_path_step = result_path / f"step_{1}"
        result_path_step.mkdir(parents=True, exist_ok=True)
        model_logging.log_text(
            f'generate label finished({sec_per_ex:.2f}/s). start eval:',
            global_step)
        result_dict = eval_dataset.dataset.evaluation(
            detections, str(result_path_step))
        for k, v in result_dict["results"].items():
            model_logging.log_text("Evaluation {}".format(k), global_step)
            model_logging.log_text(v, global_step)
        model_logging.log_metrics(result_dict["detail"], global_step)
    def predict(self, example, preds_dict):
        """start with v1.6.0, this function don't contain any kitti-specific code.
        Returns:
            predict: list of pred_dict.
            pred_dict: {
                box3d_lidar: [N, 7] 3d box.
                scores: [N]
                label_preds: [N]
                metadata: meta-data which contains dataset-specific information.
                    for kitti, it contains image idx (label idx),
                    for nuscenes, sample_token is saved in it.
            }
        """
        batch_size = example['anchors'].shape[0]
        if "metadata" not in example or len(example["metadata"]) == 0:
            meta_list = [None] * batch_size
        else:
            meta_list = example["metadata"]

        batch_anchors = example["anchors"].view(batch_size, -1, example["anchors"].shape[-1])
        if "anchors_mask" not in example:
            batch_anchors_mask = [None] * batch_size
        else:
            batch_anchors_mask = example["anchors_mask"].view(batch_size, -1)

        t = time.time()
        batch_box_preds = preds_dict["box_preds"]
        batch_cls_preds = preds_dict["cls_preds"]
        batch_box_preds = batch_box_preds.view(batch_size, -1,
                                               self._box_coder.code_size)

        num_class_with_bg = self._num_class
        if not self._encode_background_as_zeros:
            num_class_with_bg = self._num_class + 1

        batch_cls_preds = batch_cls_preds.view(batch_size, -1,
                                               num_class_with_bg)
        batch_box_preds = self._box_coder.decode_torch(batch_box_preds,
                                                       batch_anchors)
        if self._use_direction_classifier:
            batch_dir_preds = preds_dict["dir_cls_preds"]
            batch_dir_preds = batch_dir_preds.view(batch_size, -1,
                                                   self._num_direction_bins)
        else:
            batch_dir_preds = [None] * batch_size

        predictions_dicts = []
        post_center_range = None
        if len(self._post_center_range) > 0:
            post_center_range = torch.tensor(
                self._post_center_range,
                dtype=batch_box_preds.dtype,
                device=batch_box_preds.device).float()
        for box_preds, cls_preds, dir_preds, a_mask, meta in zip(
                batch_box_preds, batch_cls_preds, batch_dir_preds,
                batch_anchors_mask, meta_list):
            if a_mask is not None:
                box_preds = box_preds[a_mask]
                cls_preds = cls_preds[a_mask]
            box_preds = box_preds.float()
            cls_preds = cls_preds.float()
            if self._use_direction_classifier:
                if a_mask is not None:
                    dir_preds = dir_preds[a_mask]
                dir_labels = torch.max(dir_preds, dim=-1)[1]
            if self._encode_background_as_zeros:
                # this don't support softmax
                assert self._use_sigmoid_score is True
                total_scores = torch.sigmoid(cls_preds)
            else:
                # encode background as first element in one-hot vector
                if self._use_sigmoid_score:
                    total_scores = torch.sigmoid(cls_preds)[..., 1:]
                else:
                    total_scores = F.softmax(cls_preds, dim=-1)[..., 1:]
            # Apply NMS in birdeye view
            # Jim added
            # print("each score: ", total_scores)
            # print("total_scores > 0.05:" , np.unique(total_scores.cpu().detach().numpy() > 0.05,
            #                                             return_counts = True))
            # print("total_scores > 0.3:" , np.unique(total_scores.cpu().detach().numpy() > 0.3,
            #                                             return_counts = True))
            if self._use_rotate_nms:
                nms_func = box_torch_ops.rotate_nms
            else:
                nms_func = box_torch_ops.nms
            feature_map_size_prod = batch_box_preds.shape[
                1] // self.target_assigner.num_anchors_per_location
            if self._multiclass_nms:
                assert self._encode_background_as_zeros is True
                boxes_for_nms = box_preds[:, [0, 1, 3, 4, 6]]
                if not self._use_rotate_nms:
                    box_preds_corners = box_torch_ops.center_to_corner_box2d(
                        boxes_for_nms[:, :2], boxes_for_nms[:, 2:4],
                        boxes_for_nms[:, 4])
                    boxes_for_nms = box_torch_ops.corner_to_standup_nd(
                        box_preds_corners)

                selected_boxes, selected_labels, selected_scores = [], [], []
                selected_dir_labels = []

                scores = total_scores
                boxes = boxes_for_nms
                selected_per_class = []
                score_threshs = self._nms_score_thresholds
                pre_max_sizes = self._nms_pre_max_sizes
                post_max_sizes = self._nms_post_max_sizes
                iou_thresholds = self._nms_iou_thresholds
                for class_idx, score_thresh, pre_ms, post_ms, iou_th in zip(
                        range(self._num_class),
                        score_threshs,
                        pre_max_sizes, post_max_sizes, iou_thresholds):
                    if self._nms_class_agnostic:
                        class_scores = total_scores.view(
                            feature_map_size_prod, -1,
                            self._num_class)[..., class_idx]
                        class_scores = class_scores.contiguous().view(-1)
                        class_boxes_nms = boxes.view(-1,
                                                     boxes_for_nms.shape[-1])
                        class_boxes = box_preds
                        class_dir_labels = dir_labels
                    else:
                        anchors_range = self.target_assigner.anchors_range(class_idx)
                        class_scores = total_scores.view(
                            -1,
                            self._num_class)[anchors_range[0]:anchors_range[1], class_idx]
                        class_boxes_nms = boxes.view(-1,
                            boxes_for_nms.shape[-1])[anchors_range[0]:anchors_range[1], :]
                        class_scores = class_scores.contiguous().view(-1)
                        class_boxes_nms = class_boxes_nms.contiguous().view(
                            -1, boxes_for_nms.shape[-1])
                        class_boxes = box_preds.view(-1,
                            box_preds.shape[-1])[anchors_range[0]:anchors_range[1], :]
                        class_boxes = class_boxes.contiguous().view(
                            -1, box_preds.shape[-1])
                        if self._use_direction_classifier:
                            class_dir_labels = dir_labels.view(-1)[anchors_range[0]:anchors_range[1]]
                            class_dir_labels = class_dir_labels.contiguous(
                            ).view(-1)
                    if score_thresh > 0.0:
                        class_scores_keep = class_scores >= score_thresh
                        if class_scores_keep.shape[0] == 0:
                            selected_per_class.append(None)
                            continue
                        class_scores = class_scores[class_scores_keep]
                    if class_scores.shape[0] != 0:
                        if score_thresh > 0.0:
                            class_boxes_nms = class_boxes_nms[
                                class_scores_keep]
                            class_boxes = class_boxes[class_scores_keep]
                            class_dir_labels = class_dir_labels[
                                class_scores_keep]
                        keep = nms_func(class_boxes_nms, class_scores, pre_ms,
                                        post_ms, iou_th)
                        if keep.shape[0] != 0:
                            selected_per_class.append(keep)
                        else:
                            selected_per_class.append(None)
                    else:
                        selected_per_class.append(None)
                    selected = selected_per_class[-1]

                    if selected is not None:
                        selected_boxes.append(class_boxes[selected])
                        selected_labels.append(
                            torch.full([class_boxes[selected].shape[0]],
                                       class_idx,
                                       dtype=torch.int64,
                                       device=box_preds.device))
                        if self._use_direction_classifier:
                            selected_dir_labels.append(
                                class_dir_labels[selected])
                        selected_scores.append(class_scores[selected])
                selected_boxes = torch.cat(selected_boxes, dim=0)
                selected_labels = torch.cat(selected_labels, dim=0)
                selected_scores = torch.cat(selected_scores, dim=0)
                if self._use_direction_classifier:
                    selected_dir_labels = torch.cat(selected_dir_labels, dim=0)
            else:
                # get highest score per prediction, than apply nms
                # to remove overlapped box.
                if num_class_with_bg == 1:
                    top_scores = total_scores.squeeze(-1)
                    top_labels = torch.zeros(
                        total_scores.shape[0],
                        device=total_scores.device,
                        dtype=torch.long)
                else:
                    top_scores, top_labels = torch.max(
                        total_scores, dim=-1)
                if self._nms_score_thresholds[0] > 0.0:
                    top_scores_keep = top_scores >= self._nms_score_thresholds[0]
                    top_scores = top_scores.masked_select(top_scores_keep)
                    print("nms_score_thresholds is {} and found {} cars ".format(self._nms_score_thresholds, len(top_scores)))
                if top_scores.shape[0] != 0:
                    if self._nms_score_thresholds[0] > 0.0:
                        box_preds = box_preds[top_scores_keep]
                        if self._use_direction_classifier:
                            dir_labels = dir_labels[top_scores_keep]
                        top_labels = top_labels[top_scores_keep]
                    boxes_for_nms = box_preds[:, [0, 1, 3, 4, 6]]
                    if not self._use_rotate_nms:
                        box_preds_corners = box_torch_ops.center_to_corner_box2d(
                            boxes_for_nms[:, :2], boxes_for_nms[:, 2:4],
                            boxes_for_nms[:, 4])
                        boxes_for_nms = box_torch_ops.corner_to_standup_nd(
                            box_preds_corners)
                    # the nms in 3d detection just remove overlap boxes.
                    selected = nms_func(
                        boxes_for_nms,
                        top_scores,
                        pre_max_size=self._nms_pre_max_sizes[0],
                        post_max_size=self._nms_post_max_sizes[0],
                        iou_threshold=self._nms_iou_thresholds[0],
                    )
                    print("IOU_thresholds is {} and remove overlap found {} cars ".format(self._nms_iou_thresholds, len(top_scores)))
                else:
                    selected = []
                # if selected is not None:
                selected_boxes = box_preds[selected]
                if self._use_direction_classifier:
                    selected_dir_labels = dir_labels[selected]
                selected_labels = top_labels[selected]
                selected_scores = top_scores[selected]
            # finally generate predictions.
            if selected_boxes.shape[0] != 0:
                box_preds = selected_boxes
                scores = selected_scores
                label_preds = selected_labels
                if self._use_direction_classifier:
                    dir_labels = selected_dir_labels
                    period = (2 * np.pi / self._num_direction_bins)
                    dir_rot = box_torch_ops.limit_period(
                        box_preds[..., 6] - self._dir_offset,
                        self._dir_limit_offset, period)
                    box_preds[
                        ...,
                        6] = dir_rot + self._dir_offset + period * dir_labels.to(
                            box_preds.dtype)
                final_box_preds = box_preds
                final_scores = scores
                final_labels = label_preds
                if post_center_range is not None:
                    mask = (final_box_preds[:, :3] >=
                            post_center_range[:3]).all(1)
                    mask &= (final_box_preds[:, :3] <=
                             post_center_range[3:]).all(1)
                    predictions_dict = {
                        "box3d_lidar": final_box_preds[mask],
                        "scores": final_scores[mask],
                        "label_preds": label_preds[mask],
                        "metadata": meta,
                    }
                else:
                    predictions_dict = {
                        "box3d_lidar": final_box_preds,
                        "scores": final_scores,
                        "label_preds": label_preds,
                        "metadata": meta,
                    }
            else:
                dtype = batch_box_preds.dtype
                device = batch_box_preds.device
                predictions_dict = {
                    "box3d_lidar":
                    torch.zeros([0, box_preds.shape[-1]],
                                dtype=dtype,
                                device=device),
                    "scores":
                    torch.zeros([0], dtype=dtype, device=device),
                    "label_preds":
                    torch.zeros([0], dtype=top_labels.dtype, device=device),
                    "metadata":
                    meta,
                }

            predictions_dicts.append(predictions_dict)

        return predictions_dicts

class SegEval:
    def __init__(self,
                        test_net,
                        eval_weights,
                        prefix,
                        pretrained,
                        model_dir=None,
                        config_path=None,):
        """Initialize the SolverWrapper."""
        self.net = caffe.Net(test_net, eval_weights, caffe.TEST)
        self.pretrained = pretrained
        self.model_dir = model_dir
        self.config_path = config_path
        self.seg_pred_vis_path = "./visualization/3d_visual_eval/seg_points.pkl"

    def load_solver(self):
        return self.solver
    def train_model(self):
        caffe.set_mode_gpu()
        caffe.set_device(0)
        self.segmentation_eval_on_val(self.net)

    ############################################################################
    # For segmentation evaluation
    ############################################################################
    def segmentation_eval_on_val(self, net):
        _, eval_input_cfg, model_cfg, train_cfg = load_config(self.model_dir, self.config_path)
        voxel_generator, self.target_assigner = build_network(model_cfg)
        dataloader = segmentation_dataloader(eval_input_cfg, model_cfg,
                                                        voxel_generator,
                                                        self.target_assigner,
                                                        generate_anchors_cachae=False,
                                                        segmentation_eval=True)
        data_iter=iter(dataloader)


        try:
            os.remove(self.seg_pred_vis_path) #remove vis
            print("\n[INFO]Remove existing pickle file\n")
        except Exception as e:
            print("\n[INFO] No segmentation visualization pickle file\n")

        detections = []
        for i in tqdm(range(len(data_iter))):
            example = next(data_iter)
            seg_points = example['seg_points']
            net.blobs['top_prev'].reshape(*seg_points.shape)
            net.blobs['top_prev'].data[...] = seg_points
            net.forward()
            #seg_cls_pred output shape (1,1,1,16000)
            seg_cls_pred = net.blobs["output"].data[...].squeeze()
            detections += self.seg_predict(seg_cls_pred, example, vis=True) #if pass example then save seg vis
        self.total_segmentation_result(detections)

    def seg_predict(self, pred, example, vis=False):
        gt = example['seg_labels']
        ############### Params ###############
        eps = 1e-5
        cls_thresh = 0.5
        pos_class = 1 # Car
        scores = dict()
        list_score = []
        ############### Params ###############

        pred, gt = np.array(pred), np.array(gt)
        gt = np.squeeze(gt)
        pred = np.where(pred>cls_thresh, 1, 0)
        # print("pred class distrubution : ", np.unique(pred , return_counts=True))#

        labels = np.unique(gt)

        TPs = np.sum((gt == pos_class) * (pred == pos_class))
        TNs = np.sum((gt != pos_class) * (pred != pos_class))
        FPs = np.sum((gt != pos_class) * (pred == pos_class))
        FNs = np.sum((gt == pos_class) * (pred != pos_class))
        TargetTotal= np.sum(gt == pos_class)

        scores['accuracy'] = TPs / (TargetTotal + eps)
        scores['class_iou'] = TPs / ((TPs + FNs + FPs) + eps)
        # scores['TPR'] = TPs / (TPs + FPs)
        # scores['FPR'] = FPs / (FPs + TNs)

        if vis:
            seg_points = example['seg_points']
            image_idx = example['image_idx']
            gt_boxes = example['gt_boxes']
            pred_idx = np.where(pred>cls_thresh)
            seg_points = np.squeeze(seg_points)
            target = seg_points[pred_idx]
            # bev_map = simplevis.kitti_vis(target, gt_boxes)
            # cv2.imwrite('./visualization/seg_eval_bv/seg_bv{}.png'.format(image_idx[0]), bev_map)
            with open(self.seg_pred_vis_path, 'ab') as f:
                pickle.dump(target,f)

            ######################### For Ground Truth##########################
            # gt_bev_map = simplevis.kitti_vis(seg_points, gt_boxes)
            # cv2.imwrite('./visualization/seg_eval_bv/gt_seg_bv{}.png'.format(image_idx[0]), gt_bev_map)
            ######################### For Ground Truth##########################

        list_score.append(scores)

        return list_score
    def total_segmentation_result(self, detections):
        avg_accuracy=[]
        avg_class_iou =[]
        for det in detections:
            avg_accuracy.append(det['accuracy'])
            avg_class_iou.append(det['class_iou'])

        avg_accuracy = np.sum(np.array(avg_accuracy)) / np.sum((np.array(avg_accuracy)!=0)) #divided by none zero no Cars
        avg_class_iou = np.sum(np.array(avg_class_iou)) / np.sum((np.array(avg_class_iou)!=0))  #divided by none zero no Cars

        print('-------------------- Summary --------------------')
        print('   Accuracy: {:.3f}'.format(avg_accuracy))
        print('   Car IoU: {:.3f}'.format(avg_class_iou))

        ########################save Log #######################################
        log_path = self.model_dir+'/log.txt'
        logf = open(log_path, 'a')
        logf.write("\n")
        print('-------------------- Summary --------------------',file=logf)
        print('   Accuracy: {:.3f}'.format(avg_accuracy), file=logf)
        print('   Car IoU: {:.3f}'.format(avg_class_iou), file=logf)
