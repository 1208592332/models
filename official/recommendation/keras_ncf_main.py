# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
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
"""NCF framework to train and evaluate the NeuMF model.

The NeuMF model assembles both MF and MLP models under the NCF framework. Check
`neumf_model.py` for more details about the models.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import contextlib
import heapq
import math
import multiprocessing
import os
import signal
import typing

# pylint: disable=g-bad-import-order
import numpy as np
from absl import app as absl_app
from absl import flags
import tensorflow as tf
# pylint: enable=g-bad-import-order

from tensorflow.contrib.compiler import xla
from official.datasets import movielens
from official.recommendation import constants as rconst
from official.recommendation import data_pipeline
from official.recommendation import data_preprocessing
from official.recommendation import ncf_main
from official.recommendation import neumf_model
from official.utils.flags import core as flags_core
from official.utils.logs import hooks_helper
from official.utils.logs import logger
from official.utils.logs import mlperf_helper
from official.utils.misc import distribution_utils
from official.utils.misc import model_helpers


FLAGS = flags.FLAGS


def main(_):
  with logger.benchmark_context(FLAGS), \
       mlperf_helper.LOGGER(FLAGS.output_ml_perf_compliance_logging):
    mlperf_helper.set_ncf_root(os.path.split(os.path.abspath(__file__))[0])
    run_ncf(FLAGS)


def _logitfy(inputs, base_model):
  logits = base_model(inputs)
  zero_tensor = tf.keras.layers.Lambda(lambda x: x * 0)(logits)
  to_concatenate = [zero_tensor, logits]
  concat_layer = tf.keras.layers.Concatenate(axis=1)(to_concatenate)

  reshape_layer = tf.keras.layers.Reshape(
      target_shape=(concat_layer.shape[1].value,))(concat_layer)

  model = tf.keras.Model(inputs=inputs, outputs=reshape_layer)
  return model


def run_ncf(_):
  """Run NCF training and eval loop."""
  if FLAGS.download_if_missing and not FLAGS.use_synthetic_data:
    movielens.download(FLAGS.dataset, FLAGS.data_dir)

  if FLAGS.seed is not None:
    np.random.seed(FLAGS.seed)

  params = ncf_main.parse_flags(FLAGS)
  total_training_cycle = FLAGS.train_epochs // FLAGS.epochs_between_evals

  if FLAGS.use_synthetic_data:
    producer = data_pipeline.DummyConstructor()
    num_users, num_items = data_preprocessing.DATASET_TO_NUM_USERS_AND_ITEMS[
        FLAGS.dataset]
    num_train_steps = data_preprocessing.SYNTHETIC_BATCHES_PER_EPOCH
    num_eval_steps = data_preprocessing.SYNTHETIC_BATCHES_PER_EPOCH
  else:
    ncf_dataset, producer = data_preprocessing.instantiate_pipeline(
        dataset=FLAGS.dataset, data_dir=FLAGS.data_dir, num_data_readers=None,
        match_mlperf=FLAGS.ml_perf, deterministic=FLAGS.seed is not None,
        params=params)

    num_users = ncf_dataset.num_users
    num_items = ncf_dataset.num_items
    num_train_steps = (producer.train_batches_per_epoch //
                       params["batches_per_step"])
    num_eval_steps = (producer.eval_batches_per_epoch //
                      params["batches_per_step"])
    assert not producer.train_batches_per_epoch % params["batches_per_step"]
    assert not producer.eval_batches_per_epoch % params["batches_per_step"]
  producer.start()

  params["num_users"], params["num_items"] = num_users, num_items
  model_helpers.apply_clean(flags.FLAGS)


  train_input_fn = data_preprocessing.make_input_fn(
    producer=producer, is_training=True, use_tpu=False)

  user_input = tf.keras.layers.Input(
    shape=(1,), batch_size=FLAGS.batch_size, name="user_id", dtype=tf.int32)
  item_input = tf.keras.layers.Input(
    shape=(1,), batch_size=FLAGS.batch_size, name="item_id", dtype=tf.int32)

  base_model = neumf_model.construct_model_keras(user_input, item_input, params)
  keras_model = _logitfy([user_input, item_input], base_model)

  keras_model.summary()

  tf.logging.info("Using Keras instead of Estimator")

  def softmax_crossentropy_with_logits(y_true, y_pred):
    """A loss function replicating tf's sparse_softmax_cross_entropy
    Args:
      y_true: True labels. Tensor of shape [batch_size,]
      y_pred: Predictions. Tensor of shape [batch_size, num_classes]
    """
    y_true = tf.cast(y_true, tf.int32)
    return tf.losses.sparse_softmax_cross_entropy(
      labels=tf.reshape(y_true, [FLAGS.batch_size,]),
      logits=tf.reshape(y_pred, [FLAGS.batch_size, 2]))

  opt = neumf_model.get_optimizer(params)
  strategy = distribution_utils.get_distribution_strategy(num_gpus=1)

  keras_model.compile(loss=softmax_crossentropy_with_logits,
                optimizer=opt,
                metrics=['accuracy'],
                distribute=None)

  num_train_steps = (producer.train_batches_per_epoch //
                     params["batches_per_step"])

  print(">>>>>>>>>>> zhenzheng epochs: ", FLAGS.train_epochs)
  train_input_dataset = train_input_fn(params).repeat(FLAGS.train_epochs)

  print(">>>>>>>>>>> zhenzheng before fit")
  keras_model.fit(train_input_dataset,
            epochs=FLAGS.train_epochs,
            steps_per_epoch=num_train_steps,
            callbacks=[],
            verbose=0)
  print(">>>>>>>>>>> zhenzheng done fit")

if __name__ == "__main__":
  tf.logging.set_verbosity(tf.logging.INFO)
  ncf_main.define_ncf_flags()
  absl_app.run(main)
