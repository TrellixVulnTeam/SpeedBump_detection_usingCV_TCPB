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
"""Preprocess images and bounding boxes for detection.

We perform two sets of operations in preprocessing stage:
(a) operations that are applied to both training and testing data,
(b) operations that are applied only to training data for the purpose of
    data augmentation.

A preprocessing function receives a set of inputs,
e.g. an image and bounding boxes,
performs an operation on them, and returns them.
Some examples are: randomly cropping the image, randomly mirroring the image,
                   randomly changing the brightness, contrast, hue and
                   randomly jittering the bounding boxes.

The preprocess function receives a tensor_dict which is a dictionary that maps
different field names to their tensors. For example,
tensor_dict[fields.InputDataFields.image] holds the image tensor.
The image is a rank 4 tensor: [1, height, width, channels] with
dtype=tf.float32. The groundtruth_boxes is a rank 2 tensor: [N, 4] where
in each row there is a box with [ymin xmin ymax xmax].
Boxes are in normalized coordinates meaning
their coordinate values range in [0, 1]

To preprocess multiple images with the same operations in cases where
nondeterministic operations are used, a preprocessor_cache.PreprocessorCache
object can be passed into the preprocess function or individual operations.
All nondeterministic operations except random_jitter_boxes support caching.
E.g.
Let tensor_dict{1,2,3,4,5} be copies of the same inputs.
Let preprocess_options contain nondeterministic operation(s) excluding
random_jitter_boxes.

cache1 = preprocessor_cache.PreprocessorCache()
cache2 = preprocessor_cache.PreprocessorCache()
a = preprocess(tensor_dict1, preprocess_options, preprocess_vars_cache=cache1)
b = preprocess(tensor_dict2, preprocess_options, preprocess_vars_cache=cache1)
c = preprocess(tensor_dict3, preprocess_options, preprocess_vars_cache=cache2)
d = preprocess(tensor_dict4, preprocess_options, preprocess_vars_cache=cache2)
e = preprocess(tensor_dict5, preprocess_options)

Then correspondings tensors of object pairs (a,b) and (c,d)
are guaranteed to be equal element-wise, but the equality of any other object
pair cannot be determined.

Important Note: In tensor_dict, images is a rank 4 tensor, but preprocessing
functions receive a rank 3 tensor for processing the image. Thus, inside the
preprocess function we squeeze the image to become a rank 3 tensor and then
we pass it to the functions. At the end of the preprocess we expand the image
back to rank 4.
"""

import functools
import inspect
import sys
import tensorflow as tf

from tensorflow.python.ops import control_flow_ops

from core import box_list
from core import box_list_ops
from core import keypoint_ops
from core import preprocessor_cache
from core import standard_fields as fields
from utils import shape_utils


def _apply_with_random_selector(x,
                                func,
                                num_cases,
                                preprocess_vars_cache=None,
                                key=''):
  """Computes func(x, sel), with sel sampled from [0...num_cases-1].

  If both preprocess_vars_cache AND key are the same between two calls, sel will
  be the same value in both calls.

  Args:
    x: input Tensor.
    func: Python function to apply.
    num_cases: Python int32, number of cases to sample sel from.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.
    key: variable identifier for preprocess_vars_cache.

  Returns:
    The result of func(x, sel), where func receives the value of the
    selector as a python integer, but sel is sampled dynamically.
  """
  generator_func = functools.partial(
      tf.random_uniform, [], maxval=num_cases, dtype=tf.int32)
  rand_sel = _get_or_create_preprocess_rand_vars(
      generator_func, preprocessor_cache.PreprocessorCache.SELECTOR,
      preprocess_vars_cache, key)

  # Pass the real x only to one of the func calls.
  return control_flow_ops.merge([func(
      control_flow_ops.switch(x, tf.equal(rand_sel, case))[1], case)
                                 for case in range(num_cases)])[0]


def _apply_with_random_selector_tuples(x,
                                       func,
                                       num_cases,
                                       preprocess_vars_cache=None,
                                       key=''):
  """Computes func(x, sel), with sel sampled from [0...num_cases-1].

  If both preprocess_vars_cache AND key are the same between two calls, sel will
  be the same value in both calls.

  Args:
    x: A tuple of input tensors.
    func: Python function to apply.
    num_cases: Python int32, number of cases to sample sel from.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.
    key: variable identifier for preprocess_vars_cache.

  Returns:
    The result of func(x, sel), where func receives the value of the
    selector as a python integer, but sel is sampled dynamically.
  """
  num_inputs = len(x)
  generator_func = functools.partial(
      tf.random_uniform, [], maxval=num_cases, dtype=tf.int32)
  rand_sel = _get_or_create_preprocess_rand_vars(
      generator_func, preprocessor_cache.PreprocessorCache.SELECTOR_TUPLES,
      preprocess_vars_cache, key)

  # Pass the real x only to one of the func calls.
  tuples = [list() for t in x]
  for case in range(num_cases):
    new_x = [control_flow_ops.switch(t, tf.equal(rand_sel, case))[1] for t in x]
    output = func(tuple(new_x), case)
    for j in range(num_inputs):
      tuples[j].append(output[j])

  for i in range(num_inputs):
    tuples[i] = control_flow_ops.merge(tuples[i])[0]
  return tuple(tuples)


def _get_or_create_preprocess_rand_vars(generator_func,
                                        function_id,
                                        preprocess_vars_cache,
                                        key=''):
  """Returns a tensor stored in preprocess_vars_cache or using generator_func.

  If the tensor was previously generated and appears in the PreprocessorCache,
  the previously generated tensor will be returned. Otherwise, a new tensor
  is generated using generator_func and stored in the cache.

  Args:
    generator_func: A 0-argument function that generates a tensor.
    function_id: identifier for the preprocessing function used.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.
    key: identifier for the variable stored.
  Returns:
    The generated tensor.
  """
  if preprocess_vars_cache is not None:
    var = preprocess_vars_cache.get(function_id, key)
    if var is None:
      var = generator_func()
      preprocess_vars_cache.update(function_id, key, var)
  else:
    var = generator_func()
  return var


def _random_integer(minval, maxval, seed):
  """Returns a random 0-D tensor between minval and maxval.

  Args:
    minval: minimum value of the random tensor.
    maxval: maximum value of the random tensor.
    seed: random seed.

  Returns:
    A random 0-D tensor between minval and maxval.
  """
  return tf.random_uniform(
      [], minval=minval, maxval=maxval, dtype=tf.int32, seed=seed)


# TODO(mttang): This method is needed because the current
# tf.image.rgb_to_grayscale method does not support quantization. Replace with
# tf.image.rgb_to_grayscale after quantization support is added.
def _rgb_to_grayscale(images, name=None):
  """Converts one or more images from RGB to Grayscale.

  Outputs a tensor of the same `DType` and rank as `images`.  The size of the
  last dimension of the output is 1, containing the Grayscale value of the
  pixels.

  Args:
    images: The RGB tensor to convert. Last dimension must have size 3 and
      should contain RGB values.
    name: A name for the operation (optional).

  Returns:
    The converted grayscale image(s).
  """
  with tf.name_scope(name, 'rgb_to_grayscale', [images]) as name:
    images = tf.convert_to_tensor(images, name='images')
    # Remember original dtype to so we can convert back if needed
    orig_dtype = images.dtype
    flt_image = tf.image.convert_image_dtype(images, tf.float32)

    # Reference for converting between RGB and grayscale.
    # https://en.wikipedia.org/wiki/Luma_%28video%29
    rgb_weights = [0.2989, 0.5870, 0.1140]
    rank_1 = tf.expand_dims(tf.rank(images) - 1, 0)
    gray_float = tf.reduce_sum(
        flt_image * rgb_weights, rank_1, keep_dims=True)
    gray_float.set_shape(images.get_shape()[:-1].concatenate([1]))
    return tf.image.convert_image_dtype(gray_float, orig_dtype, name=name)


def normalize_image(image, original_minval, original_maxval, target_minval,
                    target_maxval):
  """Normalizes pixel values in the image.

  Moves the pixel values from the current [original_minval, original_maxval]
  range to a the [target_minval, target_maxval] range.

  Args:
    image: rank 3 float32 tensor containing 1
           image -> [height, width, channels].
    original_minval: current image minimum value.
    original_maxval: current image maximum value.
    target_minval: target image minimum value.
    target_maxval: target image maximum value.

  Returns:
    image: image which is the same shape as input image.
  """
  with tf.name_scope('NormalizeImage', values=[image]):
    original_minval = float(original_minval)
    original_maxval = float(original_maxval)
    target_minval = float(target_minval)
    target_maxval = float(target_maxval)
    image = tf.to_float(image)
    image = tf.subtract(image, original_minval)
    image = tf.multiply(image, (target_maxval - target_minval) /
                        (original_maxval - original_minval))
    image = tf.add(image, target_minval)
    return image


def retain_boxes_above_threshold(boxes,
                                 labels,
                                 label_weights,
                                 label_confidences=None,
                                 multiclass_scores=None,
                                 masks=None,
                                 keypoints=None,
                                 threshold=0.0):
  """Retains boxes whose label weight is above a given threshold.

  If the label weight for a box is missing (represented by NaN), the box is
  retained. The boxes that don't pass the threshold will not appear in the
  returned tensor.

  Args:
    boxes: float32 tensor of shape [num_instance, 4] representing boxes
      location in normalized coordinates.
    labels: rank 1 int32 tensor of shape [num_instance] containing the object
      classes.
    label_weights: float32 tensor of shape [num_instance] representing the
      weight for each box.
    label_confidences: float32 tensor of shape [num_instance] representing the
      confidence for each box.
    multiclass_scores: (optional) float32 tensor of shape
      [num_instances, num_classes] representing the score for each box for each
      class.
    masks: (optional) rank 3 float32 tensor with shape
      [num_instances, height, width] containing instance masks. The masks are of
      the same height, width as the input `image`.
    keypoints: (optional) rank 3 float32 tensor with shape
      [num_instances, num_keypoints, 2]. The keypoints are in y-x normalized
      coordinates.
    threshold: scalar python float.

  Returns:
    retained_boxes: [num_retained_instance, 4]
    retianed_labels: [num_retained_instance]
    retained_label_weights: [num_retained_instance]

    If multiclass_scores, masks, or keypoints are not None, the function also
      returns:

    retained_multiclass_scores: [num_retained_instance, num_classes]
    retained_masks: [num_retained_instance, height, width]
    retained_keypoints: [num_retained_instance, num_keypoints, 2]
  """
  with tf.name_scope('RetainBoxesAboveThreshold',
                     values=[boxes, labels, label_weights]):
    indices = tf.where(
        tf.logical_or(label_weights > threshold, tf.is_nan(label_weights)))
    indices = tf.squeeze(indices, axis=1)
    retained_boxes = tf.gather(boxes, indices)
    retained_labels = tf.gather(labels, indices)
    retained_label_weights = tf.gather(label_weights, indices)
    result = [retained_boxes, retained_labels, retained_label_weights]

    if label_confidences is not None:
      retained_label_confidences = tf.gather(label_confidences, indices)
      result.append(retained_label_confidences)

    if multiclass_scores is not None:
      retained_multiclass_scores = tf.gather(multiclass_scores, indices)
      result.append(retained_multiclass_scores)

    if masks is not None:
      retained_masks = tf.gather(masks, indices)
      result.append(retained_masks)

    if keypoints is not None:
      retained_keypoints = tf.gather(keypoints, indices)
      result.append(retained_keypoints)

    return result


def _flip_boxes_left_right(boxes):
  """Left-right flip the boxes.

  Args:
    boxes: rank 2 float32 tensor containing the bounding boxes -> [N, 4].
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
           Each row is in the form of [ymin, xmin, ymax, xmax].

  Returns:
    Flipped boxes.
  """
  ymin, xmin, ymax, xmax = tf.split(value=boxes, num_or_size_splits=4, axis=1)
  flipped_xmin = tf.subtract(1.0, xmax)
  flipped_xmax = tf.subtract(1.0, xmin)
  flipped_boxes = tf.concat([ymin, flipped_xmin, ymax, flipped_xmax], 1)
  return flipped_boxes


def _flip_boxes_up_down(boxes):
  """Up-down flip the boxes.

  Args:
    boxes: rank 2 float32 tensor containing the bounding boxes -> [N, 4].
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
           Each row is in the form of [ymin, xmin, ymax, xmax].

  Returns:
    Flipped boxes.
  """
  ymin, xmin, ymax, xmax = tf.split(value=boxes, num_or_size_splits=4, axis=1)
  flipped_ymin = tf.subtract(1.0, ymax)
  flipped_ymax = tf.subtract(1.0, ymin)
  flipped_boxes = tf.concat([flipped_ymin, xmin, flipped_ymax, xmax], 1)
  return flipped_boxes


def _rot90_boxes(boxes):
  """Rotate boxes counter-clockwise by 90 degrees.

  Args:
    boxes: rank 2 float32 tensor containing the bounding boxes -> [N, 4].
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
           Each row is in the form of [ymin, xmin, ymax, xmax].

  Returns:
    Rotated boxes.
  """
  ymin, xmin, ymax, xmax = tf.split(value=boxes, num_or_size_splits=4, axis=1)
  rotated_ymin = tf.subtract(1.0, xmax)
  rotated_ymax = tf.subtract(1.0, xmin)
  rotated_xmin = ymin
  rotated_xmax = ymax
  rotated_boxes = tf.concat(
      [rotated_ymin, rotated_xmin, rotated_ymax, rotated_xmax], 1)
  return rotated_boxes


def _flip_masks_left_right(masks):
  """Left-right flip masks.

  Args:
    masks: rank 3 float32 tensor with shape
      [num_instances, height, width] representing instance masks.

  Returns:
    flipped masks: rank 3 float32 tensor with shape
      [num_instances, height, width] representing instance masks.
  """
  return masks[:, :, ::-1]


def _flip_masks_up_down(masks):
  """Up-down flip masks.

  Args:
    masks: rank 3 float32 tensor with shape
      [num_instances, height, width] representing instance masks.

  Returns:
    flipped masks: rank 3 float32 tensor with shape
      [num_instances, height, width] representing instance masks.
  """
  return masks[:, ::-1, :]


