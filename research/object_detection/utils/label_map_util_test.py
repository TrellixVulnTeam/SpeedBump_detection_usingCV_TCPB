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
"""Tests for object_detection.utils.label_map_util."""

import os
import tensorflow as tf

from google.protobuf import text_format
from protos import string_int_label_map_pb2
from utils import label_map_util


class LabelMapUtilTest(tf.test.TestCase):

  def _generate_label_map(self, num_classes):
    label_map_proto = string_int_label_map_pb2.StringIntLabelMap()
    for i in range(1, num_classes + 1):
      item = label_map_proto.item.add()
      item.id = i
      item.name = 'label_' + str(i)
      item.display_name = str(i)
    return label_map_proto

  def test_get_label_map_dict(self):
    label_map_string = """
      item {
        id:2
        name:'cat'
      }
      item {
        id:1
        name:'dog'
      }
    """
    label_map_path = os.path.join(self.get_temp_dir(), 'label_map.pbtxt')
    with tf.gfile.Open(label_map_path, 'wb') as f:
      f.write(label_map_string)

    label_map_dict = label_map_util.get_label_map_dict(label_map_path)
    self.assertEqual(label_map_dict['dog'], 1)
    self.assertEqual(label_map_dict['cat'], 2)

  def test_get_label_map_dict_display(self):
    label_map_string = """
      item {
        id:2
        display_name:'cat'
      }
      item {
        id:1
        display_name:'dog'
      }
    """
    label_map_path = os.path.join(self.get_temp_dir(), 'label_map.pbtxt')
    with tf.gfile.Open(label_map_path, 'wb') as f:
      f.write(label_map_string)

    label_map_dict = label_map_util.get_label_map_dict(
        label_map_path, use_display_name=True)
    self.assertEqual(label_map_dict['dog'], 1)
    self.assertEqual(label_map_dict['cat'], 2)

  def test_load_bad_label_map(self):
    label_map_string = """
      item {
        id:0
        name:'class that should not be indexed at zero'
      }
      item {
        id:2
        name:'cat'
      }
      item {
        id:1
        name:'dog'
      }
    """
    label_map_path = os.path.join(self.get_temp_dir(), 'label_map.pbtxt')
    with tf.gfile.Open(label_map_path, 'wb') as f:
      f.write(label_map_string)

    with self.assertRaises(ValueError):
      label_map_util.load_labelmap(label_map_path)

  def test_load_label_map_with_background(self):
    label_map_string = """
      item {
        id:0
        name:'background'
      }
      item {
        id:2
        name:'cat'
      }
      item {
        id:1
        name:'dog'
      }
    """
    label_map_path = os.path.join(self.get_temp_dir(), 'label_map.pbtxt')
    with tf.gfile.Open(label_map_path, 'wb') as f:
      f.write(label_map_string)

    label_map_dict = label_map_util.get_label_map_dict(label_map_path)
    self.assertEqual(label_map_dict['background'], 0)
    self.assertEqual(label_map_dict['dog'], 1)
    self.assertEqual(label_map_dict['cat'], 2)

  def test_get_label_map_dict_with_fill_in_gaps_and_background(self):
    label_map_string = """
      item {
        id:3
        name:'cat'
      }
      item {
        id:1
        name:'dog'
      }
    """
    label_map_path = os.path.join(self.get_temp_dir(), 'label_map.pbtxt')
    with tf.gfile.Open(label_map_path, 'wb') as f:
      f.write(label_map_string)

    label_map_dict = label_map_util.get_label_map_dict(
        label_map_path, fill_in_gaps_and_background=True)

    self.assertEqual(label_map_dict['background'], 0)
    self.assertEqual(label_map_dict['dog'], 1)
    self.assertEqual(label_map_dict['class_2'], 2)
    self.assertEqual(label_map_dict['cat'], 3)
    self.assertEqual(len(label_map_dict), max(label_map_dict.values()) + 1)

  def test_keep_categories_with_unique_id(self):
    label_map_proto = string_int_label_map_pb2.StringIntLabelMap()
    label_map_string = """
      item {
        id:2
        name:'cat'
      }
      item {
        id:1
        name:'child'
      }
      item {
        id:1
        name:'person'
      }
      item {
        id:1
        name:'n00007846'
      }
    """
    text_format.Merge(label_map_string, label_map_proto)
    categories = label_map_util.convert_label_map_to_categories(
        label_map_proto, max_num_classes=3)
    self.assertListEqual([{
        'id': 2,
        'name': u'cat'
    }, {
        'id': 1,
        'name': u'child'
    }], categories)

  def test_convert_label_map_to_categories_no_label_map(self):
    categories = label_map_util.convert_label_map_to_categories(
        None, max_num_classes=3)
    expected_categories_list = [{
        'name': u'category_1',
        'id': 1
    }, {
        'name': u'category_2',
        'id': 2
    }, {
        'name': u'category_3',
        'id': 3
    }]
    self.assertListEqual(expected_categories_list, categories)

  def test_convert_label_map_to_categories(self):
    label_map_proto = self._generate_label_map(num_classes=4)
    categories = label_map_util.convert_label_map_to_categories(
        label_map_proto, max_num_classes=3)
    expected_categories_list = [{
        'name': u'1',
        'id': 1
    }, {
        'name': u'2',
        'id': 2
    }, {
        'name': u'3',
        'id': 3
    }]
    self.assertListEqual(expected_categories_list, categories)

  def test_convert_label_map_to_categories_with_few_classes(self):
    label_map_proto = self._generate_label_map(num_classes=4)
    cat_no_offset = label_map_util.convert_label_map_to_categories(
        label_map_proto, max_num_classes=2)
    expected_categories_list = [{
        'name': u'1',
        'id': 1
    }, {
        'name': u'2',
        'id': 2
    }]
    self.assertListEqual(expected_categories_list, cat_no_offset)

  def test_get_max_label_map_index(self):
    num_classes = 4
    label_map_proto = self._generate_label_map(num_classes=num_classes)
    max_index = label_map_util.get_max_label_map_index(label_map_proto)
    self.assertEqual(num_classes, max_index)

  def test_create_category_index(self):
    categories = [{'name': u'1', 'id': 1}, {'name': u'2', 'id': 2}]
    category_index = label_map_util.create_category_index(categories)
    self.assertDictEqual({
        1: {
            'name': u'1',
            'id': 1
        },
        2: {
            'name': u'2',
            'id': 2
        }
    }, category_index)

  def test_create_categories_from_labelmap(self):
    label_map_string = """
      item {
        id:1
        name:'dog'
      }
      item {
        id:2
        name:'cat'
      }
    """
    label_map_path = os.path.join(self.get_temp_dir(), 'label_map.pbtxt')
    with tf.gfile.Open(label_map_path, 'wb') as f:
      f.write(label_map_string)

    categories = label_map_util.create_categories_from_labelmap(label_map_path)
    self.assertListEqual([{
        'name': u'dog',
        'id': 1
    }, {
        'name': u'cat',
        'id': 2
    }], categories)

  def test_create_category_index_from_labelmap(self):
    label_map_string = """
      item {
        id:2
        name:'cat'
      }
      item {
        id:1
        name:'dog'
      }
    """
    label_map_path = os.path.join(self.get_temp_dir(), 'label_map.pbtxt')
    with tf.gfile.Open(label_map_path, 'wb') as f:
      f.write(label_map_string)

    category_index = label_map_util.create_category_index_from_labelmap(
        label_map_path)
    self.assertDictEqual({
        1: {
            'name': u'dog',
            'id': 1
        },
        2: {
            'name': u'cat',
            'id': 2
        }
    }, category_index)

  def test_create_category_index_from_labelmap_display(self):
    label_map_string = """
      item {
        id:2
        name:'cat'
        display_name:'meow'
      }
      item {
        id:1
        name:'dog'
        display_name:'woof'
      }
    """
    label_map_path = os.path.join(self.get_temp_dir(), 'label_map.pbtxt')
    with tf.gfile.Open(label_map_path, 'wb') as f:
      f.write(label_map_string)

    self.assertDictEqual({
        1: {
            'name': u'dog',
            'id': 1
        },
        2: {
            'name': u'cat',
            'id': 2
        }
    }, label_map_util.create_category_index_from_labelmap(
        label_map_path, False))

    self.assertDictEqual({
        1: {
            'name': u'woof',
            'id': 1
        },
        2: {
            'name': u'meow',
            'id': 2
        }
    }, label_map_util.create_category_index_from_labelmap(label_map_path))


if __name__ == '__main__':
  tf.test.main()
