import caffe
from caffe import layers as L, params as P, to_proto

def conv_bn_relu(n, name, top_prev, ks, nout, stride=1, pad=0, loop=1):

    for idx in range(loop):
        n[str(name)+"_"+str(idx)] = L.Convolution(top_prev, #name = name,
                                            convolution_param=dict(
                                                    kernel_size=ks, stride=stride,
                                                    num_output=nout, pad=pad,
                                                    engine=2,
                                                    weight_filler=dict(type = 'xavier'),
                                                    bias_term = False),
                                                    param=[dict(lr_mult=1)],
                                                    )
        top_prev = n[str(name)+"_"+str(idx)]
        n[str(name)+'_bn_'+str(idx)] = L.BatchNorm(top_prev, batch_norm_param=dict(eps=1e-3, moving_average_fraction=0.99))
        top_prev = n[str(name)+'_bn_'+str(idx)]
        n[str(name)+'_sc_'+str(idx)] = L.Scale(top_prev, scale_param=dict(bias_term=True))
        top_prev = n[str(name)+'_sc_'+str(idx)]
        n[str(name)+'_relu_'+str(idx)] = L.ReLU(top_prev, in_place=True)
        top_prev = n[str(name)+'_relu_'+str(idx)]

    return top_prev

def deconv_bn_relu(n, name, top_prev, ks, nout, stride=1, pad=0):
    n[str(name)] = L.Deconvolution(top_prev, # name = name,
                                            convolution_param=dict(kernel_size=ks, stride=stride,
                                                num_output=nout, pad=pad,
                                                engine=2,
                                                weight_filler=dict(type = 'xavier'),
                                                bias_term = False),
                                                param=[dict(lr_mult=1)],
                                                )
    top_prev = n[str(name)]
    n[str(name)+'_bn'] = L.BatchNorm(top_prev, batch_norm_param=dict(eps=1e-3, moving_average_fraction=0.99))
    top_prev = n[str(name)+'_bn']
    n[str(name)+'_sc'] = L.Scale(top_prev, scale_param=dict(bias_term=True))
    top_prev = n[str(name)+'_sc']
    n[str(name)+'_relu'] = L.ReLU(top_prev, in_place=True)
    top_prev = n[str(name)+'_relu']

    return top_prev

def bcl_bn_relu(n, name, top_prev, top_lat_feats, nout, lattic_scale=None, loop=1, skip=None):

    if skip=='concat':
        skip_params= []

    for idx in range(loop):

        if lattic_scale:

            n[str(name)+"_scale_"+str(idx)] = L.Python(top_lat_feats, python_param=dict(module='bcl_layers',
                                                                    layer='PickAndScale',
                                                                    param_str=lattic_scale[idx]))
            _top_lat_feats = n[str(name)+"_scale_"+str(idx)]


        # bltr_weight_filler = dict(type='gaussian', std=float(0.001))
        bltr_weight_filler = dict(type = 'xavier')
        n[str(name)+"_"+str(idx)] = L.Permutohedral(top_prev, _top_lat_feats, _top_lat_feats,
                                                        ntop=1,
                                                        permutohedral_param=dict(
                                                            num_output=nout[idx],
                                                            group=1,
                                                            neighborhood_size=1,
                                                            bias_term=True,
                                                            norm_type=P.Permutohedral.AFTER,
                                                            offset_type=P.Permutohedral.NONE,
                                                            filter_filler=bltr_weight_filler,
                                                            bias_filler=dict(type='constant',
                                                                             value=0)),
                                                        param=[{'lr_mult': 1, 'decay_mult': 1},
                                                               {'lr_mult': 2, 'decay_mult': 0}])

        top_prev = n[str(name)+"_"+str(idx)]
        n[str(name)+'_bn_'+str(idx)] = L.BatchNorm(top_prev, batch_norm_param=dict(eps=1e-3, moving_average_fraction=0.99))
        top_prev = n[str(name)+'_bn_'+str(idx)]
        n[str(name)+'_sc_'+str(idx)] = L.Scale(top_prev, scale_param=dict(bias_term=True))
        top_prev = n[str(name)+'_sc_'+str(idx)]
        n[str(name)+'_relu_'+str(idx)] = L.ReLU(top_prev, in_place=True)
        top_prev = n[str(name)+'_relu_'+str(idx)]

        if skip=='concat':
            skip_params.append(n[str(name)+'_relu_'+str(idx)])

    if skip=='concat':
        n['concat'] = L.Concat(*skip_params)
        top_prev = n['concat']

    return top_prev