def _rot90_masks(masks):
  """Rotate masks counter-clockwise by 90 degrees.

  Args:
    masks: rank 3 float32 tensor with shape
      [num_instances, height, width] representing instance masks.

  Returns:
    rotated masks: rank 3 float32 tensor with shape
      [num_instances, height, width] representing instance masks.
  """
  masks = tf.transpose(masks, [0, 2, 1])
  return masks[:, ::-1, :]


def random_horizontal_flip(image,
                           boxes=None,
                           masks=None,
                           keypoints=None,
                           keypoint_flip_permutation=None,
                           seed=None,
                           preprocess_vars_cache=None):
  """Randomly flips the image and detections horizontally.

  The probability of flipping the image is 50%.

  Args:
    image: rank 3 float32 tensor with shape [height, width, channels].
    boxes: (optional) rank 2 float32 tensor with shape [N, 4]
           containing the bounding boxes.
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
           Each row is in the form of [ymin, xmin, ymax, xmax].
    masks: (optional) rank 3 float32 tensor with shape
           [num_instances, height, width] containing instance masks. The masks
           are of the same height, width as the input `image`.
    keypoints: (optional) rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]. The keypoints are in y-x
               normalized coordinates.
    keypoint_flip_permutation: rank 1 int32 tensor containing the keypoint flip
                               permutation.
    seed: random seed
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: image which is the same shape as input image.

    If boxes, masks, keypoints, and keypoint_flip_permutation are not None,
    the function also returns the following tensors.

    boxes: rank 2 float32 tensor containing the bounding boxes -> [N, 4].
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
    masks: rank 3 float32 tensor with shape [num_instances, height, width]
           containing instance masks.
    keypoints: rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]

  Raises:
    ValueError: if keypoints are provided but keypoint_flip_permutation is not.
  """

  def _flip_image(image):
    # flip image
    image_flipped = tf.image.flip_left_right(image)
    return image_flipped

  if keypoints is not None and keypoint_flip_permutation is None:
    raise ValueError(
        'keypoints are provided but keypoints_flip_permutation is not provided')

  with tf.name_scope('RandomHorizontalFlip', values=[image, boxes]):
    result = []
    # random variable defining whether to do flip or not
    generator_func = functools.partial(tf.random_uniform, [], seed=seed)
    do_a_flip_random = _get_or_create_preprocess_rand_vars(
        generator_func,
        preprocessor_cache.PreprocessorCache.HORIZONTAL_FLIP,
        preprocess_vars_cache)
    do_a_flip_random = tf.greater(do_a_flip_random, 0.5)

    # flip image
    image = tf.cond(do_a_flip_random, lambda: _flip_image(image), lambda: image)
    result.append(image)

    # flip boxes
    if boxes is not None:
      boxes = tf.cond(do_a_flip_random, lambda: _flip_boxes_left_right(boxes),
                      lambda: boxes)
      result.append(boxes)

    # flip masks
    if masks is not None:
      masks = tf.cond(do_a_flip_random, lambda: _flip_masks_left_right(masks),
                      lambda: masks)
      result.append(masks)

    # flip keypoints
    if keypoints is not None and keypoint_flip_permutation is not None:
      permutation = keypoint_flip_permutation
      keypoints = tf.cond(
          do_a_flip_random,
          lambda: keypoint_ops.flip_horizontal(keypoints, 0.5, permutation),
          lambda: keypoints)
      result.append(keypoints)

    return tuple(result)


def random_vertical_flip(image,
                         boxes=None,
                         masks=None,
                         keypoints=None,
                         keypoint_flip_permutation=None,
                         seed=None,
                         preprocess_vars_cache=None):
  """Randomly flips the image and detections vertically.

  The probability of flipping the image is 50%.

  Args:
    image: rank 3 float32 tensor with shape [height, width, channels].
    boxes: (optional) rank 2 float32 tensor with shape [N, 4]
           containing the bounding boxes.
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
           Each row is in the form of [ymin, xmin, ymax, xmax].
    masks: (optional) rank 3 float32 tensor with shape
           [num_instances, height, width] containing instance masks. The masks
           are of the same height, width as the input `image`.
    keypoints: (optional) rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]. The keypoints are in y-x
               normalized coordinates.
    keypoint_flip_permutation: rank 1 int32 tensor containing the keypoint flip
                               permutation.
    seed: random seed
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: image which is the same shape as input image.

    If boxes, masks, keypoints, and keypoint_flip_permutation are not None,
    the function also returns the following tensors.

    boxes: rank 2 float32 tensor containing the bounding boxes -> [N, 4].
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
    masks: rank 3 float32 tensor with shape [num_instances, height, width]
           containing instance masks.
    keypoints: rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]

  Raises:
    ValueError: if keypoints are provided but keypoint_flip_permutation is not.
  """

  def _flip_image(image):
    # flip image
    image_flipped = tf.image.flip_up_down(image)
    return image_flipped

  if keypoints is not None and keypoint_flip_permutation is None:
    raise ValueError(
        'keypoints are provided but keypoints_flip_permutation is not provided')

  with tf.name_scope('RandomVerticalFlip', values=[image, boxes]):
    result = []
    # random variable defining whether to do flip or not
    generator_func = functools.partial(tf.random_uniform, [], seed=seed)
    do_a_flip_random = _get_or_create_preprocess_rand_vars(
        generator_func, preprocessor_cache.PreprocessorCache.VERTICAL_FLIP,
        preprocess_vars_cache)
    do_a_flip_random = tf.greater(do_a_flip_random, 0.5)

    # flip image
    image = tf.cond(do_a_flip_random, lambda: _flip_image(image), lambda: image)
    result.append(image)

    # flip boxes
    if boxes is not None:
      boxes = tf.cond(do_a_flip_random, lambda: _flip_boxes_up_down(boxes),
                      lambda: boxes)
      result.append(boxes)

    # flip masks
    if masks is not None:
      masks = tf.cond(do_a_flip_random, lambda: _flip_masks_up_down(masks),
                      lambda: masks)
      result.append(masks)

    # flip keypoints
    if keypoints is not None and keypoint_flip_permutation is not None:
      permutation = keypoint_flip_permutation
      keypoints = tf.cond(
          do_a_flip_random,
          lambda: keypoint_ops.flip_vertical(keypoints, 0.5, permutation),
          lambda: keypoints)
      result.append(keypoints)

    return tuple(result)


def random_rotation90(image,
                      boxes=None,
                      masks=None,
                      keypoints=None,
                      seed=None,
                      preprocess_vars_cache=None):
  """Randomly rotates the image and detections 90 degrees counter-clockwise.

  The probability of rotating the image is 50%. This can be combined with
  random_horizontal_flip and random_vertical_flip to produce an output with a
  uniform distribution of the eight possible 90 degree rotation / reflection
  combinations.

  Args:
    image: rank 3 float32 tensor with shape [height, width, channels].
    boxes: (optional) rank 2 float32 tensor with shape [N, 4]
           containing the bounding boxes.
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
           Each row is in the form of [ymin, xmin, ymax, xmax].
    masks: (optional) rank 3 float32 tensor with shape
           [num_instances, height, width] containing instance masks. The masks
           are of the same height, width as the input `image`.
    keypoints: (optional) rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]. The keypoints are in y-x
               normalized coordinates.
    seed: random seed
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: image which is the same shape as input image.

    If boxes, masks, and keypoints, are not None,
    the function also returns the following tensors.

    boxes: rank 2 float32 tensor containing the bounding boxes -> [N, 4].
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
    masks: rank 3 float32 tensor with shape [num_instances, height, width]
           containing instance masks.
    keypoints: rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]
  """

  def _rot90_image(image):
    # flip image
    image_rotated = tf.image.rot90(image)
    return image_rotated

  with tf.name_scope('RandomRotation90', values=[image, boxes]):
    result = []

    # random variable defining whether to rotate by 90 degrees or not
    generator_func = functools.partial(tf.random_uniform, [], seed=seed)
    do_a_rot90_random = _get_or_create_preprocess_rand_vars(
        generator_func, preprocessor_cache.PreprocessorCache.ROTATION90,
        preprocess_vars_cache)
    do_a_rot90_random = tf.greater(do_a_rot90_random, 0.5)

    # flip image
    image = tf.cond(do_a_rot90_random, lambda: _rot90_image(image),
                    lambda: image)
    result.append(image)

    # flip boxes
    if boxes is not None:
      boxes = tf.cond(do_a_rot90_random, lambda: _rot90_boxes(boxes),
                      lambda: boxes)
      result.append(boxes)

    # flip masks
    if masks is not None:
      masks = tf.cond(do_a_rot90_random, lambda: _rot90_masks(masks),
                      lambda: masks)
      result.append(masks)

    # flip keypoints
    if keypoints is not None:
      keypoints = tf.cond(
          do_a_rot90_random,
          lambda: keypoint_ops.rot90(keypoints),
          lambda: keypoints)
      result.append(keypoints)

    return tuple(result)


def random_pixel_value_scale(image,
                             minval=0.9,
                             maxval=1.1,
                             seed=None,
                             preprocess_vars_cache=None):
  """Scales each value in the pixels of the image.

     This function scales each pixel independent of the other ones.
     For each value in image tensor, draws a random number between
     minval and maxval and multiples the values with them.

  Args:
    image: rank 3 float32 tensor contains 1 image -> [height, width, channels]
           with pixel values varying between [0, 255].
    minval: lower ratio of scaling pixel values.
    maxval: upper ratio of scaling pixel values.
    seed: random seed.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: image which is the same shape as input image.
  """
  with tf.name_scope('RandomPixelValueScale', values=[image]):
    generator_func = functools.partial(
        tf.random_uniform, tf.shape(image),
        minval=minval, maxval=maxval,
        dtype=tf.float32, seed=seed)
    color_coef = _get_or_create_preprocess_rand_vars(
        generator_func,
        preprocessor_cache.PreprocessorCache.PIXEL_VALUE_SCALE,
        preprocess_vars_cache)

    image = tf.multiply(image, color_coef)
    image = tf.clip_by_value(image, 0.0, 255.0)

  return image


def random_image_scale(image,
                       masks=None,
                       min_scale_ratio=0.5,
                       max_scale_ratio=2.0,
                       seed=None,
                       preprocess_vars_cache=None):
  """Scales the image size.

  Args:
    image: rank 3 float32 tensor contains 1 image -> [height, width, channels].
    masks: (optional) rank 3 float32 tensor containing masks with
      size [height, width, num_masks]. The value is set to None if there are no
      masks.
    min_scale_ratio: minimum scaling ratio.
    max_scale_ratio: maximum scaling ratio.
    seed: random seed.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: image which is the same rank as input image.
    masks: If masks is not none, resized masks which are the same rank as input
      masks will be returned.
  """
  with tf.name_scope('RandomImageScale', values=[image]):
    result = []
    image_shape = tf.shape(image)
    image_height = image_shape[0]
    image_width = image_shape[1]
    generator_func = functools.partial(
        tf.random_uniform, [],
        minval=min_scale_ratio, maxval=max_scale_ratio,
        dtype=tf.float32, seed=seed)
    size_coef = _get_or_create_preprocess_rand_vars(
        generator_func, preprocessor_cache.PreprocessorCache.IMAGE_SCALE,
        preprocess_vars_cache)

    image_newysize = tf.to_int32(
        tf.multiply(tf.to_float(image_height), size_coef))
    image_newxsize = tf.to_int32(
        tf.multiply(tf.to_float(image_width), size_coef))
    image = tf.image.resize_images(
        image, [image_newysize, image_newxsize], align_corners=True)
    result.append(image)
    if masks is not None:
      masks = tf.image.resize_images(
          masks, [image_newysize, image_newxsize],
          method=tf.image.ResizeMethod.NEAREST_NEIGHBOR,
          align_corners=True)
      result.append(masks)
    return tuple(result)


def random_rgb_to_gray(image,
                       probability=0.1,
                       seed=None,
                       preprocess_vars_cache=None):
  """Changes the image from RGB to Grayscale with the given probability.

  Args:
    image: rank 3 float32 tensor contains 1 image -> [height, width, channels]
           with pixel values varying between [0, 255].
    probability: the probability of returning a grayscale image.
            The probability should be a number between [0, 1].
    seed: random seed.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: image which is the same shape as input image.
  """
  def _image_to_gray(image):
    image_gray1 = _rgb_to_grayscale(image)
    image_gray3 = tf.image.grayscale_to_rgb(image_gray1)
    return image_gray3

  with tf.name_scope('RandomRGBtoGray', values=[image]):
    # random variable defining whether to change to grayscale or not
    generator_func = functools.partial(tf.random_uniform, [], seed=seed)
    do_gray_random = _get_or_create_preprocess_rand_vars(
        generator_func, preprocessor_cache.PreprocessorCache.RGB_TO_GRAY,
        preprocess_vars_cache)

    image = tf.cond(
        tf.greater(do_gray_random, probability), lambda: image,
        lambda: _image_to_gray(image))

  return image


def random_adjust_brightness(image,
                             max_delta=0.2,
                             seed=None,
                             preprocess_vars_cache=None):
  """Randomly adjusts brightness.

  Makes sure the output image is still between 0 and 255.

  Args:
    image: rank 3 float32 tensor contains 1 image -> [height, width, channels]
           with pixel values varying between [0, 255].
    max_delta: how much to change the brightness. A value between [0, 1).
    seed: random seed.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: image which is the same shape as input image.
    boxes: boxes which is the same shape as input boxes.
  """
  with tf.name_scope('RandomAdjustBrightness', values=[image]):
    generator_func = functools.partial(tf.random_uniform, [],
                                       -max_delta, max_delta, seed=seed)
    delta = _get_or_create_preprocess_rand_vars(
        generator_func,
        preprocessor_cache.PreprocessorCache.ADJUST_BRIGHTNESS,
        preprocess_vars_cache)

    image = tf.image.adjust_brightness(image / 255, delta) * 255
    image = tf.clip_by_value(image, clip_value_min=0.0, clip_value_max=255.0)
    return image


