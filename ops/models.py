# Code for "TSM: Temporal Shift Module for Efficient Video Understanding"
# arXiv:1811.08383
# Ji Lin*, Chuang Gan, Song Han
# {jilin, songhan}@mit.edu, ganchuang@csail.mit.edu

from torch import nn
import torch.utils.model_zoo as model_zoo

from basic_ops import ConsensusModule
from transforms import *
from torch.nn.init import normal_, constant_
from temporal_shift import make_temporal_shift, TemporalShift
from non_local import make_non_local
from torchsummary import summary

import sys
sys.path.append(r"C:\Users\mobil\Desktop\25spring\stylePalm\temporal-shift-module\archs")
from mobilenet_v2 import mobilenet_v2, InvertedResidual
from bn_inception import bninception
from torchvision.models import efficientnet_b0, EfficientNet_B0_Weights
from torchvision.models.efficientnet import MBConv

class TSN(nn.Module):
    def __init__(self, shifted_blocks, num_class, num_segments, modality,
                 keep_rgb, return_embedding, img_feature_dim,
                 new_length, base_model='resnet101',
                 consensus_type='avg', before_softmax=True,
                 dropout=0.8,
                 crop_num=1, partial_bn=False, print_spec=False, pretrain='imagenet',
                 is_shift=False, shift_div=8, shift_place='blockres', fc_lr5=False,
                 temporal_pool=False, non_local=False):
        super(TSN, self).__init__()
        self.modality = modality
        self.num_segments = num_segments
        self.reshape = True
        self.before_softmax = before_softmax
        self.dropout = dropout
        self.crop_num = crop_num
        self.consensus_type = consensus_type
        self.img_feature_dim = img_feature_dim  # the dimension of the CNN feature to represent each frame
        self.pretrain = pretrain
        self.print_spec = print_spec
        self.shifted_blocks = shifted_blocks

        self.is_shift = is_shift
        self.shift_div = shift_div
        self.shift_place = shift_place
        self.base_model_name = base_model
        self.fc_lr5 = fc_lr5
        self.temporal_pool = temporal_pool
        self.non_local = non_local
        self.keep_rgb = keep_rgb
        self.return_embedding = return_embedding  

        if not before_softmax and consensus_type != 'avg':
            raise ValueError("Only avg consensus can be used after Softmax")

        # if new_length is None:
        #     self.new_length = 1 if modality == "RGB" else 5
        # else:
        #     self.new_length = new_length
        self.new_length = new_length
        if print_spec:
            print(("""
    Initializing TSN with base model: {}.
    TSN Configurations:
        input_modality:     {}
        num_segments:       {}
        new_length:         {}
        consensus_module:   {}
        dropout_ratio:      {}
        img_feature_dim:    {}
            """.format(base_model, self.modality, self.num_segments, self.new_length, consensus_type, self.dropout, self.img_feature_dim)))

        self._prepare_base_model(base_model)

        # feature_dim = self._prepare_tsn(num_class)
        feature_dim = self._prepare_tsn_emb()

        if self.modality == 'Flow':
            print("Converting the ImageNet model to a flow init model")
            self.base_model = self._construct_flow_model(self.base_model)
            print("Done. Flow model ready...")
        elif self.modality == 'RGBDiff':
            print("Converting the ImageNet model to RGB+Diff init model")
            self.base_model = self._construct_diff_model(self.base_model, keep_rgb)
            print("Done. RGBDiff model ready.")
        # elif self.modality == 'Gray':
        #     print("Converting the ImageNet model to Grayscale+ init model")
        #     self.base_model = self._construct_gray_model(self.base_model, self.new_length)
        #     print("Done. Gray model ready.")

        self.consensus = ConsensusModule(consensus_type)

        if not self.before_softmax:
            self.softmax = nn.Softmax()

        self._enable_pbn = partial_bn
        if partial_bn:
            self.partialBN(True)

    def _prepare_tsn(self, num_class):
        feature_dim = getattr(self.base_model, self.base_model.last_layer_name).in_features
        if self.dropout == 0:
            setattr(self.base_model, self.base_model.last_layer_name, nn.Linear(feature_dim, num_class))
            self.new_fc = None
        else:
            setattr(self.base_model, self.base_model.last_layer_name, nn.Dropout(p=self.dropout))
            self.new_fc = nn.Linear(feature_dim, num_class)

        # std = 0.001
        # if self.new_fc is None:
        #     normal_(getattr(self.base_model, self.base_model.last_layer_name).weight, 0, std)
        #     constant_(getattr(self.base_model, self.base_model.last_layer_name).bias, 0)
        # else:
        #     if hasattr(self.new_fc, 'weight'):
        #         normal_(self.new_fc.weight, 0, std)
        #         constant_(self.new_fc.bias, 0)
        return feature_dim
    
    def _prepare_tsn_emb(self):
        if self.base_model_name == 'efficientnet_b0':
            feature_dim = getattr(self.base_model, self.base_model.last_layer_name)[1].in_features
            # print(getattr(self.base_model, self.base_model.last_layer_name))
            self.base_model.features[0][0] = nn.Conv2d(
                in_channels=1, out_channels=32, kernel_size=3, stride=2, padding=1, bias=False
            )
        else:
            feature_dim = getattr(self.base_model, self.base_model.last_layer_name).in_features
        if self.dropout == 0:
            setattr(self.base_model, self.base_model.last_layer_name, nn.Linear(feature_dim, self.img_feature_dim))
            self.new_fc = None
        else:
            setattr(self.base_model, self.base_model.last_layer_name, nn.Dropout(p=self.dropout))
            self.new_fc = nn.Linear(feature_dim, self.img_feature_dim)
        return feature_dim

    def _prepare_base_model(self, base_model):
        print('=> base model: {}'.format(base_model))

        if 'resnet' in base_model:
            self.base_model = getattr(torchvision.models, base_model)(True if self.pretrain == 'imagenet' else False)
            if self.is_shift:
                print('Adding temporal shift...')
                
                make_temporal_shift(self.base_model, self.num_segments,
                                    n_div=self.shift_div, place=self.shift_place, temporal_pool=self.temporal_pool)

            if self.non_local:
                print('Adding non-local module...')
                make_non_local(self.base_model, self.num_segments)

            self.base_model.last_layer_name = 'fc'
            self.input_size = 224
            self.input_mean = [0.485, 0.456, 0.406]
            self.input_std = [0.229, 0.224, 0.225]

            self.base_model.avgpool = nn.AdaptiveAvgPool2d(1)

            if self.modality == 'Flow':
                self.input_mean = [0.5]
                self.input_std = [np.mean(self.input_std)]
            elif self.modality == 'RGBDiff':
                self.input_mean = [0.485, 0.456, 0.406] + [0] * 3 * self.new_length
                self.input_std = self.input_std + [np.mean(self.input_std) * 2] * 3 * self.new_length

        elif base_model == 'mobilenetv2':
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            if self.modality == 'Gray':
                self.base_model = mobilenet_v2(color_channel=1, pretrained=True if self.pretrain == 'imagenet' else False).to(device)
            else:
                self.base_model = mobilenet_v2(color_channel=3, pretrained=True if self.pretrain == 'imagenet' else False).to(device)
            self.base_model.last_layer_name = 'classifier'
            self.input_size = 224
            if self.modality == 'RGBDiff':
                self.input_mean = [0.485, 0.456, 0.406]
                self.input_std = [0.229, 0.224, 0.225]
            elif self.modality == 'Gray':
                self.input_mean = [0.456]
                self.input_std = [0.224]
            # model_stats = summary(self.base_model, input_size=(3, 224, 224))
            # summary_str = str(model_stats)
            self.base_model.avgpool = nn.AdaptiveAvgPool2d(1)
            # if self.is_shift:
            #     for m in self.base_model.modules():
            #         if isinstance(m, InvertedResidual) and len(m.conv) == 8 and m.use_res_connect:
            #             if self.print_spec:
            #                 print('Adding temporal shift... {}'.format(m.use_res_connect))
            #             m.conv[0] = TemporalShift(m.conv[0], n_segment=self.num_segments, n_div=self.shift_div)
            if self.is_shift:
                selected_block_indices = self.shifted_blocks  # only apply shift to specific blocks
                for idx, m in enumerate(self.base_model.modules()):
                    if isinstance(m, InvertedResidual) and len(m.conv) == 8 and m.use_res_connect:
                        if idx in selected_block_indices:
                            if self.print_spec:
                                print(f'Adding temporal shift to block {idx}... {m.use_res_connect}')
                            m.conv[0] = TemporalShift(m.conv[0], n_segment=self.num_segments, n_div=self.shift_div)
            if self.modality == 'Flow':
                self.input_mean = [0.5]
                self.input_std = [np.mean(self.input_std)]
            elif self.modality == 'RGBDiff':
                self.input_mean = [0.485, 0.456, 0.406] + [0] * 3 * self.new_length
                self.input_std = self.input_std + [np.mean(self.input_std) * 2] * 3 * self.new_length
            elif self.modality == 'Gray':
                self.input_mean = [0.456]
                self.input_std = [0.224]
        
        elif base_model == 'BNInception':
            self.base_model = bninception(pretrained=self.pretrain)
            self.input_size = self.base_model.input_size
            self.input_mean = self.base_model.mean
            self.input_std = self.base_model.std
            self.base_model.last_layer_name = 'fc'
            if self.modality == 'Flow':
                self.input_mean = [128]
            elif self.modality == 'RGBDiff':
                self.input_mean = self.input_mean * (1 + self.new_length)
            if self.is_shift:
                print('Adding temporal shift...')
                self.base_model.build_temporal_ops(
                    self.num_segments, is_temporal_shift=self.shift_place, shift_div=self.shift_div)
        elif base_model == 'efficientnet_b0':
            if self.pretrain == 'imagenet':
                print("Loading EfficientNet B0 pretrained model...")
            else:
                print("Loading EfficientNet B0 model without pretraining...")
            weights = EfficientNet_B0_Weights.IMAGENET1K_V1 if self.pretrain == 'imagenet' else None

            self.base_model = efficientnet_b0(weights=weights)

            self.base_model.last_layer_name = 'classifier'
            self.input_size = 224

            # print(self.base_model.features)
            # sys.exit(0)

            if self.is_shift:
                print("Adding temporal shift to EfficientNet MBConv blocks...")
                if self.shifted_blocks == 'all':
                    for block_idx, block in enumerate(self.base_model.features):
                        if isinstance(block, nn.Sequential):
                            for mbconv_idx, mbconv in enumerate(block):
                                if isinstance(mbconv, MBConv):
                                    old_expansion = mbconv.block[0]  # Conv2dNormActivation
                                    print(f"✅ Adding TemporalShift to Block {block_idx}, MBConv {mbconv_idx}")
                                    mbconv.block[0] = TemporalShift(
                                        old_expansion,
                                        n_segment=self.num_segments,
                                        n_div=self.shift_div,
                                        inplace=False
                                    )
                elif self.shifted_blocks == 'none':
                    print("not shifting any blocks")
                else:
                    # Inject Temporal Shift into the expansion conv (block[0]) of selected MBConv blocks
                    for block_idx, block in enumerate(self.base_model.features):
                        if block_idx in self.shifted_blocks and isinstance(block, nn.Sequential):
                            for mbconv_idx, mbconv in enumerate(block):
                                if isinstance(mbconv, MBConv):
                                    old_expansion = mbconv.block[0]  # Conv2dNormActivation
                                    print(f"✅ Adding TemporalShift to Block {block_idx}, MBConv {mbconv_idx}")
                                    mbconv.block[0] = TemporalShift(
                                        old_expansion,
                                        n_segment=self.num_segments,
                                        n_div=self.shift_div,
                                        inplace=False
                                    )
            if self.modality == 'Flow':
                self.input_mean = [0.5]
                self.input_std = [np.mean(self.input_std)]
            elif self.modality == 'RGBDiff':
                self.input_mean = self.input_mean + [0] * 3 * self.new_length
                self.input_std = self.input_std + [np.mean(self.input_std)] * 3 * self.new_length
            elif self.modality == 'Gray':
                self.input_mean = [0.456]
                self.input_std = [0.224]
        else:
            raise ValueError('Unknown base model: {}'.format(base_model))

    def train(self, mode=True):
        """
        Override the default train() to freeze the BN parameters
        :return:
        """
        super(TSN, self).train(mode)
        count = 0
        if self._enable_pbn and mode:
            print("Freezing BatchNorm2D except the first one.")
            for m in self.base_model.modules():
                if isinstance(m, nn.BatchNorm2d):
                    count += 1
                    if count >= (2 if self._enable_pbn else 1):
                        m.eval()
                        # shutdown update in frozen mode
                        m.weight.requires_grad = False
                        m.bias.requires_grad = False

    def partialBN(self, enable):
        self._enable_pbn = enable

    def get_optim_policies(self):
        first_conv_weight = []
        first_conv_bias = []
        normal_weight = []
        normal_bias = []
        lr5_weight = []
        lr10_bias = []
        bn = []
        custom_ops = []

        conv_cnt = 0
        bn_cnt = 0
        for m in self.modules():
            if isinstance(m, torch.nn.Conv2d) or isinstance(m, torch.nn.Conv1d) or isinstance(m, torch.nn.Conv3d):
                ps = list(m.parameters())
                conv_cnt += 1
                if conv_cnt == 1:
                    first_conv_weight.append(ps[0])
                    if len(ps) == 2:
                        first_conv_bias.append(ps[1])
                else:
                    normal_weight.append(ps[0])
                    if len(ps) == 2:
                        normal_bias.append(ps[1])
            elif isinstance(m, torch.nn.Linear):
                ps = list(m.parameters())
                if self.fc_lr5:
                    lr5_weight.append(ps[0])
                else:
                    normal_weight.append(ps[0])
                if len(ps) == 2:
                    if self.fc_lr5:
                        lr10_bias.append(ps[1])
                    else:
                        normal_bias.append(ps[1])

            elif isinstance(m, torch.nn.BatchNorm2d):
                bn_cnt += 1
                # later BN's are frozen
                if not self._enable_pbn or bn_cnt == 1:
                    bn.extend(list(m.parameters()))
            elif isinstance(m, torch.nn.BatchNorm3d):
                bn_cnt += 1
                # later BN's are frozen
                if not self._enable_pbn or bn_cnt == 1:
                    bn.extend(list(m.parameters()))
            elif len(m._modules) == 0:
                if len(list(m.parameters())) > 0:
                    raise ValueError("New atomic module type: {}. Need to give it a learning policy".format(type(m)))

        return [
            {'params': first_conv_weight, 'lr_mult': 5 if self.modality == 'Flow' else 1, 'decay_mult': 1,
             'name': "first_conv_weight"},
            {'params': first_conv_bias, 'lr_mult': 10 if self.modality == 'Flow' else 2, 'decay_mult': 0,
             'name': "first_conv_bias"},
            {'params': normal_weight, 'lr_mult': 1, 'decay_mult': 1,
             'name': "normal_weight"},
            {'params': normal_bias, 'lr_mult': 2, 'decay_mult': 0,
             'name': "normal_bias"},
            {'params': bn, 'lr_mult': 1, 'decay_mult': 0,
             'name': "BN scale/shift"},
            {'params': custom_ops, 'lr_mult': 1, 'decay_mult': 1,
             'name': "custom_ops"},
            # for fc
            {'params': lr5_weight, 'lr_mult': 5, 'decay_mult': 1,
             'name': "lr5_weight"},
            {'params': lr10_bias, 'lr_mult': 10, 'decay_mult': 0,
             'name': "lr10_bias"},
        ]

    def forward(self, input, no_reshape=False):
        # print(f"Input shape: {input.shape}")
        # input: [B, T, 1, H, W] (from DataLoader for Gray)
        if not no_reshape:
            if self.modality == "Gray":
                B, T, C, H, W = input.shape
                input = input.view(B * T, C, H, W)
                base_out = self.base_model(input)  
            elif self.modality == "RGB":
                sample_len = 3 * self.new_length
                input = input.view((-1, sample_len) + input.size()[-2:])
                base_out = self.base_model(input)
            elif self.modality == "RGBDiff":
                sample_len = 3 * self.new_length
                input = self._get_diff(input)
                input = input.view((-1, sample_len) + input.size()[-2:])
                base_out = self.base_model(input)
            elif self.modality == "Flow":
                sample_len = 2 * self.new_length
                input = input.view((-1, sample_len) + input.size()[-2:])
                base_out = self.base_model(input)
        else:
            base_out = self.base_model(input)

        # print(f"Base out shape: {base_out.shape}") # [B, 1280] for mobilenetv2

        if self.dropout > 0:
            base_out = self.new_fc(base_out)

        # print(f"Base out after new_Fc: {base_out.shape}") # [B, 512]
        if not self.before_softmax:
            base_out = self.softmax(base_out)

        # print(f"Nothing should happen here: {base_out.shape}") # [B, 512]
        if self.reshape:
            if self.is_shift and self.temporal_pool:
                base_out = base_out.view((-1, self.num_segments // 2) + base_out.size()[1:])
            else:
                base_out = base_out.view((-1, self.num_segments) + base_out.size()[1:])
            output = self.consensus(base_out)
            return output.squeeze(1)

    def _get_diff(self, input, keep_rgb=False):
        input_c = 3 if self.modality in ["RGB", "RGBDiff"] else 2
        input_view = input.view((-1, self.num_segments, self.new_length + 1, input_c,) + input.size()[2:])
        if keep_rgb:
            new_data = input_view.clone()
        else:
            new_data = input_view[:, :, 1:, :, :, :].clone()

        for x in reversed(list(range(1, self.new_length + 1))):
            if keep_rgb:
                new_data[:, :, x, :, :, :] = input_view[:, :, x, :, :, :] - input_view[:, :, x - 1, :, :, :]
            else:
                new_data[:, :, x - 1, :, :, :] = input_view[:, :, x, :, :, :] - input_view[:, :, x - 1, :, :, :]

        return new_data

    def _construct_flow_model(self, base_model):
        # modify the convolution layers
        # Torch models are usually defined in a hierarchical way.
        # nn.modules.children() return all sub modules in a DFS manner
        modules = list(self.base_model.modules())
        first_conv_idx = list(filter(lambda x: isinstance(modules[x], nn.Conv2d), list(range(len(modules)))))[0]
        conv_layer = modules[first_conv_idx]
        container = modules[first_conv_idx - 1]

        # modify parameters, assume the first blob contains the convolution kernels
        params = [x.clone() for x in conv_layer.parameters()]
        kernel_size = params[0].size()
        new_kernel_size = kernel_size[:1] + (2 * self.new_length, ) + kernel_size[2:]
        new_kernels = params[0].data.mean(dim=1, keepdim=True).expand(new_kernel_size).contiguous()

        new_conv = nn.Conv2d(2 * self.new_length, conv_layer.out_channels,
                             conv_layer.kernel_size, conv_layer.stride, conv_layer.padding,
                             bias=True if len(params) == 2 else False)
        new_conv.weight.data = new_kernels
        if len(params) == 2:
            new_conv.bias.data = params[1].data # add bias if neccessary
        layer_name = list(container.state_dict().keys())[0][:-7] # remove .weight suffix to get the layer name

        # replace the first convlution layer
        setattr(container, layer_name, new_conv)

        if self.base_model_name == 'BNInception':
            sd = model_zoo.load_url('https://www.dropbox.com/s/35ftw2t4mxxgjae/BNInceptionFlow-ef652051.pth.tar?dl=1')
            base_model.load_state_dict(sd)
            print('=> Loading pretrained Flow weight done...')
        else:
            print('#' * 30, 'Warning! No Flow pretrained model is found')
        return base_model

    def _construct_diff_model(self, base_model, keep_rgb):
        # modify the convolution layers
        # Torch models are usually defined in a hierarchical way.
        # nn.modules.children() return all sub modules in a DFS manner
        modules = list(self.base_model.modules())
        # summary(self.base_model, input_size=(3, 224, 224))  # or (1, H, W) if grayscale
        # print("All modules:", modules)
        # sys.exit(0)
        first_conv_idx = filter(lambda x: isinstance(modules[x], nn.Conv2d), list(range(len(modules))))[0]
        conv_layer = modules[first_conv_idx]
        container = modules[first_conv_idx - 1]

        # modify parameters, assume the first blob contains the convolution kernels
        params = [x.clone() for x in conv_layer.parameters()]
        kernel_size = params[0].size()
        if not keep_rgb:
            new_kernel_size = kernel_size[:1] + (3 * self.new_length,) + kernel_size[2:]
            new_kernels = params[0].data.mean(dim=1, keepdim=True).expand(new_kernel_size).contiguous()
        else:
            new_kernel_size = kernel_size[:1] + (3 * self.new_length,) + kernel_size[2:]
            new_kernels = torch.cat((params[0].data, params[0].data.mean(dim=1, keepdim=True).expand(new_kernel_size).contiguous()),
                                    1)
            new_kernel_size = kernel_size[:1] + (3 + 3 * self.new_length,) + kernel_size[2:]

        new_conv = nn.Conv2d(new_kernel_size[1], conv_layer.out_channels,
                             conv_layer.kernel_size, conv_layer.stride, conv_layer.padding,
                             bias=True if len(params) == 2 else False)
        new_conv.weight.data = new_kernels
        if len(params) == 2:
            new_conv.bias.data = params[1].data  # add bias if neccessary
        layer_name = list(container.state_dict().keys())[0][:-7]  # remove .weight suffix to get the layer name

        # replace the first convolution layer
        setattr(container, layer_name, new_conv)
        return base_model
    
    def _construct_gray_model(self, base_model, new_length):
        # modify the convolution layers
        # Torch models are usually defined in a hierarchical way.
        # nn.modules.children() return all sub modules in a DFS manner
        modules = list(self.base_model.modules())
        first_conv_idx = list(filter(lambda x: isinstance(modules[x], nn.Conv2d), list(range(len(modules)))))[0]
        conv_layer = modules[first_conv_idx]
        container = modules[first_conv_idx - 1]
        # print(container)

        # modify parameters, assume the first blob contains the convolution kernels
        params = [x.clone() for x in conv_layer.parameters()]
        # print(f"Params[0]: {params[0]}")
        kernel_size = params[0].size()
        # print(f"Kernel size: {kernel_size}") ## [batch, channel, kernel, kernel]
        # instead of [batch, channel, kernel, kernel], it is [batch, channel*frames, kernel, kernel]
        new_kernel_size = kernel_size[:1] + (self.new_length,) + kernel_size[2:]
        # print(f"New kernel size: {new_kernel_size}") ## [batch, channel*frames, kernel, kernel]
        new_kernels = params[0].data.mean(dim=1, keepdim=True).expand(new_kernel_size).contiguous()

        new_conv = nn.Conv2d(self.new_length, conv_layer.out_channels,
                             conv_layer.kernel_size, conv_layer.stride, conv_layer.padding,
                             bias=True if len(params) == 2 else False)
        new_conv.weight.data = new_kernels
        if len(params) == 2:
            new_conv.bias.data = params[1].data # add bias if neccessary
        layer_name = list(container.state_dict().keys())[0][:-7] # remove .weight suffix to get the layer name

        # replace the first convlution layer
        setattr(container, layer_name, new_conv)

        return base_model

    @property
    def crop_size(self):
        return self.input_size

    @property
    def scale_size(self):
        return self.input_size * 256 // 224

    def get_augmentation(self, flip=True):
        if self.modality == 'RGB':
            if flip:
                return torchvision.transforms.Compose([GroupMultiScaleCrop(self.input_size, [1, .875, .75, .66]),
                                                       GroupRandomHorizontalFlip(is_flow=False)])
            else:
                print('#' * 20, 'NO FLIP!!!')
                return torchvision.transforms.Compose([GroupMultiScaleCrop(self.input_size, [1, .875, .75, .66])])
        elif self.modality == 'Flow':
            return torchvision.transforms.Compose([GroupMultiScaleCrop(self.input_size, [1, .875, .75]),
                                                   GroupRandomHorizontalFlip(is_flow=True)])
        elif self.modality == 'RGBDiff':
            return torchvision.transforms.Compose([GroupMultiScaleCrop(self.input_size, [1, .875, .75]),
                                                   GroupRandomHorizontalFlip(is_flow=False)])