def bilateral_baseline(phase,
            dataset_params=None,
            cfg = None,
            deploy=False,
            create_prototxt=True,
            save_path=None,
            ):


    #RPN pillar config
    num_filters = [64,128,256]
    layer_strides = [2,2,2]
    layer_nums = [3,5,5]
    upsample_strides = [1, 2, 4]
    num_upsample_filters = [128, 128, 128]
    anchors_fp_w = 704 #1408
    anchors_fp_h = 800 #1600

    """
    #RPN second config
    num_filters = [64]
    layer_strides = [2]
    layer_nums = [1]
    upsample_strides = [1]
    num_upsample_filters = [64]
    anchors_fp_w = 704  #1408 176
    anchors_fp_h = 800  #1600 200
    """

    box_code_size = 7
    num_anchor_per_loc = 2
    num_cls = 1

    n = caffe.NetSpec()

    if phase == "train":

        dataset_params_train = dataset_params.copy()
        dataset_params_train['subset'] = phase
        dataset_params_train['anchors_cachae'] = True #True FOR Pillar, False For BCL

        datalayer_train = L.Python(name='data', include=dict(phase=caffe.TRAIN),
                                   ntop= 4, python_param=dict(module='bcl_layers', layer='InputKittiDataV3',
                                                     param_str=repr(dataset_params_train)))

        n.data, n.coors, n.labels, n.reg_targets = datalayer_train
        top_prev = n.data
        coords = n.coors

    elif phase == "eval":
        n['top_prev'] = L.Python(
                                name = 'top_pre_input',
                                ntop=1,
                                include=dict(phase=caffe.TEST),
                                python_param=dict(
                                module='bcl_layers',
                                layer='DataFeature',
                                ))
        top_prev = n['top_prev']
        n['top_lat_feats'] = L.Python(
                                name = 'top_lat_feats_input',
                                ntop=1,
                                include=dict(phase=caffe.TEST),
                                python_param=dict(
                                module='bcl_layers',
                                layer='LatticeFeature',
                                ))
        coords = n['top_lat_feats']

    # top_prev, lat_feats = L.Python(top_prev, coords, ntop=2, python_param=dict(module='bcl_layers',layer='BCLReshapeV3'))
    top_prev, lat_feats = L.Python(top_prev, ntop=2, python_param=dict(module='bcl_layers',layer='BCLReshape'))

    top_prev = bcl_bn_relu(n, 'bcl', top_prev, lat_feats, nout=[64, 128, 128, 64],
                          lattic_scale=["0_1_2","0/2_1/2_2/2", "0/4_1/4_2/4","0/8_1/8_2/8"], loop=4, skip='concat')
    # top_prev = bcl_bn_relu(n, 'bcl', top_prev, lat_feats, nout=[64, 128, 128, 64],
    #                       lattic_scale=["0*2_1*2","0_1", "0/2_1/2","0/4_1/4"], loop=4, skip='concat')

    top_prev = conv_bn_relu(n, "conv1", top_prev, 1, 64, stride=1, pad=0, loop=1)

    top_prev = L.Python(top_prev, coords, ntop=1,python_param=dict(
                            module='bcl_layers',
                            layer='Scatter',
                            param_str=str(dict(output_shape=[anchors_fp_h, anchors_fp_w, 64]))))

    top_prev = conv_bn_relu(n, "ini_conv1", top_prev, 3, num_filters[0], stride=layer_strides[0], pad=1, loop=1)
    top_prev = conv_bn_relu(n, "rpn_conv1", top_prev, 3, num_filters[0], stride=1, pad=1, loop=layer_nums[0]) #3
    deconv1 = deconv_bn_relu(n, "rpn_deconv1", top_prev, upsample_strides[0], num_upsample_filters[0], stride=upsample_strides[0], pad=0)

    top_prev = conv_bn_relu(n, "ini_conv2", top_prev, 3, num_filters[1], stride=layer_strides[1], pad=1, loop=1)
    top_prev = conv_bn_relu(n, "rpn_conv2", top_prev, 3, num_filters[1], stride=1, pad=1, loop=layer_nums[1]) #5
    deconv2 = deconv_bn_relu(n, "rpn_deconv2", top_prev, upsample_strides[1], num_upsample_filters[1], stride=upsample_strides[1], pad=0)

    top_prev = conv_bn_relu(n, "ini_conv3", top_prev, 3, num_filters[2], stride=layer_strides[2], pad=1, loop=1)
    top_prev = conv_bn_relu(n, "rpn_conv3", top_prev, 3, num_filters[2], stride=1, pad=1, loop=layer_nums[2]) #5
    deconv3 = deconv_bn_relu(n, "rpn_deconv3", top_prev, upsample_strides[2], num_upsample_filters[2], stride=upsample_strides[2], pad=0)
    n['rpn_out'] = L.Concat(deconv1, deconv2, deconv3)
    top_prev = n['rpn_out']

    n.cls_preds = L.Convolution(top_prev, name = "cls_head",
                         convolution_param=dict(num_output=num_anchor_per_loc * num_cls,
                                                kernel_size=1, stride=1, pad=0,
                                                weight_filler=dict(type = 'xavier'),
                                                bias_term = True,
                                                bias_filler=dict(type='constant', value=0),
                                                engine=1,
                                                ),
                         param=[dict(lr_mult=1), dict(lr_mult=1)])

    n.box_preds = L.Convolution(top_prev, name = "reg_head",
                          convolution_param=dict(num_output=num_anchor_per_loc * box_code_size,
                                                 kernel_size=1, stride=1, pad=0,
                                                 weight_filler=dict(type = 'xavier'),
                                                 bias_term = True,
                                                 bias_filler=dict(type='constant', value=0),
                                                 engine=1,
                                                 ),
                          param=[dict(lr_mult=1), dict(lr_mult=1)])

    cls_preds = n.cls_preds
    box_preds = n.box_preds

    cls_preds_permute = L.Permute(cls_preds, permute_param=dict(order=[0, 2, 3, 1])) #(B,C=2,H,W) -> (B,H,W,C=2)
    cls_preds_reshape = L.Reshape(cls_preds_permute, reshape_param=dict(shape=dict(dim=[0, -1, num_cls])))# (B,H,W,C=2)-> (B, -1, 1)

    box_preds_permute = L.Permute(box_preds, permute_param=dict(order=[0, 2, 3, 1]))  #(B,C=2*7,H,W) -> (B,H,W,C=2*7)
    box_preds_reshape = L.Reshape(box_preds_permute, reshape_param=dict(shape=dict(dim=[0, -1, box_code_size])))# (B,H,W,C=2*7)-> (B, -1, 7)

    if phase == "eval":
        n.f_cls_preds = cls_preds_reshape
        n.f_box_preds = box_preds_reshape

        return n.to_proto()

    elif phase == "train":

        n['cared'],n['reg_outside_weights'], n['cls_weights']= L.Python(n.labels,
                                                                        name = "PrepareLossWeight",
                                                                        ntop = 3,
                                                                        python_param=dict(
                                                                                    module='bcl_layers',
                                                                                    layer='PrepareLossWeight'
                                                                                    ))
        reg_outside_weights, cared, cls_weights = n['reg_outside_weights'], n['cared'], n['cls_weights']

        # Gradients cannot be computed with respect to the label inputs (bottom[1])#
        n['labels_input'] = L.Python(n.labels, cared,
                            name = "Label_Encode",
                            python_param=dict(
                                        module='bcl_layers',
                                        layer='LabelEncode',
                                        ))
        labels_input = n['labels_input']




        n.cls_loss= L.Python(cls_preds_reshape, labels_input, cls_weights,
                                name = "FocalLoss",
                                loss_weight = 1,
                                python_param=dict(
                                            module='bcl_layers',
                                            layer='WeightFocalLoss'
                                            ),
                                param_str=str(dict(focusing_parameter=2, alpha=0.25)))


        n.reg_loss= L.Python(box_preds_reshape, n.reg_targets, reg_outside_weights,
                                name = "WeightedSmoothL1Loss",
                                loss_weight = 1,
                                python_param=dict(
                                            module='bcl_layers',
                                            layer='WeightedSmoothL1Loss'
                                            ))

        return n.to_proto()