def random_adjust_contrast(image,
                           min_delta=0.8,
                           max_delta=1.25,
                           seed=None,
                           preprocess_vars_cache=None):
  """Randomly adjusts contrast.

  Makes sure the output image is still between 0 and 255.

  Args:
    image: rank 3 float32 tensor contains 1 image -> [height, width, channels]
           with pixel values varying between [0, 255].
    min_delta: see max_delta.
    max_delta: how much to change the contrast. Contrast will change with a
               value between min_delta and max_delta. This value will be
               multiplied to the current contrast of the image.
    seed: random seed.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: image which is the same shape as input image.
  """
  with tf.name_scope('RandomAdjustContrast', values=[image]):
    generator_func = functools.partial(tf.random_uniform, [],
                                       min_delta, max_delta, seed=seed)
    contrast_factor = _get_or_create_preprocess_rand_vars(
        generator_func,
        preprocessor_cache.PreprocessorCache.ADJUST_CONTRAST,
        preprocess_vars_cache)
    image = tf.image.adjust_contrast(image / 255, contrast_factor) * 255
    image = tf.clip_by_value(image, clip_value_min=0.0, clip_value_max=255.0)
    return image


def random_adjust_hue(image,
                      max_delta=0.02,
                      seed=None,
                      preprocess_vars_cache=None):
  """Randomly adjusts hue.

  Makes sure the output image is still between 0 and 255.

  Args:
    image: rank 3 float32 tensor contains 1 image -> [height, width, channels]
           with pixel values varying between [0, 255].
    max_delta: change hue randomly with a value between 0 and max_delta.
    seed: random seed.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: image which is the same shape as input image.
  """
  with tf.name_scope('RandomAdjustHue', values=[image]):
    generator_func = functools.partial(tf.random_uniform, [],
                                       -max_delta, max_delta, seed=seed)
    delta = _get_or_create_preprocess_rand_vars(
        generator_func, preprocessor_cache.PreprocessorCache.ADJUST_HUE,
        preprocess_vars_cache)
    image = tf.image.adjust_hue(image / 255, delta) * 255
    image = tf.clip_by_value(image, clip_value_min=0.0, clip_value_max=255.0)
    return image


def random_adjust_saturation(image,
                             min_delta=0.8,
                             max_delta=1.25,
                             seed=None,
                             preprocess_vars_cache=None):
  """Randomly adjusts saturation.

  Makes sure the output image is still between 0 and 255.

  Args:
    image: rank 3 float32 tensor contains 1 image -> [height, width, channels]
           with pixel values varying between [0, 255].
    min_delta: see max_delta.
    max_delta: how much to change the saturation. Saturation will change with a
               value between min_delta and max_delta. This value will be
               multiplied to the current saturation of the image.
    seed: random seed.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: image which is the same shape as input image.
  """
  with tf.name_scope('RandomAdjustSaturation', values=[image]):
    generator_func = functools.partial(tf.random_uniform, [],
                                       min_delta, max_delta, seed=seed)
    saturation_factor = _get_or_create_preprocess_rand_vars(
        generator_func,
        preprocessor_cache.PreprocessorCache.ADJUST_SATURATION,
        preprocess_vars_cache)
    image = tf.image.adjust_saturation(image / 255, saturation_factor) * 255
    image = tf.clip_by_value(image, clip_value_min=0.0, clip_value_max=255.0)
    return image


def random_distort_color(image, color_ordering=0, preprocess_vars_cache=None):
  """Randomly distorts color.

  Randomly distorts color using a combination of brightness, hue, contrast and
  saturation changes. Makes sure the output image is still between 0 and 255.

  Args:
    image: rank 3 float32 tensor contains 1 image -> [height, width, channels]
           with pixel values varying between [0, 255].
    color_ordering: Python int, a type of distortion (valid values: 0, 1).
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: image which is the same shape as input image.

  Raises:
    ValueError: if color_ordering is not in {0, 1}.
  """
  with tf.name_scope('RandomDistortColor', values=[image]):
    if color_ordering == 0:
      image = random_adjust_brightness(
          image, max_delta=32. / 255.,
          preprocess_vars_cache=preprocess_vars_cache)
      image = random_adjust_saturation(
          image, min_delta=0.5, max_delta=1.5,
          preprocess_vars_cache=preprocess_vars_cache)
      image = random_adjust_hue(
          image, max_delta=0.2,
          preprocess_vars_cache=preprocess_vars_cache)
      image = random_adjust_contrast(
          image, min_delta=0.5, max_delta=1.5,
          preprocess_vars_cache=preprocess_vars_cache)

    elif color_ordering == 1:
      image = random_adjust_brightness(
          image, max_delta=32. / 255.,
          preprocess_vars_cache=preprocess_vars_cache)
      image = random_adjust_contrast(
          image, min_delta=0.5, max_delta=1.5,
          preprocess_vars_cache=preprocess_vars_cache)
      image = random_adjust_saturation(
          image, min_delta=0.5, max_delta=1.5,
          preprocess_vars_cache=preprocess_vars_cache)
      image = random_adjust_hue(
          image, max_delta=0.2,
          preprocess_vars_cache=preprocess_vars_cache)
    else:
      raise ValueError('color_ordering must be in {0, 1}')
    return image


def random_jitter_boxes(boxes, ratio=0.05, seed=None):
  """Randomly jitter boxes in image.

  Args:
    boxes: rank 2 float32 tensor containing the bounding boxes -> [N, 4].
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
           Each row is in the form of [ymin, xmin, ymax, xmax].
    ratio: The ratio of the box width and height that the corners can jitter.
           For example if the width is 100 pixels and ratio is 0.05,
           the corners can jitter up to 5 pixels in the x direction.
    seed: random seed.

  Returns:
    boxes: boxes which is the same shape as input boxes.
  """
  def random_jitter_box(box, ratio, seed):
    """Randomly jitter box.

    Args:
      box: bounding box [1, 1, 4].
      ratio: max ratio between jittered box and original box,
      a number between [0, 0.5].
      seed: random seed.

    Returns:
      jittered_box: jittered box.
    """
    rand_numbers = tf.random_uniform(
        [1, 1, 4], minval=-ratio, maxval=ratio, dtype=tf.float32, seed=seed)
    box_width = tf.subtract(box[0, 0, 3], box[0, 0, 1])
    box_height = tf.subtract(box[0, 0, 2], box[0, 0, 0])
    hw_coefs = tf.stack([box_height, box_width, box_height, box_width])
    hw_rand_coefs = tf.multiply(hw_coefs, rand_numbers)
    jittered_box = tf.add(box, hw_rand_coefs)
    jittered_box = tf.clip_by_value(jittered_box, 0.0, 1.0)
    return jittered_box

  with tf.name_scope('RandomJitterBoxes', values=[boxes]):
    # boxes are [N, 4]. Lets first make them [N, 1, 1, 4]
    boxes_shape = tf.shape(boxes)
    boxes = tf.expand_dims(boxes, 1)
    boxes = tf.expand_dims(boxes, 2)

    distorted_boxes = tf.map_fn(
        lambda x: random_jitter_box(x, ratio, seed), boxes, dtype=tf.float32)

    distorted_boxes = tf.reshape(distorted_boxes, boxes_shape)

    return distorted_boxes


def _strict_random_crop_image(image,
                              boxes,
                              labels,
                              label_weights,
                              label_confidences=None,
                              multiclass_scores=None,
                              masks=None,
                              keypoints=None,
                              min_object_covered=1.0,
                              aspect_ratio_range=(0.75, 1.33),
                              area_range=(0.1, 1.0),
                              overlap_thresh=0.3,
                              clip_boxes=True,
                              preprocess_vars_cache=None):
  """Performs random crop.

  Note: Keypoint coordinates that are outside the crop will be set to NaN, which
  is consistent with the original keypoint encoding for non-existing keypoints.
  This function always crops the image and is supposed to be used by
  `random_crop_image` function which sometimes returns the image unchanged.

  Args:
    image: rank 3 float32 tensor containing 1 image -> [height, width, channels]
           with pixel values varying between [0, 1].
    boxes: rank 2 float32 tensor containing the bounding boxes with shape
           [num_instances, 4].
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
           Each row is in the form of [ymin, xmin, ymax, xmax].
    labels: rank 1 int32 tensor containing the object classes.
    label_weights: float32 tensor of shape [num_instances] representing the
      weight for each box.
    label_confidences: (optional) float32 tensor of shape [num_instances]
      representing the confidence for each box.
    multiclass_scores: (optional) float32 tensor of shape
      [num_instances, num_classes] representing the score for each box for each
      class.
    masks: (optional) rank 3 float32 tensor with shape
           [num_instances, height, width] containing instance masks. The masks
           are of the same height, width as the input `image`.
    keypoints: (optional) rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]. The keypoints are in y-x
               normalized coordinates.
    min_object_covered: the cropped image must cover at least this fraction of
                        at least one of the input bounding boxes.
    aspect_ratio_range: allowed range for aspect ratio of cropped image.
    area_range: allowed range for area ratio between cropped image and the
                original image.
    overlap_thresh: minimum overlap thresh with new cropped
                    image to keep the box.
    clip_boxes: whether to clip the boxes to the cropped image.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: image which is the same rank as input image.
    boxes: boxes which is the same rank as input boxes.
           Boxes are in normalized form.
    labels: new labels.

    If label_weights, multiclass_scores, masks, or keypoints is not None, the
    function also returns:
    label_weights: rank 1 float32 tensor with shape [num_instances].
    multiclass_scores: rank 2 float32 tensor with shape
                       [num_instances, num_classes]
    masks: rank 3 float32 tensor with shape [num_instances, height, width]
           containing instance masks.
    keypoints: rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]
  """
  with tf.name_scope('RandomCropImage', values=[image, boxes]):
    image_shape = tf.shape(image)

    # boxes are [N, 4]. Lets first make them [N, 1, 4].
    boxes_expanded = tf.expand_dims(
        tf.clip_by_value(
            boxes, clip_value_min=0.0, clip_value_max=1.0), 1)

    generator_func = functools.partial(
        tf.image.sample_distorted_bounding_box,
        image_shape,
        bounding_boxes=boxes_expanded,
        min_object_covered=min_object_covered,
        aspect_ratio_range=aspect_ratio_range,
        area_range=area_range,
        max_attempts=100,
        use_image_if_no_bounding_boxes=True)

    # for ssd cropping, each value of min_object_covered has its own
    # cached random variable
    sample_distorted_bounding_box = _get_or_create_preprocess_rand_vars(
        generator_func,
        preprocessor_cache.PreprocessorCache.STRICT_CROP_IMAGE,
        preprocess_vars_cache, key=min_object_covered)

    im_box_begin, im_box_size, im_box = sample_distorted_bounding_box

    new_image = tf.slice(image, im_box_begin, im_box_size)
    new_image.set_shape([None, None, image.get_shape()[2]])

    # [1, 4]
    im_box_rank2 = tf.squeeze(im_box, squeeze_dims=[0])
    # [4]
    im_box_rank1 = tf.squeeze(im_box)

    boxlist = box_list.BoxList(boxes)
    boxlist.add_field('labels', labels)

    if label_weights is not None:
      boxlist.add_field('label_weights', label_weights)

    if label_confidences is not None:
      boxlist.add_field('label_confidences', label_confidences)

    if multiclass_scores is not None:
      boxlist.add_field('multiclass_scores', multiclass_scores)

    im_boxlist = box_list.BoxList(im_box_rank2)

    # remove boxes that are outside cropped image
    boxlist, inside_window_ids = box_list_ops.prune_completely_outside_window(
        boxlist, im_box_rank1)

    # remove boxes that are outside image
    overlapping_boxlist, keep_ids = box_list_ops.prune_non_overlapping_boxes(
        boxlist, im_boxlist, overlap_thresh)

    # change the coordinate of the remaining boxes
    new_labels = overlapping_boxlist.get_field('labels')
    new_boxlist = box_list_ops.change_coordinate_frame(overlapping_boxlist,
                                                       im_box_rank1)
    new_boxes = new_boxlist.get()
    if clip_boxes:
      new_boxes = tf.clip_by_value(
          new_boxes, clip_value_min=0.0, clip_value_max=1.0)

    result = [new_image, new_boxes, new_labels]

    if label_weights is not None:
      new_label_weights = overlapping_boxlist.get_field('label_weights')
      result.append(new_label_weights)

    if label_confidences is not None:
      new_label_confidences = overlapping_boxlist.get_field('label_confidences')
      result.append(new_label_confidences)

    if multiclass_scores is not None:
      new_multiclass_scores = overlapping_boxlist.get_field('multiclass_scores')
      result.append(new_multiclass_scores)

    if masks is not None:
      masks_of_boxes_inside_window = tf.gather(masks, inside_window_ids)
      masks_of_boxes_completely_inside_window = tf.gather(
          masks_of_boxes_inside_window, keep_ids)
      masks_box_begin = [0, im_box_begin[0], im_box_begin[1]]
      masks_box_size = [-1, im_box_size[0], im_box_size[1]]
      new_masks = tf.slice(
          masks_of_boxes_completely_inside_window,
          masks_box_begin, masks_box_size)
      result.append(new_masks)

    if keypoints is not None:
      keypoints_of_boxes_inside_window = tf.gather(keypoints, inside_window_ids)
      keypoints_of_boxes_completely_inside_window = tf.gather(
          keypoints_of_boxes_inside_window, keep_ids)
      new_keypoints = keypoint_ops.change_coordinate_frame(
          keypoints_of_boxes_completely_inside_window, im_box_rank1)
      if clip_boxes:
        new_keypoints = keypoint_ops.prune_outside_window(new_keypoints,
                                                          [0.0, 0.0, 1.0, 1.0])
      result.append(new_keypoints)

    return tuple(result)


