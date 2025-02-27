# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Tests for object_detection.models.model_builder."""

from absl.testing import parameterized

import tensorflow as tf

from google.protobuf import text_format
from builders import model_builder
from meta_architectures import faster_rcnn_meta_arch
from meta_architectures import rfcn_meta_arch
from meta_architectures import ssd_meta_arch
from models import faster_rcnn_inception_resnet_v2_feature_extractor as frcnn_inc_res
from models import faster_rcnn_inception_v2_feature_extractor as frcnn_inc_v2
from models import faster_rcnn_nas_feature_extractor as frcnn_nas
from models import faster_rcnn_pnas_feature_extractor as frcnn_pnas
from models import faster_rcnn_resnet_v1_feature_extractor as frcnn_resnet_v1
from models import ssd_resnet_v1_fpn_feature_extractor as ssd_resnet_v1_fpn
from models import ssd_resnet_v1_ppn_feature_extractor as ssd_resnet_v1_ppn
from models.embedded_ssd_mobilenet_v1_feature_extractor import EmbeddedSSDMobileNetV1FeatureExtractor
from models.ssd_inception_v2_feature_extractor import SSDInceptionV2FeatureExtractor
from models.ssd_inception_v3_feature_extractor import SSDInceptionV3FeatureExtractor
from models.ssd_mobilenet_v1_feature_extractor import SSDMobileNetV1FeatureExtractor
from models.ssd_mobilenet_v1_fpn_feature_extractor import SSDMobileNetV1FpnFeatureExtractor
from models.ssd_mobilenet_v1_ppn_feature_extractor import SSDMobileNetV1PpnFeatureExtractor
from models.ssd_mobilenet_v2_feature_extractor import SSDMobileNetV2FeatureExtractor
from models.ssd_mobilenet_v2_fpn_feature_extractor import SSDMobileNetV2FpnFeatureExtractor
from models.ssd_mobilenet_v2_keras_feature_extractor import SSDMobileNetV2KerasFeatureExtractor
from predictors import convolutional_box_predictor
from predictors import convolutional_keras_box_predictor
from protos import model_pb2

FRCNN_RESNET_FEAT_MAPS = {
    'faster_rcnn_resnet50':
    frcnn_resnet_v1.FasterRCNNResnet50FeatureExtractor,
    'faster_rcnn_resnet101':
    frcnn_resnet_v1.FasterRCNNResnet101FeatureExtractor,
    'faster_rcnn_resnet152':
    frcnn_resnet_v1.FasterRCNNResnet152FeatureExtractor
}

SSD_RESNET_V1_FPN_FEAT_MAPS = {
    'ssd_resnet50_v1_fpn':
    ssd_resnet_v1_fpn.SSDResnet50V1FpnFeatureExtractor,
    'ssd_resnet101_v1_fpn':
    ssd_resnet_v1_fpn.SSDResnet101V1FpnFeatureExtractor,
    'ssd_resnet152_v1_fpn':
    ssd_resnet_v1_fpn.SSDResnet152V1FpnFeatureExtractor,
}

SSD_RESNET_V1_PPN_FEAT_MAPS = {
    'ssd_resnet50_v1_ppn':
    ssd_resnet_v1_ppn.SSDResnet50V1PpnFeatureExtractor,
    'ssd_resnet101_v1_ppn':
    ssd_resnet_v1_ppn.SSDResnet101V1PpnFeatureExtractor,
    'ssd_resnet152_v1_ppn':
    ssd_resnet_v1_ppn.SSDResnet152V1PpnFeatureExtractor
}