def random_crop_image(image,
                      boxes,
                      labels,
                      label_weights,
                      label_confidences=None,
                      multiclass_scores=None,
                      masks=None,
                      keypoints=None,
                      min_object_covered=1.0,
                      aspect_ratio_range=(0.75, 1.33),
                      area_range=(0.1, 1.0),
                      overlap_thresh=0.3,
                      clip_boxes=True,
                      random_coef=0.0,
                      seed=None,
                      preprocess_vars_cache=None):
  """Randomly crops the image.

  Given the input image and its bounding boxes, this op randomly
  crops a subimage.  Given a user-provided set of input constraints,
  the crop window is resampled until it satisfies these constraints.
  If within 100 trials it is unable to find a valid crop, the original
  image is returned. See the Args section for a description of the input
  constraints. Both input boxes and returned Boxes are in normalized
  form (e.g., lie in the unit square [0, 1]).
  This function will return the original image with probability random_coef.

  Note: Keypoint coordinates that are outside the crop will be set to NaN, which
  is consistent with the original keypoint encoding for non-existing keypoints.

  Args:
    image: rank 3 float32 tensor contains 1 image -> [height, width, channels]
           with pixel values varying between [0, 1].
    boxes: rank 2 float32 tensor containing the bounding boxes with shape
           [num_instances, 4].
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
           Each row is in the form of [ymin, xmin, ymax, xmax].
    labels: rank 1 int32 tensor containing the object classes.
    label_weights: float32 tensor of shape [num_instances] representing the
      weight for each box.
    label_confidences: (optional) float32 tensor of shape [num_instances].
      representing the confidence for each box.
    multiclass_scores: (optional) float32 tensor of shape
      [num_instances, num_classes] representing the score for each box for each
      class.
    masks: (optional) rank 3 float32 tensor with shape
           [num_instances, height, width] containing instance masks. The masks
           are of the same height, width as the input `image`.
    keypoints: (optional) rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]. The keypoints are in y-x
               normalized coordinates.
    min_object_covered: the cropped image must cover at least this fraction of
                        at least one of the input bounding boxes.
    aspect_ratio_range: allowed range for aspect ratio of cropped image.
    area_range: allowed range for area ratio between cropped image and the
                original image.
    overlap_thresh: minimum overlap thresh with new cropped
                    image to keep the box.
    clip_boxes: whether to clip the boxes to the cropped image.
    random_coef: a random coefficient that defines the chance of getting the
                 original image. If random_coef is 0, we will always get the
                 cropped image, and if it is 1.0, we will always get the
                 original image.
    seed: random seed.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: Image shape will be [new_height, new_width, channels].
    boxes: boxes which is the same rank as input boxes. Boxes are in normalized
           form.
    labels: new labels.

    If label_weights, multiclass_scores, masks, or keypoints is not None, the
    function also returns:
    label_weights: rank 1 float32 tensor with shape [num_instances].
    multiclass_scores: rank 2 float32 tensor with shape
                       [num_instances, num_classes]
    masks: rank 3 float32 tensor with shape [num_instances, height, width]
           containing instance masks.
    keypoints: rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]
  """

  def strict_random_crop_image_fn():
    return _strict_random_crop_image(
        image,
        boxes,
        labels,
        label_weights,
        label_confidences=label_confidences,
        multiclass_scores=multiclass_scores,
        masks=masks,
        keypoints=keypoints,
        min_object_covered=min_object_covered,
        aspect_ratio_range=aspect_ratio_range,
        area_range=area_range,
        overlap_thresh=overlap_thresh,
        clip_boxes=clip_boxes,
        preprocess_vars_cache=preprocess_vars_cache)

  # avoids tf.cond to make faster RCNN training on borg. See b/140057645.
  if random_coef < sys.float_info.min:
    result = strict_random_crop_image_fn()
  else:
    generator_func = functools.partial(tf.random_uniform, [], seed=seed)
    do_a_crop_random = _get_or_create_preprocess_rand_vars(
        generator_func, preprocessor_cache.PreprocessorCache.CROP_IMAGE,
        preprocess_vars_cache)
    do_a_crop_random = tf.greater(do_a_crop_random, random_coef)

    outputs = [image, boxes, labels]

    if label_weights is not None:
      outputs.append(label_weights)
    if label_confidences is not None:
      outputs.append(label_confidences)
    if multiclass_scores is not None:
      outputs.append(multiclass_scores)
    if masks is not None:
      outputs.append(masks)
    if keypoints is not None:
      outputs.append(keypoints)

    result = tf.cond(do_a_crop_random, strict_random_crop_image_fn,
                     lambda: tuple(outputs))
  return result


def random_pad_image(image,
                     boxes,
                     min_image_size=None,
                     max_image_size=None,
                     pad_color=None,
                     seed=None,
                     preprocess_vars_cache=None):
  """Randomly pads the image.

  This function randomly pads the image with zeros. The final size of the
  padded image will be between min_image_size and max_image_size.
  if min_image_size is smaller than the input image size, min_image_size will
  be set to the input image size. The same for max_image_size. The input image
  will be located at a uniformly random location inside the padded image.
  The relative location of the boxes to the original image will remain the same.

  Args:
    image: rank 3 float32 tensor containing 1 image -> [height, width, channels]
           with pixel values varying between [0, 1].
    boxes: rank 2 float32 tensor containing the bounding boxes -> [N, 4].
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
           Each row is in the form of [ymin, xmin, ymax, xmax].
    min_image_size: a tensor of size [min_height, min_width], type tf.int32.
                    If passed as None, will be set to image size
                    [height, width].
    max_image_size: a tensor of size [max_height, max_width], type tf.int32.
                    If passed as None, will be set to twice the
                    image [height * 2, width * 2].
    pad_color: padding color. A rank 1 tensor of [3] with dtype=tf.float32.
               if set as None, it will be set to average color of the input
               image.
    seed: random seed.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: Image shape will be [new_height, new_width, channels].
    boxes: boxes which is the same rank as input boxes. Boxes are in normalized
           form.
  """
  if pad_color is None:
    pad_color = tf.reduce_mean(image, axis=[0, 1])

  image_shape = tf.shape(image)
  image_height = image_shape[0]
  image_width = image_shape[1]

  if max_image_size is None:
    max_image_size = tf.stack([image_height * 2, image_width * 2])
  max_image_size = tf.maximum(max_image_size,
                              tf.stack([image_height, image_width]))

  if min_image_size is None:
    min_image_size = tf.stack([image_height, image_width])
  min_image_size = tf.maximum(min_image_size,
                              tf.stack([image_height, image_width]))

  target_height = tf.cond(
      max_image_size[0] > min_image_size[0],
      lambda: _random_integer(min_image_size[0], max_image_size[0], seed),
      lambda: max_image_size[0])

  target_width = tf.cond(
      max_image_size[1] > min_image_size[1],
      lambda: _random_integer(min_image_size[1], max_image_size[1], seed),
      lambda: max_image_size[1])

  offset_height = tf.cond(
      target_height > image_height,
      lambda: _random_integer(0, target_height - image_height, seed),
      lambda: tf.constant(0, dtype=tf.int32))

  offset_width = tf.cond(
      target_width > image_width,
      lambda: _random_integer(0, target_width - image_width, seed),
      lambda: tf.constant(0, dtype=tf.int32))

  gen_func = lambda: (target_height, target_width, offset_height, offset_width)
  params = _get_or_create_preprocess_rand_vars(
      gen_func, preprocessor_cache.PreprocessorCache.PAD_IMAGE,
      preprocess_vars_cache)
  target_height, target_width, offset_height, offset_width = params

  new_image = tf.image.pad_to_bounding_box(
      image,
      offset_height=offset_height,
      offset_width=offset_width,
      target_height=target_height,
      target_width=target_width)

  # Setting color of the padded pixels
  image_ones = tf.ones_like(image)
  image_ones_padded = tf.image.pad_to_bounding_box(
      image_ones,
      offset_height=offset_height,
      offset_width=offset_width,
      target_height=target_height,
      target_width=target_width)
  image_color_padded = (1.0 - image_ones_padded) * pad_color
  new_image += image_color_padded

  # setting boxes
  new_window = tf.to_float(
      tf.stack([
          -offset_height, -offset_width, target_height - offset_height,
          target_width - offset_width
      ]))
  new_window /= tf.to_float(
      tf.stack([image_height, image_width, image_height, image_width]))
  boxlist = box_list.BoxList(boxes)
  new_boxlist = box_list_ops.change_coordinate_frame(boxlist, new_window)
  new_boxes = new_boxlist.get()

  return new_image, new_boxes


def random_crop_pad_image(image,
                          boxes,
                          labels,
                          label_weights,
                          label_confidences=None,
                          multiclass_scores=None,
                          min_object_covered=1.0,
                          aspect_ratio_range=(0.75, 1.33),
                          area_range=(0.1, 1.0),
                          overlap_thresh=0.3,
                          clip_boxes=True,
                          random_coef=0.0,
                          min_padded_size_ratio=(1.0, 1.0),
                          max_padded_size_ratio=(2.0, 2.0),
                          pad_color=None,
                          seed=None,
                          preprocess_vars_cache=None):
  """Randomly crops and pads the image.

  Given an input image and its bounding boxes, this op first randomly crops
  the image and then randomly pads the image with background values. Parameters
  min_padded_size_ratio and max_padded_size_ratio, determine the range of the
  final output image size.  Specifically, the final image size will have a size
  in the range of min_padded_size_ratio * tf.shape(image) and
  max_padded_size_ratio * tf.shape(image). Note that these ratios are with
  respect to the size of the original image, so we can't capture the same
  effect easily by independently applying RandomCropImage
  followed by RandomPadImage.

  Args:
    image: rank 3 float32 tensor containing 1 image -> [height, width, channels]
           with pixel values varying between [0, 1].
    boxes: rank 2 float32 tensor containing the bounding boxes -> [N, 4].
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
           Each row is in the form of [ymin, xmin, ymax, xmax].
    labels: rank 1 int32 tensor containing the object classes.
    label_weights: rank 1 float32 containing the label weights.
    label_confidences: rank 1 float32 containing the label confidences.
    multiclass_scores: (optional) float32 tensor of shape
      [num_instances, num_classes] representing the score for each box for each
      class.
    min_object_covered: the cropped image must cover at least this fraction of
                        at least one of the input bounding boxes.
    aspect_ratio_range: allowed range for aspect ratio of cropped image.
    area_range: allowed range for area ratio between cropped image and the
                original image.
    overlap_thresh: minimum overlap thresh with new cropped
                    image to keep the box.
    clip_boxes: whether to clip the boxes to the cropped image.
    random_coef: a random coefficient that defines the chance of getting the
                 original image. If random_coef is 0, we will always get the
                 cropped image, and if it is 1.0, we will always get the
                 original image.
    min_padded_size_ratio: min ratio of padded image height and width to the
                           input image's height and width.
    max_padded_size_ratio: max ratio of padded image height and width to the
                           input image's height and width.
    pad_color: padding color. A rank 1 tensor of [3] with dtype=tf.float32.
               if set as None, it will be set to average color of the randomly
               cropped image.
    seed: random seed.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    padded_image: padded image.
    padded_boxes: boxes which is the same rank as input boxes. Boxes are in
                  normalized form.
    cropped_labels: cropped labels.
    if label_weights is not None also returns:
    cropped_label_weights: cropped label weights.
    if multiclass_scores is not None also returns:
    cropped_multiclass_scores: cropped_multiclass_scores.

  """
  image_size = tf.shape(image)
  image_height = image_size[0]
  image_width = image_size[1]
  result = random_crop_image(
      image=image,
      boxes=boxes,
      labels=labels,
      label_weights=label_weights,
      label_confidences=label_confidences,
      multiclass_scores=multiclass_scores,
      min_object_covered=min_object_covered,
      aspect_ratio_range=aspect_ratio_range,
      area_range=area_range,
      overlap_thresh=overlap_thresh,
      clip_boxes=clip_boxes,
      random_coef=random_coef,
      seed=seed,
      preprocess_vars_cache=preprocess_vars_cache)

  cropped_image, cropped_boxes, cropped_labels = result[:3]

  min_image_size = tf.to_int32(
      tf.to_float(tf.stack([image_height, image_width])) *
      min_padded_size_ratio)
  max_image_size = tf.to_int32(
      tf.to_float(tf.stack([image_height, image_width])) *
      max_padded_size_ratio)

  padded_image, padded_boxes = random_pad_image(
      cropped_image,
      cropped_boxes,
      min_image_size=min_image_size,
      max_image_size=max_image_size,
      pad_color=pad_color,
      seed=seed,
      preprocess_vars_cache=preprocess_vars_cache)

  cropped_padded_output = (padded_image, padded_boxes, cropped_labels)

  index = 3
  if label_weights is not None:
    cropped_label_weights = result[index]
    cropped_padded_output += (cropped_label_weights,)
    index += 1

  if label_confidences is not None:
    cropped_label_confidences = result[index]
    cropped_padded_output += (cropped_label_confidences,)
    index += 1

  if multiclass_scores is not None:
    cropped_multiclass_scores = result[index]
    cropped_padded_output += (cropped_multiclass_scores,)

  return cropped_padded_output


def random_crop_to_aspect_ratio(image,
                                boxes,
                                labels,
                                label_weights,
                                label_confidences=None,
                                multiclass_scores=None,
                                masks=None,
                                keypoints=None,
                                aspect_ratio=1.0,
                                overlap_thresh=0.3,
                                clip_boxes=True,
                                seed=None,
                                preprocess_vars_cache=None):
  """Randomly crops an image to the specified aspect ratio.

  Randomly crops the a portion of the image such that the crop is of the
  specified aspect ratio, and the crop is as large as possible. If the specified
  aspect ratio is larger than the aspect ratio of the image, this op will
  randomly remove rows from the top and bottom of the image. If the specified
  aspect ratio is less than the aspect ratio of the image, this op will randomly
  remove cols from the left and right of the image. If the specified aspect
  ratio is the same as the aspect ratio of the image, this op will return the
  image.

  Args:
    image: rank 3 float32 tensor contains 1 image -> [height, width, channels]
           with pixel values varying between [0, 1].
    boxes: rank 2 float32 tensor containing the bounding boxes -> [N, 4].
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
           Each row is in the form of [ymin, xmin, ymax, xmax].
    labels: rank 1 int32 tensor containing the object classes.
    label_weights: float32 tensor of shape [num_instances] representing the
      weight for each box.
    label_confidences: (optional) float32 tensor of shape [num_instances]
      representing the confidence for each box.
    multiclass_scores: (optional) float32 tensor of shape
      [num_instances, num_classes] representing the score for each box for each
      class.
    masks: (optional) rank 3 float32 tensor with shape
           [num_instances, height, width] containing instance masks. The masks
           are of the same height, width as the input `image`.
    keypoints: (optional) rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]. The keypoints are in y-x
               normalized coordinates.
    aspect_ratio: the aspect ratio of cropped image.
    overlap_thresh: minimum overlap thresh with new cropped
                    image to keep the box.
    clip_boxes: whether to clip the boxes to the cropped image.
    seed: random seed.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: image which is the same rank as input image.
    boxes: boxes which is the same rank as input boxes.
           Boxes are in normalized form.
    labels: new labels.

    If label_weights, masks, keypoints, or multiclass_scores is not None, the
    function also returns:
    label_weights: rank 1 float32 tensor with shape [num_instances].
    masks: rank 3 float32 tensor with shape [num_instances, height, width]
           containing instance masks.
    keypoints: rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]
    multiclass_scores: rank 2 float32 tensor with shape
                       [num_instances, num_classes]

  Raises:
    ValueError: If image is not a 3D tensor.
  """
  if len(image.get_shape()) != 3:
    raise ValueError('Image should be 3D tensor')

  with tf.name_scope('RandomCropToAspectRatio', values=[image]):
    image_shape = tf.shape(image)
    orig_height = image_shape[0]
    orig_width = image_shape[1]
    orig_aspect_ratio = tf.to_float(orig_width) / tf.to_float(orig_height)
    new_aspect_ratio = tf.constant(aspect_ratio, dtype=tf.float32)
    def target_height_fn():
      return tf.to_int32(tf.round(tf.to_float(orig_width) / new_aspect_ratio))

    target_height = tf.cond(orig_aspect_ratio >= new_aspect_ratio,
                            lambda: orig_height, target_height_fn)

    def target_width_fn():
      return tf.to_int32(tf.round(tf.to_float(orig_height) * new_aspect_ratio))

    target_width = tf.cond(orig_aspect_ratio <= new_aspect_ratio,
                           lambda: orig_width, target_width_fn)

    # either offset_height = 0 and offset_width is randomly chosen from
    # [0, offset_width - target_width), or else offset_width = 0 and
    # offset_height is randomly chosen from [0, offset_height - target_height)
    offset_height = _random_integer(0, orig_height - target_height + 1, seed)
    offset_width = _random_integer(0, orig_width - target_width + 1, seed)

    generator_func = lambda: (offset_height, offset_width)
    offset_height, offset_width = _get_or_create_preprocess_rand_vars(
        generator_func,
        preprocessor_cache.PreprocessorCache.CROP_TO_ASPECT_RATIO,
        preprocess_vars_cache)

    new_image = tf.image.crop_to_bounding_box(
        image, offset_height, offset_width, target_height, target_width)

    im_box = tf.stack([
        tf.to_float(offset_height) / tf.to_float(orig_height),
        tf.to_float(offset_width) / tf.to_float(orig_width),
        tf.to_float(offset_height + target_height) / tf.to_float(orig_height),
        tf.to_float(offset_width + target_width) / tf.to_float(orig_width)
    ])

    boxlist = box_list.BoxList(boxes)
    boxlist.add_field('labels', labels)

    boxlist.add_field('label_weights', label_weights)

    if label_confidences is not None:
      boxlist.add_field('label_confidences', label_confidences)

    if multiclass_scores is not None:
      boxlist.add_field('multiclass_scores', multiclass_scores)

    im_boxlist = box_list.BoxList(tf.expand_dims(im_box, 0))

    # remove boxes whose overlap with the image is less than overlap_thresh
    overlapping_boxlist, keep_ids = box_list_ops.prune_non_overlapping_boxes(
        boxlist, im_boxlist, overlap_thresh)

    # change the coordinate of the remaining boxes
    new_labels = overlapping_boxlist.get_field('labels')
    new_boxlist = box_list_ops.change_coordinate_frame(overlapping_boxlist,
                                                       im_box)
    if clip_boxes:
      new_boxlist = box_list_ops.clip_to_window(
          new_boxlist, tf.constant([0.0, 0.0, 1.0, 1.0], tf.float32))
    new_boxes = new_boxlist.get()

    result = [new_image, new_boxes, new_labels]

    new_label_weights = overlapping_boxlist.get_field('label_weights')
    result.append(new_label_weights)

    if label_confidences is not None:
      new_label_confidences = (
          overlapping_boxlist.get_field('label_confidences'))
      result.append(new_label_confidences)

    if multiclass_scores is not None:
      new_multiclass_scores = overlapping_boxlist.get_field('multiclass_scores')
      result.append(new_multiclass_scores)

    if masks is not None:
      masks_inside_window = tf.gather(masks, keep_ids)
      masks_box_begin = tf.stack([0, offset_height, offset_width])
      masks_box_size = tf.stack([-1, target_height, target_width])
      new_masks = tf.slice(masks_inside_window, masks_box_begin, masks_box_size)
      result.append(new_masks)

    if keypoints is not None:
      keypoints_inside_window = tf.gather(keypoints, keep_ids)
      new_keypoints = keypoint_ops.change_coordinate_frame(
          keypoints_inside_window, im_box)
      if clip_boxes:
        new_keypoints = keypoint_ops.prune_outside_window(new_keypoints,
                                                          [0.0, 0.0, 1.0, 1.0])
      result.append(new_keypoints)

    return tuple(result)


def random_pad_to_aspect_ratio(image,
                               boxes,
                               masks=None,
                               keypoints=None,
                               aspect_ratio=1.0,
                               min_padded_size_ratio=(1.0, 1.0),
                               max_padded_size_ratio=(2.0, 2.0),
                               seed=None,
                               preprocess_vars_cache=None):
  """Randomly zero pads an image to the specified aspect ratio.

  Pads the image so that the resulting image will have the specified aspect
  ratio without scaling less than the min_padded_size_ratio or more than the
  max_padded_size_ratio. If the min_padded_size_ratio or max_padded_size_ratio
  is lower than what is possible to maintain the aspect ratio, then this method
  will use the least padding to achieve the specified aspect ratio.

  Args:
    image: rank 3 float32 tensor contains 1 image -> [height, width, channels]
           with pixel values varying between [0, 1].
    boxes: rank 2 float32 tensor containing the bounding boxes -> [N, 4].
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
           Each row is in the form of [ymin, xmin, ymax, xmax].
    masks: (optional) rank 3 float32 tensor with shape
           [num_instances, height, width] containing instance masks. The masks
           are of the same height, width as the input `image`.
    keypoints: (optional) rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]. The keypoints are in y-x
               normalized coordinates.
    aspect_ratio: aspect ratio of the final image.
    min_padded_size_ratio: min ratio of padded image height and width to the
                           input image's height and width.
    max_padded_size_ratio: max ratio of padded image height and width to the
                           input image's height and width.
    seed: random seed.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: image which is the same rank as input image.
    boxes: boxes which is the same rank as input boxes.
           Boxes are in normalized form.
    labels: new labels.

    If masks, or keypoints is not None, the function also returns:
    masks: rank 3 float32 tensor with shape [num_instances, height, width]
           containing instance masks.
    keypoints: rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]

  Raises:
    ValueError: If image is not a 3D tensor.
  """
  if len(image.get_shape()) != 3:
    raise ValueError('Image should be 3D tensor')

  with tf.name_scope('RandomPadToAspectRatio', values=[image]):
    image_shape = tf.shape(image)
    image_height = tf.to_float(image_shape[0])
    image_width = tf.to_float(image_shape[1])
    image_aspect_ratio = image_width / image_height
    new_aspect_ratio = tf.constant(aspect_ratio, dtype=tf.float32)
    target_height = tf.cond(
        image_aspect_ratio <= new_aspect_ratio,
        lambda: image_height,
        lambda: image_width / new_aspect_ratio)
    target_width = tf.cond(
        image_aspect_ratio >= new_aspect_ratio,
        lambda: image_width,
        lambda: image_height * new_aspect_ratio)

    min_height = tf.maximum(
        min_padded_size_ratio[0] * image_height, target_height)
    min_width = tf.maximum(
        min_padded_size_ratio[1] * image_width, target_width)
    max_height = tf.maximum(
        max_padded_size_ratio[0] * image_height, target_height)
    max_width = tf.maximum(
        max_padded_size_ratio[1] * image_width, target_width)

    max_scale = tf.minimum(max_height / target_height, max_width / target_width)
    min_scale = tf.minimum(
        max_scale,
        tf.maximum(min_height / target_height, min_width / target_width))

    generator_func = functools.partial(tf.random_uniform, [],
                                       min_scale, max_scale, seed=seed)
    scale = _get_or_create_preprocess_rand_vars(
        generator_func,
        preprocessor_cache.PreprocessorCache.PAD_TO_ASPECT_RATIO,
        preprocess_vars_cache)

    target_height = tf.round(scale * target_height)
    target_width = tf.round(scale * target_width)

    new_image = tf.image.pad_to_bounding_box(
        image, 0, 0, tf.to_int32(target_height), tf.to_int32(target_width))

    im_box = tf.stack([
        0.0,
        0.0,
        target_height / image_height,
        target_width / image_width
    ])
    boxlist = box_list.BoxList(boxes)
    new_boxlist = box_list_ops.change_coordinate_frame(boxlist, im_box)
    new_boxes = new_boxlist.get()

    result = [new_image, new_boxes]

    if masks is not None:
      new_masks = tf.expand_dims(masks, -1)
      new_masks = tf.image.pad_to_bounding_box(new_masks, 0, 0,
                                               tf.to_int32(target_height),
                                               tf.to_int32(target_width))
      new_masks = tf.squeeze(new_masks, [-1])
      result.append(new_masks)

    if keypoints is not None:
      new_keypoints = keypoint_ops.change_coordinate_frame(keypoints, im_box)
      result.append(new_keypoints)

    return tuple(result)


def random_black_patches(image,
                         max_black_patches=10,
                         probability=0.5,
                         size_to_image_ratio=0.1,
                         random_seed=None,
                         preprocess_vars_cache=None):
  """Randomly adds some black patches to the image.

  This op adds up to max_black_patches square black patches of a fixed size
  to the image where size is specified via the size_to_image_ratio parameter.

  Args:
    image: rank 3 float32 tensor containing 1 image -> [height, width, channels]
           with pixel values varying between [0, 1].
    max_black_patches: number of times that the function tries to add a
                       black box to the image.
    probability: at each try, what is the chance of adding a box.
    size_to_image_ratio: Determines the ratio of the size of the black patches
                         to the size of the image.
                         box_size = size_to_image_ratio *
                                    min(image_width, image_height)
    random_seed: random seed.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image
  """
  def add_black_patch_to_image(image, idx):
    """Function for adding one patch to the image.

    Args:
      image: image
      idx: counter for number of patches that could have been added

    Returns:
      image with a randomly added black box
    """
    image_shape = tf.shape(image)
    image_height = image_shape[0]
    image_width = image_shape[1]
    box_size = tf.to_int32(
        tf.multiply(
            tf.minimum(tf.to_float(image_height), tf.to_float(image_width)),
            size_to_image_ratio))

    generator_func = functools.partial(tf.random_uniform, [], minval=0.0,
                                       maxval=(1.0 - size_to_image_ratio),
                                       seed=random_seed)
    normalized_y_min = _get_or_create_preprocess_rand_vars(
        generator_func,
        preprocessor_cache.PreprocessorCache.ADD_BLACK_PATCH,
        preprocess_vars_cache, key=str(idx) + 'y')
    normalized_x_min = _get_or_create_preprocess_rand_vars(
        generator_func,
        preprocessor_cache.PreprocessorCache.ADD_BLACK_PATCH,
        preprocess_vars_cache, key=str(idx) + 'x')

    y_min = tf.to_int32(normalized_y_min * tf.to_float(image_height))
    x_min = tf.to_int32(normalized_x_min * tf.to_float(image_width))
    black_box = tf.ones([box_size, box_size, 3], dtype=tf.float32)
    mask = 1.0 - tf.image.pad_to_bounding_box(black_box, y_min, x_min,
                                              image_height, image_width)
    image = tf.multiply(image, mask)
    return image

  with tf.name_scope('RandomBlackPatchInImage', values=[image]):
    for idx in range(max_black_patches):
      generator_func = functools.partial(tf.random_uniform, [],
                                         minval=0.0, maxval=1.0,
                                         dtype=tf.float32, seed=random_seed)
      random_prob = _get_or_create_preprocess_rand_vars(
          generator_func,
          preprocessor_cache.PreprocessorCache.BLACK_PATCHES,
          preprocess_vars_cache, key=idx)
      image = tf.cond(
          tf.greater(random_prob, probability), lambda: image,
          functools.partial(add_black_patch_to_image, image=image, idx=idx))
    return image


def image_to_float(image):
  """Used in Faster R-CNN. Casts image pixel values to float.

  Args:
    image: input image which might be in tf.uint8 or sth else format

  Returns:
    image: image in tf.float32 format.
  """
  with tf.name_scope('ImageToFloat', values=[image]):
    image = tf.to_float(image)
    return image


def random_resize_method(image, target_size, preprocess_vars_cache=None):
  """Uses a random resize method to resize the image to target size.

  Args:
    image: a rank 3 tensor.
    target_size: a list of [target_height, target_width]
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    resized image.
  """

  resized_image = _apply_with_random_selector(
      image,
      lambda x, method: tf.image.resize_images(x, target_size, method),
      num_cases=4,
      preprocess_vars_cache=preprocess_vars_cache,
      key=preprocessor_cache.PreprocessorCache.RESIZE_METHOD)

  return resized_image


def _compute_new_static_size(image, min_dimension, max_dimension):
  """Compute new static shape for resize_to_range method."""
  image_shape = image.get_shape().as_list()
  orig_height = image_shape[0]
  orig_width = image_shape[1]
  num_channels = image_shape[2]
  orig_min_dim = min(orig_height, orig_width)
  # Calculates the larger of the possible sizes
  large_scale_factor = min_dimension / float(orig_min_dim)
  # Scaling orig_(height|width) by large_scale_factor will make the smaller
  # dimension equal to min_dimension, save for floating point rounding errors.
  # For reasonably-sized images, taking the nearest integer will reliably
  # eliminate this error.
  large_height = int(round(orig_height * large_scale_factor))
  large_width = int(round(orig_width * large_scale_factor))
  large_size = [large_height, large_width]
  if max_dimension:
    # Calculates the smaller of the possible sizes, use that if the larger
    # is too big.
    orig_max_dim = max(orig_height, orig_width)
    small_scale_factor = max_dimension / float(orig_max_dim)
    # Scaling orig_(height|width) by small_scale_factor will make the larger
    # dimension equal to max_dimension, save for floating point rounding
    # errors. For reasonably-sized images, taking the nearest integer will
    # reliably eliminate this error.
    small_height = int(round(orig_height * small_scale_factor))
    small_width = int(round(orig_width * small_scale_factor))
    small_size = [small_height, small_width]
    new_size = large_size
    if max(large_size) > max_dimension:
      new_size = small_size
  else:
    new_size = large_size
  return tf.constant(new_size + [num_channels])