class ModelBuilderTest(tf.test.TestCase, parameterized.TestCase):

  def create_model(self, model_config):
    """Builds a DetectionModel based on the model config.

    Args:
      model_config: A model.proto object containing the config for the desired
        DetectionModel.

    Returns:
      DetectionModel based on the config.
    """
    return model_builder.build(model_config, is_training=True)

  def test_create_ssd_inception_v2_model_from_config(self):
    model_text_proto = """
      ssd {
        feature_extractor {
          type: 'ssd_inception_v2'
          conv_hyperparams {
            regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
          }
          override_base_feature_extractor_hyperparams: true
        }
        box_coder {
          faster_rcnn_box_coder {
          }
        }
        matcher {
          argmax_matcher {
          }
        }
        similarity_calculator {
          iou_similarity {
          }
        }
        anchor_generator {
          ssd_anchor_generator {
            aspect_ratios: 1.0
          }
        }
        image_resizer {
          fixed_shape_resizer {
            height: 320
            width: 320
          }
        }
        box_predictor {
          convolutional_box_predictor {
            conv_hyperparams {
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
          }
        }
        loss {
          classification_loss {
            weighted_softmax {
            }
          }
          localization_loss {
            weighted_smooth_l1 {
            }
          }
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)
    model = self.create_model(model_proto)
    self.assertIsInstance(model, ssd_meta_arch.SSDMetaArch)
    self.assertIsInstance(model._feature_extractor,
                          SSDInceptionV2FeatureExtractor)
    self.assertIsNone(model._expected_loss_weights_fn)



  def test_create_ssd_inception_v3_model_from_config(self):
    model_text_proto = """
      ssd {
        feature_extractor {
          type: 'ssd_inception_v3'
          conv_hyperparams {
            regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
          }
          override_base_feature_extractor_hyperparams: true
        }
        box_coder {
          faster_rcnn_box_coder {
          }
        }
        matcher {
          argmax_matcher {
          }
        }
        similarity_calculator {
          iou_similarity {
          }
        }
        anchor_generator {
          ssd_anchor_generator {
            aspect_ratios: 1.0
          }
        }
        image_resizer {
          fixed_shape_resizer {
            height: 320
            width: 320
          }
        }
        box_predictor {
          convolutional_box_predictor {
            conv_hyperparams {
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
          }
        }
        loss {
          classification_loss {
            weighted_softmax {
            }
          }
          localization_loss {
            weighted_smooth_l1 {
            }
          }
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)
    model = self.create_model(model_proto)
    self.assertIsInstance(model, ssd_meta_arch.SSDMetaArch)
    self.assertIsInstance(model._feature_extractor,
                          SSDInceptionV3FeatureExtractor)

  def test_create_ssd_resnet_v1_fpn_model_from_config(self):
    model_text_proto = """
      ssd {
        feature_extractor {
          type: 'ssd_resnet50_v1_fpn'
          fpn {
            min_level: 3
            max_level: 7
          }
          conv_hyperparams {
            regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
          }
        }
        box_coder {
          faster_rcnn_box_coder {
          }
        }
        matcher {
          argmax_matcher {
          }
        }
        similarity_calculator {
          iou_similarity {
          }
        }
        encode_background_as_zeros: true
        anchor_generator {
          multiscale_anchor_generator {
            aspect_ratios: [1.0, 2.0, 0.5]
            scales_per_octave: 2
          }
        }
        image_resizer {
          fixed_shape_resizer {
            height: 320
            width: 320
          }
        }
        box_predictor {
          weight_shared_convolutional_box_predictor {
            depth: 32
            conv_hyperparams {
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                random_normal_initializer {
                }
              }
            }
            num_layers_before_predictor: 1
          }
        }
        normalize_loss_by_num_matches: true
        normalize_loc_loss_by_codesize: true
        loss {
          classification_loss {
            weighted_sigmoid_focal {
              alpha: 0.25
              gamma: 2.0
            }
          }
          localization_loss {
            weighted_smooth_l1 {
              delta: 0.1
            }
          }
          classification_weight: 1.0
          localization_weight: 1.0
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)

    for extractor_type, extractor_class in SSD_RESNET_V1_FPN_FEAT_MAPS.items():
      model_proto.ssd.feature_extractor.type = extractor_type
      model = model_builder.build(model_proto, is_training=True)
      self.assertIsInstance(model, ssd_meta_arch.SSDMetaArch)
      self.assertIsInstance(model._feature_extractor, extractor_class)

  def test_create_ssd_resnet_v1_ppn_model_from_config(self):
    model_text_proto = """
      ssd {
        feature_extractor {
          type: 'ssd_resnet_v1_50_ppn'
          conv_hyperparams {
            regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
          }
        }
        box_coder {
          mean_stddev_box_coder {
          }
        }
        matcher {
          bipartite_matcher {
          }
        }
        similarity_calculator {
          iou_similarity {
          }
        }
        anchor_generator {
          ssd_anchor_generator {
            aspect_ratios: 1.0
          }
        }
        image_resizer {
          fixed_shape_resizer {
            height: 320
            width: 320
          }
        }
        box_predictor {
          weight_shared_convolutional_box_predictor {
            depth: 1024
            class_prediction_bias_init: -4.6
            conv_hyperparams {
              activation: RELU_6,
              regularizer {
                l2_regularizer {
                  weight: 0.0004
                }
              }
              initializer {
                variance_scaling_initializer {
                }
              }
            }
            num_layers_before_predictor: 2
            kernel_size: 1
          }
        }
        loss {
          classification_loss {
            weighted_softmax {
            }
          }
          localization_loss {
            weighted_l2 {
            }
          }
          classification_weight: 1.0
          localization_weight: 1.0
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)

    for extractor_type, extractor_class in SSD_RESNET_V1_PPN_FEAT_MAPS.items():
      model_proto.ssd.feature_extractor.type = extractor_type
      model = model_builder.build(model_proto, is_training=True)
      self.assertIsInstance(model, ssd_meta_arch.SSDMetaArch)
      self.assertIsInstance(model._feature_extractor, extractor_class)

  def test_create_ssd_mobilenet_v1_model_from_config(self):
    model_text_proto = """
      ssd {
        freeze_batchnorm: true
        inplace_batchnorm_update: true
        feature_extractor {
          type: 'ssd_mobilenet_v1'
          conv_hyperparams {
            regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
          }
        }
        box_coder {
          faster_rcnn_box_coder {
          }
        }
        matcher {
          argmax_matcher {
          }
        }
        similarity_calculator {
          iou_similarity {
          }
        }
        anchor_generator {
          ssd_anchor_generator {
            aspect_ratios: 1.0
          }
        }
        image_resizer {
          fixed_shape_resizer {
            height: 320
            width: 320
          }
        }
        box_predictor {
          convolutional_box_predictor {
            conv_hyperparams {
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
          }
        }
        normalize_loc_loss_by_codesize: true
        loss {
          classification_loss {
            weighted_softmax {
            }
          }
          localization_loss {
            weighted_smooth_l1 {
            }
          }
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)
    model = self.create_model(model_proto)
    self.assertIsInstance(model, ssd_meta_arch.SSDMetaArch)
    self.assertIsInstance(model._feature_extractor,
                          SSDMobileNetV1FeatureExtractor)
    self.assertTrue(model._normalize_loc_loss_by_codesize)
    self.assertTrue(model._freeze_batchnorm)
    self.assertTrue(model._inplace_batchnorm_update)

  def test_create_ssd_mobilenet_v1_fpn_model_from_config(self):
    model_text_proto = """
      ssd {
        freeze_batchnorm: true
        inplace_batchnorm_update: true
        feature_extractor {
          type: 'ssd_mobilenet_v1_fpn'
          fpn {
            min_level: 3
            max_level: 7
          }
          conv_hyperparams {
            regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
          }
        }
        box_coder {
          faster_rcnn_box_coder {
          }
        }
        matcher {
          argmax_matcher {
          }
        }
        similarity_calculator {
          iou_similarity {
          }
        }
        anchor_generator {
          ssd_anchor_generator {
            aspect_ratios: 1.0
          }
        }
        image_resizer {
          fixed_shape_resizer {
            height: 320
            width: 320
          }
        }
        box_predictor {
          convolutional_box_predictor {
            conv_hyperparams {
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
          }
        }
        normalize_loc_loss_by_codesize: true
        loss {
          classification_loss {
            weighted_softmax {
            }
          }
          localization_loss {
            weighted_smooth_l1 {
            }
          }
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)
    model = self.create_model(model_proto)
    self.assertIsInstance(model, ssd_meta_arch.SSDMetaArch)
    self.assertIsInstance(model._feature_extractor,
                          SSDMobileNetV1FpnFeatureExtractor)
    self.assertTrue(model._normalize_loc_loss_by_codesize)
    self.assertTrue(model._freeze_batchnorm)
    self.assertTrue(model._inplace_batchnorm_update)

  def test_create_ssd_mobilenet_v1_ppn_model_from_config(self):
    model_text_proto = """
      ssd {
        freeze_batchnorm: true
        inplace_batchnorm_update: true
        feature_extractor {
          type: 'ssd_mobilenet_v1_ppn'
          conv_hyperparams {
            regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
          }
        }
        box_coder {
          faster_rcnn_box_coder {
          }
        }
        matcher {
          argmax_matcher {
          }
        }
        similarity_calculator {
          iou_similarity {
          }
        }
        anchor_generator {
          ssd_anchor_generator {
            aspect_ratios: 1.0
          }
        }
        image_resizer {
          fixed_shape_resizer {
            height: 320
            width: 320
          }
        }
        box_predictor {
          convolutional_box_predictor {
            conv_hyperparams {
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
          }
        }
        normalize_loc_loss_by_codesize: true
        loss {
          classification_loss {
            weighted_softmax {
            }
          }
          localization_loss {
            weighted_smooth_l1 {
            }
          }
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)
    model = self.create_model(model_proto)
    self.assertIsInstance(model, ssd_meta_arch.SSDMetaArch)
    self.assertIsInstance(model._feature_extractor,
                          SSDMobileNetV1PpnFeatureExtractor)
    self.assertTrue(model._normalize_loc_loss_by_codesize)
    self.assertTrue(model._freeze_batchnorm)
    self.assertTrue(model._inplace_batchnorm_update)

  def test_create_ssd_mobilenet_v2_model_from_config(self):
    model_text_proto = """
      ssd {
        feature_extractor {
          type: 'ssd_mobilenet_v2'
          conv_hyperparams {
            regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
          }
        }
        box_coder {
          faster_rcnn_box_coder {
          }
        }
        matcher {
          argmax_matcher {
          }
        }
        similarity_calculator {
          iou_similarity {
          }
        }
        anchor_generator {
          ssd_anchor_generator {
            aspect_ratios: 1.0
          }
        }
        image_resizer {
          fixed_shape_resizer {
            height: 320
            width: 320
          }
        }
        box_predictor {
          convolutional_box_predictor {
            conv_hyperparams {
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
          }
        }
        normalize_loc_loss_by_codesize: true
        loss {
          classification_loss {
            weighted_softmax {
            }
          }
          localization_loss {
            weighted_smooth_l1 {
            }
          }
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)
    model = self.create_model(model_proto)
    self.assertIsInstance(model, ssd_meta_arch.SSDMetaArch)
    self.assertIsInstance(model._feature_extractor,
                          SSDMobileNetV2FeatureExtractor)
    self.assertIsInstance(model._box_predictor,
                          convolutional_box_predictor.ConvolutionalBoxPredictor)
    self.assertTrue(model._normalize_loc_loss_by_codesize)

  def test_create_ssd_mobilenet_v2_keras_model_from_config(self):
    model_text_proto = """
      ssd {
        feature_extractor {
          type: 'ssd_mobilenet_v2_keras'
          conv_hyperparams {
            regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
          }
        }
        box_coder {
          faster_rcnn_box_coder {
          }
        }
        matcher {
          argmax_matcher {
          }
        }
        similarity_calculator {
          iou_similarity {
          }
        }
        anchor_generator {
          ssd_anchor_generator {
            aspect_ratios: 1.0
          }
        }
        image_resizer {
          fixed_shape_resizer {
            height: 320
            width: 320
          }
        }
        box_predictor {
          convolutional_box_predictor {
            conv_hyperparams {
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
          }
        }
        normalize_loc_loss_by_codesize: true
        loss {
          classification_loss {
            weighted_softmax {
            }
          }
          localization_loss {
            weighted_smooth_l1 {
            }
          }
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)
    model = self.create_model(model_proto)
    self.assertIsInstance(model, ssd_meta_arch.SSDMetaArch)
    self.assertIsInstance(model._feature_extractor,
                          SSDMobileNetV2KerasFeatureExtractor)
    self.assertIsInstance(
        model._box_predictor,
        convolutional_keras_box_predictor.ConvolutionalBoxPredictor)
    self.assertTrue(model._normalize_loc_loss_by_codesize)

  def test_create_ssd_mobilenet_v2_fpn_model_from_config(self):
    model_text_proto = """
      ssd {
        freeze_batchnorm: true
        inplace_batchnorm_update: true
        feature_extractor {
          type: 'ssd_mobilenet_v2_fpn'
          fpn {
            min_level: 3
            max_level: 7
          }
          conv_hyperparams {
            regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
          }
        }
        box_coder {
          faster_rcnn_box_coder {
          }
        }
        matcher {
          argmax_matcher {
          }
        }
        similarity_calculator {
          iou_similarity {
          }
        }
        anchor_generator {
          ssd_anchor_generator {
            aspect_ratios: 1.0
          }
        }
        image_resizer {
          fixed_shape_resizer {
            height: 320
            width: 320
          }
        }
        box_predictor {
          convolutional_box_predictor {
            conv_hyperparams {
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
          }
        }
        normalize_loc_loss_by_codesize: true
        loss {
          classification_loss {
            weighted_softmax {
            }
          }
          localization_loss {
            weighted_smooth_l1 {
            }
          }
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)
    model = self.create_model(model_proto)
    self.assertIsInstance(model, ssd_meta_arch.SSDMetaArch)
    self.assertIsInstance(model._feature_extractor,
                          SSDMobileNetV2FpnFeatureExtractor)
    self.assertTrue(model._normalize_loc_loss_by_codesize)
    self.assertTrue(model._freeze_batchnorm)
    self.assertTrue(model._inplace_batchnorm_update)

  def test_create_ssd_mobilenet_v2_fpnlite_model_from_config(self):
    model_text_proto = """
      ssd {
        freeze_batchnorm: true
        inplace_batchnorm_update: true
        feature_extractor {
          type: 'ssd_mobilenet_v2_fpn'
          use_depthwise: true
          fpn {
            min_level: 3
            max_level: 7
            additional_layer_depth: 128
          }
          conv_hyperparams {
            regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
          }
        }
        box_coder {
          faster_rcnn_box_coder {
          }
        }
        matcher {
          argmax_matcher {
          }
        }
        similarity_calculator {
          iou_similarity {
          }
        }
        anchor_generator {
          ssd_anchor_generator {
            aspect_ratios: 1.0
          }
        }
        image_resizer {
          fixed_shape_resizer {
            height: 320
            width: 320
          }
        }
        box_predictor {
          convolutional_box_predictor {
            conv_hyperparams {
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
          }
        }
        normalize_loc_loss_by_codesize: true
        loss {
          classification_loss {
            weighted_softmax {
            }
          }
          localization_loss {
            weighted_smooth_l1 {
            }
          }
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)
    model = self.create_model(model_proto)
    self.assertIsInstance(model, ssd_meta_arch.SSDMetaArch)
    self.assertIsInstance(model._feature_extractor,
                          SSDMobileNetV2FpnFeatureExtractor)
    self.assertTrue(model._normalize_loc_loss_by_codesize)
    self.assertTrue(model._freeze_batchnorm)
    self.assertTrue(model._inplace_batchnorm_update)

  def test_create_embedded_ssd_mobilenet_v1_model_from_config(self):
    model_text_proto = """
      ssd {
        feature_extractor {
          type: 'embedded_ssd_mobilenet_v1'
          conv_hyperparams {
            regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
          }
        }
        box_coder {
          faster_rcnn_box_coder {
          }
        }
        matcher {
          argmax_matcher {
          }
        }
        similarity_calculator {
          iou_similarity {
          }
        }
        anchor_generator {
          ssd_anchor_generator {
            aspect_ratios: 1.0
          }
        }
        image_resizer {
          fixed_shape_resizer {
            height: 256
            width: 256
          }
        }
        box_predictor {
          convolutional_box_predictor {
            conv_hyperparams {
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
          }
        }
        loss {
          classification_loss {
            weighted_softmax {
            }
          }
          localization_loss {
            weighted_smooth_l1 {
            }
          }
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)
    model = self.create_model(model_proto)
    self.assertIsInstance(model, ssd_meta_arch.SSDMetaArch)
    self.assertIsInstance(model._feature_extractor,
                          EmbeddedSSDMobileNetV1FeatureExtractor)

  def test_create_faster_rcnn_resnet_v1_models_from_config(self):
    model_text_proto = """
      faster_rcnn {
        inplace_batchnorm_update: false
        num_classes: 3
        image_resizer {
          keep_aspect_ratio_resizer {
            min_dimension: 600
            max_dimension: 1024
          }
        }
        feature_extractor {
          type: 'faster_rcnn_resnet101'
        }
        first_stage_anchor_generator {
          grid_anchor_generator {
            scales: [0.25, 0.5, 1.0, 2.0]
            aspect_ratios: [0.5, 1.0, 2.0]
            height_stride: 16
            width_stride: 16
          }
        }
        first_stage_box_predictor_conv_hyperparams {
          regularizer {
            l2_regularizer {
            }
          }
          initializer {
            truncated_normal_initializer {
            }
          }
        }
        initial_crop_size: 14
        maxpool_kernel_size: 2
        maxpool_stride: 2
        second_stage_box_predictor {
          mask_rcnn_box_predictor {
            fc_hyperparams {
              op: FC
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
          }
        }
        second_stage_post_processing {
          batch_non_max_suppression {
            score_threshold: 0.01
            iou_threshold: 0.6
            max_detections_per_class: 100
            max_total_detections: 300
          }
          score_converter: SOFTMAX
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)

    for extractor_type, extractor_class in FRCNN_RESNET_FEAT_MAPS.items():
      model_proto.faster_rcnn.feature_extractor.type = extractor_type
      model = model_builder.build(model_proto, is_training=True)
      self.assertIsInstance(model, faster_rcnn_meta_arch.FasterRCNNMetaArch)
      self.assertIsInstance(model._feature_extractor, extractor_class)

  @parameterized.parameters(
      {'use_matmul_crop_and_resize': False},
      {'use_matmul_crop_and_resize': True},
  )
  def test_create_faster_rcnn_resnet101_with_mask_prediction_enabled(
      self, use_matmul_crop_and_resize):
    model_text_proto = """
      faster_rcnn {
        num_classes: 3
        image_resizer {
          keep_aspect_ratio_resizer {
            min_dimension: 600
            max_dimension: 1024
          }
        }
        feature_extractor {
          type: 'faster_rcnn_resnet101'
        }
        first_stage_anchor_generator {
          grid_anchor_generator {
            scales: [0.25, 0.5, 1.0, 2.0]
            aspect_ratios: [0.5, 1.0, 2.0]
            height_stride: 16
            width_stride: 16
          }
        }
        first_stage_box_predictor_conv_hyperparams {
          regularizer {
            l2_regularizer {
            }
          }
          initializer {
            truncated_normal_initializer {
            }
          }
        }
        initial_crop_size: 14
        maxpool_kernel_size: 2
        maxpool_stride: 2
        second_stage_box_predictor {
          mask_rcnn_box_predictor {
            fc_hyperparams {
              op: FC
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
            conv_hyperparams {
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
            predict_instance_masks: true
          }
        }
        second_stage_mask_prediction_loss_weight: 3.0
        second_stage_post_processing {
          batch_non_max_suppression {
            score_threshold: 0.01
            iou_threshold: 0.6
            max_detections_per_class: 100
            max_total_detections: 300
          }
          score_converter: SOFTMAX
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)
    model_proto.faster_rcnn.use_matmul_crop_and_resize = (
        use_matmul_crop_and_resize)
    model = model_builder.build(model_proto, is_training=True)
    self.assertAlmostEqual(model._second_stage_mask_loss_weight, 3.0)

  def test_create_faster_rcnn_nas_model_from_config(self):
    model_text_proto = """
      faster_rcnn {
        num_classes: 3
        image_resizer {
          keep_aspect_ratio_resizer {
            min_dimension: 600
            max_dimension: 1024
          }
        }
        feature_extractor {
          type: 'faster_rcnn_nas'
        }
        first_stage_anchor_generator {
          grid_anchor_generator {
            scales: [0.25, 0.5, 1.0, 2.0]
            aspect_ratios: [0.5, 1.0, 2.0]
            height_stride: 16
            width_stride: 16
          }
        }
        first_stage_box_predictor_conv_hyperparams {
          regularizer {
            l2_regularizer {
            }
          }
          initializer {
            truncated_normal_initializer {
            }
          }
        }
        initial_crop_size: 17
        maxpool_kernel_size: 1
        maxpool_stride: 1
        second_stage_box_predictor {
          mask_rcnn_box_predictor {
            fc_hyperparams {
              op: FC
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
          }
        }
        second_stage_post_processing {
          batch_non_max_suppression {
            score_threshold: 0.01
            iou_threshold: 0.6
            max_detections_per_class: 100
            max_total_detections: 300
          }
          score_converter: SOFTMAX
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)
    model = model_builder.build(model_proto, is_training=True)
    self.assertIsInstance(model, faster_rcnn_meta_arch.FasterRCNNMetaArch)
    self.assertIsInstance(
        model._feature_extractor,
        frcnn_nas.FasterRCNNNASFeatureExtractor)

  def test_create_faster_rcnn_pnas_model_from_config(self):
    model_text_proto = """
      faster_rcnn {
        num_classes: 3
        image_resizer {
          keep_aspect_ratio_resizer {
            min_dimension: 600
            max_dimension: 1024
          }
        }
        feature_extractor {
          type: 'faster_rcnn_pnas'
        }
        first_stage_anchor_generator {
          grid_anchor_generator {
            scales: [0.25, 0.5, 1.0, 2.0]
            aspect_ratios: [0.5, 1.0, 2.0]
            height_stride: 16
            width_stride: 16
          }
        }
        first_stage_box_predictor_conv_hyperparams {
          regularizer {
            l2_regularizer {
            }
          }
          initializer {
            truncated_normal_initializer {
            }
          }
        }
        initial_crop_size: 17
        maxpool_kernel_size: 1
        maxpool_stride: 1
        second_stage_box_predictor {
          mask_rcnn_box_predictor {
            fc_hyperparams {
              op: FC
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
          }
        }
        second_stage_post_processing {
          batch_non_max_suppression {
            score_threshold: 0.01
            iou_threshold: 0.6
            max_detections_per_class: 100
            max_total_detections: 300
          }
          score_converter: SOFTMAX
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)
    model = model_builder.build(model_proto, is_training=True)
    self.assertIsInstance(model, faster_rcnn_meta_arch.FasterRCNNMetaArch)
    self.assertIsInstance(
        model._feature_extractor,
        frcnn_pnas.FasterRCNNPNASFeatureExtractor)

  def test_create_faster_rcnn_inception_resnet_v2_model_from_config(self):
    model_text_proto = """
      faster_rcnn {
        num_classes: 3
        image_resizer {
          keep_aspect_ratio_resizer {
            min_dimension: 600
            max_dimension: 1024
          }
        }
        feature_extractor {
          type: 'faster_rcnn_inception_resnet_v2'
        }
        first_stage_anchor_generator {
          grid_anchor_generator {
            scales: [0.25, 0.5, 1.0, 2.0]
            aspect_ratios: [0.5, 1.0, 2.0]
            height_stride: 16
            width_stride: 16
          }
        }
        first_stage_box_predictor_conv_hyperparams {
          regularizer {
            l2_regularizer {
            }
          }
          initializer {
            truncated_normal_initializer {
            }
          }
        }
        initial_crop_size: 17
        maxpool_kernel_size: 1
        maxpool_stride: 1
        second_stage_box_predictor {
          mask_rcnn_box_predictor {
            fc_hyperparams {
              op: FC
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
          }
        }
        second_stage_post_processing {
          batch_non_max_suppression {
            score_threshold: 0.01
            iou_threshold: 0.6
            max_detections_per_class: 100
            max_total_detections: 300
          }
          score_converter: SOFTMAX
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)
    model = model_builder.build(model_proto, is_training=True)
    self.assertIsInstance(model, faster_rcnn_meta_arch.FasterRCNNMetaArch)
    self.assertIsInstance(
        model._feature_extractor,
        frcnn_inc_res.FasterRCNNInceptionResnetV2FeatureExtractor)

  def test_create_faster_rcnn_inception_v2_model_from_config(self):
    model_text_proto = """
      faster_rcnn {
        num_classes: 3
        image_resizer {
          keep_aspect_ratio_resizer {
            min_dimension: 600
            max_dimension: 1024
          }
        }
        feature_extractor {
          type: 'faster_rcnn_inception_v2'
        }
        first_stage_anchor_generator {
          grid_anchor_generator {
            scales: [0.25, 0.5, 1.0, 2.0]
            aspect_ratios: [0.5, 1.0, 2.0]
            height_stride: 16
            width_stride: 16
          }
        }
        first_stage_box_predictor_conv_hyperparams {
          regularizer {
            l2_regularizer {
            }
          }
          initializer {
            truncated_normal_initializer {
            }
          }
        }
        initial_crop_size: 14
        maxpool_kernel_size: 2
        maxpool_stride: 2
        second_stage_box_predictor {
          mask_rcnn_box_predictor {
            fc_hyperparams {
              op: FC
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
          }
        }
        second_stage_post_processing {
          batch_non_max_suppression {
            score_threshold: 0.01
            iou_threshold: 0.6
            max_detections_per_class: 100
            max_total_detections: 300
          }
          score_converter: SOFTMAX
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)
    model = model_builder.build(model_proto, is_training=True)
    self.assertIsInstance(model, faster_rcnn_meta_arch.FasterRCNNMetaArch)
    self.assertIsInstance(model._feature_extractor,
                          frcnn_inc_v2.FasterRCNNInceptionV2FeatureExtractor)

  def test_create_faster_rcnn_model_from_config_with_example_miner(self):
    model_text_proto = """
      faster_rcnn {
        num_classes: 3
        feature_extractor {
          type: 'faster_rcnn_inception_resnet_v2'
        }
        image_resizer {
          keep_aspect_ratio_resizer {
            min_dimension: 600
            max_dimension: 1024
          }
        }
        first_stage_anchor_generator {
          grid_anchor_generator {
            scales: [0.25, 0.5, 1.0, 2.0]
            aspect_ratios: [0.5, 1.0, 2.0]
            height_stride: 16
            width_stride: 16
          }
        }
        first_stage_box_predictor_conv_hyperparams {
          regularizer {
            l2_regularizer {
            }
          }
          initializer {
            truncated_normal_initializer {
            }
          }
        }
        second_stage_box_predictor {
          mask_rcnn_box_predictor {
            fc_hyperparams {
              op: FC
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
          }
        }
        hard_example_miner {
          num_hard_examples: 10
          iou_threshold: 0.99
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)
    model = model_builder.build(model_proto, is_training=True)
    self.assertIsNotNone(model._hard_example_miner)

  def test_create_rfcn_resnet_v1_model_from_config(self):
    model_text_proto = """
      faster_rcnn {
        num_classes: 3
        image_resizer {
          keep_aspect_ratio_resizer {
            min_dimension: 600
            max_dimension: 1024
          }
        }
        feature_extractor {
          type: 'faster_rcnn_resnet101'
        }
        first_stage_anchor_generator {
          grid_anchor_generator {
            scales: [0.25, 0.5, 1.0, 2.0]
            aspect_ratios: [0.5, 1.0, 2.0]
            height_stride: 16
            width_stride: 16
          }
        }
        first_stage_box_predictor_conv_hyperparams {
          regularizer {
            l2_regularizer {
            }
          }
          initializer {
            truncated_normal_initializer {
            }
          }
        }
        initial_crop_size: 14
        maxpool_kernel_size: 2
        maxpool_stride: 2
        second_stage_box_predictor {
          rfcn_box_predictor {
            conv_hyperparams {
              op: CONV
              regularizer {
                l2_regularizer {
                }
              }
              initializer {
                truncated_normal_initializer {
                }
              }
            }
          }
        }
        second_stage_post_processing {
          batch_non_max_suppression {
            score_threshold: 0.01
            iou_threshold: 0.6
            max_detections_per_class: 100
            max_total_detections: 300
          }
          score_converter: SOFTMAX
        }
      }"""
    model_proto = model_pb2.DetectionModel()
    text_format.Merge(model_text_proto, model_proto)
    for extractor_type, extractor_class in FRCNN_RESNET_FEAT_MAPS.items():
      model_proto.faster_rcnn.feature_extractor.type = extractor_type
      model = model_builder.build(model_proto, is_training=True)
      self.assertIsInstance(model, rfcn_meta_arch.RFCNMetaArch)
      self.assertIsInstance(model._feature_extractor, extractor_class)


if __name__ == '__main__':
  tf.test.main()