def _compute_new_dynamic_size(image, min_dimension, max_dimension):
  """Compute new dynamic shape for resize_to_range method."""
  image_shape = tf.shape(image)
  orig_height = tf.to_float(image_shape[0])
  orig_width = tf.to_float(image_shape[1])
  num_channels = image_shape[2]
  orig_min_dim = tf.minimum(orig_height, orig_width)
  # Calculates the larger of the possible sizes
  min_dimension = tf.constant(min_dimension, dtype=tf.float32)
  large_scale_factor = min_dimension / orig_min_dim
  # Scaling orig_(height|width) by large_scale_factor will make the smaller
  # dimension equal to min_dimension, save for floating point rounding errors.
  # For reasonably-sized images, taking the nearest integer will reliably
  # eliminate this error.
  large_height = tf.to_int32(tf.round(orig_height * large_scale_factor))
  large_width = tf.to_int32(tf.round(orig_width * large_scale_factor))
  large_size = tf.stack([large_height, large_width])
  if max_dimension:
    # Calculates the smaller of the possible sizes, use that if the larger
    # is too big.
    orig_max_dim = tf.maximum(orig_height, orig_width)
    max_dimension = tf.constant(max_dimension, dtype=tf.float32)
    small_scale_factor = max_dimension / orig_max_dim
    # Scaling orig_(height|width) by small_scale_factor will make the larger
    # dimension equal to max_dimension, save for floating point rounding
    # errors. For reasonably-sized images, taking the nearest integer will
    # reliably eliminate this error.
    small_height = tf.to_int32(tf.round(orig_height * small_scale_factor))
    small_width = tf.to_int32(tf.round(orig_width * small_scale_factor))
    small_size = tf.stack([small_height, small_width])
    new_size = tf.cond(
        tf.to_float(tf.reduce_max(large_size)) > max_dimension,
        lambda: small_size, lambda: large_size)
  else:
    new_size = large_size
  return tf.stack(tf.unstack(new_size) + [num_channels])


def resize_to_range(image,
                    masks=None,
                    min_dimension=None,
                    max_dimension=None,
                    method=tf.image.ResizeMethod.BILINEAR,
                    align_corners=False,
                    pad_to_max_dimension=False,
                    per_channel_pad_value=(0, 0, 0)):
  """Resizes an image so its dimensions are within the provided value.

  The output size can be described by two cases:
  1. If the image can be rescaled so its minimum dimension is equal to the
     provided value without the other dimension exceeding max_dimension,
     then do so.
  2. Otherwise, resize so the largest dimension is equal to max_dimension.

  Args:
    image: A 3D tensor of shape [height, width, channels]
    masks: (optional) rank 3 float32 tensor with shape
           [num_instances, height, width] containing instance masks.
    min_dimension: (optional) (scalar) desired size of the smaller image
                   dimension.
    max_dimension: (optional) (scalar) maximum allowed size
                   of the larger image dimension.
    method: (optional) interpolation method used in resizing. Defaults to
            BILINEAR.
    align_corners: bool. If true, exactly align all 4 corners of the input
                   and output. Defaults to False.
    pad_to_max_dimension: Whether to resize the image and pad it with zeros
      so the resulting image is of the spatial size
      [max_dimension, max_dimension]. If masks are included they are padded
      similarly.
    per_channel_pad_value: A tuple of per-channel scalar value to use for
      padding. By default pads zeros.

  Returns:
    Note that the position of the resized_image_shape changes based on whether
    masks are present.
    resized_image: A 3D tensor of shape [new_height, new_width, channels],
      where the image has been resized (with bilinear interpolation) so that
      min(new_height, new_width) == min_dimension or
      max(new_height, new_width) == max_dimension.
    resized_masks: If masks is not None, also outputs masks. A 3D tensor of
      shape [num_instances, new_height, new_width].
    resized_image_shape: A 1D tensor of shape [3] containing shape of the
      resized image.

  Raises:
    ValueError: if the image is not a 3D tensor.
  """
  if len(image.get_shape()) != 3:
    raise ValueError('Image should be 3D tensor')

  with tf.name_scope('ResizeToRange', values=[image, min_dimension]):
    if image.get_shape().is_fully_defined():
      new_size = _compute_new_static_size(image, min_dimension, max_dimension)
    else:
      new_size = _compute_new_dynamic_size(image, min_dimension, max_dimension)
    new_image = tf.image.resize_images(
        image, new_size[:-1], method=method, align_corners=align_corners)

    if pad_to_max_dimension:
      channels = tf.unstack(new_image, axis=2)
      if len(channels) != len(per_channel_pad_value):
        raise ValueError('Number of channels must be equal to the length of '
                         'per-channel pad value.')
      new_image = tf.stack(
          [
              tf.pad(
                  channels[i], [[0, max_dimension - new_size[0]],
                                [0, max_dimension - new_size[1]]],
                  constant_values=per_channel_pad_value[i])
              for i in range(len(channels))
          ],
          axis=2)
      new_image.set_shape([max_dimension, max_dimension, 3])

    result = [new_image]
    if masks is not None:
      new_masks = tf.expand_dims(masks, 3)
      new_masks = tf.image.resize_images(
          new_masks,
          new_size[:-1],
          method=tf.image.ResizeMethod.NEAREST_NEIGHBOR,
          align_corners=align_corners)
      if pad_to_max_dimension:
        new_masks = tf.image.pad_to_bounding_box(
            new_masks, 0, 0, max_dimension, max_dimension)
      new_masks = tf.squeeze(new_masks, 3)
      result.append(new_masks)

    result.append(new_size)
    return result


# TODO(alirezafathi): Make sure the static shapes are preserved.
def resize_to_min_dimension(image, masks=None, min_dimension=600):
  """Resizes image and masks given the min size maintaining the aspect ratio.

  If one of the image dimensions is smaller that min_dimension, it will scale
  the image such that its smallest dimension is equal to min_dimension.
  Otherwise, will keep the image size as is.

  Args:
    image: a tensor of size [height, width, channels].
    masks: (optional) a tensors of size [num_instances, height, width].
    min_dimension: minimum image dimension.

  Returns:
    Note that the position of the resized_image_shape changes based on whether
    masks are present.
    resized_image: A tensor of size [new_height, new_width, channels].
    resized_masks: If masks is not None, also outputs masks. A 3D tensor of
      shape [num_instances, new_height, new_width]
    resized_image_shape: A 1D tensor of shape [3] containing the shape of the
      resized image.

  Raises:
    ValueError: if the image is not a 3D tensor.
  """
  if len(image.get_shape()) != 3:
    raise ValueError('Image should be 3D tensor')

  with tf.name_scope('ResizeGivenMinDimension', values=[image, min_dimension]):
    image_height = tf.shape(image)[0]
    image_width = tf.shape(image)[1]
    num_channels = tf.shape(image)[2]
    min_image_dimension = tf.minimum(image_height, image_width)
    min_target_dimension = tf.maximum(min_image_dimension, min_dimension)
    target_ratio = tf.to_float(min_target_dimension) / tf.to_float(
        min_image_dimension)
    target_height = tf.to_int32(tf.to_float(image_height) * target_ratio)
    target_width = tf.to_int32(tf.to_float(image_width) * target_ratio)
    image = tf.image.resize_bilinear(
        tf.expand_dims(image, axis=0),
        size=[target_height, target_width],
        align_corners=True)
    result = [tf.squeeze(image, axis=0)]

    if masks is not None:
      masks = tf.image.resize_nearest_neighbor(
          tf.expand_dims(masks, axis=3),
          size=[target_height, target_width],
          align_corners=True)
      result.append(tf.squeeze(masks, axis=3))

    result.append(tf.stack([target_height, target_width, num_channels]))
    return result


def scale_boxes_to_pixel_coordinates(image, boxes, keypoints=None):
  """Scales boxes from normalized to pixel coordinates.

  Args:
    image: A 3D float32 tensor of shape [height, width, channels].
    boxes: A 2D float32 tensor of shape [num_boxes, 4] containing the bounding
      boxes in normalized coordinates. Each row is of the form
      [ymin, xmin, ymax, xmax].
    keypoints: (optional) rank 3 float32 tensor with shape
      [num_instances, num_keypoints, 2]. The keypoints are in y-x normalized
      coordinates.

  Returns:
    image: unchanged input image.
    scaled_boxes: a 2D float32 tensor of shape [num_boxes, 4] containing the
      bounding boxes in pixel coordinates.
    scaled_keypoints: a 3D float32 tensor with shape
      [num_instances, num_keypoints, 2] containing the keypoints in pixel
      coordinates.
  """
  boxlist = box_list.BoxList(boxes)
  image_height = tf.shape(image)[0]
  image_width = tf.shape(image)[1]
  scaled_boxes = box_list_ops.scale(boxlist, image_height, image_width).get()
  result = [image, scaled_boxes]
  if keypoints is not None:
    scaled_keypoints = keypoint_ops.scale(keypoints, image_height, image_width)
    result.append(scaled_keypoints)
  return tuple(result)


# TODO(alirezafathi): Investigate if instead the function should return None if
# masks is None.
# pylint: disable=g-doc-return-or-yield
def resize_image(image,
                 masks=None,
                 new_height=600,
                 new_width=1024,
                 method=tf.image.ResizeMethod.BILINEAR,
                 align_corners=False):
  """Resizes images to the given height and width.

  Args:
    image: A 3D tensor of shape [height, width, channels]
    masks: (optional) rank 3 float32 tensor with shape
           [num_instances, height, width] containing instance masks.
    new_height: (optional) (scalar) desired height of the image.
    new_width: (optional) (scalar) desired width of the image.
    method: (optional) interpolation method used in resizing. Defaults to
            BILINEAR.
    align_corners: bool. If true, exactly align all 4 corners of the input
                   and output. Defaults to False.

  Returns:
    Note that the position of the resized_image_shape changes based on whether
    masks are present.
    resized_image: A tensor of size [new_height, new_width, channels].
    resized_masks: If masks is not None, also outputs masks. A 3D tensor of
      shape [num_instances, new_height, new_width]
    resized_image_shape: A 1D tensor of shape [3] containing the shape of the
      resized image.
  """
  with tf.name_scope(
      'ResizeImage',
      values=[image, new_height, new_width, method, align_corners]):
    new_image = tf.image.resize_images(
        image, tf.stack([new_height, new_width]),
        method=method,
        align_corners=align_corners)
    image_shape = shape_utils.combined_static_and_dynamic_shape(image)
    result = [new_image]
    if masks is not None:
      num_instances = tf.shape(masks)[0]
      new_size = tf.stack([new_height, new_width])
      def resize_masks_branch():
        new_masks = tf.expand_dims(masks, 3)
        new_masks = tf.image.resize_nearest_neighbor(
            new_masks, new_size, align_corners=align_corners)
        new_masks = tf.squeeze(new_masks, axis=3)
        return new_masks

      def reshape_masks_branch():
        # The shape function will be computed for both branches of the
        # condition, regardless of which branch is actually taken. Make sure
        # that we don't trigger an assertion in the shape function when trying
        # to reshape a non empty tensor into an empty one.
        new_masks = tf.reshape(masks, [-1, new_size[0], new_size[1]])
        return new_masks

      masks = tf.cond(num_instances > 0, resize_masks_branch,
                      reshape_masks_branch)
      result.append(masks)

    result.append(tf.stack([new_height, new_width, image_shape[2]]))
    return result


def subtract_channel_mean(image, means=None):
  """Normalizes an image by subtracting a mean from each channel.

  Args:
    image: A 3D tensor of shape [height, width, channels]
    means: float list containing a mean for each channel
  Returns:
    normalized_images: a tensor of shape [height, width, channels]
  Raises:
    ValueError: if images is not a 4D tensor or if the number of means is not
      equal to the number of channels.
  """
  with tf.name_scope('SubtractChannelMean', values=[image, means]):
    if len(image.get_shape()) != 3:
      raise ValueError('Input must be of size [height, width, channels]')
    if len(means) != image.get_shape()[-1]:
      raise ValueError('len(means) must match the number of channels')
    return image - [[means]]


def one_hot_encoding(labels, num_classes=None):
  """One-hot encodes the multiclass labels.

  Example usage:
    labels = tf.constant([1, 4], dtype=tf.int32)
    one_hot = OneHotEncoding(labels, num_classes=5)
    one_hot.eval()    # evaluates to [0, 1, 0, 0, 1]

  Args:
    labels: A tensor of shape [None] corresponding to the labels.
    num_classes: Number of classes in the dataset.
  Returns:
    onehot_labels: a tensor of shape [num_classes] corresponding to the one hot
      encoding of the labels.
  Raises:
    ValueError: if num_classes is not specified.
  """
  with tf.name_scope('OneHotEncoding', values=[labels]):
    if num_classes is None:
      raise ValueError('num_classes must be specified')

    labels = tf.one_hot(labels, num_classes, 1, 0)
    return tf.reduce_max(labels, 0)


def rgb_to_gray(image):
  """Converts a 3 channel RGB image to a 1 channel grayscale image.

  Args:
    image: Rank 3 float32 tensor containing 1 image -> [height, width, 3]
           with pixel values varying between [0, 1].

  Returns:
    image: A single channel grayscale image -> [image, height, 1].
  """
  return _rgb_to_grayscale(image)


def ssd_random_crop(image,
                    boxes,
                    labels,
                    label_weights,
                    label_confidences=None,
                    multiclass_scores=None,
                    masks=None,
                    keypoints=None,
                    min_object_covered=(0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0),
                    aspect_ratio_range=((0.5, 2.0),) * 7,
                    area_range=((0.1, 1.0),) * 7,
                    overlap_thresh=(0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0),
                    clip_boxes=(True,) * 7,
                    random_coef=(0.15,) * 7,
                    seed=None,
                    preprocess_vars_cache=None):
  """Random crop preprocessing with default parameters as in SSD paper.

  Liu et al., SSD: Single shot multibox detector.
  For further information on random crop preprocessing refer to RandomCrop
  function above.

  Args:
    image: rank 3 float32 tensor contains 1 image -> [height, width, channels]
           with pixel values varying between [0, 1].
    boxes: rank 2 float32 tensor containing the bounding boxes -> [N, 4].
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
           Each row is in the form of [ymin, xmin, ymax, xmax].
    labels: rank 1 int32 tensor containing the object classes.
    label_weights: rank 1 float32 tensor containing the weights.
    label_confidences: rank 1 float32 tensor containing the confidences.
    multiclass_scores: (optional) float32 tensor of shape
      [num_instances, num_classes] representing the score for each box for each
      class.
    masks: (optional) rank 3 float32 tensor with shape
           [num_instances, height, width] containing instance masks. The masks
           are of the same height, width as the input `image`.
    keypoints: (optional) rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]. The keypoints are in y-x
               normalized coordinates.
    min_object_covered: the cropped image must cover at least this fraction of
                        at least one of the input bounding boxes.
    aspect_ratio_range: allowed range for aspect ratio of cropped image.
    area_range: allowed range for area ratio between cropped image and the
                original image.
    overlap_thresh: minimum overlap thresh with new cropped
                    image to keep the box.
    clip_boxes: whether to clip the boxes to the cropped image.
    random_coef: a random coefficient that defines the chance of getting the
                 original image. If random_coef is 0, we will always get the
                 cropped image, and if it is 1.0, we will always get the
                 original image.
    seed: random seed.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: image which is the same rank as input image.
    boxes: boxes which is the same rank as input boxes.
           Boxes are in normalized form.
    labels: new labels.

    If label_weights, multiclass_scores, masks, or keypoints  is not None, the
    function also returns:
    label_weights: rank 1 float32 tensor with shape [num_instances].
    multiclass_scores: rank 2 float32 tensor with shape
                       [num_instances, num_classes]
    masks: rank 3 float32 tensor with shape [num_instances, height, width]
           containing instance masks.
    keypoints: rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]
  """

  def random_crop_selector(selected_result, index):
    """Applies random_crop_image to selected result.

    Args:
      selected_result: A tuple containing image, boxes, labels, keypoints (if
                       not None), and masks (if not None).
      index: The index that was randomly selected.

    Returns: A tuple containing image, boxes, labels, keypoints (if not None),
             and masks (if not None).
    """

    i = 3
    image, boxes, labels = selected_result[:i]
    selected_label_weights = None
    selected_label_confidences = None
    selected_multiclass_scores = None
    selected_masks = None
    selected_keypoints = None
    if label_weights is not None:
      selected_label_weights = selected_result[i]
      i += 1
    if label_confidences is not None:
      selected_label_confidences = selected_result[i]
      i += 1
    if multiclass_scores is not None:
      selected_multiclass_scores = selected_result[i]
      i += 1
    if masks is not None:
      selected_masks = selected_result[i]
      i += 1
    if keypoints is not None:
      selected_keypoints = selected_result[i]

    return random_crop_image(
        image=image,
        boxes=boxes,
        labels=labels,
        label_weights=selected_label_weights,
        label_confidences=selected_label_confidences,
        multiclass_scores=selected_multiclass_scores,
        masks=selected_masks,
        keypoints=selected_keypoints,
        min_object_covered=min_object_covered[index],
        aspect_ratio_range=aspect_ratio_range[index],
        area_range=area_range[index],
        overlap_thresh=overlap_thresh[index],
        clip_boxes=clip_boxes[index],
        random_coef=random_coef[index],
        seed=seed,
        preprocess_vars_cache=preprocess_vars_cache)

  result = _apply_with_random_selector_tuples(
      tuple(
          t for t in (image, boxes, labels, label_weights, label_confidences,
                      multiclass_scores, masks, keypoints) if t is not None),
      random_crop_selector,
      num_cases=len(min_object_covered),
      preprocess_vars_cache=preprocess_vars_cache,
      key=preprocessor_cache.PreprocessorCache.SSD_CROP_SELECTOR_ID)
  return result


def ssd_random_crop_pad(image,
                        boxes,
                        labels,
                        label_weights,
                        label_confidences=None,
                        multiclass_scores=None,
                        min_object_covered=(0.1, 0.3, 0.5, 0.7, 0.9, 1.0),
                        aspect_ratio_range=((0.5, 2.0),) * 6,
                        area_range=((0.1, 1.0),) * 6,
                        overlap_thresh=(0.1, 0.3, 0.5, 0.7, 0.9, 1.0),
                        clip_boxes=(True,) * 6,
                        random_coef=(0.15,) * 6,
                        min_padded_size_ratio=((1.0, 1.0),) * 6,
                        max_padded_size_ratio=((2.0, 2.0),) * 6,
                        pad_color=(None,) * 6,
                        seed=None,
                        preprocess_vars_cache=None):
  """Random crop preprocessing with default parameters as in SSD paper.

  Liu et al., SSD: Single shot multibox detector.
  For further information on random crop preprocessing refer to RandomCrop
  function above.

  Args:
    image: rank 3 float32 tensor containing 1 image -> [height, width, channels]
           with pixel values varying between [0, 1].
    boxes: rank 2 float32 tensor containing the bounding boxes -> [N, 4].
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
           Each row is in the form of [ymin, xmin, ymax, xmax].
    labels: rank 1 int32 tensor containing the object classes.
    label_weights: float32 tensor of shape [num_instances] representing the
      weight for each box.
    label_confidences: float32 tensor of shape [num_instances] representing the
      confidences for each box.
    multiclass_scores: (optional) float32 tensor of shape
      [num_instances, num_classes] representing the score for each box for each
      class.
    min_object_covered: the cropped image must cover at least this fraction of
                        at least one of the input bounding boxes.
    aspect_ratio_range: allowed range for aspect ratio of cropped image.
    area_range: allowed range for area ratio between cropped image and the
                original image.
    overlap_thresh: minimum overlap thresh with new cropped
                    image to keep the box.
    clip_boxes: whether to clip the boxes to the cropped image.
    random_coef: a random coefficient that defines the chance of getting the
                 original image. If random_coef is 0, we will always get the
                 cropped image, and if it is 1.0, we will always get the
                 original image.
    min_padded_size_ratio: min ratio of padded image height and width to the
                           input image's height and width.
    max_padded_size_ratio: max ratio of padded image height and width to the
                           input image's height and width.
    pad_color: padding color. A rank 1 tensor of [3] with dtype=tf.float32.
               if set as None, it will be set to average color of the randomly
               cropped image.
    seed: random seed.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: Image shape will be [new_height, new_width, channels].
    boxes: boxes which is the same rank as input boxes. Boxes are in normalized
           form.
    new_labels: new labels.
    new_label_weights: new label weights.
  """

  def random_crop_pad_selector(image_boxes_labels, index):
    """Random crop preprocessing helper."""
    i = 3
    image, boxes, labels = image_boxes_labels[:i]
    selected_label_weights = None
    selected_label_confidences = None
    selected_multiclass_scores = None
    if label_weights is not None:
      selected_label_weights = image_boxes_labels[i]
      i += 1
    if label_confidences is not None:
      selected_label_confidences = image_boxes_labels[i]
      i += 1
    if multiclass_scores is not None:
      selected_multiclass_scores = image_boxes_labels[i]

    return random_crop_pad_image(
        image,
        boxes,
        labels,
        label_weights=selected_label_weights,
        label_confidences=selected_label_confidences,
        multiclass_scores=selected_multiclass_scores,
        min_object_covered=min_object_covered[index],
        aspect_ratio_range=aspect_ratio_range[index],
        area_range=area_range[index],
        overlap_thresh=overlap_thresh[index],
        clip_boxes=clip_boxes[index],
        random_coef=random_coef[index],
        min_padded_size_ratio=min_padded_size_ratio[index],
        max_padded_size_ratio=max_padded_size_ratio[index],
        pad_color=pad_color[index],
        seed=seed,
        preprocess_vars_cache=preprocess_vars_cache)

  return _apply_with_random_selector_tuples(
      tuple(t for t in (image, boxes, labels, label_weights, label_confidences,
                        multiclass_scores) if t is not None),
      random_crop_pad_selector,
      num_cases=len(min_object_covered),
      preprocess_vars_cache=preprocess_vars_cache,
      key=preprocessor_cache.PreprocessorCache.SSD_CROP_PAD_SELECTOR_ID)


def ssd_random_crop_fixed_aspect_ratio(
    image,
    boxes,
    labels,
    label_weights,
    label_confidences=None,
    multiclass_scores=None,
    masks=None,
    keypoints=None,
    min_object_covered=(0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0),
    aspect_ratio=1.0,
    area_range=((0.1, 1.0),) * 7,
    overlap_thresh=(0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0),
    clip_boxes=(True,) * 7,
    random_coef=(0.15,) * 7,
    seed=None,
    preprocess_vars_cache=None):
  """Random crop preprocessing with default parameters as in SSD paper.

  Liu et al., SSD: Single shot multibox detector.
  For further information on random crop preprocessing refer to RandomCrop
  function above.

  The only difference is that the aspect ratio of the crops are fixed.

  Args:
    image: rank 3 float32 tensor contains 1 image -> [height, width, channels]
           with pixel values varying between [0, 1].
    boxes: rank 2 float32 tensor containing the bounding boxes -> [N, 4].
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
           Each row is in the form of [ymin, xmin, ymax, xmax].
    labels: rank 1 int32 tensor containing the object classes.
    label_weights: float32 tensor of shape [num_instances] representing the
      weight for each box.
    label_confidences: (optional) float32 tensor of shape [num_instances]
      representing the confidences for each box.
    multiclass_scores: (optional) float32 tensor of shape
      [num_instances, num_classes] representing the score for each box for each
      class.
    masks: (optional) rank 3 float32 tensor with shape
           [num_instances, height, width] containing instance masks. The masks
           are of the same height, width as the input `image`.
    keypoints: (optional) rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]. The keypoints are in y-x
               normalized coordinates.
    min_object_covered: the cropped image must cover at least this fraction of
                        at least one of the input bounding boxes.
    aspect_ratio: aspect ratio of the cropped image.
    area_range: allowed range for area ratio between cropped image and the
                original image.
    overlap_thresh: minimum overlap thresh with new cropped
                    image to keep the box.
    clip_boxes: whether to clip the boxes to the cropped image.
    random_coef: a random coefficient that defines the chance of getting the
                 original image. If random_coef is 0, we will always get the
                 cropped image, and if it is 1.0, we will always get the
                 original image.
    seed: random seed.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: image which is the same rank as input image.
    boxes: boxes which is the same rank as input boxes.
           Boxes are in normalized form.
    labels: new labels.

    If mulitclass_scores, masks, or keypoints is not None, the function also
      returns:

    multiclass_scores: rank 2 float32 tensor with shape
                       [num_instances, num_classes]
    masks: rank 3 float32 tensor with shape [num_instances, height, width]
           containing instance masks.
    keypoints: rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]
  """
  aspect_ratio_range = ((aspect_ratio, aspect_ratio),) * len(area_range)

  crop_result = ssd_random_crop(
      image,
      boxes,
      labels,
      label_weights=label_weights,
      label_confidences=label_confidences,
      multiclass_scores=multiclass_scores,
      masks=masks,
      keypoints=keypoints,
      min_object_covered=min_object_covered,
      aspect_ratio_range=aspect_ratio_range,
      area_range=area_range,
      overlap_thresh=overlap_thresh,
      clip_boxes=clip_boxes,
      random_coef=random_coef,
      seed=seed,
      preprocess_vars_cache=preprocess_vars_cache)
  i = 3
  new_image, new_boxes, new_labels = crop_result[:i]
  new_label_weights = None
  new_label_confidences = None
  new_multiclass_scores = None
  new_masks = None
  new_keypoints = None
  if label_weights is not None:
    new_label_weights = crop_result[i]
    i += 1
  if label_confidences is not None:
    new_label_confidences = crop_result[i]
    i += 1
  if multiclass_scores is not None:
    new_multiclass_scores = crop_result[i]
    i += 1
  if masks is not None:
    new_masks = crop_result[i]
    i += 1
  if keypoints is not None:
    new_keypoints = crop_result[i]

  result = random_crop_to_aspect_ratio(
      new_image,
      new_boxes,
      new_labels,
      label_weights=new_label_weights,
      label_confidences=new_label_confidences,
      multiclass_scores=new_multiclass_scores,
      masks=new_masks,
      keypoints=new_keypoints,
      aspect_ratio=aspect_ratio,
      clip_boxes=clip_boxes,
      seed=seed,
      preprocess_vars_cache=preprocess_vars_cache)

  return result


def ssd_random_crop_pad_fixed_aspect_ratio(
    image,
    boxes,
    labels,
    label_weights,
    label_confidences=None,
    multiclass_scores=None,
    masks=None,
    keypoints=None,
    min_object_covered=(0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0),
    aspect_ratio=1.0,
    aspect_ratio_range=((0.5, 2.0),) * 7,
    area_range=((0.1, 1.0),) * 7,
    overlap_thresh=(0.0, 0.1, 0.3, 0.5, 0.7, 0.9, 1.0),
    clip_boxes=(True,) * 7,
    random_coef=(0.15,) * 7,
    min_padded_size_ratio=(1.0, 1.0),
    max_padded_size_ratio=(2.0, 2.0),
    seed=None,
    preprocess_vars_cache=None):
  """Random crop and pad preprocessing with default parameters as in SSD paper.

  Liu et al., SSD: Single shot multibox detector.
  For further information on random crop preprocessing refer to RandomCrop
  function above.

  The only difference is that after the initial crop, images are zero-padded
  to a fixed aspect ratio instead of being resized to that aspect ratio.

  Args:
    image: rank 3 float32 tensor contains 1 image -> [height, width, channels]
           with pixel values varying between [0, 1].
    boxes: rank 2 float32 tensor containing the bounding boxes -> [N, 4].
           Boxes are in normalized form meaning their coordinates vary
           between [0, 1].
           Each row is in the form of [ymin, xmin, ymax, xmax].
    labels: rank 1 int32 tensor containing the object classes.
    label_weights: float32 tensor of shape [num_instances] representing the
      weight for each box.
    label_confidences: (optional) float32 tensor of shape [num_instances]
      representing the confidence for each box.
    multiclass_scores: (optional) float32 tensor of shape
      [num_instances, num_classes] representing the score for each box for each
      class.
    masks: (optional) rank 3 float32 tensor with shape
           [num_instances, height, width] containing instance masks. The masks
           are of the same height, width as the input `image`.
    keypoints: (optional) rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]. The keypoints are in y-x
               normalized coordinates.
    min_object_covered: the cropped image must cover at least this fraction of
                        at least one of the input bounding boxes.
    aspect_ratio: the final aspect ratio to pad to.
    aspect_ratio_range: allowed range for aspect ratio of cropped image.
    area_range: allowed range for area ratio between cropped image and the
                original image.
    overlap_thresh: minimum overlap thresh with new cropped
                    image to keep the box.
    clip_boxes: whether to clip the boxes to the cropped image.
    random_coef: a random coefficient that defines the chance of getting the
                 original image. If random_coef is 0, we will always get the
                 cropped image, and if it is 1.0, we will always get the
                 original image.
    min_padded_size_ratio: min ratio of padded image height and width to the
                           input image's height and width.
    max_padded_size_ratio: max ratio of padded image height and width to the
                           input image's height and width.
    seed: random seed.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    image: image which is the same rank as input image.
    boxes: boxes which is the same rank as input boxes.
           Boxes are in normalized form.
    labels: new labels.

    If multiclass_scores, masks, or keypoints is not None, the function also
    returns:

    multiclass_scores: rank 2 with shape [num_instances, num_classes]
    masks: rank 3 float32 tensor with shape [num_instances, height, width]
           containing instance masks.
    keypoints: rank 3 float32 tensor with shape
               [num_instances, num_keypoints, 2]
  """
  crop_result = ssd_random_crop(
      image,
      boxes,
      labels,
      label_weights=label_weights,
      label_confidences=label_confidences,
      multiclass_scores=multiclass_scores,
      masks=masks,
      keypoints=keypoints,
      min_object_covered=min_object_covered,
      aspect_ratio_range=aspect_ratio_range,
      area_range=area_range,
      overlap_thresh=overlap_thresh,
      clip_boxes=clip_boxes,
      random_coef=random_coef,
      seed=seed,
      preprocess_vars_cache=preprocess_vars_cache)
  i = 3
  new_image, new_boxes, new_labels = crop_result[:i]
  new_label_weights = None
  new_label_confidences = None
  new_multiclass_scores = None
  new_masks = None
  new_keypoints = None
  if label_weights is not None:
    new_label_weights = crop_result[i]
    i += 1
  if label_confidences is not None:
    new_label_confidences = crop_result[i]
    i += 1
  if multiclass_scores is not None:
    new_multiclass_scores = crop_result[i]
    i += 1
  if masks is not None:
    new_masks = crop_result[i]
    i += 1
  if keypoints is not None:
    new_keypoints = crop_result[i]

  result = random_pad_to_aspect_ratio(
      new_image,
      new_boxes,
      masks=new_masks,
      keypoints=new_keypoints,
      aspect_ratio=aspect_ratio,
      min_padded_size_ratio=min_padded_size_ratio,
      max_padded_size_ratio=max_padded_size_ratio,
      seed=seed,
      preprocess_vars_cache=preprocess_vars_cache)

  result = list(result)
  i = 3
  result.insert(2, new_labels)
  if new_label_weights is not None:
    result.insert(i, new_label_weights)
    i += 1
  if new_label_confidences is not None:
    result.insert(i, new_label_confidences)
    i += 1
  if multiclass_scores is not None:
    result.insert(i, new_multiclass_scores)
  result = tuple(result)

  return result


def convert_class_logits_to_softmax(multiclass_scores, temperature=1.0):
  """Converts multiclass logits to softmax scores after applying temperature.

  Args:
    multiclass_scores: float32 tensor of shape
      [num_instances, num_classes] representing the score for each box for each
      class.
    temperature: Scale factor to use prior to applying softmax. Larger
      temperatures give more uniform distruibutions after softmax.

  Returns:
    multiclass_scores: float32 tensor of shape
      [num_instances, num_classes] with scaling and softmax applied.
  """

  # Multiclass scores must be stored as logits. Apply temp and softmax.
  multiclass_scores_scaled = tf.divide(
      multiclass_scores, temperature, name='scale_logits')
  multiclass_scores = tf.nn.softmax(multiclass_scores_scaled, name='softmax')

  return multiclass_scores


def get_default_func_arg_map(include_label_weights=True,
                             include_label_confidences=False,
                             include_multiclass_scores=False,
                             include_instance_masks=False,
                             include_keypoints=False):
  """Returns the default mapping from a preprocessor function to its args.

  Args:
    include_label_weights: If True, preprocessing functions will modify the
      label weights, too.
    include_label_confidences: If True, preprocessing functions will modify the
      label confidences, too.
    include_multiclass_scores: If True, preprocessing functions will modify the
      multiclass scores, too.
    include_instance_masks: If True, preprocessing functions will modify the
      instance masks, too.
    include_keypoints: If True, preprocessing functions will modify the
      keypoints, too.

  Returns:
    A map from preprocessing functions to the arguments they receive.
  """
  groundtruth_label_weights = None
  if include_label_weights:
    groundtruth_label_weights = (
        fields.InputDataFields.groundtruth_weights)

  groundtruth_label_confidences = None
  if include_label_confidences:
    groundtruth_label_confidences = (
        fields.InputDataFields.groundtruth_confidences)

  multiclass_scores = None
  if include_multiclass_scores:
    multiclass_scores = (fields.InputDataFields.multiclass_scores)

  groundtruth_instance_masks = None
  if include_instance_masks:
    groundtruth_instance_masks = (
        fields.InputDataFields.groundtruth_instance_masks)

  groundtruth_keypoints = None
  if include_keypoints:
    groundtruth_keypoints = fields.InputDataFields.groundtruth_keypoints

  prep_func_arg_map = {
      normalize_image: (fields.InputDataFields.image,),
      random_horizontal_flip: (
          fields.InputDataFields.image,
          fields.InputDataFields.groundtruth_boxes,
          groundtruth_instance_masks,
          groundtruth_keypoints,
      ),
      random_vertical_flip: (
          fields.InputDataFields.image,
          fields.InputDataFields.groundtruth_boxes,
          groundtruth_instance_masks,
          groundtruth_keypoints,
      ),
      random_rotation90: (
          fields.InputDataFields.image,
          fields.InputDataFields.groundtruth_boxes,
          groundtruth_instance_masks,
          groundtruth_keypoints,
      ),
      random_pixel_value_scale: (fields.InputDataFields.image,),
      random_image_scale: (
          fields.InputDataFields.image,
          groundtruth_instance_masks,
      ),
      random_rgb_to_gray: (fields.InputDataFields.image,),
      random_adjust_brightness: (fields.InputDataFields.image,),
      random_adjust_contrast: (fields.InputDataFields.image,),
      random_adjust_hue: (fields.InputDataFields.image,),
      random_adjust_saturation: (fields.InputDataFields.image,),
      random_distort_color: (fields.InputDataFields.image,),
      random_jitter_boxes: (fields.InputDataFields.groundtruth_boxes,),
      random_crop_image: (fields.InputDataFields.image,
                          fields.InputDataFields.groundtruth_boxes,
                          fields.InputDataFields.groundtruth_classes,
                          groundtruth_label_weights,
                          groundtruth_label_confidences,
                          multiclass_scores,
                          groundtruth_instance_masks, groundtruth_keypoints),
      random_pad_image: (fields.InputDataFields.image,
                         fields.InputDataFields.groundtruth_boxes),
      random_crop_pad_image: (fields.InputDataFields.image,
                              fields.InputDataFields.groundtruth_boxes,
                              fields.InputDataFields.groundtruth_classes,
                              groundtruth_label_weights,
                              groundtruth_label_confidences,
                              multiclass_scores),
      random_crop_to_aspect_ratio: (
          fields.InputDataFields.image,
          fields.InputDataFields.groundtruth_boxes,
          fields.InputDataFields.groundtruth_classes,
          groundtruth_label_weights,
          groundtruth_label_confidences,
          multiclass_scores,
          groundtruth_instance_masks,
          groundtruth_keypoints,
      ),
      random_pad_to_aspect_ratio: (
          fields.InputDataFields.image,
          fields.InputDataFields.groundtruth_boxes,
          groundtruth_instance_masks,
          groundtruth_keypoints,
      ),
      random_black_patches: (fields.InputDataFields.image,),
      retain_boxes_above_threshold: (
          fields.InputDataFields.groundtruth_boxes,
          fields.InputDataFields.groundtruth_classes,
          groundtruth_label_weights,
          groundtruth_label_confidences,
          multiclass_scores,
          groundtruth_instance_masks,
          groundtruth_keypoints,
      ),
      image_to_float: (fields.InputDataFields.image,),
      random_resize_method: (fields.InputDataFields.image,),
      resize_to_range: (
          fields.InputDataFields.image,
          groundtruth_instance_masks,
      ),
      resize_to_min_dimension: (
          fields.InputDataFields.image,
          groundtruth_instance_masks,
      ),
      scale_boxes_to_pixel_coordinates: (
          fields.InputDataFields.image,
          fields.InputDataFields.groundtruth_boxes,
          groundtruth_keypoints,
      ),
      resize_image: (
          fields.InputDataFields.image,
          groundtruth_instance_masks,
      ),
      subtract_channel_mean: (fields.InputDataFields.image,),
      one_hot_encoding: (fields.InputDataFields.groundtruth_image_classes,),
      rgb_to_gray: (fields.InputDataFields.image,),
      ssd_random_crop: (fields.InputDataFields.image,
                        fields.InputDataFields.groundtruth_boxes,
                        fields.InputDataFields.groundtruth_classes,
                        groundtruth_label_weights,
                        groundtruth_label_confidences,
                        multiclass_scores,
                        groundtruth_instance_masks,
                        groundtruth_keypoints),
      ssd_random_crop_pad: (fields.InputDataFields.image,
                            fields.InputDataFields.groundtruth_boxes,
                            fields.InputDataFields.groundtruth_classes,
                            groundtruth_label_weights,
                            groundtruth_label_confidences,
                            multiclass_scores),
      ssd_random_crop_fixed_aspect_ratio: (
          fields.InputDataFields.image,
          fields.InputDataFields.groundtruth_boxes,
          fields.InputDataFields.groundtruth_classes,
          groundtruth_label_weights,
          groundtruth_label_confidences,
          multiclass_scores,
          groundtruth_instance_masks,
          groundtruth_keypoints),
      ssd_random_crop_pad_fixed_aspect_ratio: (
          fields.InputDataFields.image,
          fields.InputDataFields.groundtruth_boxes,
          fields.InputDataFields.groundtruth_classes,
          groundtruth_label_weights,
          groundtruth_label_confidences,
          multiclass_scores,
          groundtruth_instance_masks,
          groundtruth_keypoints,
      ),
      convert_class_logits_to_softmax: (multiclass_scores,),
  }

  return prep_func_arg_map


def preprocess(tensor_dict,
               preprocess_options,
               func_arg_map=None,
               preprocess_vars_cache=None):
  """Preprocess images and bounding boxes.

  Various types of preprocessing (to be implemented) based on the
  preprocess_options dictionary e.g. "crop image" (affects image and possibly
  boxes), "white balance image" (affects only image), etc. If self._options
  is None, no preprocessing is done.

  Args:
    tensor_dict: dictionary that contains images, boxes, and can contain other
                 things as well.
                 images-> rank 4 float32 tensor contains
                          1 image -> [1, height, width, 3].
                          with pixel values varying between [0, 1]
                 boxes-> rank 2 float32 tensor containing
                         the bounding boxes -> [N, 4].
                         Boxes are in normalized form meaning
                         their coordinates vary between [0, 1].
                         Each row is in the form
                         of [ymin, xmin, ymax, xmax].
    preprocess_options: It is a list of tuples, where each tuple contains a
                        function and a dictionary that contains arguments and
                        their values.
    func_arg_map: mapping from preprocessing functions to arguments that they
                  expect to receive and return.
    preprocess_vars_cache: PreprocessorCache object that records previously
                           performed augmentations. Updated in-place. If this
                           function is called multiple times with the same
                           non-null cache, it will perform deterministically.

  Returns:
    tensor_dict: which contains the preprocessed images, bounding boxes, etc.

  Raises:
    ValueError: (a) If the functions passed to Preprocess
                    are not in func_arg_map.
                (b) If the arguments that a function needs
                    do not exist in tensor_dict.
                (c) If image in tensor_dict is not rank 4
  """
  if func_arg_map is None:
    func_arg_map = get_default_func_arg_map()

  # changes the images to image (rank 4 to rank 3) since the functions
  # receive rank 3 tensor for image
  if fields.InputDataFields.image in tensor_dict:
    images = tensor_dict[fields.InputDataFields.image]
    if len(images.get_shape()) != 4:
      raise ValueError('images in tensor_dict should be rank 4')
    image = tf.squeeze(images, axis=0)
    tensor_dict[fields.InputDataFields.image] = image

  # Preprocess inputs based on preprocess_options
  for option in preprocess_options:
    func, params = option
    if func not in func_arg_map:
      raise ValueError('The function %s does not exist in func_arg_map' %
                       (func.__name__))
    arg_names = func_arg_map[func]
    for a in arg_names:
      if a is not None and a not in tensor_dict:
        raise ValueError('The function %s requires argument %s' %
                         (func.__name__, a))

    def get_arg(key):
      return tensor_dict[key] if key is not None else None

    args = [get_arg(a) for a in arg_names]
    if (preprocess_vars_cache is not None and
        'preprocess_vars_cache' in inspect.getargspec(func).args):
      params['preprocess_vars_cache'] = preprocess_vars_cache
    results = func(*args, **params)
    if not isinstance(results, (list, tuple)):
      results = (results,)
    # Removes None args since the return values will not contain those.
    arg_names = [arg_name for arg_name in arg_names if arg_name is not None]
    for res, arg_name in zip(results, arg_names):
      tensor_dict[arg_name] = res

  # changes the image to images (rank 3 to rank 4) to be compatible to what
  # we received in the first place
  if fields.InputDataFields.image in tensor_dict:
    image = tensor_dict[fields.InputDataFields.image]
    images = tf.expand_dims(image, 0)
    tensor_dict[fields.InputDataFields.image] = images

  return tensor_dict
